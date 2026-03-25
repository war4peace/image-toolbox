#Requires -Version 5.1
<#
.SYNOPSIS
    image-toolbox setup script

.DESCRIPTION
    Checks prerequisites, installs missing components, lets you select
    SeedVR2 and Ollama models based on your GPU VRAM, downloads the
    ComfyUI workflow, and generates config.json.

    Installs / configures:
      - SeedVR2 ComfyUI custom node (git clone or verifies existing)
      - SeedVR2 Python requirements (pip into ComfyUI venv)
      - piexif Python package (pip into ComfyUI venv)
      - Ollama model (ollama pull, with VRAM-based recommendation)
      - ComfyUI workflow JSON (downloaded from GitHub)
      - config.json (generated with all detected and selected values)

    Covered by README (manual install):
      - Python 3.12
      - Git
      - NVIDIA CUDA Toolkit
      - ComfyUI Desktop
      - Ollama

.NOTES
    Run from the image-toolbox directory:
        powershell -ExecutionPolicy Bypass -File setup.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------
#  CONSTANTS
# ---------------------------------------------------------------

$GITHUB_RAW        = "https://raw.githubusercontent.com/war4peace/image-toolbox/main"
$WORKFLOW_SUBDIR   = "workflows"
$WORKFLOW_FILE     = "seedvr2_upscale_workflow.json"
$SEEDVR2_NODE_REPO = "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git"
$SEEDVR2_NODE_DIR  = "seedvr2_videoupscaler"
$SCRIPT_DIR        = Split-Path -Parent $MyInvocation.MyCommand.Path
$CONFIG_PATH       = Join-Path $SCRIPT_DIR "config.json"
$SELECTION_TIMEOUT = 15

$VRAM_TIERS = @(
    [ordered]@{ Label="24GB+"; MinVRAM=24000; DiTModel="seedvr2_ema_7b_fp16.safetensors";                              DiTSize="~16GB"; BlocksToSwap=0;  EncodeTiled=$false; DecodeTiled=$false; OllamaModel="llava:34b"; OllamaSize="~20GB" },
    [ordered]@{ Label="16GB";  MinVRAM=16000; DiTModel="seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors";    DiTSize="~8GB";  BlocksToSwap=10; EncodeTiled=$false; DecodeTiled=$false; OllamaModel="llava:13b"; OllamaSize="~8GB"  },
    [ordered]@{ Label="12GB";  MinVRAM=12000; DiTModel="seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors";    DiTSize="~8GB";  BlocksToSwap=20; EncodeTiled=$true;  DecodeTiled=$true;  OllamaModel="llava:7b";  OllamaSize="~5GB"  },
    [ordered]@{ Label="8GB";   MinVRAM=0;     DiTModel="seedvr2_ema_7b-Q4_K_M.gguf";                                  DiTSize="~4GB";  BlocksToSwap=30; EncodeTiled=$true;  DecodeTiled=$true;  OllamaModel="llava:7b";  OllamaSize="~5GB"  }
)

# ---------------------------------------------------------------
#  SUMMARY TRACKING
# ---------------------------------------------------------------

$summaryItems = [System.Collections.Generic.List[hashtable]]::new()

function Add-Summary {
    param([string]$Component, [string]$Status, [bool]$AutoSelected = $false)
    $summaryItems.Add(@{ Component = $Component; Status = $Status; AutoSelected = $AutoSelected })
}

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

function Confirm-Continue {
    param([string]$Prompt)
    Write-Host ""
    $r = Read-Host "  $Prompt [Y/N]"
    return $r -match "^[Yy]"
}

function Find-Command {
    param([string]$Name)
    try { return (Get-Command $Name -ErrorAction Stop).Source }
    catch { return $null }
}

