; Inno Setup script for the Image Toolbox Windows installer.
; Compiled in CI by .github/workflows/build-installer.yml, or locally with:
;   ISCC.exe installer\ImageToolbox.iss
;
; The installer itself is small: it ships only the toolbox scripts and the
; bootstrap. The first launch of the app downloads the heavy components
; (Python, PyTorch CUDA, the SeedVR2 engine — about 3 GB) via bootstrap.ps1,
; and the AI model weights (~16 GB) are fetched by the app on first upscale.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{B4D4B6F2-1B0E-4A41-9C4B-1D0A6E4B7C21}
AppName=Image Toolbox
AppVersion={#MyAppVersion}
AppPublisher=war4peace
AppPublisherURL=https://github.com/war4peace/image-toolbox
DefaultDirName={localappdata}\Programs\Image Toolbox
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=ImageToolboxSetup
SetupIconFile=toolbox.ico
UninstallDisplayIcon={app}\toolbox.ico
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Ship every app Python module. The modules now live in scripts\ (0.2.8); a glob
; (not a hand-maintained list) so a new module can never again be silently left
; out of the installer — the failure that broke 0.2.5 (system_telemetry.py /
; crash_logger.py missing). Non-recursive, so the vendored seedvr2\ tree and
; tools\ are not swept in.
Source: "..\scripts\*.py";        DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\bootstrap.ps1";       DestDir: "{app}"; Flags: ignoreversion
Source: "..\Image Toolbox.cmd";   DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "toolbox.ico";            DestDir: "{app}"; Flags: ignoreversion
; Never overwrite the user's configuration on upgrades, never delete it on uninstall
Source: "..\config.json";         DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[InstallDelete]
; Remove the first-launch marker on every (re)install so the bootstrap re-runs
; after an upgrade and installs any newly added Python dependencies (e.g. timm
; for auto-straighten). Bootstrap is idempotent — already-present components
; (Python, the venv, torch, the SeedVR2 engine) are detected and skipped.
Type: files; Name: "{app}\.setup_complete"
; 0.2.8 moved the modules into scripts\. Delete the stale root-level .py and
; their compiled cache from pre-0.2.8 installs, or old and new copies coexist
; (and an import could resolve the wrong one).
Type: files;          Name: "{app}\*.py"
Type: filesandordirs; Name: "{app}\__pycache__"

[Icons]
Name: "{autoprograms}\Image Toolbox"; Filename: "{app}\Image Toolbox.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\toolbox.ico"
Name: "{autodesktop}\Image Toolbox";  Filename: "{app}\Image Toolbox.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\toolbox.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\Image Toolbox.cmd"; Description: "Launch Image Toolbox now (first launch downloads ~3 GB of components)"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; Remove what the bootstrap and the app created inside the install folder.
; The user's photos are never touched — they live elsewhere.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\seedvr2"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\scans"
Type: filesandordirs; Name: "{app}\trcache"
Type: filesandordirs; Name: "{app}\db"
Type: filesandordirs; Name: "{app}\__pycache__"
; The modules live in scripts\ (0.2.8); remove the folder and its runtime
; __pycache__ (the shipped .py are removed by their [Files] entry anyway).
Type: filesandordirs; Name: "{app}\scripts"
Type: files; Name: "{app}\.setup_complete"
Type: files; Name: "{app}\gui_settings.json"
Type: files; Name: "{app}\bootstrap.log"
