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
#      and ffmpeg (GitHub build with a curl.exe progress bar - Video Upscaler)
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
# Pinned SHA-256 of python-3.12.9-amd64.exe (confirmed against python.org's own
# published MD5 at pin time). Pinning the digest IN THIS FILE - not fetching a
# hash sidecar from python.org - is what gives tamper protection: an attacker who
# swapped the file on the server would control any hash served alongside it, but
# not this git-tracked value. python.org publishes only an MD5 for the installer,
# so this SHA-256 was computed locally from the MD5-verified download.
$PYTHON_SHA256  = "2a52993092a19cfdffe126e2eeac46a4265e25705614546604ad44988e040c0f"
# Pinned to a specific commit (not the moving main branch) so a fresh install
# always gets the known-good engine snapshot this app version was validated
# against. GitHub regenerates archive zips (compression can change), so the bytes
# can't be hash-pinned; the commit pin is the reproducibility guarantee. Bump
# deliberately after validating a newer engine.
$SEEDVR2_COMMIT = "4490bd1f482e026674543386bb2a4d176da245b9"   # v2.5.24, 2025-12-24
$SEEDVR2_ZIP    = "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/$SEEDVR2_COMMIT.zip"
$TORCH_INDEX    = "https://download.pytorch.org/whl/cu128"
# ffmpeg for the Video Upscaler (a GPL build with nvenc + libx264/libx265 - see
# docs/video-upscaler.md 6.4). Primary source is BtbN's GitHub build: it comes off
# the github.com CDN (fast, unlike gyan.dev's ~275 KB/s) and the URL is durable,
# pinned to the ffmpeg 8.1 branch. BtbN rebuilds that build in place and ships no
# .sha256 sidecar, so it can't be hash-pinned; integrity there rests on HTTPS to
# github.com plus a functional version check after extraction. gyan.dev's moving
# release-essentials build is the fallback, verified against its .sha256 sidecar.
$FFMPEG_LABEL    = "n8.1 win64-gpl"
$FFMPEG_BTBN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip"
$FFMPEG_GYAN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

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

function Get-ExpectedSha256($url) {
    # Fetch the .sha256 sidecar published next to a gyan.dev build and return
    # the bare hex digest, or $null when it can't be read (caller then warns).
    try {
        $resp = Invoke-WebRequest -Uri "$url.sha256" -UseBasicParsing
        $content = $resp.Content
        if ($content -is [byte[]]) { $content = [System.Text.Encoding]::ASCII.GetString($content) }
        if ("$content" -match "([0-9a-fA-F]{64})") { return $Matches[1].ToLower() }
    } catch {}
    return $null
}

function Get-Download($url, $dest) {
    # Prefer curl.exe (ships in Windows 10/11 since build 17063): it shows a real
    # progress bar AND downloads far faster than Invoke-WebRequest, whose progress
    # rendering cripples throughput on PowerShell 5.1 (why the earlier version had
    # to silence it, leaving the user with no feedback for minutes). Fall back to
    # Invoke-WebRequest only if curl is somehow absent. Throws on failure.
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (-not (Test-Path $curl)) {
        $c = Get-Command curl.exe -ErrorAction SilentlyContinue
        $curl = if ($c) { $c.Source } else { $null }
    }
    if ($curl) {
        # No 2>&1: curl writes its progress meter to stderr; redirecting it under
        # $ErrorActionPreference='Stop' would turn the meter into a terminating
        # error. Left alone it renders in the console; we gate on the exit code.
        & $curl -L --fail --retry 3 --retry-delay 2 -o $dest $url
        if ($LASTEXITCODE -ne 0) { throw "download failed (curl exit $LASTEXITCODE): $url" }
    } else {
        Write-Host "  (curl not found - downloading without a progress bar; this can take several minutes) ..."
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    }
}

