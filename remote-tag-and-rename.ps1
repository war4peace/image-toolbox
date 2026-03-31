#Requires -Version 5.1
<#
.SYNOPSIS
    image-toolbox remote tag and rename script for RunPod

.DESCRIPTION
    Connects to a RunPod pod via SSH, starts Ollama, opens a local SSH tunnel,
    runs tag_and_rename.py, then stops the pod automatically when done.

    Prerequisites (manual -- see documentation):
      1. A RunPod account with funds and an API key
      2. A running pod with the PyTorch 2.8 template and SSH access configured
      3. Ollama installed on the pod with llava:34b pulled to /workspace
      4. Your SSH key added to RunPod and the pod

    Documentation: https://github.com/war4peace/image-toolbox/wiki/remote-tag-rename

.NOTES
    Run from the image-toolbox directory:
        powershell -ExecutionPolicy Bypass -File remote-tag-rename.ps1 <source_dir> [-ftag] [-frename]
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SCRIPT_DIR  = Split-Path -Parent $MyInvocation.MyCommand.Path
$CONFIG_PATH = Join-Path $SCRIPT_DIR "config.json"
$RUNPOD_API  = "https://rest.runpod.io/v1"
$DEFAULT_HOURLY_RATE = 0.90
$POD_STOP_COUNTDOWN  = 60

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
Write-Host "  image-toolbox - Remote Tag and Rename" -ForegroundColor Cyan
Write-Host "  ======================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script connects to your RunPod pod, starts Ollama," -ForegroundColor Gray
Write-Host "  opens a local SSH tunnel, runs tag_and_rename.py, and" -ForegroundColor Gray
Write-Host "  automatically stops the pod when processing completes." -ForegroundColor Gray
Write-Host ""
Write-Host "  BEFORE YOU CONTINUE, make sure you have:" -ForegroundColor Yellow
Write-Host ""
Write-Host "    1. A RunPod pod running with the PyTorch 2.8 template" -ForegroundColor White
Write-Host "    2. Ollama installed on the pod with llava:34b pulled" -ForegroundColor White
Write-Host "    3. Your SSH key registered with RunPod and working" -ForegroundColor White
Write-Host "    4. Your RunPod API key (from Settings > API Keys)" -ForegroundColor White
Write-Host "    5. The pod ID (shown in your RunPod dashboard)" -ForegroundColor White
Write-Host "    6. The SSH host and port (shown in Connect tab)" -ForegroundColor White
Write-Host ""
Write-Host "  Full setup instructions:" -ForegroundColor Gray
Write-Host "  https://github.com/war4peace/image-toolbox/wiki/remote-tag-rename" -ForegroundColor Cyan
Write-Host ""
Read-Host "  Press Enter to continue, or Ctrl+C to exit"

# ---------------------------------------------------------------
#  STEP 2 - Parse arguments
# ---------------------------------------------------------------

Write-Header "STEP 2 - Arguments"

$passArgs   = @($args)
$forceTag   = $passArgs -contains "-ftag"
$forceRename = $passArgs -contains "-frename"
$passArgs   = @($passArgs | Where-Object { $_ -notin @("-ftag", "-frename") })

if ($passArgs.Count -eq 0) {
    Write-Warn "No source directory specified."
    Write-Info "Usage: remote-tag-rename.ps1 <source_dir> [-ftag] [-frename]"
    exit 1
}

$sourceDir = $passArgs[0]
if (-not (Test-Path $sourceDir)) {
    Write-Warn "Source directory not found: $sourceDir"
    exit 1
}

Write-OK "Source directory: $sourceDir"
if ($forceTag)    { Write-Info "Force tag:    enabled (all images will be tagged)" }
if ($forceRename) { Write-Info "Force rename: enabled (all images will be renamed)" }

# ---------------------------------------------------------------
#  STEP 3 - Load config and RunPod settings
# ---------------------------------------------------------------

Write-Header "STEP 3 - RunPod Configuration"

$config = Get-RunpodConfig

# Ensure runpod section exists (shared with remote-image-upscale.ps1)
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

if (-not $rp.pod_id)      { $rp.pod_id      = (Read-Host "  RunPod Pod ID").Trim() }
if (-not $rp.api_key)     { $rp.api_key     = (Read-Host "  RunPod API Key").Trim() }
if (-not $rp.ssh_host)    { $rp.ssh_host    = (Read-Host "  SSH Host (e.g. 40.142.99.102)").Trim() }
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

# ---------------------------------------------------------------
#  STEP 4 - Hourly rate
# ---------------------------------------------------------------

Write-Header "STEP 4 - Session Hourly Rate"

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
            Write-Host ""
            $rateInput = Read-Host "  Hourly rate (USD)"
            $parsed = 0.0
            if ([double]::TryParse($rateInput, [ref]$parsed) -and $parsed -gt 0) {
                $chosenRate = [math]::Round($parsed, 2)
            }
            break
        }
    }
} catch {}