function Get-TimedSelection {
    param([string]$Prompt, [string[]]$Options, [string[]]$Labels)

    Write-Host ""
    Write-Host "  $Prompt" -ForegroundColor Cyan
    Write-Host "  Press Enter to accept, or a number key within $SELECTION_TIMEOUT seconds:" -ForegroundColor Gray
    Write-Host ""
    Write-Host "    [Enter]  $($Labels[0])  [RECOMMENDED]" -ForegroundColor Green
    for ($i = 1; $i -lt $Options.Count; $i++) {
        Write-Host "    [$i]      $($Labels[$i])"
    }
    Write-Host ""

    $chosen       = 0
    $autoSelected = $false

    try {
        $deadline  = (Get-Date).AddSeconds($SELECTION_TIMEOUT)
        $remaining = $SELECTION_TIMEOUT

        while ((Get-Date) -lt $deadline) {
            Write-Host "`r  Auto-selecting in $remaining seconds...   " -NoNewline
            Start-Sleep -Milliseconds 500

            if ($host.UI.RawUI.KeyAvailable) {
                $key  = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
                $char = $key.Character
                if ($char -eq "`r" -or $char -eq "`n") { break }
                $num = 0
                if ([int]::TryParse($char, [ref]$num)) {
                    if ($num -ge 1 -and $num -lt $Options.Count) {
                        $chosen = $num
                        break
                    }
                }
            }
            $remaining = [math]::Max(0, [int](($deadline - (Get-Date)).TotalSeconds))
        }
        if ((Get-Date) -ge $deadline) { $autoSelected = $true }
    } catch {
        $autoSelected = $true
    }

    Write-Host ""
    return @{ Index = $chosen; AutoSelected = $autoSelected }
}

# ---------------------------------------------------------------
#  DETECTION FUNCTIONS
# ---------------------------------------------------------------

function Get-PythonInfo {
    $path = Find-Command "python"
    if (-not $path) { return $null }
    try { $ver = & python --version 2>&1; return @{ Path = $path; Version = $ver.ToString().Trim() } }
    catch { return $null }
}

function Get-GitInfo {
    $path = Find-Command "git"
    if (-not $path) { return $null }
    try { $ver = & git --version 2>&1; return @{ Path = $path; Version = $ver.ToString().Trim() } }
    catch { return $null }
}

function Get-GPUInfo {
    try {
        $output = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        $parts  = $output.ToString().Trim().Split(",")
        $vramMB = 0
        if ($parts.Count -ge 3) {
            $vramStr = $parts[2].Trim() -replace " MiB", ""
            [int]::TryParse($vramStr, [ref]$vramMB) | Out-Null
        }
        return @{ Info = $output.ToString().Trim(); VRAM_MB = $vramMB }
    } catch { return $null }
}

function Get-CudaInfo {
    $cudaPath = $env:CUDA_PATH
    if (-not $cudaPath) {
        $nvcc = Find-Command "nvcc"
        if ($nvcc) { $cudaPath = Split-Path (Split-Path $nvcc -Parent) -Parent }
    }
    if (-not $cudaPath -or -not (Test-Path $cudaPath)) { return $null }
    try {
        $ver = & nvcc --version 2>&1 | Select-String "release"
        return @{ Path = $cudaPath; Version = $ver.ToString().Trim() }
    } catch { return @{ Path = $cudaPath; Version = "version unknown" } }
}

function Get-OllamaInfo {
    $path = Find-Command "ollama"
    if (-not $path) { return $null }
    try { $ver = & ollama --version 2>&1; return @{ Path = $path; Version = $ver.ToString().Trim() } }
    catch { return $null }
}

function Get-OllamaModels {
    $local:ErrorActionPreference = "SilentlyContinue"
    try {
        $result = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -Method Get -TimeoutSec 5
        if ($null -eq $result) { return $null }
        if ($result.models.Count -eq 0) { return @("__RUNNING_NO_MODELS__") }
        return @($result.models | ForEach-Object { $_.name })
    } catch { return $null }
}

function Find-ComfyUIVenv {
    foreach ($c in @(
        "$env:USERPROFILE\Documents\ComfyUI\.venv",
        "$env:USERPROFILE\AppData\Local\ComfyUI\.venv",
        "C:\ComfyUI\.venv"
    )) {
        if (Test-Path (Join-Path $c "Scripts\python.exe")) { return $c }
    }
    return $null
}

