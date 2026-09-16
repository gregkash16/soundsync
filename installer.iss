; Inno Setup script for Sound Syncing.
;
; Wraps the PyInstaller onedir output (dist\SoundSync\) in a normal Windows
; installer: Start Menu entry, optional desktop shortcut, Add/Remove Programs
; listing, clean uninstall. Nothing about the app changes -- it is the same
; folder, just put somewhere sensible by a setup wizard instead of by hand.
;
; Don't run this directly; build.py --installer compiles it and passes the
; defines below. If you must run it by hand:
;   ISCC /DAppVersion=2.1 /DSourceDir="dist\SoundSync" /DOutputDir="dist" installer.iss
;
; Installs per-user by default (no UAC prompt, lands in
; %LOCALAPPDATA%\Programs\Sound Syncing). The wizard lets the user pick
; all-users instead, which then needs admin.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\SoundSync"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif

#define AppName "Sound Syncing"
#define AppExe "SoundSync.exe"
#define AppPublisher "Greg Kash"

[Setup]
; Never change AppId -- it is how Windows knows a new version replaces the old
; install instead of sitting next to it.
AppId={{6F1E0C7A-9B2D-4E7B-8C41-2A5D3F0B9E11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} installer

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir={#OutputDir}
OutputBaseFilename=SoundSync-Setup-{#AppVersion}
; 300 MB of numpy/scipy/ffmpeg -- worth the compression time.
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes

WizardStyle=modern
; If the app is open, offer to close it rather than fail mid-copy on a locked
; .pyd -- same trap build.py guards against.
CloseApplications=yes
RestartApplications=no

#ifexist "icon.ico"
SetupIconFile=icon.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Sync dual-system audio to camera clips"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// Settings live in %LOCALAPPDATA%\SoundSync (see soundsync/options.py), not
// under {app}, so a plain uninstall leaves them. Ask rather than assume:
// someone reinstalling wants their presets back; someone removing it for
// good probably doesn't want a stray folder.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  SettingsDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    SettingsDir := ExpandConstant('{localappdata}\SoundSync');
    if DirExists(SettingsDir) then
    begin
      if MsgBox('Also remove your saved settings and presets?' + #13#10 + #13#10 +
                SettingsDir, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(SettingsDir, True, True, True);
    end;
  end;
end;
