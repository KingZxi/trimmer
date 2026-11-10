; Inno Setup script for the Trimmer installer (https://jrsoftware.org/isinfo.php).
; packaging\build.ps1 runs it after PyInstaller: ISCC /DAppVersion=x.y.z packaging\installer.iss
; Installs for the current user by default (no admin needed); the user can
; choose "all users" instead. Settings in %APPDATA%\Trimmer are left alone
; on uninstall.

#ifndef AppVersion
  #define AppVersion "1.3.0"
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
UninstallDisplayName=Trimmer
OutputDir=..\dist
OutputBaseFilename=Trimmer-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; Updates: the only part of Trimmer that uses the internet. "Local features only" leaves the update
; file out, so the installed app has no code that connects to anything.
Name: "updates"; Description: "Allow update checks (Trimmer asks GitHub for new versions when it opens; nothing about you or your clips is sent)"; GroupDescription: "Updates:"; Flags: exclusive
Name: "localonly"; Description: "Local features only (Trimmer never connects to the internet)"; GroupDescription: "Updates:"; Flags: exclusive unchecked

[InstallDelete]
; Changing to "Local features only" on a reinstall removes an update file left by an earlier install.
Type: filesandordirs; Name: "{app}\_internal\updater"

[Files]
Source: "..\dist\Trimmer\*"; DestDir: "{app}"; Excludes: "\_internal\updater"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\Trimmer\_internal\updater\*"; DestDir: "{app}\_internal\updater"; Tasks: updates; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Trimmer"; Filename: "{app}\Trimmer.exe"
Name: "{autodesktop}\Trimmer"; Filename: "{app}\Trimmer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Trimmer.exe"; Description: "{cm:LaunchProgram,Trimmer}"; Flags: nowait postinstall skipifsilent
; Install update in Trimmer runs this installer silently with /RELAUNCH=1: reopen Trimmer afterwards.
Filename: "{app}\Trimmer.exe"; Flags: nowait runasoriginaluser; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
