#Requires -Version 5.1
<#
.SYNOPSIS
    image-toolbox remote setup script for RunPod

.DESCRIPTION
    Connects to a RunPod pod via SSH, starts ComfyUI, opens a local SSH tunnel,
    runs batch_upscale.py, then stops the pod automatically when done.

    Prerequisites (manual -- see documentation):
      1. A RunPod account with funds and an API key
      2. A running pod with the PyTorch 2.8 template and SSH access configured
      3. ComfyUI and SeedVR2 installed on the pod (see README)
      4. Your SSH key added to RunPod and the pod

    Documentation: https://github.com/war4peace/image-toolbox/wiki/remote-setup

.NOTES
    Run from the image-toolbox directory:
        powershell -ExecutionPolicy Bypass -File remote-setup.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SCRIPT_DIR  = Split-Path -Parent $MyInvocation.MyCommand.Path
$CONFIG_PATH = Join-Path $SCRIPT_DIR "config.json"
$RUNPOD_API  = "https://rest.runpod.io/v1"
$DEFAULT_HOURLY_RATE = 0.90
$POD_STOP_COUNTDOWN  = 60   # seconds before auto-stopping pod

# ---------------------------------------------------------------
#  HELPERS
# ---------------------------------------------------------------

function Write-Header {
    param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step { param([string]$T) Write-Host "  >> $T" -ForegroundColor Yellow }
function Write-OK   { param([string]$T) Write-Host "  [OK]  $T" -ForegroundColor Green }
function Write-Warn { param([string]$T) Write-Host "  [!!]  $T" -ForegroundColor Red }
function Write-Info { param([string]$T) Write-Host "        $T" -ForegroundColor Gray }

function Get-RunpodConfig {
    if (-not (Test-Path $CONFIG_PATH)) {
        Write-Warn "config.json not found at: $CONFIG_PATH"
        Write-Info "Run setup.ps1 first to generate it."
        exit 1
    }
    $raw = [System.IO.File]::ReadAllText($CONFIG_PATH, [System.Text.UTF8Encoding]::new($false))
    return $raw | ConvertFrom-Json
}

function Save-Config {
    param($Config)
    [System.IO.File]::WriteAllText(
        $CONFIG_PATH,
        ($Config | ConvertTo-Json -Depth 10),
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Stop-RunpodPod {
    param([string]$PodId, [string]$ApiKey)
    $local:ErrorActionPreference = "SilentlyContinue"
    try {
        Invoke-RestMethod `
            -Uri "$RUNPOD_API/pods/$PodId/stop" `
            -Method Post `
            -Headers @{ Authorization = "Bearer $ApiKey" } `
            -TimeoutSec 15 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Send-DiscordNotification {
    param([string]$WebhookUrl, [string]$Title, [string]$Description,
          [int]$Color, [array]$Fields)
    if (-not $WebhookUrl) { return }
    $local:ErrorActionPreference = "SilentlyContinue"
    try {
        $payload = @{
            username = "image-toolbox"
            embeds   = @(@{
                title       = $Title
                description = $Description
                color       = $Color
                fields      = $Fields
            })
        } | ConvertTo-Json -Depth 5
        Invoke-RestMethod -Uri $WebhookUrl -Method Post `
            -ContentType "application/json" -Body $payload `
            -Headers @{ "User-Agent" = "Mozilla/5.0" } -TimeoutSec 10 | Out-Null
    } catch {}
}

# ---------------------------------------------------------------
#  STEP 1 - Prerequisites and documentation
# ---------------------------------------------------------------

Clear-Host
Write-Host ""
Write-Host "  image-toolbox - Remote RunPod Setup" -ForegroundColor Cyan
Write-Host "  =====================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script connects to your RunPod pod, starts ComfyUI," -ForegroundColor Gray
Write-Host "  opens a local SSH tunnel, runs batch_upscale.py, and" -ForegroundColor Gray
Write-Host "  automatically stops the pod when processing completes." -ForegroundColor Gray
Write-Host ""
Write-Host "  BEFORE YOU CONTINUE, make sure you have:" -ForegroundColor Yellow
Write-Host ""
Write-Host "    1. A RunPod pod running with the PyTorch 2.8 template" -ForegroundColor White
Write-Host "    2. ComfyUI and SeedVR2 installed on the pod" -ForegroundColor White
Write-Host "    3. Your SSH key registered with RunPod and working" -ForegroundColor White
Write-Host "    4. Your RunPod API key (from Settings > API Keys)" -ForegroundColor White
Write-Host "    5. The pod ID (shown in your RunPod dashboard)" -ForegroundColor White
Write-Host "    6. The SSH host and port (shown in Connect tab)" -ForegroundColor White
Write-Host ""
Write-Host "  Full setup instructions:" -ForegroundColor Gray
Write-Host "  https://github.com/war4peace/image-toolbox/wiki/remote-setup" -ForegroundColor Cyan
Write-Host ""
Read-Host "  Press Enter to continue, or Ctrl+C to exit"

# ---------------------------------------------------------------
#  STEP 2 - Load config and RunPod settings
# ---------------------------------------------------------------

Write-Header "STEP 2 - RunPod Configuration"

$config = Get-RunpodConfig

# Ensure runpod section exists
if (-not $config.PSObject.Properties["runpod"]) {
    $config | Add-Member -NotePropertyName "runpod" -NotePropertyValue ([PSCustomObject]@{
        pod_id       = ""
        api_key      = ""
        ssh_host     = ""
        ssh_port     = 22
        ssh_key_path = "$env:USERPROFILE\.ssh\id_ed25519_runpod"
        hourly_rate  = $DEFAULT_HOURLY_RATE
        stop_pod_when_done = $true
    })
    Save-Config $config
    Write-Info "Added runpod section to config.json."
}

$rp = $config.runpod

# Prompt for any missing required values
if (-not $rp.pod_id) {
    $rp.pod_id = (Read-Host "  RunPod Pod ID").Trim()
}
if (-not $rp.api_key) {
    $rp.api_key = (Read-Host "  RunPod API Key").Trim()
}
if (-not $rp.ssh_host) {
    $rp.ssh_host = (Read-Host "  SSH Host (e.g. 40.142.99.102)").Trim()
}
if (-not $rp.ssh_port -or $rp.ssh_port -eq 22) {
    $portInput = (Read-Host "  SSH Port").Trim()
    if ($portInput) { $rp.ssh_port = [int]$portInput }
}
if (-not $rp.ssh_key_path -or -not (Test-Path $rp.ssh_key_path)) {
    $keyInput = (Read-Host "  SSH Key Path [$($rp.ssh_key_path)]").Trim()
    if ($keyInput) { $rp.ssh_key_path = $keyInput }
}

Save-Config $config
Write-OK "RunPod config saved."
Write-Info "Pod ID:   $($rp.pod_id)"
Write-Info "SSH:      $($rp.ssh_host):$($rp.ssh_port)"
Write-Info "SSH Key:  $($rp.ssh_key_path)"

# ---------------------------------------------------------------
#  STEP 3 - Hourly rate (timed selection)
# ---------------------------------------------------------------

Write-Header "STEP 3 - Session Hourly Rate"

$lastRate   = if ($rp.hourly_rate) { $rp.hourly_rate } else { $DEFAULT_HOURLY_RATE }
$timeout    = 15
$deadline   = (Get-Date).AddSeconds($timeout)
$chosenRate = $lastRate

Write-Host "  Current hourly rate: `$$lastRate/h" -ForegroundColor Gray
Write-Host "  Press Enter to accept, or type a new rate within $timeout seconds:" -ForegroundColor Cyan
Write-Host ""

try {
    while ((Get-Date) -lt $deadline) {
        $remaining = [math]::Max(0, [int](($deadline - (Get-Date)).TotalSeconds))
        Write-Host "`r  Auto-accepting `$$lastRate/h in $remaining seconds...   " -NoNewline
        Start-Sleep -Milliseconds 500
        if ($host.UI.RawUI.KeyAvailable) {
            $key = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
            if ($key.Character -eq "`r" -or $key.Character -eq "`n") { break }
            # User started typing -- let them complete input
            Write-Host ""
            $rateInput = Read-Host "  Hourly rate (USD)"
            $parsed = 0.0
            if ([double]::TryParse($rateInput, [ref]$parsed) -and $parsed -gt 0) {
                $chosenRate = [math]::Round($parsed, 2)
            }
            break
        }
    }
} catch {
    # Non-interactive host -- use default
}

Write-Host ""
$rp.hourly_rate = $chosenRate
Save-Config $config
Write-OK "Hourly rate: `$$chosenRate/h"

# ---------------------------------------------------------------
#  STEP 4 - SSH connectivity check
# ---------------------------------------------------------------

Write-Header "STEP 4 - SSH Connectivity"

Write-Step "Testing SSH connection to $($rp.ssh_host):$($rp.ssh_port) ..."

$sshArgs = @(
    "-i", $rp.ssh_key_path,
    "-p", $rp.ssh_port,
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "root@$($rp.ssh_host)",
    "echo connected && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"
)

$sshResult = & ssh @sshArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warn "SSH connection failed."
    Write-Info "Check that the pod is running and your SSH key is registered."
    Write-Info "Error: $sshResult"
    exit 1
}

Write-OK "SSH connected."
$lines = $sshResult -split "`n" | Where-Object { $_ -match "\S" }
foreach ($line in $lines) { Write-Info $line.Trim() }

# ---------------------------------------------------------------
#  STEP 5 - Start ComfyUI on pod (or detect already running)
# ---------------------------------------------------------------

Write-Header "STEP 5 - ComfyUI on Pod"

$venvPython  = "/workspace/venv/bin/python"
$comfyDir    = "/workspace/ComfyUI"
$modelPaths  = "/workspace/ComfyUI/extra_model_paths.yaml"
$comfyLog    = "/workspace/comfyui.log"
$comfyArgs   = "main.py --listen 0.0.0.0 --port 8188 --extra-model-paths-config $modelPaths"

# Check if ComfyUI is already running
Write-Step "Checking if ComfyUI is already running on pod ..."
$checkCmd = "curl -s -o /dev/null -w '%{http_code}' http://localhost:8188/system_stats --max-time 3"
$checkResult = & ssh -i $rp.ssh_key_path -p $rp.ssh_port `
    -o StrictHostKeyChecking=no root@$($rp.ssh_host) $checkCmd 2>&1

if ($checkResult -eq "200") {
    Write-OK "ComfyUI is already running on the pod."
} else {
    Write-Step "Starting ComfyUI on pod ..."
    $startCmd = "source $venvPython/../activate 2>/dev/null || true; cd $comfyDir && nohup $venvPython $comfyArgs > $comfyLog 2>&1 &"
    & ssh -i $rp.ssh_key_path -p $rp.ssh_port `
        -o StrictHostKeyChecking=no root@$($rp.ssh_host) $startCmd 2>&1 | Out-Null

    # Wait for ComfyUI to become ready
    Write-Info "Waiting for ComfyUI to start (up to 30 seconds) ..."
    $ready    = $false
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $status = & ssh -i $rp.ssh_key_path -p $rp.ssh_port `
            -o StrictHostKeyChecking=no root@$($rp.ssh_host) $checkCmd 2>&1
        if ($status -eq "200") { $ready = $true; break }
        Write-Info "  Still waiting ..."
    }
    if (-not $ready) {
        Write-Warn "ComfyUI did not start in time. Check the pod log:"
        Write-Info "  ssh root@$($rp.ssh_host) -p $($rp.ssh_port) -i $($rp.ssh_key_path) 'tail -50 $comfyLog'"
        exit 1
    }
    Write-OK "ComfyUI started on pod."
}

# ---------------------------------------------------------------
#  STEP 6 - Open SSH tunnel
# ---------------------------------------------------------------

Write-Header "STEP 6 - SSH Tunnel"

Write-Step "Opening SSH tunnel: localhost:8188 -> pod:8188 ..."

$tunnelArgs = "-i `"$($rp.ssh_key_path)`" -p $($rp.ssh_port) " +
              "-o StrictHostKeyChecking=no -o ServerAliveInterval=30 " +
              "-L 8188:localhost:8188 -N root@$($rp.ssh_host)"

$tunnelProcess = Start-Process -FilePath "ssh" -ArgumentList $tunnelArgs `
    -PassThru -WindowStyle Hidden

Start-Sleep -Seconds 3

# Verify tunnel works
$local:ErrorActionPreference = "SilentlyContinue"
try {
    $stats = Invoke-RestMethod -Uri "http://127.0.0.1:8188/system_stats" -TimeoutSec 5
    Write-OK "Tunnel active. ComfyUI reachable at http://127.0.0.1:8188"
    Write-Info "ComfyUI version: $($stats.system.comfyui_version)"
} catch {
    Write-Warn "Tunnel opened but ComfyUI not reachable. Check pod status."
    $tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------
#  STEP 7 - Update config.json
# ---------------------------------------------------------------

Write-Header "STEP 7 - Update config.json"

$config = Get-RunpodConfig
$config.comfyui.url = "http://127.0.0.1:8188"
Save-Config $config
Write-OK "config.json updated: comfyui.url = http://127.0.0.1:8188"

# ---------------------------------------------------------------
#  STEP 8 - Run batch_upscale.py
# ---------------------------------------------------------------

Write-Header "STEP 8 - Running batch_upscale.py"

$venvPythonLocal = $config.comfyui.venv_python
if (-not $venvPythonLocal -or -not (Test-Path $venvPythonLocal)) {
    Write-Warn "Local venv_python not found in config.json."
    Write-Info "Check that setup.ps1 was run and config.json is correct."
    $tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

$batchScript = Join-Path $SCRIPT_DIR "batch_upscale.py"
if (-not (Test-Path $batchScript)) {
    Write-Warn "batch_upscale.py not found at: $batchScript"
    $tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Info "Starting batch_upscale.py -- processing will begin now."
Write-Info "Press Q in this window to stop after the current image."
Write-Host ""

$sessionStart = Get-Date

# Pass through remaining arguments to batch_upscale.py
$batchArgs = @($batchScript) + $args
& $venvPythonLocal @batchArgs

$sessionEnd     = Get-Date
$sessionElapsed = $sessionEnd - $sessionStart
$sessionHours   = $sessionElapsed.TotalHours
$sessionCost    = [math]::Round($sessionHours * $chosenRate, 2)
$elapsedStr     = "{0}h {1}m" -f [int]$sessionElapsed.TotalHours, $sessionElapsed.Minutes

# Parse processed count and avg time from the most recent log file
$avgTimeStr    = "n/a"
$processedCount = 0
try {
    $logDir  = Join-Path $SCRIPT_DIR "logs"
    $logFile = Get-ChildItem $logDir -Filter "log_*.log" |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($logFile) {
        $logContent = Get-Content $logFile.FullName -Raw
        # Match summary line: (N processed, ...)
        if ($logContent -match "\((\d+) processed,") {
            $processedCount = [int]$Matches[1]
            if ($processedCount -gt 0) {
                $avgSeconds = [int]($sessionElapsed.TotalSeconds / $processedCount)
                $avgTimeStr = "$avgSeconds sec/image"
            }
        }
    }
} catch {}

# ---------------------------------------------------------------
#  STEP 9 - Send Discord notification
# ---------------------------------------------------------------

Write-Header "STEP 9 - Session Complete"

Write-OK "batch_upscale.py finished."
Write-Info "Session duration: $elapsedStr"
Write-Info "Estimated cost:   `$$sessionCost"

$webhookUrl = $config.upscale.discord_webhook_url
Send-DiscordNotification `
    -WebhookUrl $webhookUrl `
    -Title      "image-toolbox - Remote Session Complete" `
    -Description "batch_upscale.py finished on RunPod pod $($rp.pod_id)." `
    -Color      3066993 `
    -Fields     @(
        @{ name = "Duration";     value = $elapsedStr;        inline = $true  },
        @{ name = "Est. Cost";    value = "`$$sessionCost";   inline = $true  },
        @{ name = "Rate";         value = "`$$chosenRate/h";  inline = $true  },
        @{ name = "Processed";    value = "$processedCount images"; inline = $true },
        @{ name = "Avg. time";    value = $avgTimeStr;        inline = $true  },
        @{ name = "Pod ID";       value = $rp.pod_id;         inline = $false },
        @{ name = "Completed at"; value = $sessionEnd.ToString("yyyy-MM-dd HH:mm:ss"); inline = $false }
    )

# ---------------------------------------------------------------
#  STEP 10 - Stop pod countdown
# ---------------------------------------------------------------

Write-Header "STEP 10 - Stopping Pod"

Write-Host "  Pod will be stopped automatically in $POD_STOP_COUNTDOWN seconds." -ForegroundColor Yellow
Write-Host "  Press any key to cancel." -ForegroundColor Gray
Write-Host ""

$cancelled  = $false
$deadline   = (Get-Date).AddSeconds($POD_STOP_COUNTDOWN)

try {
    while ((Get-Date) -lt $deadline) {
        $remaining = [math]::Max(0, [int](($deadline - (Get-Date)).TotalSeconds))
        Write-Host "`r  Stopping pod in $remaining seconds... (press Escape to cancel)   " -NoNewline
        Start-Sleep -Milliseconds 500
        if ($host.UI.RawUI.KeyAvailable) {
            $key = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
            # Only cancel on Escape -- ignore window focus clicks and other spurious keys
            if ($key.VirtualKeyCode -eq 27) {
                $cancelled = $true
                break
            }
        }
    }
} catch {
    # Non-interactive -- proceed with stop
}

Write-Host ""

# Close tunnel regardless
$tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
Write-OK "SSH tunnel closed."

if ($cancelled) {
    Write-Warn "Pod stop cancelled by user."
    Write-Info "Stop the pod manually from the RunPod dashboard."
    Write-Info "Pod ID: $($rp.pod_id)"
} else {
    Write-Step "Stopping pod $($rp.pod_id) via RunPod API ..."
    $stopped = Stop-RunpodPod -PodId $rp.pod_id -ApiKey $rp.api_key
    if ($stopped) {
        Write-OK "Pod stopped successfully. Billing has ended."
    } else {
        Write-Warn "API call failed. Stop the pod manually from the RunPod dashboard."
        Write-Info "Pod ID: $($rp.pod_id)"
    }
}

# Restore local ComfyUI URL in config
Write-Host ""
Write-Step "Restoring local ComfyUI URL in config.json ..."
$config = Get-RunpodConfig
$config.comfyui.url = "http://127.0.0.1:8000"
Save-Config $config
Write-OK "config.json restored: comfyui.url = http://127.0.0.1:8000"

Write-Host ""
Write-Host "  Session summary:" -ForegroundColor Cyan
Write-Host "    Duration:   $elapsedStr"
Write-Host "    Processed:  $processedCount images"
Write-Host "    Avg. time:  $avgTimeStr"
Write-Host "    Est. cost:  `$$sessionCost"
Write-Host "    Pod:        $($rp.pod_id)"
Write-Host ""
Read-Host "  Press Enter to exit"