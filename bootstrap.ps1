# bootstrap.ps1 - first-launch setup for Image Toolbox
# -----------------------------------------------------
# Runs automatically the first time "Image Toolbox" is started (see
# "Image Toolbox.cmd"). Idempotent: every step is skipped if its result
# already exists, so it is safe to re-run after a failed or interrupted
# setup. On success it writes ".setup_complete" and launches the GUI.
#
# What it does:
#   1. Checks for an NVIDIA GPU (warning only)
#   2. Installs Python 3.12 if not present (downloads from python.org)
#   3. Creates the .venv virtual environment
#   4. Downloads the SeedVR2 engine (GitHub zip - no git required)
#   5. Installs PyTorch with CUDA and the remaining Python packages
#
# Internet connection required. Downloads roughly 3 GB of components.
# (The AI model weights, ~16 GB, are downloaded separately by the app
# the first time an upscale is started.)
#
# NOTE: this file must stay ASCII-only - PowerShell 5.1 misparses
# BOM-less UTF-8 and turns smart dashes/quotes into syntax errors.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Installation mode picked in the installer (Local / Remote / Both), written to
# install_mode.txt. Remote-only skips the heavy local GPU stack - PyTorch CUDA,
# the SeedVR2 engine + weights, timm and Ollama - because upscaling and tagging
# run on a rented RunPod pod instead. A missing marker means "both", so an
# in-place upgrade of an existing (full) install keeps doing the full setup.
$installMode = "both"
$modeFile = Join-Path $root "install_mode.txt"
if (Test-Path $modeFile) {
    try { $installMode = ((Get-Content $modeFile -Raw).Trim().ToLower()) } catch {}
}
if (@("local","remote","both") -notcontains $installMode) { $installMode = "both" }
$remoteOnly = ($installMode -eq "remote")

# Everything shown below is also saved to bootstrap.log for later review.
try { Start-Transcript -Path (Join-Path $root "bootstrap.log") -Append | Out-Null } catch {}

$PYTHON_VERSION = "3.12.9"
$PYTHON_URL     = "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-amd64.exe"
$SEEDVR2_ZIP    = "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/refs/heads/main.zip"
$TORCH_INDEX    = "https://download.pytorch.org/whl/cu128"

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Find-Python312 {
    # Prefer the py launcher; fall back to the default install locations.
    $exe = cmd /c "py -3.12 -c ""import sys; print(sys.executable)"" 2>nul"
    if ($LASTEXITCODE -eq 0 -and $exe) { return "$exe".Trim() }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Invoke-Pip {
    param([string[]]$PipArgs)
    & ".venv\Scripts\python.exe" -m pip @PipArgs
    if ($LASTEXITCODE -ne 0) { throw "pip failed: pip $($PipArgs -join ' ')" }
}

function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $candidate) { return $candidate }
    return $null
}

function Confirm-Yes($prompt) {
    $answer = Read-Host "$prompt [Y/n]"
    return ($answer -eq "" -or $answer -match "^[Yy]")
}

function Close-Countdown($seconds, $message) {
    # Count down on a single rewritten line, then return so the window closes.
    # Any key press closes immediately. Falls back to a plain wait when there
    # is no interactive console (e.g. output redirected).
    try {
        for ($s = $seconds; $s -gt 0; $s--) {
            Write-Host -NoNewline ("`r" + ($message -f $s))
            for ($i = 0; $i -lt 10; $i++) {
                if ([Console]::KeyAvailable) { $null = [Console]::ReadKey($true); Write-Host ""; return }
                Start-Sleep -Milliseconds 100
            }
        }
    } catch {
        Start-Sleep -Seconds $seconds
    }
    Write-Host ""
}

