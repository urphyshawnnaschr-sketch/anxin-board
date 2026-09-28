"""Source-level and Windows runtime safety contract for the guarded browser-session launcher."""
from pathlib import Path
import os
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS_POWERSHELL = "powershell.exe"


def test_start_command_routes_through_secure_wrapper():
    cmd = (ROOT / "Start-AnxinBoard.cmd").read_text(encoding="utf-8")
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass" in cmd
    assert "scripts\\run-local-secure.ps1" in cmd
    assert "scripts\\run-local.ps1" not in cmd


def test_secure_wrapper_uses_csprng_transient_fragment_bootstrap_and_never_persists_or_prints_it():
    source = (ROOT / "scripts" / "run-local-secure.ps1").read_text(encoding="utf-8-sig")
    assert "RandomNumberGenerator" in source
    assert "ANXINBOARD_LOCAL_BOOTSTRAP_SECRET" in source
    assert "& $runLocal -NoBrowser" in source
    assert '#/projects?anxin_bootstrap=$escaped' in source
    assert '/?anxin_bootstrap=$escaped' not in source
    assert "Write-LocalState" not in source
    assert "Set-Content" not in source
    assert "Out-File" not in source
    for line in source.splitlines():
        if re.search(r"\bWrite-(?:Host|Output|Verbose|Debug|Warning|Error)\b", line, re.I):
            assert "$bootstrap" not in line.lower()
            assert "$escaped" not in line.lower()


def test_secure_wrapper_serializes_start_cross_session_before_bootstrap_and_runtime():
    source = (ROOT / "scripts" / "run-local-secure.ps1").read_text(encoding="utf-8-sig")

    assert "Local\\AnxinBoard.SecureLauncher" not in source
    assert "[System.Threading.Mutex]" not in source
    assert "$launchLockFile = Join-Path $stateDir 'secure-launch.lock'" in source
    assert "[System.IO.File]::Open(" in source
    assert "[System.IO.FileMode]::OpenOrCreate" in source
    assert "[System.IO.FileAccess]::ReadWrite" in source
    assert "[System.IO.FileShare]::None" in source
    assert "catch [System.IO.IOException]" in source
    assert "$launchLock.Dispose()" in source

    lock_index = source.index("$launchLock = [System.IO.File]::Open")
    bootstrap_index = source.index("$bootstrap = New-LauncherBootstrap")
    runtime_index = source.index("& $runLocal -NoBrowser")
    assert lock_index < bootstrap_index < runtime_index

    rejection_index = source.index("catch [System.IO.IOException]")
    assert lock_index < rejection_index < bootstrap_index
    rejection_block = source[rejection_index:bootstrap_index]
    assert "exit 1" in rejection_block
    assert "第二套本地运行时" in rejection_block


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_windows_exclusive_file_lock_blocks_second_process_and_recovers_after_owner_exit(tmp_path):
    lock_path = tmp_path / "launcher.lock"
    escaped = str(lock_path).replace("'", "''")
    owner_script = (
        "$h=[System.IO.File]::Open('"
        + escaped
        + "',[System.IO.FileMode]::OpenOrCreate,[System.IO.FileAccess]::ReadWrite,[System.IO.FileShare]::None);"
        + "[Console]::Out.WriteLine('LOCKED');[Console]::Out.Flush();Start-Sleep -Seconds 30"
    )
    contender_script = (
        "try {$h=[System.IO.File]::Open('"
        + escaped
        + "',[System.IO.FileMode]::OpenOrCreate,[System.IO.FileAccess]::ReadWrite,[System.IO.FileShare]::None);"
        + "$h.Dispose();exit 0} catch [System.IO.IOException] {exit 23}"
    )

    owner = subprocess.Popen(
        [WINDOWS_POWERSHELL, "-NoProfile", "-Command", owner_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "LOCKED"
        contender = subprocess.run(
            [WINDOWS_POWERSHELL, "-NoProfile", "-Command", contender_script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert contender.returncode == 23
    finally:
        owner.terminate()
        owner.wait(timeout=10)

    recovered = subprocess.run(
        [WINDOWS_POWERSHELL, "-NoProfile", "-Command", contender_script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert recovered.returncode == 0


def test_no_browser_mode_never_opens_a_bootstrap_url():
    source = (ROOT / "scripts" / "run-local-secure.ps1").read_text(encoding="utf-8-sig")
    no_browser_index = source.index("if ($NoBrowser)")
    browser_url_index = source.index('$browserUrl = "http://127.0.0.1:')
    assert no_browser_index < browser_url_index
    no_browser_block = source[no_browser_index:browser_url_index]
    assert "exit 0" in no_browser_block
    assert "Start-Process $browserUrl" not in no_browser_block