function Install-Ffmpeg($appRoot) {
    # Bundled ffmpeg/ffprobe for the Video Upscaler (docs/video-upscaler.md 6.4):
    # ALL container work (probe/split/reassemble/audio-mux) runs locally, so this
    # is needed in BOTH install modes - even Remote-only. Extracted to ffmpeg\bin,
    # where video_pipeline.find_ffmpeg() looks for it. Only ffmpeg.exe + ffprobe.exe
    # are kept (ffplay is not needed); the build's GPL LICENSE ships alongside.
    $binDir = Join-Path $appRoot "ffmpeg\bin"
    if ((Test-Path (Join-Path $binDir "ffmpeg.exe")) -and (Test-Path (Join-Path $binDir "ffprobe.exe"))) {
        Write-Host "  Already present - keeping it."
        return
    }
    $zip = Join-Path $env:TEMP "ffmpeg-imgtbx.zip"
    Remove-Item $zip -ErrorAction SilentlyContinue
    $hashChecked = $false
    # 1) Primary: BtbN's GitHub build (fast github.com CDN; no hash sidecar, so
    #    integrity is HTTPS + the functional version check below).
    try {
        Write-Host "  Downloading ffmpeg ($FFMPEG_LABEL) from GitHub (~160 MB) ..."
        Get-Download $FFMPEG_BTBN_URL $zip
    } catch {
        # 2) Fallback: gyan.dev's release-essentials build, verified against its
        #    published .sha256 sidecar.
        Write-Host "  GitHub source unavailable ($_)" -ForegroundColor Yellow
        Write-Host "  Trying the gyan.dev build (may be slower) ..." -ForegroundColor Yellow
        Remove-Item $zip -ErrorAction SilentlyContinue
        Get-Download $FFMPEG_GYAN_URL $zip
        $expected = Get-ExpectedSha256 $FFMPEG_GYAN_URL
        if ($expected) {
            $actual = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expected) {
                Remove-Item $zip -ErrorAction SilentlyContinue
                throw "ffmpeg download failed its SHA-256 check (got $actual, expected $expected)."
            }
            Write-Host "  SHA-256 verified."
            $hashChecked = $true
        }
    }
    # Extract ffmpeg.exe + ffprobe.exe (both builds put them in <top>\bin\).
    $extract = Join-Path $env:TEMP "ffmpeg_extract"
    Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    $pkg = Get-ChildItem $extract -Directory | Select-Object -First 1
    if (-not $pkg) { throw "The ffmpeg archive did not contain the expected build folder." }
    New-Item -ItemType Directory -Force $binDir | Out-Null
    foreach ($exe in @("ffmpeg.exe", "ffprobe.exe")) {
        $srcExe = Join-Path $pkg.FullName "bin\$exe"
        if (-not (Test-Path $srcExe)) { throw "The ffmpeg archive is missing bin\$exe." }
        Copy-Item $srcExe (Join-Path $binDir $exe) -Force
    }
    # Ship the build's GPL license (gyan names it LICENSE, BtbN LICENSE.txt).
    $lic = Get-ChildItem $pkg.FullName -Filter "LICENSE*" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($lic) { Copy-Item $lic.FullName (Join-Path $appRoot "ffmpeg\LICENSE.txt") -Force }
    # Functional check: prove the extracted binary runs and is the expected major
    # version. This is the real integrity gate on the BtbN path (no hash sidecar),
    # and catches a corrupt download on either. Capture-then-slice so the process
    # exit code is read cleanly (no broken pipe from Select-Object).
    $out  = & (Join-Path $binDir "ffprobe.exe") -version
    $code = $LASTEXITCODE
    $line = ($out | Select-Object -First 1)
    if ($code -ne 0 -or "$line" -notmatch "ffprobe version") {
        throw "the downloaded ffprobe.exe did not run correctly (exit $code): $line"
    }
    if (-not $hashChecked) { Write-Host "  Verified: $line" }
    Remove-Item $zip -ErrorAction SilentlyContinue
    Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  Installed: $(Join-Path $binDir 'ffmpeg.exe')"
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
        Get-Download $PYTHON_URL $tmp
        $actual = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $PYTHON_SHA256) {
            Remove-Item $tmp -ErrorAction SilentlyContinue
            throw "Python installer failed its SHA-256 check (got $actual, expected $PYTHON_SHA256). Download may be corrupt or tampered; re-run to retry."
        }
        Write-Host "  SHA-256 verified."
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
        Write-Host "  Downloading the SeedVR2 engine (commit $($SEEDVR2_COMMIT.Substring(0,7))) ..."
        Get-Download $SEEDVR2_ZIP $zip
        Expand-Archive -Path $zip -DestinationPath "$env:TEMP\seedvr2_extract" -Force
        $extracted = Get-ChildItem "$env:TEMP\seedvr2_extract" -Directory | Select-Object -First 1
        Move-Item $extracted.FullName (Join-Path $root "seedvr2")
        Remove-Item $zip -ErrorAction SilentlyContinue
        Remove-Item "$env:TEMP\seedvr2_extract" -Recurse -Force -ErrorAction SilentlyContinue
    }

    # -- 4b. ffmpeg (Video Upscaler: local split/reassemble/mux) ---------------
    # Runs in BOTH install modes: even a Remote-only install does the video
    # split/reassembly locally (only the upscale itself runs on the pod).
    Step "Setting up ffmpeg (used by the Video Upscaler)"
    try {
        Install-Ffmpeg $root
    } catch {
        # One optional feature must not abort the whole setup. The Video
        # Upscaler tab shows "Not ready: ffmpeg not found" with guidance
        # until the binaries exist.
        Write-Host "  WARNING: ffmpeg setup failed: $_" -ForegroundColor Yellow
        Write-Host "  The Video Upscaler will be unavailable. Fix: reinstall, or place"    -ForegroundColor Yellow
        Write-Host "  ffmpeg.exe + ffprobe.exe in $root\ffmpeg\bin (or on the PATH)."      -ForegroundColor Yellow
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
        # The vision model is deliberately NOT pre-pulled here anymore: which model
        # suits this PC depends on its GPU, so the app's first-start Wizard detects
        # the card and recommends + downloads a fitting model (a weak GPU gets a
        # lighter model than the old hardcoded 7B). If that download is skipped or
        # fails, the Tag & Rename tab checks the model and offers to pull it when the
        # user clicks Start.
        Write-Host "  Ollama is ready. Image Toolbox recommends and downloads a"
        Write-Host "  vision model that suits your GPU the first time it starts."
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
