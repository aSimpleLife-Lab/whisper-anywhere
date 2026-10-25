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

[Registry]
; The app adds this when "Start with Windows" is on; take it away on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Whisper Anywhere"; ValueType: none; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Whisper Anywhere.exe"; Description: "Start Whisper Anywhere"; Flags: nowait postinstall skipifsilent
