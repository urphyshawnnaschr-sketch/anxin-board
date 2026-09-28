"""Fake-only HTTP tests for the exact-scope Human send bridge."""
from __future__ import annotations
from pathlib import Path
import sys
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
from app import local_session_api  # noqa: E402
from app import report_send_authorization_api as api  # noqa: E402


def make_client(monkeypatch):
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.invalidate_local_session_guard()
    local_session_api.configure_local_session_guard("bootstrap-test")
    client = TestClient(app)
    base_headers = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
    exchanged = client.post("/api/local-session/exchange", json={"bootstrap_secret": "bootstrap-test"}, headers=base_headers)
    assert exchanged.status_code == 200
    session = exchanged.json()["session_token"]
    return client, {**base_headers, "x-anxin-session": session, "x-request-id": "req-1"}


def test_preview_requires_live_launcher_session_and_never_executes(monkeypatch):
    calls = {"preview": 0, "execute": 0}
    monkeypatch.setattr(api, "build_report_send_authorization_preview", lambda **_kwargs: calls.__setitem__("preview", calls["preview"] + 1) or {"authorization_state": "awaiting_human_confirmation", "model_call_id": 41, "data_scope_hash": "a" * 64})
    monkeypatch.setattr(api, "execute_prepared_report_generation", lambda **_kwargs: calls.__setitem__("execute", calls["execute"] + 1))
    client, headers = make_client(monkeypatch)
    response = client.post("/api/projects/7/report-generation-tasks/tsk-demo/send-authorization-preview", json={"model_call_id": 41}, headers=headers)
    assert response.status_code == 200
    assert calls == {"preview": 1, "execute": 0}
    local_session_api.invalidate_local_session_guard()


def test_authorize_requires_idempotency_key_before_permit_owner(monkeypatch):
    called = {"authorize": 0}
    monkeypatch.setattr(api, "authorize_report_send_scope", lambda **_kwargs: called.__setitem__("authorize", called["authorize"] + 1) or {})
    client, headers = make_client(monkeypatch)
    response = client.post("/api/projects/7/report-generation-tasks/tsk-demo/authorize-send", json={"model_call_id": 41, "data_scope_hash": "a" * 64, "human_confirmed": True}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_SESSION_IDEMPOTENCY_KEY_REQUIRED"
    assert called["authorize"] == 0
    local_session_api.invalidate_local_session_guard()


def test_execute_is_one_explicit_fake_attempt_and_always_finishes_permit(monkeypatch):
    events = []
    monkeypatch.setattr(api, "begin_authorized_report_execution", lambda **_kwargs: events.append("begin") or {"model_call_id": 41})
    monkeypatch.setattr(api, "execute_prepared_report_generation", lambda **_kwargs: events.append("execute") or {"task_state": "succeeded", "model_result_id": 9, "report_version_id": 10})
    monkeypatch.setattr(api, "finish_authorized_report_execution", lambda scope: events.append(("finish", scope)))
    client, headers = make_client(monkeypatch)
    headers["local-idempotency-key"] = "idem-1"
    response = client.post("/api/projects/7/report-generation-tasks/tsk-demo/execute-authorized", json={"model_call_id": 41, "data_scope_hash": "a" * 64}, headers=headers)
    assert response.status_code == 200
    assert response.json()["task_state"] == "succeeded"
    assert events == ["begin", "execute", ("finish", "a" * 64)]
    local_session_api.invalidate_local_session_guard()


def test_execute_exception_still_revokes_permit_and_is_not_retried(monkeypatch):
    events = []
    monkeypatch.setattr(api, "begin_authorized_report_execution", lambda **_kwargs: events.append("begin") or {"model_call_id": 41})
    def fail_once(**_kwargs):
        events.append("execute")
        raise HTTPException(status_code=503, detail={"code": "FAKE_PROVIDER_UNAVAILABLE", "message": "fake"})
    monkeypatch.setattr(api, "execute_prepared_report_generation", fail_once)
    monkeypatch.setattr(api, "finish_authorized_report_execution", lambda scope: events.append(("finish", scope)))
    client, headers = make_client(monkeypatch)
    headers["local-idempotency-key"] = "idem-2"
    response = client.post("/api/projects/7/report-generation-tasks/tsk-demo/execute-authorized", json={"model_call_id": 41, "data_scope_hash": "b" * 64}, headers=headers)
    assert response.status_code == 503
    assert events == ["begin", "execute", ("finish", "b" * 64)]
    local_session_api.invalidate_local_session_guard()
