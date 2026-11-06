; Inno Setup script: one setup .exe built from the lite build (scripts\build_installer.ps1).
; Installs per user, so it needs no administrator rights.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{1F9D66B4-41F7-4B94-B0EC-4C84B1BCE8A7}
AppName=Whisper Anywhere
AppVersion={#AppVersion}
AppVerName=Whisper Anywhere {#AppVersion}
AppPublisher=aSimpleLife Lab
AppPublisherURL=https://github.com/aSimpleLife-Lab/whisper-anywhere
AppSupportURL=https://github.com/aSimpleLife-Lab/whisper-anywhere/issues
DefaultDirName={localappdata}\Programs\Whisper Anywhere
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=WhisperAnywhere-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Whisper Anywhere.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist-lite\Whisper Anywhere\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Whisper Anywhere"; Filename: "{app}\Whisper Anywhere.exe"
Name: "{autodesktop}\Whisper Anywhere"; Filename: "{app}\Whisper Anywhere.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Whisper Anywhere.exe"; Description: "Start Whisper Anywhere"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

// The app adds this Run value when "Start with Windows" is on. Every copy
// (installed, portable, from source) shares the one value, so uninstall takes
// it away only when it starts this install.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
begin
  if (CurUninstallStep = usUninstall)
    and RegQueryStringValue(HKCU, RunKey, 'Whisper Anywhere', Command)
    and (Pos(Lowercase(ExpandConstant('{app}\')), Lowercase(Command)) > 0) then
    RegDeleteValue(HKCU, RunKey, 'Whisper Anywhere');
end;