Write-Host ""
$rp.hourly_rate = $chosenRate
Save-Config $config
Write-OK "Hourly rate: `$$chosenRate/h"

# ---------------------------------------------------------------
#  STEP 5 - SSH connectivity check
# ---------------------------------------------------------------

Write-Header "STEP 5 - SSH Connectivity"

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
    Write-Info "Error: $sshResult"
    exit 1
}

Write-OK "SSH connected."
$lines = $sshResult -split "`n" | Where-Object { $_ -match "\S" }
foreach ($line in $lines) { Write-Info $line.Trim() }

# ---------------------------------------------------------------
#  STEP 6 - Start Ollama on pod (or detect already running)
# ---------------------------------------------------------------

Write-Header "STEP 6 - Ollama on Pod"

$ollamaModels = "/workspace/ollama/models"
$ollamaLog    = "/workspace/ollama.log"
$ollamaModel  = $config.ollama.model

# Check if Ollama is already running
Write-Step "Checking if Ollama is already running on pod ..."
$checkCmd    = "curl -s -o /dev/null -w '%{http_code}' http://localhost:11434/api/tags --max-time 3"
$checkResult = & ssh -i $rp.ssh_key_path -p $rp.ssh_port `
    -o StrictHostKeyChecking=no root@$($rp.ssh_host) $checkCmd 2>&1

if ($checkResult -eq "200") {
    Write-OK "Ollama is already running on the pod."
} else {
    Write-Step "Starting Ollama on pod ..."
    $startCmd = "OLLAMA_HOST=0.0.0.0:11434 OLLAMA_MODELS=$ollamaModels ollama serve > $ollamaLog 2>&1 &"
    & ssh -i $rp.ssh_key_path -p $rp.ssh_port `
        -o StrictHostKeyChecking=no root@$($rp.ssh_host) $startCmd 2>&1 | Out-Null

    Write-Info "Waiting for Ollama to start (up to 30 seconds) ..."
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
        Write-Warn "Ollama did not start in time. Check the pod log:"
        Write-Info "  ssh root@$($rp.ssh_host) -p $($rp.ssh_port) 'tail -20 $ollamaLog'"
        exit 1
    }
    Write-OK "Ollama started on pod."
}

# Verify the required model is available
Write-Step "Checking model $ollamaModel is available on pod ..."
$modelCheckCmd = "curl -s http://localhost:11434/api/tags"
$modelJson = & ssh -i $rp.ssh_key_path -p $rp.ssh_port `
    -o StrictHostKeyChecking=no root@$($rp.ssh_host) $modelCheckCmd 2>&1

if ($modelJson -match [regex]::Escape($ollamaModel.Split(":")[0])) {
    Write-OK "Model $ollamaModel is available."
} else {
    Write-Warn "Model $ollamaModel not found on pod."
    Write-Info "Currently available models: $modelJson"
    Write-Info "Pull the model on the pod with:"
    Write-Info "  OLLAMA_MODELS=$ollamaModels ollama pull $ollamaModel"
    Write-Info "Then re-run this script."
    exit 1
}

# ---------------------------------------------------------------
#  STEP 7 - Open SSH tunnel
# ---------------------------------------------------------------

Write-Header "STEP 7 - SSH Tunnel"

Write-Step "Opening SSH tunnel: localhost:11434 -> pod:11434 ..."

$tunnelArgs = "-i `"$($rp.ssh_key_path)`" -p $($rp.ssh_port) " +
              "-o StrictHostKeyChecking=no -o ServerAliveInterval=30 " +
              "-L 11434:localhost:11434 -N root@$($rp.ssh_host)"

