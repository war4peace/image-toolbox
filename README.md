# image-toolbox
AI-leveraged image alteration toolbox. Rescale, describe, rename files based on a variety of parameters, using local and remote AI models.

## WARNING: This toolbox is work-in-progress. Do NOT use it on important data. Always test with a small sample before committing to your precious files!
## I am not reponsible for data loss. Use this tool at your own risk.

This is a set of tools created to help with upscaling, describing and renaming image files. Useful to enhance your personal image collection and improve old pictures which were taken using older cameras. 

## Prerequisite software:

1. [ComfyUI Desktop](https://www.comfy.org) 
2. [Ollama](https://ollama.com)
3. [Python 3.12.9](https://www.python.org/downloads/windows/) (Tested. More recent versions might work as well)
4. [Git for Windows](https://gitforwindows.org)
5. [NVIDIA CUDA Toolkit 12.4](https://developer.nvidia.com/cuda-12-4-0-download-archive?target_os=Windows&target_arch=x86_64)

## Files in this repository:

1. [setup.ps1](https://github.com/war4peace/image-toolbox/blob/main/setup.ps1) (configuration script)
2. [batch_upscale.py](https://github.com/war4peace/image-toolbox/blob/main/batch_upscale.py) (image upscale script)
3. [config.json](https://github.com/war4peace/image-toolbox/blob/main/config.json) (configuration file)
4. [tag_and_rename.py](https://github.com/war4peace/image-toolbox/blob/main/tag_and_rename.py) (describes and renames images)
5. [seedvr2_upscale_workflow.json](https://github.com/war4peace/image-toolbox/blob/main/workflows/seedvr2_upscale_workflow.json) (ComfyUI Upscale workflow)

## Features:

### Batch Image Upscaling:

- **High Quality Results**: Uses [SeedVR 2.5](https://github.com/ByteDance-Seed/SeedVR) through a ComfyUI workflow to batch-upscale low-resolution images into high-resolution images, focusing on quality, rather than speed. By default, the upscaling process limits destination image resolution to a maximum of 3840 pixels (horizontally) or 2160 pixels (vertically). This allows the resulting images to be shown on 4K screens using their native resolutions.
- **Accommodates different GPUs**: The tool offers several options which allow usage on a variety of nVidia graphics cards (minimum VRAM required: 8 GB). *However, the tool has only been tested using a RTX 3090 GPU with 24 GB VRAM.*
- **Source image resolution cut-off**: By default, the upscaling process skips source files with a resolution greater than 66% of either X or Y target resolution. This is to avoid upscaling images which are already large enough and wasting time on a less than 1.5x upscale target resolution. This value can be changed during setup.
- **Separate output destination**: You can specify the destination path. The tool outputs images to that destination folder but mirrors the original path tree and image filenames. Example: Source_path\Wedding\Evening\pic009.jpg will be upscaled to Destination_path\Wedding\Evening\pic009.png. *Source files and paths are never modified in any way, ensuring your original data is safe*.
- **Logging**: The tool logs each run to a new log file. The log file is located in the *.\logs* subfolder of the tool location. This allows you to review output.
- **Corrupted and missing file management**: The tool safely detects, logs and skips corrupted image files. It also skips images which have been removed from the source paths and continues the process, but logs them as missing.
- **Interactive options**: You can pause, resume or quit the batch upscaling process at any time. If an image upscale process is currently ongoing, the tool will wait until the process completes, logs progress and quit immediately afterwards. Press P or Space to pause the process, press Q to exit the tool.
- **File cache and process resuming**: The tool recursively analyzes all images from the data set and creates an initial cache of information for each file. When the batch upscale process starts again, the image files are compared with their statuses from the cache file. Already-processed and non-eligible images are then skipped, but new files (if any) are added to the cache. This speeds up the process in case the batch job is stopped and resumed later. *Make sure you reuse the same parameters (source path and destination path) when resuming the process.*
- **Condensed terminal output**: For each processed image, the image upscaling time and total session elapsed time are displayed, together with total progres (processed/total files). The following elements are clickable (using ctrl+click in Powershell terminal):
  1. *The first folder icon*: Opens the source file's folder.
  2. *The source image full path*: Opens the source file in the default image viewer.
  3. *The second folder icon*: Opens the upscaled file's folder.
  4. *The checkmark character*: Opens the upscaled file in the default image viewer.
- **Clear, detailed session summary**: When the session ends (or is interrupted), the tool displays a detailed activity summary.

Terminal output example for one folder:

`📁  !Canon A430\2007-05-17
2026-03-28 | 16:58:19 |   [2/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3868.jpg 📁 ✓ | 00:28 | Total: 00:00:28
2026-03-28 | 16:58:48 |   [3/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3869.jpg 📁 ✓ | 00:27 | Total: 00:00:56
2026-03-28 | 16:59:17 |   [4/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3870.jpg 📁 ✓ | 00:27 | Total: 00:01:25
2026-03-28 | 16:59:46 |   [5/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3871.jpg 📁 ✓ | 00:27 | Total: 00:01:54
2026-03-28 | 17:00:15 |   [6/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3872.jpg 📁 ✓ | 00:27 | Total: 00:02:23
2026-03-28 | 17:00:43 |   [7/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3873.jpg 📁 ✓ | 00:27 | Total: 00:02:52
2026-03-28 | 17:01:12 |   [8/39688] 800x600px -> 2880x2160px 📁 X:\Personale\Poze\!Canon A430\2007-05-17\IMG_3874.jpg 📁 ✓ | 00:31 | Total: 00:03:24
  Folder done in 3m 25s`


How to run the Batch Upscale tool:
Open PowerShell in the folder where the python file has been saved.
- Using defaults: `python .\batch_upscale.py X:\source\path\`. The tool will ask for target path; press Enter to use the default value ("__upscaled__" subfolder)