function Find-ComfyUIDir {
    param([string]$VenvPath)
    $dataDir = Split-Path $VenvPath -Parent
    foreach ($c in @($dataDir, "$env:USERPROFILE\Documents\ComfyUI",
                     "$env:USERPROFILE\AppData\Local\Programs\ComfyUI\resources\ComfyUI")) {
        if (Test-Path (Join-Path $c "custom_nodes")) { return $c }
    }
    return $null
}

function Get-RecommendedTier {
    param([int]$VRAM_MB)
    foreach ($t in $VRAM_TIERS) {
        if ($VRAM_MB -ge $t.MinVRAM) { return $t }
    }
    return $VRAM_TIERS[-1]
}

# ---------------------------------------------------------------
#  MAIN
# ---------------------------------------------------------------

Clear-Host
Write-Host ""
Write-Host "  image-toolbox - Setup and Prerequisite Check" -ForegroundColor Cyan
Write-Host "  =============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This script checks your system, installs missing components," -ForegroundColor Gray
Write-Host "  and generates config.json for batch_upscale.py and tag_and_rename.py." -ForegroundColor Gray
Write-Host ""
Write-Host "  Nothing will be installed without your confirmation." -ForegroundColor Gray
Write-Host ""
Write-Host "  IMPORTANT BEFORE YOU START:" -ForegroundColor Yellow
Write-Host "  1. If you just installed Python, Git, CUDA or Ollama, close this" -ForegroundColor Yellow
Write-Host "     terminal and open a new one. New installs are only visible in" -ForegroundColor Yellow
Write-Host "     terminals opened after them." -ForegroundColor Yellow
Write-Host "  2. Open ComfyUI Desktop at least once before running this script," -ForegroundColor Yellow
Write-Host "     even if it shows a GPU warning. This creates the Python" -ForegroundColor Yellow
Write-Host "     environment the scripts depend on." -ForegroundColor Yellow
Write-Host ""
Read-Host "  Press Enter to begin"


# ---------------------------------------------------------------
#  STEP 1 - Detect Prerequisites
# ---------------------------------------------------------------

Write-Header "STEP 1 - Detecting Prerequisites"

$pythonInfo = Get-PythonInfo
if ($pythonInfo) {
    Write-OK "Python found: $($pythonInfo.Version)"
    Write-Info "Path: $($pythonInfo.Path)"
} else {
    Write-Warn "Python not found in PATH."
    Write-Info "Install Python 3.12 from https://www.python.org/downloads/"
    Write-Info "During install, check 'Add Python to environment variables'."
}

$gitInfo = Get-GitInfo
if ($gitInfo) {
    Write-OK "Git found: $($gitInfo.Version)"
} else {
    Write-Warn "Git not found. Install from https://git-scm.com/download/win"
}

$gpuInfo     = Get-GPUInfo
$detectedVRAM = 0
if ($gpuInfo) {
    Write-OK "NVIDIA GPU: $($gpuInfo.Info)"
    $detectedVRAM = $gpuInfo.VRAM_MB
} else {
    Write-Warn "NVIDIA GPU not detected. Defaulting to 8GB tier for recommendations."
}

$cudaInfo = Get-CudaInfo
if ($cudaInfo) {
    Write-OK "CUDA Toolkit: $($cudaInfo.Version)"
    Write-Info "Path: $($cudaInfo.Path)"
} else {
    Write-Warn "CUDA Toolkit not found."
    Write-Info "Install CUDA 12.4 from https://developer.nvidia.com/cuda-toolkit-archive"
}

$ollamaInfo = Get-OllamaInfo
if ($ollamaInfo) {
    Write-OK "Ollama: $($ollamaInfo.Version)"
} else {
    Write-Warn "Ollama not found. Install from https://ollama.com/download/windows"
}

$venvPath = Find-ComfyUIVenv
$venvPython      = ""
$comfyDir        = ""
$customNodesPath = ""
$modelsPath      = ""
$workflowsPath   = ""

