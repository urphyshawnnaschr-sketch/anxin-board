#ifndef PayloadRoot
  #error PayloadRoot must be supplied by build-installer.ps1
#endif
#ifndef OutputRoot
  #error OutputRoot must be supplied by build-installer.ps1
#endif
#ifndef LauncherRoot
  #error LauncherRoot must be supplied by build-installer.ps1
#endif
#ifndef SourceCommit
  #error SourceCommit must be supplied by build-installer.ps1
#endif
#ifndef SourceCommitShort
  #error SourceCommitShort must be supplied by build-installer.ps1
#endif

#define ProductAppId "{{06AEBA51-E4DC-4F31-83FE-790A9BCE45CB}"

[Setup]
AppId={#ProductAppId}
AppName=AnxinBoard
AppVersion=0.1.0
AppVerName=AnxinBoard Windows Candidate {#SourceCommitShort}
AppPublisher=AnxinBoard
DefaultDirName={localappdata}\Programs\AnxinBoard Installer Control
DefaultGroupName=AnxinBoard
DisableProgramGroupPage=yes
DirExistsWarning=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputRoot}
OutputBaseFilename=AnxinBoard-Setup-candidate-{#SourceCommitShort}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
SetupIconFile={#LauncherRoot}\AnxinBoard.ico
UninstallDisplayIcon={app}\安心看板.exe
CloseApplications=no
RestartIfNeededByRun=no
Uninstallable=yes
UninstallDisplayName=AnxinBoard
VersionInfoVersion=0.1.0.0
VersionInfoTextVersion=0.1.0-candidate-{#SourceCommitShort}
VersionInfoProductName=AnxinBoard
VersionInfoDescription=AnxinBoard current-user installer candidate
VersionInfoCompany=AnxinBoard

; The secure launcher/session authority remains bounded to Start + Exit.
; Offline restore is exposed only through restore-product-backup.ps1, which takes the
; shared launcher lifecycle lock and requires an explicit Human confirmation.
;
; Product binaries are installed by install-runtime.ps1 into the separate
; content-addressed current-user product root under %LOCALAPPDATA%\Programs\AnxinBoard.

[InstallDelete]
; Remove stale installer-owned controls before replacing the accepted Start/Exit pair.
; Restore-backup remains gated and is deleted if an older owner-test candidate left it.
Type: files; Name: "{app}\tools\start-installed-product.ps1"
Type: files; Name: "{app}\tools\stop-installed-product.ps1"
Type: files; Name: "{app}\tools\restore-product-backup.ps1"
Type: files; Name: "{group}\安心看板.lnk"
Type: files; Name: "{group}\安心看板 - 退出.lnk"
Type: files; Name: "{group}\安心看板 - 恢复备份.lnk"

[Files]
Source: "{#LauncherRoot}\安心看板.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#LauncherRoot}\AnxinBoard.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "uninstall-product.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "rollback-runtime.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "resolve-installed-runtime.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "start-installed-product.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "stop-installed-product.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "restore-product-backup.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

; Upgrade cleanup uses the same fail-closed lifecycle identity check as the shipped Exit
; shortcut. Keep a temporary copy so InstallProductRuntime does not depend on an older
; control-root copy during an in-place upgrade.
Source: "stop-installed-product.ps1"; DestDir: "{tmp}\AnxinBoardInstaller"; Flags: ignoreversion

; The runtime payload is embedded in Setup but extracted only into Setup's temporary
; directory. install-runtime.ps1 verifies the runtime manifest/member hashes again and
; atomically stages the content-addressed Product version into the Product-owned root.
Source: "{#PayloadRoot}\AnxinBoard.Runtime\*"; DestDir: "{tmp}\AnxinBoardPayload\AnxinBoard.Runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadRoot}\runtime-manifest.json"; DestDir: "{tmp}\AnxinBoardPayload"; Flags: ignoreversion
Source: "install-runtime.ps1"; DestDir: "{tmp}\AnxinBoardInstaller"; Flags: ignoreversion

[Icons]
Name: "{group}\安心看板"; Filename: "{app}\安心看板.exe"; WorkingDir: "{app}"; IconFilename: "{app}\AnxinBoard.ico"
Name: "{autodesktop}\安心看板"; Filename: "{app}\安心看板.exe"; WorkingDir: "{app}"; IconFilename: "{app}\AnxinBoard.ico"
Name: "{group}\安心看板 - 退出"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\stop-installed-product.ps1"""; WorkingDir: "{app}\tools"; IconFilename: "{app}\AnxinBoard.ico"
Name: "{group}\安心看板 - 恢复备份"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\restore-product-backup.ps1"""; WorkingDir: "{app}\tools"; IconFilename: "{app}\AnxinBoard.ico"

[Code]
function WindowsPowerShellPath(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

procedure InstallProductRuntime(); forward;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Params: String;
begin
  Result := '';
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$g=Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1; ' +
    'if ($null -eq $g) { exit 20 }; ' +
    '& $g.Source --no-lazy-fetch --version *> $null; ' +
    'if ($LASTEXITCODE -ne 0) { exit 21 }; exit 0"';

  if not Exec(WindowsPowerShellPath(), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'AnxinBoard could not verify the required Git runtime. Install Git for Windows 2.45 or newer and retry.';
    exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := 'AnxinBoard requires Git for Windows 2.45 or newer with --no-lazy-fetch support. Install or upgrade Git, then run Setup again.';
    exit;
  end;

  // A failed AfterInstall callback is not a fatal Setup error. PrepareToInstall
  // returns a documented nonzero Setup exit before controls/shortcuts are changed.
  try
    ExtractTemporaryFiles('{tmp}\AnxinBoardInstaller\*');
    ExtractTemporaryFiles('{tmp}\AnxinBoardPayload\*');
    InstallProductRuntime();
  except
    Result := GetExceptionMessage;
    Log('ANXIN_RUNTIME_PREPARE=FAILED: ' + Result);
  end;
end;

procedure StopPreviousProductRuntime();
var
  ResultCode: Integer;
  ScriptPath: String;
  Params: String;
begin
  ScriptPath := ExpandConstant('{tmp}\AnxinBoardInstaller\stop-installed-product.ps1');
  if not FileExists(ScriptPath) then
    RaiseException('AnxinBoard upgrade lifecycle stop contract is missing.');

  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ScriptPath + '"';
  if not ExecAndLogOutput(WindowsPowerShellPath(), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode, nil) then
    RaiseException('AnxinBoard could not run the upgrade lifecycle stop contract.');
  if ResultCode <> 0 then
    RaiseException(Format('AnxinBoard refused to replace a running or unidentified runtime (exit code %d).', [ResultCode]));

  Log('ANXIN_UPGRADE_PREVIOUS_RUNTIME=STOPPED_OR_ABSENT');
end;

procedure InstallProductRuntime();
var
  ResultCode: Integer;
  ScriptPath: String;
  PayloadPath: String;
  Params: String;
begin
  // A Human-started Setup may update install state only after a prior owner-test runtime
  // has either been positively stopped by its durable run identity or proven absent.
  StopPreviousProductRuntime();

  ScriptPath := ExpandConstant('{tmp}\AnxinBoardInstaller\install-runtime.ps1');
  PayloadPath := ExpandConstant('{tmp}\AnxinBoardPayload');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ScriptPath + '" -PayloadRoot "' + PayloadPath + '"';

  if not ExecAndLogOutput(WindowsPowerShellPath(), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode, nil) then
    RaiseException('AnxinBoard runtime installer could not be started.');
  if ResultCode <> 0 then
    RaiseException(Format('AnxinBoard runtime installer failed with exit code %d.', [ResultCode]));
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  ScriptPath: String;
  Params: String;
begin
  Result := False;
  ScriptPath := ExpandConstant('{app}\tools\uninstall-product.ps1');
  if not FileExists(ScriptPath) then
  begin
    MsgBox('AnxinBoard uninstall contract is missing. Program files were not removed.', mbError, MB_OK);
    exit;
  end;

  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ScriptPath + '" -ConfirmProgramRemoval';
  if not Exec(WindowsPowerShellPath(), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('AnxinBoard uninstall contract could not be started. Program files were not removed.', mbError, MB_OK);
    exit;
  end;
  if ResultCode <> 0 then
  begin
    MsgBox(
      Format('AnxinBoard program removal was refused or failed (exit code %d). User data remains preserved.', [ResultCode]),
      mbError,
      MB_OK
    );
    exit;
  end;

  Result := True;
end;
