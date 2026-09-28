"""Real Windows PowerShell, synthetic payload and isolated install root only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer/windows/install-runtime.ps1"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell required")
@pytest.mark.parametrize("polluted", [True, False])
@pytest.mark.parametrize("tampered", [False, True])
def test_install_runtime_module_path_and_hash_gate(tmp_path, polluted, tampered):
    payload = tmp_path / "payload"
    runtime = payload / "AnxinBoard.Runtime"
    runtime.mkdir(parents=True)
    content = b"synthetic runtime; never executable"
    for name in ("fixture.txt", "frontend.txt", "diagnostics.txt"):
        (runtime / name).write_bytes(content)
    manifest = {
        "schema_version": "anxin_windows_runtime_payload_v1",
        "source_commit": "a" * 40,
        "runtime": "fixture.txt", "frontend": "frontend.txt", "diagnostics": "diagnostics.txt",
        "files": [{"path": name, "size": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()}
                  for name in ("fixture.txt", "frontend.txt", "diagnostics.txt")],
    }
    (payload / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if tampered:
        (runtime / "fixture.txt").write_bytes(b"X" * len(content))
    target = tmp_path / "installed"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    # Assign inside the child too: WinPS startup may rewrite inherited paths.
    # This deterministically models a host carrying a non-native module path.
    module_path = str(Path(os.environ["ProgramFiles"]) / "PowerShell/7/Modules") if polluted else str(
        Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/Modules")
    if polluted and not Path(module_path).is_dir():
        pytest.skip("PowerShell 7 modules required for incompatible-path regression")
    command = f"$env:PSModulePath={quote(module_path)}; & {quote(SCRIPT)} -PayloadRoot {quote(payload)} -TestInstallRoot {quote(target)}"
    env = dict(os.environ, ANXINBOARD_INSTALLER_TEST_MODE="1", PSModulePath=module_path)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-EncodedCommand", base64.b64encode(command.encode("utf-16le")).decode()],
                            env=env, capture_output=True, timeout=30)
    output = (result.stdout + result.stderr).decode(errors="replace")
    if tampered:
        assert result.returncode != 0
        assert "Runtime file digest mismatch" in output
        assert not target.exists()
    else:
        assert result.returncode == 0, output
        state = json.loads((target / "install-state.json").read_text(encoding="utf-8-sig"))
        assert state["source_commit"] == "a" * 40
        assert (target / state["runtime_relative"] / "fixture.txt").read_bytes() == content