if ($venvPath) {
    $venvPython = Join-Path $venvPath "Scripts\python.exe"
    Write-OK "ComfyUI venv: $venvPath"
    try {
        $torchVer = & $venvPython -c "import torch; print(torch.__version__)" 2>&1
        Write-Info "PyTorch: $torchVer"
    } catch { Write-Info "PyTorch: (could not detect)" }

    $comfyDir = Find-ComfyUIDir $venvPath
    if ($comfyDir) {
        $customNodesPath = Join-Path $comfyDir "custom_nodes"
        $modelsPath      = Join-Path $comfyDir "models"
        $workflowsPath   = Join-Path $comfyDir "user\default\workflows"
        Write-OK "ComfyUI data: $comfyDir"
    }
} else {
    Write-Warn "ComfyUI venv not found."
    Write-Info "Install ComfyUI Desktop from https://www.comfy.org/download"
    Write-Info "Open it at least once, then re-run this script."
}

if (-not $venvPath) {
    Write-Host ""
    Write-Warn "ComfyUI venv is required. Exiting."
    Read-Host "  Press Enter to exit"
    exit 1
}

if (-not $gitInfo) {
    Write-Host ""
    Write-Warn "Git is required to install the SeedVR2 node. Exiting."
    Read-Host "  Press Enter to exit"
    exit 1
}

Write-Host ""
Read-Host "  Prerequisites checked. Press Enter to continue"


# ---------------------------------------------------------------
#  STEP 2 - piexif
# ---------------------------------------------------------------

Write-Header "STEP 2 - Python Package: piexif"

try {
    $piexifVer = & $venvPython -c "import piexif; print(piexif.VERSION)" 2>&1
    Write-OK "piexif already installed: $piexifVer"
    Add-Summary "piexif" "already installed"
} catch {
    Write-Warn "piexif not found."
    if (Confirm-Continue "Install piexif into the ComfyUI venv?") {
        Write-Step "Installing piexif..."
        & $venvPython -m pip install piexif
        Write-OK "piexif installed."
        Add-Summary "piexif" "installed this run"
    } else {
        Write-Warn "Skipped. tag_and_rename.py requires piexif."
        Add-Summary "piexif" "skipped by user"
    }
}


# ---------------------------------------------------------------
#  STEP 3 - SeedVR2 Node
# ---------------------------------------------------------------

Write-Header "STEP 3 - SeedVR2 ComfyUI Node"

if (-not $customNodesPath) {
    Write-Warn "Cannot install SeedVR2 node - custom_nodes not found."
    Add-Summary "SeedVR2 node" "skipped (custom_nodes not found)"
} else {
    $nodeTargetPath = Join-Path $customNodesPath $SEEDVR2_NODE_DIR

    if (Test-Path $nodeTargetPath) {
        Write-OK "SeedVR2 node already installed: $nodeTargetPath"
        $gitDir = Join-Path $nodeTargetPath ".git"
        if (Test-Path $gitDir) {
            Write-Step "Checking for updates..."
            Push-Location $nodeTargetPath
            try   { $r = & git pull 2>&1; Write-Info $r }
            finally { Pop-Location }
            Add-Summary "SeedVR2 node" "already installed (updated via git)"
        } else {
            Write-Info "Installed via ComfyUI-Manager - use Manager to update."
            Add-Summary "SeedVR2 node" "already installed (ComfyUI-Manager)"
        }
    } else {
        Write-Warn "SeedVR2 node not found at: $nodeTargetPath"
        if (Confirm-Continue "Clone SeedVR2 node from GitHub?") {
            Write-Step "Cloning..."
            & git clone $SEEDVR2_NODE_REPO $nodeTargetPath
            Write-OK "SeedVR2 node cloned."
            Add-Summary "SeedVR2 node" "cloned from GitHub this run"
        } else {
            Write-Warn "Skipped. batch_upscale.py requires the SeedVR2 node."
            Add-Summary "SeedVR2 node" "skipped by user"
        }
    }

    $nodeReqs = Join-Path $nodeTargetPath "requirements.txt"
    if (Test-Path $nodeReqs) {
        Write-Step "Installing SeedVR2 Python requirements..."
        & $venvPython -m pip install -r $nodeReqs
        Write-OK "SeedVR2 requirements installed."
    }
}


# ---------------------------------------------------------------
#  STEP 4 - SeedVR2 Model Selection
# ---------------------------------------------------------------

Write-Header "STEP 4 - SeedVR2 Model Selection"

$recommendedTier = Get-RecommendedTier $detectedVRAM

