"""Acceptance tests for the frozen Local Session Guard Core V1 slice."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import socket
import sqlite3
import sys
import threading

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import local_session_guard  # noqa: E402
from app.local_session_guard import (  # noqa: E402
    LocalSessionGuard,
    LocalSessionGuardError,
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_REQUEST_ID_CHARS,
    SESSION_ENTROPY_BYTES,
    validate_exact_http_origin,
    validate_idempotency_key,
    validate_loopback_host,
    validate_request_id,
)


def _guard(bootstrap: str = "bootstrap-secret-for-test") -> LocalSessionGuard:
    return LocalSessionGuard(bootstrap)


def _session(bootstrap: str = "bootstrap-secret-for-test") -> tuple[LocalSessionGuard, str]:
    guard = _guard(bootstrap)
    return guard, guard.exchange_bootstrap(bootstrap)


def test_handoff_is_single_use_without_revoking_existing_browser():
    guard, original = _session()
    handoff = guard.mint_handoff()
    assert handoff != original
    guard.validate_session(original)
    assert guard.exchange_bootstrap(handoff) == original
    guard.validate_session(original)
    with pytest.raises(LocalSessionGuardError):
        guard.exchange_bootstrap(handoff)


def test_handoff_expires_and_replacement_revokes_only_pending(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(local_session_guard.time, "monotonic", lambda: clock[0])
    guard, original = _session()
    old = guard.mint_handoff()
    fresh = guard.mint_handoff()
    with pytest.raises(LocalSessionGuardError):
        guard.exchange_bootstrap(old)
    clock[0] += 61
    with pytest.raises(LocalSessionGuardError):
        guard.exchange_bootstrap(fresh)
    guard.validate_session(original)


def test_handoff_can_authorize_first_browser_and_invalidation_revokes_it():
    guard = _guard()
    handoff = guard.mint_handoff()
    session = guard.exchange_bootstrap(handoff)
    guard.validate_session(session)
    with pytest.raises(LocalSessionGuardError):
        guard.exchange_bootstrap("bootstrap-secret-for-test")
    pending = guard.mint_handoff()
    guard.invalidate()
    with pytest.raises(LocalSessionGuardError):
        guard.exchange_bootstrap(pending)
    with pytest.raises(LocalSessionGuardError):
        guard.mint_handoff()


def test_concurrent_handoff_exchange_has_one_winner():
    guard, original = _session()
    handoff = guard.mint_handoff()
    barrier = threading.Barrier(5)
    winners = []
    def exchange():
        barrier.wait()
        try:
            winners.append(guard.exchange_bootstrap(handoff))
        except LocalSessionGuardError:
            pass
    threads = [threading.Thread(target=exchange) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert winners == [original]


def test_bootstrap_first_exchange_succeeds_and_second_use_fails() -> None:
    guard = _guard()

    token = guard.exchange_bootstrap("bootstrap-secret-for-test")

    assert guard.bootstrap_consumed is True
    assert guard.session_active is True
    assert token
    with pytest.raises(LocalSessionGuardError) as error:
        guard.exchange_bootstrap("bootstrap-secret-for-test")
    assert error.value.code == "BOOTSTRAP_UNAVAILABLE"


def test_bootstrap_exchange_is_atomic_under_concurrent_attempts() -> None:
    bootstrap = "concurrent-bootstrap-secret"
    guard = _guard(bootstrap)
    barrier = threading.Barrier(8)
    successes: list[str] = []
    failures: list[str] = []
    result_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            session = guard.exchange_bootstrap(bootstrap)
        except LocalSessionGuardError as error:
            with result_lock:
                failures.append(error.code)
        else:
            with result_lock:
                successes.append(session)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert failures == ["BOOTSTRAP_UNAVAILABLE"] * 7
    guard.validate_session(successes[0])


def test_wrong_or_empty_bootstrap_fails_without_consuming_correct_secret() -> None:
    guard = _guard()

    for bad in (None, "", "wrong-bootstrap"):
        with pytest.raises(LocalSessionGuardError) as error:
            guard.exchange_bootstrap(bad)
        assert error.value.code == "BOOTSTRAP_INVALID"

    assert guard.bootstrap_consumed is False
    assert guard.exchange_bootstrap("bootstrap-secret-for-test")


def test_generated_bootstrap_and_session_have_independent_csprng_contract() -> None:
    guard_a, bootstrap_a = LocalSessionGuard.with_generated_bootstrap()
    guard_b, bootstrap_b = LocalSessionGuard.with_generated_bootstrap()

    session_a = guard_a.exchange_bootstrap(bootstrap_a)
    session_b = guard_b.exchange_bootstrap(bootstrap_b)

    assert SESSION_ENTROPY_BYTES >= 32
    assert len(session_a) >= 43
    assert len(session_b) >= 43
    assert session_a != bootstrap_a
    assert session_b != bootstrap_b
    assert bootstrap_a != bootstrap_b
    assert session_a != session_b


def test_session_validation_is_fail_closed_for_wrong_empty_and_missing_values() -> None:
    guard, session = _session()

    guard.validate_session(session)
    for bad in (None, "", "not-the-session"):
        with pytest.raises(LocalSessionGuardError) as error:
            guard.validate_session(bad)
        assert error.value.code == "SESSION_INVALID"


def test_invalidate_revokes_existing_session_and_bootstrap_state() -> None:
    guard, session = _session()

    guard.invalidate()

    assert guard.session_active is False
    with pytest.raises(LocalSessionGuardError, match="SESSION_INVALID"):
        guard.validate_session(session)
    with pytest.raises(LocalSessionGuardError, match="BOOTSTRAP_UNAVAILABLE"):
        guard.exchange_bootstrap("bootstrap-secret-for-test")


def test_new_guard_instance_rejects_old_instance_session() -> None:
    guard_a, session_a = _session("bootstrap-a")
    guard_b, session_b = _session("bootstrap-b")

    assert session_a != session_b
    with pytest.raises(LocalSessionGuardError) as error:
        guard_b.validate_session(session_a)
    assert error.value.code == "SESSION_INVALID"


def test_loopback_host_allowlist_accepts_only_frozen_local_forms() -> None:
    assert validate_loopback_host("localhost") == "localhost"
    assert validate_loopback_host("LOCALHOST") == "localhost"
    assert validate_loopback_host("localhost:8000") == "localhost:8000"
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert validate_loopback_host("127.0.0.1:65535") == "127.0.0.1:65535"

    rejected = (
        None,
        "",
        "0.0.0.0",
        "192.168.1.10",
        "localhost.evil.example",
        "evil-localhost",
        "127.0.0.1.evil",
        "localhost.",
        "[::1]",
        "::1",
        "localhost:0",
        "localhost:65536",
        "localhost:08000",
        "localhost/path",
        "localhost:8000/path",
        " localhost",
        "localhost ",
        "localhost\n",
    )
    for host in rejected:
        with pytest.raises(LocalSessionGuardError) as error:
            validate_loopback_host(host)
        assert error.value.code == "HOST_NOT_LOOPBACK"


def test_origin_must_be_exact_http_origin_derived_from_current_host() -> None:
    assert validate_exact_http_origin("http://localhost:8000", "LOCALHOST:8000") == (
        "http://localhost:8000"
    )
    assert validate_exact_http_origin("http://127.0.0.1", "127.0.0.1") == (
        "http://127.0.0.1"
    )

    rejected = (
        None,
        "",
        "https://localhost:8000",
        "http://localhost:8001",
        "http://127.0.0.1:8000",
        "http://localhost:8000/",
        "http://localhost:8000/path",
        "HTTP://localhost:8000",
        "http://LOCALHOST:8000",
        "http://localhost.evil:8000",
    )
    for origin in rejected:
        with pytest.raises(LocalSessionGuardError) as error:
            validate_exact_http_origin(origin, "localhost:8000")
        assert error.value.code == "ORIGIN_INVALID"


def test_browser_write_request_requires_origin_session_host_and_request_id() -> None:
    guard, session = _session()

    guard.validate_write_request(
        session_token=session,
        host="localhost:9000",
        origin="http://localhost:9000",
        request_id="req-123",
    )

    with pytest.raises(LocalSessionGuardError, match="ORIGIN_INVALID"):
        guard.validate_write_request(
            session_token=session,
            host="localhost:9000",
            origin=None,
            request_id="req-123",
        )


def test_request_id_is_bounded_ascii_and_control_character_safe() -> None:
    assert validate_request_id("req-123_ABC:part.1") == "req-123_ABC:part.1"

    bad_values = (
        None,
        "",
        "with space",
        "line\nfeed",
        "carriage\rreturn",
        "nul\x00byte",
        "unicode-请求",
        "x" * (MAX_REQUEST_ID_CHARS + 1),
    )
    for value in bad_values:
        with pytest.raises(LocalSessionGuardError):
            validate_request_id(value)


def test_idempotency_key_is_optional_unless_required_and_is_bounded_ascii() -> None:
    assert validate_idempotency_key(None) is None
    assert validate_idempotency_key("idem-123") == "idem-123"

    with pytest.raises(LocalSessionGuardError) as missing:
        validate_idempotency_key(None, required=True)
    assert missing.value.code == "IDEMPOTENCY_KEY_REQUIRED"

    for bad in (
        "",
        "bad key",
        "bad\nkey",
        "bad\x01key",
        "非ascii",
        "x" * (MAX_IDEMPOTENCY_KEY_CHARS + 1),
    ):
        with pytest.raises(LocalSessionGuardError) as error:
            validate_idempotency_key(bad, required=True)
        assert error.value.code in {"IDEMPOTENCY_KEY_REQUIRED", "IDEMPOTENCY_KEY_INVALID"}


def test_required_idempotency_key_is_enforced_by_write_validator() -> None:
    guard, session = _session()

    with pytest.raises(LocalSessionGuardError, match="IDEMPOTENCY_KEY_REQUIRED"):
        guard.validate_write_request(
            session_token=session,
            host="127.0.0.1:9999",
            origin="http://127.0.0.1:9999",
            request_id="req-1",
            require_idempotency_key=True,
        )

    guard.validate_write_request(
        session_token=session,
        host="127.0.0.1:9999",
        origin="http://127.0.0.1:9999",
        request_id="req-2",
        idempotency_key="idem-2",
        require_idempotency_key=True,
    )


def test_secret_values_never_enter_guard_or_exception_repr_or_str() -> None:
    bootstrap = "ULTRA-SENSITIVE-BOOTSTRAP-DO-NOT-LEAK"
    guard = _guard(bootstrap)

    with pytest.raises(LocalSessionGuardError) as bootstrap_error:
        guard.exchange_bootstrap("ATTACKER-SUPPLIED-SECRET")
    assert bootstrap not in str(bootstrap_error.value)
    assert bootstrap not in repr(bootstrap_error.value)
    assert "ATTACKER-SUPPLIED-SECRET" not in str(bootstrap_error.value)
    assert "ATTACKER-SUPPLIED-SECRET" not in repr(bootstrap_error.value)
    assert bootstrap not in repr(guard)

    session = guard.exchange_bootstrap(bootstrap)
    assert bootstrap not in repr(guard)
    assert session not in repr(guard)

    with pytest.raises(LocalSessionGuardError) as session_error:
        guard.validate_session("ATTACKER-SESSION")
    rendered = f"{session_error.value!s} {session_error.value!r}"
    assert session not in rendered
    assert "ATTACKER-SESSION" not in rendered


def test_module_import_has_no_network_file_db_or_environment_side_effect_code(monkeypatch) -> None:
    source_path = Path(local_session_guard.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imported_roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            assert not isinstance(value, ast.Call), "module import must not execute top-level calls"
        elif isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)

    assert imported_roots <= {"__future__", "re", "secrets", "threading", "typing", "time"}

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("network side effect"))
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("db side effect"))
    reloaded = importlib.reload(local_session_guard)
    assert reloaded.LocalSessionGuard is not None
