"""HTTP tests for launcher bootstrap exchange and protected local actions."""
from __future__ import annotations
from pathlib import Path
import sys
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
from app import local_session_api  # noqa: E402


def test_runtime_handoff_lifecycle_is_explicit_and_closed(monkeypatch):
    import sys
    from types import SimpleNamespace
    stopped = []
    callbacks = []
    def start(mint):
        callbacks.append(mint)
        return SimpleNamespace(stop=lambda: stopped.append(True))
    monkeypatch.setitem(sys.modules, "app.local_session_handoff", SimpleNamespace(start_handoff_server=start))
    local_session_api.configure_local_session_guard("synthetic-runtime", enable_handoff=True)
    with TestClient(app_with_probe()) as client:
        handoff = callbacks[0]()
        response = client.post("/api/local-session/exchange", json={"bootstrap_secret": handoff}, headers=headers())
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/local-session/exchange", json={"bootstrap_secret": handoff}, headers=headers()).status_code == 403
    local_session_api.invalidate_local_session_guard()
    assert stopped == [True]


def test_handoff_start_failure_invalidates_guard_and_has_safe_error(monkeypatch):
    from types import SimpleNamespace
    def failed_start(mint):
        raise OSError("synthetic details must not escape")
    monkeypatch.setitem(sys.modules, "app.local_session_handoff", SimpleNamespace(start_handoff_server=failed_start))
    with pytest.raises(RuntimeError, match="^LOCAL_SESSION_HANDOFF_START_FAILED$"):
        local_session_api.configure_local_session_guard("synthetic-runtime", enable_handoff=True)
    with TestClient(app_with_probe()) as client:
        response = client.post("/api/local-session/exchange", json={"bootstrap_secret": "synthetic-runtime"}, headers=headers())
        assert response.status_code == 503


def app_with_probe() -> FastAPI:
    app = FastAPI()
    app.include_router(local_session_api.router)

    @app.post("/protected")
    def protected(request: Request):
        local_session_api.require_local_write_request(request, require_idempotency_key=True)
        return {"ok": True}

    @app.get("/protected-read")
    def protected_read(request: Request):
        local_session_api.require_local_read_request(request)
        return {"ok": True}

    return app


def headers(**extra):
    value = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
    value.update(extra)
    return value


def _live_token(client: TestClient) -> str:
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "bootstrap-test"},
        headers=headers(),
    )
    assert exchanged.status_code == 200
    return exchanged.json()["session_token"]


def test_bootstrap_exchange_is_same_origin_one_time_and_no_store():
    local_session_api.invalidate_local_session_guard()
    assert local_session_api.configure_local_session_guard("bootstrap-test") is True
    client = TestClient(app_with_probe())
    first = client.post("/api/local-session/exchange", json={"bootstrap_secret": "bootstrap-test"}, headers=headers())
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    token = first.json()["session_token"]
    assert token and token != "bootstrap-test"
    second = client.post("/api/local-session/exchange", json={"bootstrap_secret": "bootstrap-test"}, headers=headers())
    assert second.status_code == 403
    assert second.json()["detail"]["code"] == "LOCAL_SESSION_BOOTSTRAP_UNAVAILABLE"
    local_session_api.invalidate_local_session_guard()


def test_cross_origin_or_missing_launcher_session_cannot_reach_sensitive_write():
    local_session_api.invalidate_local_session_guard()
    assert local_session_api.configure_local_session_guard("bootstrap-test") is True
    client = TestClient(app_with_probe())
    bad_origin = client.post("/api/local-session/exchange", json={"bootstrap_secret": "bootstrap-test"}, headers={"host": "127.0.0.1:5173", "origin": "http://evil.example"})
    assert bad_origin.status_code == 403
    assert bad_origin.json()["detail"]["code"] == "LOCAL_SESSION_ORIGIN_INVALID"
    local_session_api.invalidate_local_session_guard()
    client = TestClient(app_with_probe())
    unavailable = client.post("/protected", headers=headers(**{"x-anxin-session": "anything", "x-request-id": "req-1", "local-idempotency-key": "idem-1"}))
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "LOCAL_SESSION_UNAVAILABLE"


def test_sensitive_write_requires_live_session_request_id_and_idempotency_key():
    local_session_api.invalidate_local_session_guard()
    local_session_api.configure_local_session_guard("bootstrap-test")
    client = TestClient(app_with_probe())
    token = _live_token(client)
    missing_idem = client.post("/protected", headers=headers(**{"x-anxin-session": token, "x-request-id": "req-1"}))
    assert missing_idem.status_code == 403
    ok = client.post("/protected", headers=headers(**{"x-anxin-session": token, "x-request-id": "req-2", "local-idempotency-key": "idem-2"}))
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    local_session_api.invalidate_local_session_guard()


def test_sensitive_read_requires_live_session_and_request_id():
    local_session_api.invalidate_local_session_guard()
    client = TestClient(app_with_probe())
    unavailable = client.get(
        "/protected-read",
        headers={"host": "127.0.0.1:5173", "x-anxin-session": "anything", "x-request-id": "read-1"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "LOCAL_SESSION_UNAVAILABLE"

    local_session_api.configure_local_session_guard("bootstrap-test")
    client = TestClient(app_with_probe())
    token = _live_token(client)

    missing_request_id = client.get(
        "/protected-read",
        headers={"host": "127.0.0.1:5173", "x-anxin-session": token},
    )
    assert missing_request_id.status_code == 403

    ok_without_origin = client.get(
        "/protected-read",
        headers={"host": "127.0.0.1:5173", "x-anxin-session": token, "x-request-id": "read-2"},
    )
    assert ok_without_origin.status_code == 200
    assert ok_without_origin.json() == {"ok": True}

    ok_with_origin = client.get(
        "/protected-read",
        headers=headers(**{"x-anxin-session": token, "x-request-id": "read-3"}),
    )
    assert ok_with_origin.status_code == 200

    evil_origin = client.get(
        "/protected-read",
        headers={
            "host": "127.0.0.1:5173",
            "origin": "http://evil.example",
            "x-anxin-session": token,
            "x-request-id": "read-4",
        },
    )
    assert evil_origin.status_code == 403
    assert evil_origin.json()["detail"]["code"] == "LOCAL_SESSION_ORIGIN_INVALID"
    local_session_api.invalidate_local_session_guard()