if ($detectedVRAM -gt 0) {
    Write-OK "GPU VRAM: $detectedVRAM MB  ->  Recommended tier: $($recommendedTier.Label)"
} else {
    Write-Warn "Could not detect VRAM - defaulting to 8GB tier."
}

# Build deduplicated list: recommendation first, others after (skip duplicate models)
$seenDiT     = @{}
$ditOptions  = [System.Collections.Generic.List[object]]::new()
$seenDiT[$recommendedTier.DiTModel] = $true
$ditOptions.Add($recommendedTier)
foreach ($t in $VRAM_TIERS) {
    if ($t.Label -ne $recommendedTier.Label -and -not $seenDiT.ContainsKey($t.DiTModel)) {
        $seenDiT[$t.DiTModel] = $true
        $ditOptions.Add($t)
    }
}

$ditLabels = @()
foreach ($t in $ditOptions) {
    $ditLabels += "$($t.DiTModel)  [$($t.Label) tier, $($t.DiTSize)]"
}

$ditResult    = Get-TimedSelection -Prompt "Select SeedVR2 DiT model:" -Options ($ditOptions | ForEach-Object { $_.DiTModel }) -Labels $ditLabels
$selectedTier = $ditOptions[$ditResult.Index]

$autoLbl = if ($ditResult.AutoSelected) { " (autoselected - timeout)" } else { " (user selected)" }
Write-OK "SeedVR2 model: $($selectedTier.DiTModel)$autoLbl"
Write-Info "NOTE: This model will be downloaded automatically by ComfyUI on first use."
Write-Info "      Source: https://huggingface.co/models?other=seedvr"
Add-Summary "SeedVR2 model" "$($selectedTier.DiTModel) [$($selectedTier.Label) tier]$autoLbl" $ditResult.AutoSelected


# ---------------------------------------------------------------
#  STEP 5 - Ollama Model Selection and Pull
# ---------------------------------------------------------------

Write-Header "STEP 5 - Ollama Model"

# Build deduplicated options list
$seenOllama    = @{}
$ollamaOptions = [System.Collections.Generic.List[hashtable]]::new()
$recOllama     = @{ Model = $recommendedTier.OllamaModel; Size = $recommendedTier.OllamaSize; Tier = $recommendedTier.Label }
$seenOllama[$recOllama.Model] = $true
$ollamaOptions.Add($recOllama)
foreach ($t in $VRAM_TIERS) {
    if (-not $seenOllama.ContainsKey($t.OllamaModel)) {
        $seenOllama[$t.OllamaModel] = $true
        $ollamaOptions.Add(@{ Model = $t.OllamaModel; Size = $t.OllamaSize; Tier = $t.Label })
    }
}

$ollamaLabels = @()
foreach ($o in $ollamaOptions) { $ollamaLabels += "$($o.Model)  [$($o.Tier) tier, $($o.Size)]" }

$ollamaResult        = Get-TimedSelection -Prompt "Select Ollama vision model:" -Options ($ollamaOptions | ForEach-Object { $_.Model }) -Labels $ollamaLabels
$selectedOllamaModel = $ollamaOptions[$ollamaResult.Index].Model
$autoLbl             = if ($ollamaResult.AutoSelected) { " (autoselected - timeout)" } else { " (user selected)" }
Write-OK "Ollama model: $selectedOllamaModel$autoLbl"