try {
    Write-Host "=========================================" -ForegroundColor Green
    Write-Host "  Image Toolbox - first-launch setup"      -ForegroundColor Green
    Write-Host "=========================================" -ForegroundColor Green
    if ($remoteOnly) {
        Write-Host "  Remote mode: a small one-time setup (no local GPU stack)."
    } else {
        Write-Host "  This one-time setup downloads about 3 GB of components."
    }
    Write-Host "  Please keep this window open until it finishes."

    # -- 1. GPU check (warning only) --------------------------------------
    Step "Checking for an NVIDIA GPU"
    $gpu = cmd /c "nvidia-smi -L 2>nul"
    if ($LASTEXITCODE -eq 0 -and $gpu) {
        Write-Host "  Found: $("$gpu".Trim() -split "`n" | Select-Object -First 1)"
    } elseif ($remoteOnly) {
        Write-Host "  No local NVIDIA GPU - fine for Remote mode (the GPU work runs on the pod)."
    } else {
        Write-Host "  WARNING: No NVIDIA GPU detected. The Batch Upscaler" -ForegroundColor Yellow
        Write-Host "  requires an NVIDIA GPU with current drivers."        -ForegroundColor Yellow
        Read-Host  "  Press Enter to continue anyway, or close this window to abort"
    }

    # -- 2. Python 3.12 ----------------------------------------------------
    Step "Looking for Python 3.12"
    $python = Find-Python312
    if ($python) {
        Write-Host "  Found: $python"
    } else {
        Write-Host "  Not found - downloading Python $PYTHON_VERSION (~25 MB) ..."
        $tmp = Join-Path $env:TEMP "python-$PYTHON_VERSION-amd64.exe"
        Invoke-WebRequest -Uri $PYTHON_URL -OutFile $tmp -UseBasicParsing
        Write-Host "  Installing Python (a progress window will appear) ..."
        Start-Process -Wait $tmp -ArgumentList "/passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0"
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $python = Find-Python312
        if (-not $python) { throw "Python 3.12 installation did not complete." }
        Write-Host "  Installed: $python"
    }

    # -- 3. Virtual environment ---------------------------------------------
    Step "Creating the Python environment (.venv)"
    if (Test-Path ".venv\Scripts\python.exe") {
        Write-Host "  Already exists - keeping it."
    } else {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
    }

    # -- 4. SeedVR2 engine ----------------------------------------------------
    Step "Downloading the SeedVR2 upscaling engine"
    if ($remoteOnly) {
        Write-Host "  Skipped (Remote mode - the engine runs on the pod)."
    } elseif (Test-Path "seedvr2\inference_cli.py") {
        Write-Host "  Already present - keeping it."
    } else {
        $zip = Join-Path $env:TEMP "seedvr2.zip"
        Invoke-WebRequest -Uri $SEEDVR2_ZIP -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath "$env:TEMP\seedvr2_extract" -Force
        $extracted = Get-ChildItem "$env:TEMP\seedvr2_extract" -Directory | Select-Object -First 1
        Move-Item $extracted.FullName (Join-Path $root "seedvr2")
        Remove-Item $zip -ErrorAction SilentlyContinue
        Remove-Item "$env:TEMP\seedvr2_extract" -Recurse -Force -ErrorAction SilentlyContinue
    }

    # -- 5. Python packages ---------------------------------------------------
    if ($remoteOnly) {
        # No local GPU stack: the GUI needs only Pillow (display/comparison),
        # piexif and paho-mqtt. torch / SeedVR2 / timm all live on the pod.
        Step "Installing the lightweight components (Remote mode - no local GPU stack)"
        Invoke-Pip @("install", "--upgrade", "pip", "--quiet")
        Invoke-Pip @("install", "pillow", "piexif", "paho-mqtt")
    } else {
        Step "Installing PyTorch with CUDA support (~3 GB - this is the long part)"
        Invoke-Pip @("install", "--upgrade", "pip", "--quiet")
        Invoke-Pip @("install", "torch", "torchvision", "--index-url", $TORCH_INDEX)

        Step "Installing the remaining components"
        Invoke-Pip @("install", "-r", "seedvr2\requirements.txt", "pillow", "piexif", "timm", "paho-mqtt")
    }

    # -- 5b. OpenSSH (Remote mode reaches the pod over SSH) -------------------
    if ($remoteOnly) {
        Step "Checking for the OpenSSH client (used to reach the remote pod)"
        $sshCmd = Get-Command ssh -ErrorAction SilentlyContinue
        if ($sshCmd) {
            Write-Host "  Found: $($sshCmd.Source)"
        } else {
            Write-Host "  WARNING: OpenSSH client not found." -ForegroundColor Yellow
            Write-Host "  Enable it via Settings > Apps > Optional features > 'OpenSSH Client'." -ForegroundColor Yellow
            Write-Host "  (The app can also generate its SSH key for you once OpenSSH is present.)" -ForegroundColor Yellow
        }
    }

    # -- 6. Ollama (optional - powers the Tag & Rename feature) ---------------
    # Remote mode tags via Ollama running ON the pod (reached over an SSH tunnel),
    # so no local Ollama is installed here.
    if ($remoteOnly) {
        Step "Skipping Ollama (Remote mode - Tag & Rename uses Ollama on the pod)"
    } else {
    Step "Checking for Ollama (used by the Tag & Rename feature)"
    $ollamaExe = Find-Ollama
    if ($ollamaExe) {
        Write-Host "  Found: $ollamaExe"
    } else {
        Write-Host "  Ollama is not installed. It is OPTIONAL - only the"
        Write-Host "  Tag & Rename feature needs it; upscaling works without it."
        if (Confirm-Yes "  Install Ollama now (~1 GB download)?") {
            $tmp = Join-Path $env:TEMP "OllamaSetup.exe"
            Write-Host "  Downloading Ollama ..."
            Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $tmp -UseBasicParsing
            Write-Host "  Installing Ollama (this can take a minute) ..."
            # The Ollama installer auto-launches its desktop app at the end and,
            # via its Inno Setup [Run] entry, does NOT return until that window is
            # closed -- so a blocking '-Wait' hangs the bootstrap here until the
            # user manually closes Ollama. Start it non-blocking instead and
            # detect completion by the appearance of ollama.exe, with a timeout.
            $proc = Start-Process $tmp -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -PassThru
            $deadline = (Get-Date).AddMinutes(5)
            do {
                Start-Sleep -Seconds 2
                $ollamaExe = Find-Ollama
            } until ($ollamaExe -or $proc.HasExited -or (Get-Date) -gt $deadline)
            Start-Sleep -Seconds 1
            $ollamaExe = Find-Ollama          # final re-check (covers exit/copy races)
            Remove-Item $tmp -ErrorAction SilentlyContinue
            if ($ollamaExe) {
                Write-Host "  Installed: $ollamaExe"
                Write-Host "  (You can close the Ollama window that just opened; setup continues here.)"
            } else {
                Write-Host "  WARNING: Ollama installation did not complete - skipping." -ForegroundColor Yellow
            }
        } else {
            Write-Host "  Skipped. You can install it later from https://ollama.com"
        }
    }

    if ($ollamaExe) {
        # The vision model the toolbox is configured to use (config.json)
        $model = "qwen2.5vl:7b"
        try {
            $cfg = Get-Content (Join-Path $root "config.json") -Raw | ConvertFrom-Json
            if ($cfg.ollama.model) { $model = $cfg.ollama.model }
        } catch {}

        # Make sure the Ollama background service is running before talking to it
        $list = cmd /c "`"$ollamaExe`" list 2>nul"
        if ($LASTEXITCODE -ne 0) {
            $app = Join-Path (Split-Path $ollamaExe) "ollama app.exe"
            if (Test-Path $app) {
                Start-Process $app | Out-Null
                Start-Sleep -Seconds 8
                $list = cmd /c "`"$ollamaExe`" list 2>nul"
            }
        }

        $base = ($model -split ":")[0]
        if ($LASTEXITCODE -eq 0 -and "$list" -match [regex]::Escape($base)) {
            Write-Host "  Vision model '$model' is already available."
        } else {
            Write-Host "  The Tag & Rename feature uses the vision model '$model'."
            Write-Host "  It is a large download (about 6 GB) and needs a capable GPU (~16 GB VRAM)."
            if (Confirm-Yes "  Download '$model' now?") {
                & $ollamaExe pull $model
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "  WARNING: the model download did not finish." -ForegroundColor Yellow
                    Write-Host "  You can retry later with:  ollama pull $model" -ForegroundColor Yellow
                }
            } else {
                Write-Host "  Skipped. You can download it later with:  ollama pull $model"
            }
        }
    }
    }

    # -- Done -------------------------------------------------------------------
    Set-Content -Path ".setup_complete" -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding utf8
    Step "Setup complete - launching Image Toolbox"
    Write-Host "  Note: the first upscale you run will download the AI model"
    Write-Host "  weights (~16 GB). The app shows progress while this happens."
    Start-Process (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $root 'scripts\toolbox_gui.py')`""
    Write-Host ""
    Write-Host "  A full copy of this output was saved to bootstrap.log"
    try { Stop-Transcript | Out-Null } catch {}
    Close-Countdown 10 "  Image Toolbox is starting - this window closes in {0,2}s (press a key to close now) "
}
catch {
    Write-Host ""
    Write-Host "SETUP FAILED: $_" -ForegroundColor Red
    Write-Host "You can safely run Image Toolbox again to retry - completed steps are skipped." -ForegroundColor Yellow
    Write-Host "A full copy of this output was saved to bootstrap.log" -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch {}
    Read-Host "Press Enter to close"
    exit 1
}
