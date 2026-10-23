; installer.iss
#define MyAppName "CommsPlan"
#define MyAppVersion "2.0.6.2"
#define MyAppExeName "CommsPlan.exe"

[Setup]
; Preserve the historical implicit AppId and previous installation discovery.
AppId=FreqInOut
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=N1MAG
AppPublisherURL=https://github.com/N1MAG/CommsPlan
AppSupportURL=https://github.com/N1MAG/CommsPlan/issues
AppUpdatesURL=https://github.com/N1MAG/CommsPlan/releases
DefaultDirName={autopf}\FreqInOut
UsePreviousAppDir=yes
; Refuse active apps instead of silently closing/restarting station control.
CloseApplications=no
RestartApplications=no
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputBaseFilename=CommsPlan-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}

; Use a custom installer icon
SetupIconFile="assets\CommsPlan.ico"

[Files]
; Install the built app folder; exclude git/venv/logs/db/config files
Source: "dist\CommsPlan\*"; DestDir: "{app}"; Flags: recursesubdirs; Excludes: ".git*;*.pdb;*.obj;__pycache__\*;venv\*;.venv\*;*.log;*.db;config\config.json;freqinout\logs\*"
; Same signed build under the historical path keeps old shortcuts/pins working.
; Never leave the historical binary paired with a replaced _internal runtime.
Source: "dist\CommsPlan\CommsPlan.exe"; DestDir: "{app}"; DestName: "FreqInOut.exe"; Flags: ignoreversion
Source: "assets\CommsPlan.ico"; DestDir: "{app}"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function StationProcessBlockReason: String;
var
  Locator, Services, Processes: Variant;
begin
  Result := '';
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery(
      'SELECT ProcessId FROM Win32_Process WHERE Name = ''FreqInOut.exe'' OR Name = ''CommsPlan.exe''');
    if Processes.Count > 0 then
      Result := 'Close FreqInOut and CommsPlan before changing this installation. Save your work and retry; Setup will not stop station applications.';
  except
    Log('Unable to verify station processes: ' + GetExceptionMessage);
    Result := 'Setup could not verify whether FreqInOut or CommsPlan is running. No installation changes were started. Resolve Windows process inspection and retry.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := StationProcessBlockReason;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Reason: String;
begin
  if CurStep = ssInstall then
  begin
    Reason := StationProcessBlockReason;
    if Reason <> '' then
      RaiseException(Reason);
  end;
end;

function InitializeUninstall: Boolean;
var
  Reason: String;
begin
  Reason := StationProcessBlockReason;
  Result := Reason = '';
  if not Result then
  begin
    Log(Reason);
    if not UninstallSilent then
      MsgBox(Reason, mbError, MB_OK);
  end;
end;
