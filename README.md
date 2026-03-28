# image-toolbox
AI-leveraged image alteration toolbox. Rescale, describe, rename files based on a variety of parameters, using local and remote AI models.

## WARNING: ==This toolbox is work-in-progress. Do NOT use it on important data. Always test with a small sample before committing to your precious files!==

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