if (-not $ollamaInfo) {
    Write-Warn "Ollama not installed - skipping pull."
    Add-Summary "Ollama ($selectedOllamaModel)" "skipped (Ollama not installed)" $ollamaResult.AutoSelected
} else {
    $runningModels = Get-OllamaModels

    if ($null -eq $runningModels) {
        Write-Warn "Ollama is not running - attempting to start..."
        Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            $runningModels = Get-OllamaModels
            if ($null -ne $runningModels) { Write-OK "Ollama started."; break }
            Write-Info "  Still waiting..."
        }
        if ($null -eq $runningModels) {
            Write-Warn "Ollama did not start. Pull manually: ollama pull $selectedOllamaModel"
            Add-Summary "Ollama ($selectedOllamaModel)" "skipped (failed to start)" $ollamaResult.AutoSelected
            $runningModels = @()
        }
    }

    $realModels = @($runningModels | Where-Object { $_ -ne "__RUNNING_NO_MODELS__" })
    $modelFound = @($realModels | Where-Object { $_ -eq $selectedOllamaModel -or $_ -like "$selectedOllamaModel-*" })

    if ($modelFound.Count -gt 0) {
        Write-OK "$selectedOllamaModel is already pulled."
        Add-Summary "Ollama ($selectedOllamaModel)" "already pulled$autoLbl" $ollamaResult.AutoSelected
    } else {
        $display = if ($realModels.Count -gt 0) { $realModels -join ', ' } else { "(none pulled yet)" }
        Write-Info "Currently pulled: $display"
        $ollamaSize = ($ollamaOptions | Where-Object { $_.Model -eq $selectedOllamaModel }).Size
        Write-Info "NOTE: $selectedOllamaModel is approximately $ollamaSize."

        if (Confirm-Continue "Pull $selectedOllamaModel now?") {
            Write-Step "Pulling $selectedOllamaModel - this may take a while..."
            & ollama pull $selectedOllamaModel
            Write-OK "$selectedOllamaModel pulled."
            Add-Summary "Ollama ($selectedOllamaModel)" "pulled this run$autoLbl" $ollamaResult.AutoSelected
        } else {
            Write-Warn "Skipped. Run manually: ollama pull $selectedOllamaModel"
            Add-Summary "Ollama ($selectedOllamaModel)" "skipped by user$autoLbl" $ollamaResult.AutoSelected
        }
    }
}


# ---------------------------------------------------------------
#  STEP 6 - ComfyUI Workflow
# ---------------------------------------------------------------

Write-Header "STEP 6 - ComfyUI Workflow"

if (-not $workflowsPath) {
    Write-Warn "Could not find ComfyUI workflows directory - skipping."
    Add-Summary "Workflow JSON" "skipped (workflows dir not found)"
} else {
    if (-not (Test-Path $workflowsPath)) {
        New-Item -ItemType Directory -Path $workflowsPath -Force | Out-Null
        Write-Info "Created: $workflowsPath"
    }

    $workflowDest = Join-Path $workflowsPath $WORKFLOW_FILE
    $workflowUrl  = "$GITHUB_RAW/$WORKFLOW_SUBDIR/$WORKFLOW_FILE"

    $doDownload = $true
    if (Test-Path $workflowDest) {
        Write-Info "Workflow already exists: $workflowDest"
        $doDownload = Confirm-Continue "Re-download and overwrite?"
    }

    if ($doDownload) {
        Write-Step "Downloading workflow from GitHub..."
        try {
            $local:ErrorActionPreference = "Stop"
            Invoke-WebRequest -Uri $workflowUrl -OutFile $workflowDest -UseBasicParsing
            Write-OK "Workflow installed: $workflowDest"
            Add-Summary "Workflow JSON" "downloaded to $workflowDest"
        } catch {
            if ($_.Exception.Message -like "*404*") {
                Write-Warn "Workflow not found (404). The GitHub repository may be private."
                Write-Info "This will work once the repository is made public."
                Write-Info "Or download manually and place in: $workflowsPath"
                Write-Info "URL: $workflowUrl"
            } else {
                Write-Warn "Download failed: $_"
                Write-Info "Download manually from: $workflowUrl"
            }
            Add-Summary "Workflow JSON" "download failed (404 - repo may be private)"
        }
    } else {
        Write-Info "Keeping existing workflow."
        Add-Summary "Workflow JSON" "already present (kept)"
    }
}


# ---------------------------------------------------------------
#  STEP 7 - Generate config.json
# ---------------------------------------------------------------

Write-Header "STEP 7 - Generating config.json"

