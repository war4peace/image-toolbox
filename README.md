# image-toolbox
AI-leveraged image alteration toolbox. Rescale, describe, rename files based on a variety of parameters, using local and remote AI models.

## WARNING: This toolbox is work-in-progress. Do NOT use it on important data. Always test with a small sample before committing to your precious files!
## I am not responsible for data loss. Use this tool at your own risk.

This is a set of tools created to help with upscaling, describing and renaming image files. Useful to enhance your personal image collection and improve old pictures which were taken using older cameras. 

## NOTE: These tools were written using Claude Sonnet 4.6. I am not a trained developer. As such, the code present in this repository contains results of what's known as *"Vibecoding"*, which is frowned upon by many members of the software development extended community. This is a personal project that I am using and is shared with everyone to use, at no cost.

## Table of Contents

### [Prerequisite Software](#prerequisite-software)
### [Repository files](#files-in-this-repository)
### [Setup](#setup-script)
### [Batch Imaging Tool](#batch-image-upscaling)
### [Tag and Rename Tool](#tag-and-rename)
### [Samples](#sample-files)

## Prerequisite software:

1. [ComfyUI Desktop](https://www.comfy.org) 
2. [Ollama](https://ollama.com)
3. [Python 3.12.9](https://www.python.org/downloads/windows/) (Tested. More recent versions might work as well)
4. [Git for Windows](https://gitforwindows.org)
5. [NVIDIA CUDA Toolkit 12.4](https://developer.nvidia.com/cuda-12-4-0-download-archive?target_os=Windows&target_arch=x86_64)
6. [piexif](https://pypi.org/project/piexif/) (installed automatically during setup)
[Back to ToC](#table-of-contents)

## Files in this repository:

1. [setup.ps1](https://github.com/war4peace/image-toolbox/blob/main/setup.ps1) (configuration script)
2. [batch_upscale.py](https://github.com/war4peace/image-toolbox/blob/main/batch_upscale.py) (image upscale script)
3. [config.json](https://github.com/war4peace/image-toolbox/blob/main/config.json) (configuration file)
4. [tag_and_rename.py](https://github.com/war4peace/image-toolbox/blob/main/tag_and_rename.py) (describes and renames images)
5. [seedvr2_upscale_workflow.json](https://github.com/war4peace/image-toolbox/blob/main/workflows/seedvr2_upscale_workflow.json) (ComfyUI Upscale workflow)
[Back to ToC](#table-of-contents)

## Features:

### Setup script:

- **Checks for prerequisites**: Checks prerequisites and recommends which ones to install, in case they are not found.
- **Installs missing components**: Only performed if you confirm the requests. Components are NOT installed automatically.
- **Selectable models**: Detects GPU VRAM, recommends best model and lets you select SeedVR2 and Ollama models based on your GPU VRAM. There are four tiers:
  1. *24 GB and above*
  2. *16-24 GB*
  3. *12-16 GB*
  4. *Below 12 GB*
  You can override recommendations (e.g. if you want to use a smaller model)
- **Generates config**: The script generates a *config.json* file which saves your options. It also downloads the ComfyUI workflow used for image upscaling. The workflow can also be manually used from within ComfyUI for single-image upscaling.

Download it to a folder of your choice and run:

```powershell
.\setup.ps1
```
[Back to ToC](#table-of-contents)

### Setup script steps, explained:

**Step 1**: Prerequisites detection:

The script detects presence of prerequisite software. Example of successful detection, below:

```powershell
================================================================
  STEP 1 - Detecting Prerequisites
================================================================

  [OK]  Python found: Python 3.10.10
        Path: C:\Users\[username]\AppData\Local\Microsoft\WindowsApps\python.exe
  [OK]  Git found: git version 2.43.0.windows.1
  [OK]  NVIDIA GPU: NVIDIA GeForce RTX 3090, 591.86, 24576 MiB
  [OK]  CUDA Toolkit: Cuda compilation tools, release 12.4, V12.4.99
        Path: C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4
  [OK]  Ollama: ollama version is 0.18.3
  [OK]  ComfyUI venv: C:\Users\[username]\Documents\ComfyUI\.venv
        PyTorch: 2.6.0+cu124
  [OK]  ComfyUI data: C:\Users\[username]\Documents\ComfyUI
```

**Step 2**: Installs piexif module

**Step 3**: Installs SeedVR2 ComfyUI Node. This is the node which performs image upscaling

**Step 4**: Prompts for selecting the upscaled image's target resolution. There is a 15-second countdown, after which the recommended target resolution is selected. This is to allow the setup script to continue in case you steppoed away from the machine. Example below:

```powershell
================================================================
  STEP 4 - Target Resolution
================================================================

  Select the target upscale resolution:


  Select target resolution:
  Press Enter to accept, or a number key within 15 seconds:

    [Enter]  4K (3840x2160) [RECOMMENDED]  [RECOMMENDED]
    [1]      2K (2560x1440)
    [2]      1080p (1920x1080)

  Auto-selecting in 15 seconds...
  [OK]  Target resolution: 4K (3840x2160) (user selected)
  ```

**Step 5**: Prompts for Source Resolution Cutoff. Default value is 66%. This is useful to avoid wasting time and resources upscaling images which are close to the target resolution. The actual Maximum X and Maximum Y of the source resolution cutoff depend on Step 4 selection. In case you have selected "4K" at Step 4, the 66% default value prevents images with resolution just below 2K to be marked for processing. Entering a value of "0" will allow upscaling of images of any size below the target resolution. Example below:

```powershell
================================================================
  STEP 5 - Source Resolution Cutoff
================================================================

  Images already close to the target offer minimal upscale benefit.
  The cutoff skips sources at or above a percentage of the target resolution.

  At 66% with 4K target, images >= 2534x1426px will be skipped.
  (These would be upscaled less than 1.5x, with minimal visible gain.)

  Press Enter to accept 66%, or type a value (0-99) and press Enter:
  (0 = no cutoff - process all images regardless of source resolution)

  Cutoff percentage [66]:
  [OK]  Source cutoff: 66% (skip images >= 2534x1426px).
```

**Step 6**: SeedVR2 Model selection. This allows you to pick the appropriate SeedVR2 model, displaying the estimated VRAM usage. Just like at Step 4, a 15-second countdown helps auto-selecting the recommended value.

```powershell
================================================================
  STEP 6 - SeedVR2 Model Selection
================================================================

  [OK]  GPU VRAM: 24576 MB  ->  Recommended tier: 24GB+

  Select SeedVR2 DiT model:
  Press Enter to accept, or a number key within 15 seconds:

    [Enter]  seedvr2_ema_7b_fp16.safetensors  [24GB+ tier, ~16GB]  [RECOMMENDED]
    [1]      seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors  [16GB tier, ~8GB]
    [2]      seedvr2_ema_7b-Q4_K_M.gguf  [8GB tier, ~4GB]

  Auto-selecting in 15 seconds...
  [OK]  SeedVR2 model: seedvr2_ema_7b_fp16.safetensors (user selected)
        NOTE: This model will be downloaded automatically by ComfyUI on first use.
              Source: https://huggingface.co/models?other=seedvr
```

**Step 7**: Ollama Model Selection (for the "Tag and Rename" tool). The default recommended model is picked based on detected available VRAM. Example below:

```powershell
================================================================
  STEP 7 - Ollama Model
================================================================


  Select Ollama vision model:
  Press Enter to accept, or a number key within 15 seconds:

    [Enter]  llava:34b  [24GB+ tier, ~20GB]  [RECOMMENDED]
    [1]      llava:13b  [16GB tier, ~8GB]
    [2]      llava:7b  [12GB tier, ~5GB]

  Auto-selecting in 15 seconds...
  [OK]  Ollama model: llava:34b (user selected)
  [OK]  llava:34b is already pulled.
```

**Step 8**: ComfyUI Workflow. The tool downloads the ConfyUI workflow used for image upscaling from the repository, then adds it to ComfyUI's workflows path. If the workflow already exists, the script asks whether the existing workflow should be overwritten. Example below:

```powershell
================================================================
  STEP 8 - ComfyUI Workflow
================================================================

        Workflow already exists: C:\Users\[username]\Documents\ComfyUI\user\default\workflows\seedvr2_upscale_workflow.json

  Re-download and overwrite? [Y/N]: N
        Keeping existing workflow.
```

**Step 9**: Generation of the config.json file. This step takes all chosen settings and generates a simple configuration JSON. Editing this file directly can be performed if you decide some settings need to be changed. Alternatively, you can run setup again and pick different values. Example below:

```powershell
================================================================
  STEP 9 - Generating config.json
================================================================

  [!!]  config.json already exists.

  Overwrite with freshly detected values? [Y/N]: N
        Keeping existing config.json.
```

**Final step**: Discord webhook. This step is optional (you don't need to add one), but in case you use Discord and have a server, it is useful for notifications from the tools. The script verifies the webhook's validity and displays the retrieved Discord channel ther webhook connects to. Pressing Enter without entering a webhook URL will populate the config.json with an empty value, and this functionality will be disabled. Example below:

```powershell
  Discord webhook URL for script outage notifications (optional).
  How to create one: https://support.discord.com/hc/en-us/articles/228383668

  Enter webhook URL, or press Enter to skip: https://discord.com/api/webhooks/[webhook_string]
  [OK]  Discord webhook verified - connected to channel: image-toolbox
```

**Summary**: The script generates a setup summary and presents next steps.

```powershell
================================================================
  SETUP COMPLETE
================================================================

  Detected paths:

          ComfyUI venv     : C:\Users\[username]\Documents\ComfyUI\.venv
          Custom nodes     : C:\Users\[username]\Documents\ComfyUI\custom_nodes
          Models dir       : C:\Users\[username]\Documents\ComfyUI\models
          Workflows dir    : C:\Users\[username]\Documents\ComfyUI\user\default\workflows
          Config file      : X:\Work\AI\image-toolbox-testing\config.json

  Summary of actions taken this run:

    piexif                                 already installed
    SeedVR2 node                           already installed (ComfyUI-Manager)
    Target resolution                      4K (3840x2160) (user selected)
    Source cutoff                          66% (skip images >= 2534x1426px)
    SeedVR2 model                          seedvr2_ema_7b_fp16.safetensors [24GB+ tier] (user selected)
    Ollama (llava:34b)                     already pulled (user selected)
    Workflow JSON                          already present (kept)
    config.json                            kept existing (not overwritten)
    Discord webhook                        configured and verified (channel: image-toolbox)

  Next steps:

  1. Start ComfyUI Desktop and verify the SeedVR2 node loads
     without errors.
  2. Open the workflow in ComfyUI to verify it works:
     C:\Users\[username]\Documents\ComfyUI\user\default\workflows\seedvr2_upscale_workflow.json
  3. Run one image through SeedVR2 to trigger automatic model
     weight download (first run only, may take a while).
  4. Run batch_upscale.py:
     C:\Users\[username]\Documents\ComfyUI\.venv\Scripts\python.exe batch_upscale.py "X:\Your\Photos"
  5. Run tag_and_rename.py:
     C:\Users\[username]\Documents\ComfyUI\.venv\Scripts\python.exe tag_and_rename.py "X:\Your\Photos"
```

**Known issues**:

- If the Powershell terminal is open and you perform installations of prerequisite software, the terminal environment will not ba aware of the changes. If you run the script again, it will still flag software as missing. New installs are only visible from terminals open after software installation has finished. Close the terminal, install the software, then open the terminal again and re-run the setup.
- ComfyUI Desktop needs to be started at least once before the script could etect the Python Virtual Environment (venv) required for the script to perform necessary actions.
- In order to trigger automatic model weight download, the ComfyUI image upscale workflow needs to be triggered at least once.
[Back to ToC](#table-of-contents)


### Batch Image Upscaling:

- **High Quality Results**: Uses [SeedVR 2.5](https://github.com/ByteDance-Seed/SeedVR) through a ComfyUI workflow to batch-upscale low-resolution images into high-resolution images, focusing on quality, rather than speed. By default, the upscaling process limits destination image resolution to a maximum of 3840 pixels (horizontally) or 2160 pixels (vertically). This allows the resulting images to be shown on 4K screens using their native resolutions.

- **Accommodates different GPUs**: The tool offers several options which allow usage on a variety of nVidia graphics cards (minimum VRAM required: 8 GB). *Note: the tool has only been tested using a RTX 3090 GPU with 24 GB VRAM.*

- **Works with network drives**: As long as the network drive is mapped locally, the tool can access it and process the images in it. *Note: the tool has been tested on 10g and 1g wired connections to a remote NAS. Wi-Fi performance might be impacted by bandwidth, especially when performing the initial source path analysis.*

- **Source image resolution cut-off**: By default, the upscaling process skips source files with a resolution greater than 66% of either X or Y target resolution. This is to avoid upscaling images which are already large enough and wasting time on a less than 1.5x upscale target resolution. This value can be changed during setup.

- **Separate output destination**: You can specify the destination path. The tool outputs images to that destination folder but mirrors the original path tree and image filenames. Example: Source_path\Wedding\Evening\pic009.jpg will be upscaled to Destination_path\Wedding\Evening\pic009.png. *Source files and paths are never modified in any way, ensuring your original data is safe*.

- **Logging**: The tool logs each run to a new log file. The log file is located in the *.\logs* subfolder of the tool location. This allows you to review output.

- **Corrupted and missing file management**: The tool safely detects, logs and skips corrupted image files. It also skips images which have been removed from the source paths and continues the process, but logs them as missing.

- **Second pass**: When the tool finishes its current batch, it parses the source path again, looking for changes (in case new images were added to the source path while processing) . If new images are found, they are processed as well, using the same paramaters as initially used. This is useful in case you want to add more images to process. *This feature was inspired by those cloth washers with their little window where one could add clothing while the washer is working*.

- **Interactive options**: You can pause, resume or quit the batch upscaling process at any time. If an image upscale process is currently ongoing, the tool will wait until the process completes, logs progress and quit immediately afterwards. Press P or Space to pause the process, press Q to exit the tool.

- **File cache and process resuming**: The tool recursively analyzes all images from the data set and creates an initial cache of information for each file (located in the "scans" subfolder of the tool location). When the batch upscale process starts again, the image files are compared with their statuses from the cache file. Already-processed and non-eligible images are then skipped, but new files (if any) are added to the cache. This speeds up the process in case the batch job is stopped and resumed later. *Make sure you reuse the same parameters (source path and destination path) when resuming the process.*

- **Condensed terminal output**: For each processed image, the image upscaling time and total session elapsed time are displayed, together with total progres (processed/total files). The following elements are clickable (using ctrl+click in Powershell terminal):
  1. *The first folder icon*: Opens the source file's folder.
  2. *The source image full path*: Opens the source file in the default image viewer.
  3. *The second folder icon*: Opens the upscaled file's folder.
  4. *The checkmark character*: Opens the upscaled file in the default image viewer.

Terminal output example for one folder:
```
📁  !Canon A430\2007-05-17
2026-03-28 | 16:58:19 |   [2/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3868.jpg 📁 ✓ | 00:28 | Total: 00:00:28
2026-03-28 | 16:58:48 |   [3/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3869.jpg 📁 ✓ | 00:27 | Total: 00:00:56
2026-03-28 | 16:59:17 |   [4/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3870.jpg 📁 ✓ | 00:27 | Total: 00:01:25
2026-03-28 | 16:59:46 |   [5/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3871.jpg 📁 ✓ | 00:27 | Total: 00:01:54
2026-03-28 | 17:00:15 |   [6/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3872.jpg 📁 ✓ | 00:27 | Total: 00:02:23
2026-03-28 | 17:00:43 |   [7/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3873.jpg 📁 ✓ | 00:27 | Total: 00:02:52
2026-03-28 | 17:01:12 |   [8/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3874.jpg 📁 ✓ | 00:31 | Total: 00:03:24
  Folder done in 3m 25s
```

- **Clear, detailed session summary**: When the session ends (or is interrupted), the tool displays a detailed activity summary. The "Folder" column's rows and Log path entries are all clickable, and open using the default local method for each type.

Terminal output example of session summary:

```
=============================================================================================
  Folder                        Total  Processed  Skipped  Corrupt  Failed  Elapsed
---------------------------------------------------------------------------------------------
  !Canon A430\!Samsung A5 2016      1          0        0        1       0       0s
  !Canon A430\2007-05-18           15         15        0        0       0   7m 16s
  !Canon A430\2007-05-19           32         32        0        0       0  15m 13s
=============================================================================================
  TOTAL                            48         47        0        1       0  00:22:30
=============================================================================================
  (47 processed, 0 already done, 0 too large, 1 corrupted, 0 failed)
Log written to: X:\Work\AI\image-toolbox\logs\batch_upscale_2026-03-28_17-19-01.log
```


**How to run the Batch Upscale tool**:
Open PowerShell in the folder where the python file has been saved.
- Using defaults: `python .\batch_upscale.py X:\source\path\`. The tool will ask for target path; press Enter to use the default value ("__upscaled__" subfolder)
- Using specific parameters: `python .\batch_upscale.py X:\source\path\ Z:\destination\path`.

**Known issues**:

- On long batch runs (24+ hours) *and* at least one batch process pause (with VRAM re-use by a different tool, such as another AI process or a demanding game), ComfyUI occasionally locks up. Resuming the batch upscale script might work for a while, then it would not be able to connect to ComfyUI any more. Restarting ComfyUI does not solve the problem. This issue seems to be caused by either VRAM fragmentation or ComfyUI sub-process hanging. The simplest workaround is to restart the OS.
[Back to ToC](#table-of-contents)


### Tag and Rename:

**NOTE**: This tool is in early alpha. It is missing some Quality-of-Life functionality. It works, but only at a basic level. I recommend to only use it for testing, on disposable sample imagesets, for the time being. Its documentation will be updated as more features are developed.
[Back to ToC](#table-of-contents)

### Sample Files

The [Samples](/samples/) subfolder contains a set of sample images: [original images](/samples/original/) and their [upscaled](/samples/upscaled/) versions. You can take a close look and compare pairs side-by-side, to see the upscale process, with its strengths and weaknesses. 
[Back to ToC](#table-of-contents)