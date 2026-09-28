from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "installer" / "windows"
START = WINDOWS / "start-installed-product.ps1"
STOP = WINDOWS / "stop-installed-product.ps1"
RESOLVER = WINDOWS / "resolve-installed-runtime.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_start_serializes_lifecycle_and_keeps_bootstrap_process_local() -> None:
    text = _text(START)
    assert "secure-launch.lock" in text
    assert "[System.IO.FileShare]::None" in text
    assert "New-BootstrapSecret" in text
    assert "RandomNumberGenerator" in text
    assert "ANXINBOARD_LOCAL_BOOTSTRAP_SECRET" in text
    assert "SetEnvironmentVariable($bootstrapEnv, $bootstrap, 'Process')" in text
    assert "SetEnvironmentVariable($bootstrapEnv, $oldBootstrap, 'Process')" in text
    assert "EscapeDataString($bootstrap)" in text
    assert "anxin_bootstrap=$escaped" in text
    assert "ConvertTo-Json" in text
    run_state_start = text.index("$runState = [ordered]@{")
    run_state_end = text.index("$tempState =", run_state_start)
    assert "bootstrap" not in text[run_state_start:run_state_end].casefold()


def test_start_revalidates_active_payload_before_existing_process_reuse() -> None:
    text = _text(START)
    assert "$resolved = Resolve-CurrentRuntime" in text
    assert text.index("$resolved = Resolve-CurrentRuntime") < text.index("$existing = Read-ExistingRunState")
    assert "Installed runtime state source commit no longer matches the active payload" in text
    assert "Installed runtime state payload digest no longer matches the active payload" in text
    assert "Installed runtime state executable no longer matches the active payload" in text
    assert "Installed runtime state belongs to a different install root" in text
    assert "Installed runtime state points at a different executable; refusing reuse" in text
    assert "$processPath = [string]$Process.Path" in text
    assert "Wait-HealthyRuntime -Process $existingProcess -Origin $existingOrigin" in text


def test_start_only_reuses_exact_loopback_http_origin() -> None:
    text = _text(START)
    assert "function Get-LoopbackOriginPort" in text
    assert "function Assert-LoopbackOrigin" in text
    assert "^http://127\\.0\\.0\\.1:(?<port>[1-9][0-9]{0,4})$" in text
    assert "Installed runtime state origin is not an exact loopback HTTP origin" in text
    assert "$port -lt 1 -or $port -gt 65535" in text
    assert "Request-LocalSessionHandoff -RuntimePid $existingPid" in text
    assert 'Start-Process "$existingOrigin/#/projects?anxin_bootstrap=$escaped"' in text
    assert "Start-Process ([string]$existing.origin)" not in text


def test_start_proves_loopback_listener_is_owned_by_exact_runtime_pid() -> None:
    text = _text(START)
    assert "function Assert-LoopbackListenerOwnership" in text
    assert "Get-NetTCPConnection -LocalAddress '127.0.0.1'" in text
    assert "-State Listen -ErrorAction Stop" in text
    assert "Installed runtime loopback listener identity is ambiguous" in text
    assert "Installed runtime loopback listener is owned by a different process" in text
    assert "[int]$listeners[0].OwningProcess -ne [int]$Process.Id" in text
    existing_wait = text.index("Wait-HealthyRuntime -Process $existingProcess -Origin $existingOrigin")
    existing_owner = text.index("Assert-LoopbackListenerOwnership -Process $existingProcess -Origin $existingOrigin")
    existing_browser = text.index('Start-Process "$existingOrigin/#/projects?anxin_bootstrap=$escaped"')
    handoff = text.index("Request-LocalSessionHandoff -RuntimePid $existingPid")
    assert existing_owner < handoff < existing_browser
    assert existing_wait < existing_owner < existing_browser
    new_wait = text.index("Wait-HealthyRuntime -Process $runtimeProcess -Origin $origin")
    new_owner = text.index("Assert-LoopbackListenerOwnership -Process $runtimeProcess -Origin $origin")
    state_write = text.index("$runState = [ordered]@{")
    assert new_wait < new_owner < state_write


def test_existing_runtime_handoff_uses_pid_bound_pipe_and_bounded_io():
    text = _text(START)
    assert "GetNamedPipeServerProcessId" in text
    assert "serverPid != runtimePid" in text
    assert '"AnxinBoard.LocalSession." + runtimePid' in text
    assert "TokenImpersonationLevel.Anonymous" in text
    assert "pipe.Connect(3000)" in text
    assert "Wait(3000)" in text
    assert "LOCAL_SESSION_HANDOFF_UNAVAILABLE" in text


def test_start_resolver_is_integrity_gate_not_launcher_authority() -> None:
    start = _text(START)
    resolver = _text(RESOLVER)
    assert "resolve-installed-runtime.ps1" in start
    assert "anxin_installed_runtime_resolution_v1" in start
    assert "payload_digest" in start
    assert "source_commit" in start
    assert "runtime_executable_relative" in start
    assert "launcher_authority = 'not_performed'" in resolver
    assert "credential_access = 'not_performed'" in resolver


def test_stop_requires_same_lifecycle_lock_and_exact_runtime_process_path() -> None:
    text = _text(STOP)
    assert "secure-launch.lock" in text
    assert "[System.IO.FileShare]::None" in text
    assert "Installed runtime state belongs to a different install root" in text
    assert "Installed runtime state executable escapes the product root" in text
    assert "Installed runtime state executable is a reparse point" in text
    assert "$processPath = [string]$process.Path" in text
    assert "Installed runtime state points at a different executable; refusing to terminate it" in text
    assert text.index("Installed runtime state points at a different executable") < text.index(
        "Stop-Process -Id $pidValue -Force"
    )


def test_stop_keeps_pid_name_start_time_identity_checks() -> None:
    text = _text(STOP)
    assert "$sameName" in text
    assert "$sameStart" in text
    assert "Installed runtime state no longer identifies the live process; refusing to terminate it" in text
    assert "Stop-Process -Id $pidValue -Force -ErrorAction Stop" in text


def test_launcher_test_root_override_is_explicitly_gated() -> None:
    for path in (START, STOP, RESOLVER):
        text = _text(path)
        assert "ANXINBOARD_INSTALLER_TEST_MODE" in text
        assert "TestInstallRoot" in text