$config = [ordered]@{
    comfyui = [ordered]@{
        url              = "http://127.0.0.1:8000"
        venv_python      = $venvPython
        custom_nodes_dir = $customNodesPath
        models_dir       = $modelsPath
    }
    ollama = [ordered]@{
        url   = "http://127.0.0.1:11434"
        model = $selectedOllamaModel
    }
    upscale = [ordered]@{
        resolution          = 2160
        max_resolution      = 3840
        output_subdir       = "upscaled"
        poll_interval       = 3
        poll_timeout        = 600
        attention_mode      = "sdpa"
        color_correction    = "lab"
        dit_model           = $selectedTier.DiTModel
        vae_model           = "ema_vae_fp16.safetensors"
        blocks_to_swap      = $selectedTier.BlocksToSwap
        encode_tiled        = $selectedTier.EncodeTiled
        decode_tiled        = $selectedTier.DecodeTiled
        encode_tile_size    = 1024
        decode_tile_size    = 1024
        outage_threshold    = 3
        discord_webhook_url = ""
    }
    tagging = [ordered]@{
        min_width           = 3840
        min_height          = 2160
        upscaled_subdir     = "upscaled"
        condensed_max_words = 5
        ollama_timeout      = 120
        outage_threshold    = 3
        camera_filename_patterns = @(
            "^IMG_\d+",    "^DSC\d+",       "^DSCF\d+",
            "^DSCN\d+",    "^STA\d+",       "^HPIM\d+",
            "^IMAG\d+",    "^P\d{7}",       "^MVI_\d+",
            "^MOV_\d+",    "^GOPR\d+",      "^PXL_\d{8}",
            "^PANO_\d+",   "^VID_\d+",      "^WP_\d+",
            "^DCIM\d*",    "^\d{8}_\d{6}$", "^\d+$"
        )
    }
}

$configExists = Test-Path $CONFIG_PATH
$overwrite    = $true
if ($configExists) {
    Write-Warn "config.json already exists."
    $overwrite = Confirm-Continue "Overwrite with freshly detected values?"
}

if ($overwrite) {
    [System.IO.File]::WriteAllText($CONFIG_PATH, ($config | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
    Write-OK "config.json $(if ($configExists) { 'updated' } else { 'created' }): $CONFIG_PATH"
    Add-Summary "config.json" "$(if ($configExists) { 'updated' } else { 'generated' })"
} else {
    Write-Info "Keeping existing config.json."
    Add-Summary "config.json" "kept existing (not overwritten)"
}


# ---------------------------------------------------------------
#  SETUP COMPLETE - Summary
# ---------------------------------------------------------------

Write-Header "SETUP COMPLETE"

Write-Host "  Detected paths:" -ForegroundColor Cyan
Write-Host ""
Write-Info "  ComfyUI venv     : $venvPath"
Write-Info "  Custom nodes     : $customNodesPath"
Write-Info "  Models dir       : $modelsPath"
Write-Info "  Workflows dir    : $workflowsPath"
Write-Info "  Config file      : $CONFIG_PATH"
Write-Host ""

Write-Host "  Summary of actions taken this run:" -ForegroundColor Cyan
Write-Host ""
$colW = 38
$hasAuto = $false
foreach ($item in $summaryItems) {
    $suffix = if ($item.AutoSelected) { " *"; $hasAuto = $true } else { "" }
    Write-Host "    $($item.Component.PadRight($colW)) $($item.Status)$suffix"
}
Write-Host ""
if ($hasAuto) {
    Write-Host "    * autoselected due to timeout" -ForegroundColor Gray
    Write-Host ""
}

Write-Host "  Next steps:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Start ComfyUI Desktop and verify the SeedVR2 node loads" -ForegroundColor White
Write-Host "     without errors." -ForegroundColor White
Write-Host "  2. Open the workflow in ComfyUI to verify it works:" -ForegroundColor White
Write-Host "     $workflowsPath\$WORKFLOW_FILE" -ForegroundColor Gray
Write-Host "  3. Run one image through SeedVR2 to trigger automatic model" -ForegroundColor White
Write-Host "     weight download (first run only, may take a while)." -ForegroundColor White
Write-Host "  4. Run batch_upscale.py:" -ForegroundColor White
Write-Host "     $venvPython batch_upscale.py ""X:\Your\Photos""" -ForegroundColor Gray
Write-Host "  5. Run tag_and_rename.py:" -ForegroundColor White
Write-Host "     $venvPython tag_and_rename.py ""X:\Your\Photos""" -ForegroundColor Gray
Write-Host ""

Read-Host "  Press Enter to exit"