#ifndef BundleRoot
  #error BundleRoot must be defined
#endif
#ifndef AppVersion
  #error AppVersion must be defined
#endif
#ifndef OutputDir
  #error OutputDir must be defined
#endif

#define AppName "Virtual Companion"
#define AppPublisher "Achenyiyi"
#define AppUrl "https://github.com/Achenyiyi/virtual_girl"
#define InstallerBaseName "VirtualCompanion-" + AppVersion + "-windows-x64"

[Setup]
AppId={{74956216-5E1F-4EFF-A9BD-675365241AF5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={localappdata}\Programs\Virtual Companion
DefaultGroupName=Virtual Companion
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#OutputDir}
OutputBaseFilename={#InstallerBaseName}
Compression=lzma2/ultra64
SolidCompression=yes
#ifdef SignToolName
SignTool={#SignToolName}
SignedUninstaller=yes
#else
SignedUninstaller=no
#endif
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\airi\airi.exe
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Windows installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Files]
Source: "{#BundleRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Virtual Companion"; Filename: "{app}\launch-companion.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\airi\airi.exe"
Name: "{group}\Diagnostics"; Filename: "{app}\diagnostics.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\airi\airi.exe"
Name: "{group}\Uninstall Virtual Companion"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\launch-companion.cmd"; Description: "Launch Virtual Companion"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent shellexec

[Code]
function SkipAvatarTokenProvisioning: Boolean;
begin
  Result := CompareText(ExpandConstant('{param:SkipAvatarToken|0}'), '1') = 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  PythonPath: String;
  ConfigPath: String;
  Parameters: String;
begin
  if (CurStep <> ssPostInstall) or SkipAvatarTokenProvisioning then
    Exit;

  PythonPath := ExpandConstant('{app}\runtime\python.exe');
  ConfigPath := ExpandConstant('{app}\config\production.yaml');
  Parameters := '-I -s -B -m companion --config "' + ConfigPath + '" --provision-avatar-token';
  if not Exec(PythonPath, Parameters, ExpandConstant('{app}'), SW_HIDE,
    ewWaitUntilTerminated, ResultCode) then
    RaiseException('Unable to initialize the local Avatar Bridge credential.');

  { Exit code 1 means the credential already exists, which is valid during an upgrade. }
  if (ResultCode <> 0) and (ResultCode <> 1) then
    RaiseException('Avatar Bridge credential initialization failed.');
end;
