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

try {
    Write-Host "=========================================" -ForegroundColor Green
    Write-Host "  Image Toolbox - first-launch setup"      -ForegroundColor Green
    Write-Host "=========================================" -ForegroundColor Green
    Write-Host "  This one-time setup downloads about 3 GB of components."
    Write-Host "  Please keep this window open until it finishes."

    # -- 1. GPU check (warning only) --------------------------------------
    Step "Checking for an NVIDIA GPU"
    $gpu = cmd /c "nvidia-smi -L 2>nul"
    if ($LASTEXITCODE -eq 0 -and $gpu) {
        Write-Host "  Found: $("$gpu".Trim() -split "`n" | Select-Object -First 1)"
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
    if (Test-Path "seedvr2\inference_cli.py") {
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
    Step "Installing PyTorch with CUDA support (~3 GB - this is the long part)"
    Invoke-Pip @("install", "--upgrade", "pip", "--quiet")
    Invoke-Pip @("install", "torch", "torchvision", "--index-url", $TORCH_INDEX)

    Step "Installing the remaining components"
    Invoke-Pip @("install", "-r", "seedvr2\requirements.txt", "pillow", "piexif")

    # -- 6. Ollama (optional - powers the Tag & Rename feature) ---------------
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
            Start-Process -Wait $tmp -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
            Remove-Item $tmp -ErrorAction SilentlyContinue
            $ollamaExe = Find-Ollama
            if ($ollamaExe) { Write-Host "  Installed: $ollamaExe" }
            else { Write-Host "  WARNING: Ollama installation did not complete - skipping." -ForegroundColor Yellow }
        } else {
            Write-Host "  Skipped. You can install it later from https://ollama.com"
        }
    }

    if ($ollamaExe) {
        # The vision model the toolbox is configured to use (config.json)
        $model = "minicpm-v:latest"
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
            Write-Host "  It is a LARGE download (roughly 20 GB) and needs a strong GPU."
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

    # -- Done -------------------------------------------------------------------
    Set-Content -Path ".setup_complete" -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding utf8
    Step "Setup complete - launching Image Toolbox"
    Write-Host "  Note: the first upscale you run will download the AI model"
    Write-Host "  weights (~16 GB). The app shows progress while this happens."
    Start-Process (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $root 'toolbox_gui.py')`""
    Write-Host ""
    Write-Host "  A full copy of this output was saved to bootstrap.log"
    try { Stop-Transcript | Out-Null } catch {}
    Read-Host "Image Toolbox is starting - press Enter to close this window"
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
