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

    # -- Done -------------------------------------------------------------------
    Set-Content -Path ".setup_complete" -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding utf8
    Step "Setup complete - launching Image Toolbox"
    Write-Host "  Note: the first upscale you run will download the AI model"
    Write-Host "  weights (~16 GB). The app shows progress while this happens."
    Start-Sleep -Seconds 2
    Start-Process (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $root 'toolbox_gui.py')`""
}
catch {
    Write-Host ""
    Write-Host "SETUP FAILED: $_" -ForegroundColor Red
    Write-Host "You can safely run Image Toolbox again to retry - completed steps are skipped." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}
