from __future__ import annotations

from pathlib import Path
import base64
import re
import shutil
import subprocess
import os

import pytest


ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "installer" / "windows" / "AnxinBoard.iss"
BUILD = ROOT / "installer" / "windows" / "build-installer.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize("available,first_exit,expected", [(True, 0, 0), (False, 0, 20), (True, 1, 21)])
def test_prepare_git_probe_uses_first_application_and_keeps_capability_gate(available, first_exit, expected):
    text = _text(ISS)
    body = text[text.index("function PrepareToInstall("):]
    assignment = body[body.index("Params :="):body.index("if not Exec(")]
    params = "".join(value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", assignment))
    probe = params.split('-Command "', 1)[1][:-1]
    # Execute the shipped probe in Windows PowerShell 5, controlling discovery only.
    # Two application resolutions must not become one concatenated command, and
    # the second application must never be invoked as a fallback.
    discovered = "[pscustomobject]@{Source='Invoke-FirstGit'}; [pscustomobject]@{Source='Invoke-SecondGit'}" if available else "return"
    script = f"""
function Get-Command {{ param($Name,$CommandType,$ErrorAction) {discovered} }}
function Invoke-FirstGit {{
    if ($args.Count -ne 2 -or $args[0] -cne '--no-lazy-fetch' -or $args[1] -cne '--version') {{ $global:LASTEXITCODE=31; return }}
    $global:LASTEXITCODE={first_exit}
}}
function Invoke-SecondGit {{ exit 32 }}
$global:LASTEXITCODE=99
{probe}
"""
    result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16le")).decode("ascii")], capture_output=True, timeout=20)
    assert result.returncode == expected, result.stderr.decode(errors="replace")


@pytest.mark.skipif(os.name != "nt", reason="Windows installer regression")
@pytest.mark.parametrize("stop_exit,runtime_exit,expected", [(0, 1, 7), (1, 0, 7), (0, 0, 0)])
def test_runtime_failure_stops_setup_before_control_install(tmp_path, stop_exit, runtime_exit, expected):
    compiler = shutil.which("ISCC.exe") or str(Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe")
    if not Path(compiler).is_file():
        pytest.skip("Inno Setup 6 required")
    # Compile the real event code and real temporary-payload entries, replacing
    # only external lifecycle programs with bounded synthetic exit-code fixtures.
    source = _text(ISS)
    payload = tmp_path / "payload"
    (payload / "AnxinBoard.Runtime").mkdir(parents=True)
    (payload / "AnxinBoard.Runtime/fake.txt").write_text("synthetic")
    (payload / "runtime-manifest.json").write_text("{}")
    (tmp_path / "stop-installed-product.ps1").write_text(f"exit {stop_exit}\n")
    (tmp_path / "install-runtime.ps1").write_text(f"""param([string]$PayloadRoot)
if (-not (Test-Path -LiteralPath (Join-Path $PayloadRoot 'runtime-manifest.json')) -or -not (Test-Path -LiteralPath (Join-Path $PayloadRoot 'AnxinBoard.Runtime/fake.txt'))) {{ exit 41 }}
Write-Output 'SYNTHETIC_RUNTIME_STDOUT'
[Console]::Error.WriteLine('SYNTHETIC_RUNTIME_STDERR')
exit {runtime_exit}
""")
    (tmp_path / "control.txt").write_text("synthetic installer control")
    entries = source.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    entries = "\n".join(line for line in entries.splitlines() if line.startswith("Source:") and 'DestDir: "{tmp}' in line)
    code = source.split("[Code]", 1)[1]
    script = tmp_path / "probe.iss"
    script.write_text(f'''#define PayloadRoot "{payload}"
[Setup]
AppName=AnxinSyntheticRuntimeGate
AppVersion=1
DefaultDirName={tmp_path / 'installed'}
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
Uninstallable=no
OutputDir={tmp_path / 'out'}
OutputBaseFilename=probe
[Files]
Source: "control.txt"; DestDir: "{{app}}"
{entries}
[Code]
{code}
''', encoding="utf-8-sig")
    built = subprocess.run([compiler, "/Q", str(script)], capture_output=True, timeout=60)
    assert built.returncode == 0, built.stdout.decode(errors="replace") + built.stderr.decode(errors="replace")
    log = tmp_path / "setup.log"
    installed = subprocess.run([str(tmp_path / "out/probe.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={log}"], capture_output=True, timeout=60)
    assert installed.returncode == expected
    assert (tmp_path / "installed/control.txt").exists() is (expected == 0)
    recorded = log.read_text(encoding="utf-8-sig")
    if stop_exit == 0:
        assert 'SYNTHETIC_RUNTIME_STDOUT' in recorded
        assert 'SYNTHETIC_RUNTIME_STDERR' in recorded
    if expected:
        assert "exit code 1" in recorded


def test_inno_shell_is_current_user_x64_and_exposes_owned_lifecycle_controls() -> None:
    text = _text(ISS)
    assert 'AppId={#ProductAppId}' in text
    assert 'PrivilegesRequired=lowest' in text
    assert 'ArchitecturesAllowed=x64compatible' in text
    assert 'ArchitecturesInstallIn64BitMode=x64compatible' in text
    assert 'DefaultDirName={localappdata}\\Programs\\AnxinBoard Installer Control' in text
    assert '[Icons]' in text
    assert 'Source: "start-installed-product.ps1"; DestDir: "{app}\\tools"' in text
    assert 'Source: "stop-installed-product.ps1"; DestDir: "{app}\\tools"' in text
    assert 'Name: "{group}\\安心看板"' in text
    assert 'Name: "{group}\\安心看板 - 退出"' in text
    assert 'Source: "restore-product-backup.ps1"; DestDir: "{app}\\tools"' in text
    assert 'Name: "{group}\\安心看板 - 恢复备份"' in text


def test_upgrade_purges_stale_owner_launcher_and_restore_artifacts() -> None:
    text = _text(ISS)
    assert '[InstallDelete]' in text
    for stale in (
        '{app}\\tools\\start-installed-product.ps1',
        '{app}\\tools\\stop-installed-product.ps1',
        '{app}\\tools\\restore-product-backup.ps1',
        '{group}\\安心看板.lnk',
        '{group}\\安心看板 - 退出.lnk',
        '{group}\\安心看板 - 恢复备份.lnk',
    ):
        assert f'Type: files; Name: "{stale}"' in text


def test_inno_shell_delegates_product_lifecycle_instead_of_reimplementing_it() -> None:
    text = _text(ISS)
    assert 'Source: "install-runtime.ps1"' in text
    assert 'AfterInstall: InstallProductRuntime' not in text
    assert 'function PrepareToInstall(var NeedsRestart: Boolean): String;' in text
    assert 'Source: "uninstall-product.ps1"; DestDir: "{app}\\tools"' in text
    assert '-PayloadRoot' in text
    assert '-ConfirmProgramRemoval' in text
    assert 'function InitializeUninstall(): Boolean;' in text
    assert 'AnxinBoardPayload' in text
    assert 'AnxinBoard.Runtime\\*' in text
    assert 'runtime-manifest.json' in text


def test_in_place_upgrade_hands_off_running_runtime_fail_closed() -> None:
    text = _text(ISS)
    assert (
        'Source: "stop-installed-product.ps1"; DestDir: "{tmp}\\AnxinBoardInstaller"; Flags: ignoreversion'
        in text
    )
    assert 'procedure StopPreviousProductRuntime();' in text
    assert "StopPreviousProductRuntime();" in text
    install_start = text.index('procedure InstallProductRuntime();')
    install_body = text[install_start:text.index('function InitializeUninstall(): Boolean;')]
    assert install_body.index('StopPreviousProductRuntime();') < install_body.index('install-runtime.ps1')
    assert 'RaiseException(\'AnxinBoard upgrade lifecycle stop contract is missing.\')' in text
    assert 'RaiseException(\'AnxinBoard could not run the upgrade lifecycle stop contract.\')' in text
    assert 'AnxinBoard refused to replace a running or unidentified runtime' in text
    assert 'ANXIN_UPGRADE_PREVIOUS_RUNTIME=STOPPED_OR_ABSENT' in text


def test_inno_shell_does_not_embed_or_operate_credentials_or_user_data() -> None:
    lowered = _text(ISS).casefold()
    assert 'cmdkey' not in lowered
    assert 'credential' not in lowered
    assert 'password' not in lowered
    assert 'secret' not in lowered
    assert 'anxinboard\\anxinboard.db' not in lowered


def test_installer_builder_rebinds_payload_to_exact_product_and_only_compiles() -> None:
    text = _text(BUILD)
    assert "runtimeManifest.source_commit" in text
    assert "git -C $repoRoot rev-parse HEAD" in text
    assert "Runtime payload is not bound to exact current Product HEAD" in text
    assert "Get-FileHash" in text
    assert "ISCC.exe" in text
    assert "Get-AuthenticodeSignature" in text
    assert "formal_release = $false" in text
    assert "real_install_executed = $false" in text
    assert "real_uninstall_executed = $false" in text
    assert "$gitRuntimePrerequisite = 'Git for Windows 2.45+ with --no-lazy-fetch capability'" in text
    assert "normal_user_machine_prerequisites = @($gitRuntimePrerequisite)" in text
    assert "WINDOWS_INNO_INSTALLER_CANDIDATE=BUILT_NOT_INSTALLED_NOT_RELEASED" in text
    assert "install-runtime.ps1 -PayloadRoot" not in text
    assert "uninstall-product.ps1 -ConfirmProgramRemoval" not in text
