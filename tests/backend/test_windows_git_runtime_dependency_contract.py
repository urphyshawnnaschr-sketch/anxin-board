from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from app.git_runtime_capability import (
    GitRuntimeCapabilityError,
    require_git_runtime_capability,
)


ROOT = Path(__file__).resolve().parents[2]


def _runner(version: str, *, capability_returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, f"git version {version}\n", "")
        if argv[1:] == ["--no-lazy-fetch", "--version"]:
            if capability_returncode:
                return subprocess.CompletedProcess(argv, capability_returncode, "", "unknown option")
            return subprocess.CompletedProcess(argv, 0, f"git version {version}\n", "")
        raise AssertionError(argv)

    return calls, run


def test_missing_git_fails_closed_before_any_process_probe(tmp_path: Path) -> None:
    calls, runner = _runner("2.45.0")
    with pytest.raises(GitRuntimeCapabilityError) as exc_info:
        require_git_runtime_capability(which=lambda _name: None, runner=runner)
    assert exc_info.value.code == "GIT_RUNTIME_CAPABILITY_REQUIRED"
    assert calls == []


def test_pre_245_git_fails_before_no_lazy_fetch_probe(tmp_path: Path) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"test")
    calls, runner = _runner("2.44.9.windows.1")
    with pytest.raises(GitRuntimeCapabilityError):
        require_git_runtime_capability(str(executable), runner=runner)
    assert [call[1:] for call in calls] == [["--version"]]


def test_claimed_new_git_without_no_lazy_fetch_capability_fails_closed(tmp_path: Path) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"test")
    calls, runner = _runner("2.45.0.windows.1", capability_returncode=129)
    with pytest.raises(GitRuntimeCapabilityError):
        require_git_runtime_capability(str(executable), runner=runner)
    assert [call[1:] for call in calls] == [
        ["--version"],
        ["--no-lazy-fetch", "--version"],
    ]


def test_git_245_plus_with_exact_capability_is_admitted(tmp_path: Path) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"test")
    calls, runner = _runner("2.53.0.windows.1")
    capability = require_git_runtime_capability(str(executable), runner=runner)
    assert capability.version == (2, 53, 0)
    assert capability.no_lazy_fetch is True
    assert [call[1:] for call in calls] == [
        ["--version"],
        ["--no-lazy-fetch", "--version"],
    ]


def test_formal_runtime_probes_git_before_ui_or_server_start() -> None:
    source = (ROOT / "apps" / "backend" / "product_entry.py").read_text(encoding="utf-8")
    probe = source.index("require_git_runtime_capability()")
    configure = source.index("configure_product_ui()", probe)
    serve = source.index("uvicorn.run(", probe)
    assert probe < configure < serve
    assert "GIT_RUNTIME_CAPABILITY_REQUIRED" not in source  # use typed exception, not forged literals
    assert "return 78" in source


def test_inno_setup_checks_exact_capability_before_install() -> None:
    source = (ROOT / "installer" / "windows" / "AnxinBoard.iss").read_text(encoding="utf-8")
    assert "function PrepareToInstall" in source
    assert "Get-Command git.exe -CommandType Application" in source
    assert "--no-lazy-fetch --version" in source
    assert "Git for Windows 2.45 or newer" in source
    prepare = source[source.index("function PrepareToInstall"):source.index("procedure StopPreviousProductRuntime")]
    # A forward declaration is not execution. Check the actual admission order.
    assert prepare.index("--no-lazy-fetch --version") < prepare.index("ExtractTemporaryFiles")
    assert prepare.index("ExtractTemporaryFiles") < prepare.index("InstallProductRuntime();")


def test_delivery_manifest_declares_truthful_normal_user_git_prerequisite() -> None:
    manifest = json.loads(
        (ROOT / "installer" / "windows-delivery-gate-v1.json").read_text(encoding="utf-8")
    )
    assert manifest["snapshot_kind"] == "public_source_candidate_contract"
    prerequisites = manifest["candidate_runtime"]["normal_user_machine_prerequisites"]
    assert prerequisites == ["Git for Windows 2.45+ with --no-lazy-fetch capability"]
    assert "Git 2.45+ with --no-lazy-fetch capability" in manifest["candidate_runtime"][
        "build_machine_prerequisites"
    ]
    assert manifest["capability_snapshot"]["git_runtime_dependency_contract"] == (
        "explicit_prerequisite_plus_prebusiness_capability_probe_implemented_evidence_pending"
    )
