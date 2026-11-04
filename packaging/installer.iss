; Inno Setup script for the Trimmer installer (https://jrsoftware.org/isinfo.php).
; packaging\build.ps1 runs it after PyInstaller: ISCC /DAppVersion=x.y.z packaging\installer.iss
; Installs for the current user by default (no admin needed); the user can
; choose "all users" instead. Settings in %APPDATA%\Trimmer are left alone
; on uninstall.

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif

[Setup]
AppId={{8B121058-7B33-484F-8098-37A6F0E9560E}
AppName=Trimmer
AppVersion={#AppVersion}
AppPublisher=KingZxi
AppPublisherURL=https://github.com/KingZxi
AppCopyright=© 2026 KingZxi
DefaultDirName={autopf}\Trimmer
DefaultGroupName=Trimmer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\LICENSE
SetupIconFile=..\trimmer\gui\assets\trimmer.ico
UninstallDisplayIcon={app}\Trimmer.exe
OutputDir=..\dist
OutputBaseFilename=Trimmer-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Trimmer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Trimmer"; Filename: "{app}\Trimmer.exe"
Name: "{autodesktop}\Trimmer"; Filename: "{app}\Trimmer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Trimmer.exe"; Description: "{cm:LaunchProgram,Trimmer}"; Flags: nowait postinstall skipifsilent
