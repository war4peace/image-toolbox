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

; Referral link shown to users who pick a Remote/Both install (they need a
; RunPod account with credit to rent a GPU). Update here if the link changes.
#define RunPodReferral "https://runpod.io?ref=760zp6wq"

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
; The GUI package (scripts\gui\, split out of toolbox_gui.py in 0.4.3). The glob
; above is NON-recursive, so this subfolder needs its own entry or the whole GUI
; ships broken. Kept as a glob (like scripts\) so a new gui/ module is never left
; out. The import smoke test (tests/test_import_smoke.py) guards this too.
Source: "..\scripts\gui\*.py";    DestDir: "{app}\scripts\gui"; Flags: ignoreversion
; Remote-upscaling (#1) pod-side code: worker.py / deadman.py are SCP'd to the
; RunPod pod at runtime, provision.sh fills the model volume. provision.sh must
; stay LF (the .gitattributes rule keeps it LF; Inno ships byte-for-byte).
; Non-recursive, so pod\__pycache__ is not swept in.
Source: "..\pod\*.py";            DestDir: "{app}\pod"; Flags: ignoreversion
Source: "..\pod\*.sh";            DestDir: "{app}\pod"; Flags: ignoreversion
Source: "..\bootstrap.ps1";       DestDir: "{app}"; Flags: ignoreversion
Source: "..\Image Toolbox.cmd";   DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";           DestDir: "{app}"; Flags: ignoreversion
; Author benchmark data — drives the remote-pod "$ / 100 images" cost estimate
; (benchmarks.py reads it from {app}\docs).
Source: "..\docs\Benchmarks.csv";  DestDir: "{app}\docs"; Flags: ignoreversion
Source: "toolbox.ico";            DestDir: "{app}"; Flags: ignoreversion
; Running-app icon: shown on the main window title bar and taskbar button at
; runtime (gui/app.py loads {app}\app.ico via APP_ROOT). Distinct from
; toolbox.ico, which is the setup/uninstall/shortcut icon.
Source: "..\app.ico";             DestDir: "{app}"; Flags: ignoreversion
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
; Remote/Both need a RunPod account with credit to rent a GPU. Offer to open the
; referral sign-up page (opt-in checkbox on the finished page; only shown for a
; Remote/Both install). shellexec opens the URL in the default browser.
;
; The default checked-state differs between a first install and an upgrade, so the
; same offer is defined twice with mutually-exclusive Check functions:
;  - FIRST INSTALL: checked by default (the common first-run onboarding case).
;  - UPGRADE: shown UNCHECKED - by now the user already has an account or already
;    declined, so pre-ticking it would re-open the browser on every version bump.
; (Inno's `unchecked` flag is static per line, hence the two lines.)
Filename: "{#RunPodReferral}"; Description: "Create a RunPod account (opens your browser) - needed to rent a remote GPU"; Flags: postinstall shellexec nowait skipifsilent; Check: OfferRunPodChecked
Filename: "{#RunPodReferral}"; Description: "Create a RunPod account (opens your browser) - needed to rent a remote GPU"; Flags: postinstall shellexec nowait skipifsilent unchecked; Check: OfferRunPodUnchecked
; Launch the app when the wizard finishes - checked by default on BOTH a first
; install and an upgrade (so no `unchecked` flag).
Filename: "{app}\Image Toolbox.cmd"; Description: "Launch Image Toolbox now (first launch downloads ~3 GB of components)"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Remove what the bootstrap and the app created inside the install folder.
; The user's photos are never touched — they live elsewhere.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\seedvr2"
; The bundled ffmpeg build downloaded by bootstrap.ps1 (Video Upscaler)
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\scans"
Type: filesandordirs; Name: "{app}\trcache"
Type: filesandordirs; Name: "{app}\db"
Type: filesandordirs; Name: "{app}\__pycache__"
; The modules live in scripts\ (0.2.8); remove the folder and its runtime
; __pycache__ (the shipped .py are removed by their [Files] entry anyway).
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\pod"
Type: files; Name: "{app}\.setup_complete"
Type: files; Name: "{app}\gui_settings.json"
Type: files; Name: "{app}\bootstrap.log"
Type: files; Name: "{app}\install_mode.txt"

[Code]
{ Install-mode wizard page (#1 remote onboarding): lets the user pick whether the
  app upscales locally, on a rented RunPod pod, or both. The choice is written to
  install_mode.txt, which bootstrap.ps1 reads to decide what to download — Remote
  skips the ~3 GB local GPU stack (PyTorch CUDA + SeedVR2 + Ollama). }
var
  InstallModePage: TInputOptionWizardPage;
  gIsUpgrade: Boolean;      { set once in InitializeSetup, read by the Check funcs }

function InitializeSetup(): Boolean;
begin
  { Decide first-install vs upgrade BEFORE anything is installed. This run writes
    its OWN Inno uninstall key during the install step, so the check must happen
    here (the earliest event) or it would always look like an upgrade. A prior
    version's "..._is1" uninstall key present now = this is an upgrade. The app is
    always a per-user (lowest-privilege) install, so the key lives under HKCU;
    HKLM is checked too as a harmless belt-and-suspenders. }
  gIsUpgrade :=
    RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B4D4B6F2-1B0E-4A41-9C4B-1D0A6E4B7C21}_is1') or
    RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B4D4B6F2-1B0E-4A41-9C4B-1D0A6E4B7C21}_is1');
  Result := True;
end;

procedure InitializeWizard;
begin
  InstallModePage := CreateInputOptionPage(wpSelectTasks,
    'Installation mode', 'Where should Image Toolbox do the upscaling?',
    'Remote and Both rent a paid GPU on RunPod, so they need a RunPod account '
    + 'with credit - if you pick one, the last page can open a sign-up link for '
    + 'you. You can change this later by re-running the installer.',
    True,   { Exclusive: radio buttons }
    False);
  InstallModePage.Add('Local - upscale on this PC''s NVIDIA GPU (downloads ~3 GB now, AI weights ~16 GB on first run)');
  InstallModePage.Add('Remote - upscale on a rented RunPod GPU (lightweight; no local GPU or large downloads needed)');
  InstallModePage.Add('Both - local and remote (full install)');
  InstallModePage.SelectedValueIndex := 0;
end;

function InstallModeValue: String;
begin
  case InstallModePage.SelectedValueIndex of
    1: Result := 'remote';
    2: Result := 'both';
  else
    Result := 'local';
  end;
end;

{ Gate the referral [Run] checkbox: only a Remote/Both install needs a RunPod
  account. Local users never see the sign-up offer. }
function NeedsRunPodAccount: Boolean;
begin
  Result := (InstallModeValue = 'remote') or (InstallModeValue = 'both');
end;

{ First install + Remote/Both: show the sign-up offer CHECKED by default. }
function OfferRunPodChecked: Boolean;
begin
  Result := NeedsRunPodAccount and (not gIsUpgrade);
end;

{ Upgrade + Remote/Both: still show the offer, but UNCHECKED (see the [Run] note). }
function OfferRunPodUnchecked: Boolean;
begin
  Result := NeedsRunPodAccount and gIsUpgrade;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Write the marker after files are copied so bootstrap (run on first launch)
    can read it. Rewritten on every (re)install, so changing the mode is just a
    reinstall. }
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\install_mode.txt'), InstallModeValue, False);
end;