$tunnelProcess = Start-Process -FilePath "ssh" -ArgumentList $tunnelArgs `
    -PassThru -WindowStyle Hidden

Start-Sleep -Seconds 3

$local:ErrorActionPreference = "SilentlyContinue"
try {
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
    Write-OK "Tunnel active. Ollama reachable at http://127.0.0.1:11434"
    $modelNames = @($tags.models | ForEach-Object { $_.name })
    Write-Info "Available models: $($modelNames -join ', ')"
} catch {
    Write-Warn "Tunnel opened but Ollama not reachable. Check pod status."
    $tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------
#  STEP 8 - Update config.json
# ---------------------------------------------------------------

Write-Header "STEP 8 - Update config.json"

$config = Get-RunpodConfig
$originalOllamaUrl   = $config.ollama.url
$config.ollama.url   = "http://127.0.0.1:11434"
Save-Config $config
Write-OK "config.json updated: ollama.url = http://127.0.0.1:11434"
Write-Info "(will be restored to '$originalOllamaUrl' after session)"

# ---------------------------------------------------------------
#  STEP 9 - Run tag_and_rename.py
# ---------------------------------------------------------------

Write-Header "STEP 9 - Running tag_and_rename.py"

$venvPythonLocal = $config.comfyui.venv_python
if (-not $venvPythonLocal -or -not (Test-Path $venvPythonLocal)) {
    Write-Warn "Local venv_python not found in config.json."
    $tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

$tagScript = Join-Path $SCRIPT_DIR "tag_and_rename.py"
if (-not (Test-Path $tagScript)) {
    Write-Warn "tag_and_rename.py not found at: $tagScript"
    $tunnelProcess | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Info "Starting tag_and_rename.py -- processing will begin now."
Write-Host ""

$sessionStart = Get-Date

# Build argument list, forwarding force flags
$tagArgs = @($tagScript, $sourceDir)
if ($forceTag)    { $tagArgs += "-ftag" }
if ($forceRename) { $tagArgs += "-frename" }

& $venvPythonLocal @tagArgs

$sessionEnd     = Get-Date
$sessionElapsed = $sessionEnd - $sessionStart
$sessionHours   = $sessionElapsed.TotalHours
$sessionCost    = [math]::Round($sessionHours * $chosenRate, 2)
$elapsedStr     = "{0}h {1}m" -f [int]$sessionElapsed.TotalHours, $sessionElapsed.Minutes

# Parse processed count from most recent log file
$avgTimeStr     = "n/a"
$processedCount = 0
try {
    $logDir  = Join-Path $SCRIPT_DIR "logs"
    $logFile = Get-ChildItem $logDir -Filter "log_*.log" |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($logFile) {
        $logContent = Get-Content $logFile.FullName -Raw
        if ($logContent -match "\((\d+) processed") {
            $processedCount = [int]$Matches[1]
            if ($processedCount -gt 0) {
                $avgSeconds = [int]($sessionElapsed.TotalSeconds / $processedCount)
                $avgTimeStr = "$avgSeconds sec/image"
            }
        }
    }
} catch {}

# ---------------------------------------------------------------
#  STEP 10 - Send Discord notification
# ---------------------------------------------------------------

Write-Header "STEP 10 - Session Complete"

Write-OK "tag_and_rename.py finished."
Write-Info "Session duration: $elapsedStr"
Write-Info "Images processed: $processedCount"
Write-Info "Avg. time:        $avgTimeStr"
Write-Info "Estimated cost:   `$$sessionCost"

$webhookUrl = $config.upscale.discord_webhook_url
Send-DiscordNotification `
    -WebhookUrl $webhookUrl `
    -Title      "image-toolbox - Remote Tag & Rename Complete" `
    -Description "tag_and_rename.py finished on RunPod pod $($rp.pod_id)." `
    -Color      3066993 `
    -Fields     @(
        @{ name = "Duration";     value = $elapsedStr;              inline = $true  },
        @{ name = "Est. Cost";    value = "`$$sessionCost";         inline = $true  },
        @{ name = "Rate";         value = "`$$chosenRate/h";        inline = $true  },
        @{ name = "Processed";    value = "$processedCount images"; inline = $true  },
        @{ name = "Avg. time";    value = $avgTimeStr;              inline = $true  },
        @{ name = "Pod ID";       value = $rp.pod_id;              inline = $false },
        @{ name = "Completed at"; value = $sessionEnd.ToString("yyyy-MM-dd HH:mm:ss"); inline = $false }
    )

# ---------------------------------------------------------------
#  STEP 11 - Stop pod countdown
# ---------------------------------------------------------------

Write-Header "STEP 11 - Stopping Pod"

Write-Host "  Pod will be stopped automatically in $POD_STOP_COUNTDOWN seconds." -ForegroundColor Yellow
Write-Host "  Press Escape to cancel." -ForegroundColor Gray
Write-Host ""

$cancelled = $false
$deadline  = (Get-Date).AddSeconds($POD_STOP_COUNTDOWN)

try {
    while ((Get-Date) -lt $deadline) {
        $remaining = [math]::Max(0, [int](($deadline - (Get-Date)).TotalSeconds))
        Write-Host "`r  Stopping pod in $remaining seconds... (press Escape to cancel)   " -NoNewline
        Start-Sleep -Milliseconds 500
        if ($host.UI.RawUI.KeyAvailable) {
            $key = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
            if ($key.VirtualKeyCode -eq 27) {
                $cancelled = $true
                break
            }
        }
    }
} catch {}

Write-Host ""

# Close tunnel
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

# Restore original Ollama URL in config
Write-Host ""
Write-Step "Restoring Ollama URL in config.json ..."
$config = Get-RunpodConfig
$config.ollama.url = $originalOllamaUrl
Save-Config $config
Write-OK "config.json restored: ollama.url = $originalOllamaUrl"

Write-Host ""
Write-Host "  Session summary:" -ForegroundColor Cyan
Write-Host "    Duration:   $elapsedStr"
Write-Host "    Processed:  $processedCount images"
Write-Host "    Avg. time:  $avgTimeStr"
Write-Host "    Est. cost:  `$$sessionCost"
Write-Host "    Pod:        $($rp.pod_id)"
Write-Host ""
Read-Host "  Press Enter to exit"