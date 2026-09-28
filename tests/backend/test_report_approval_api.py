from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import local_session_api  # noqa: E402
from app import report_approval_api as api  # noqa: E402


def make_client():
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.invalidate_local_session_guard()
    local_session_api.configure_local_session_guard("approval-bootstrap")
    client = TestClient(app)
    base = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "approval-bootstrap"},
        headers=base,
    )
    assert exchanged.status_code == 200
    session = exchanged.json()["session_token"]
    return client, {
        **base,
        "x-anxin-session": session,
        "x-request-id": "approval-req-1",
    }


def payload():
    return {
        "expected_report_state_version": 1,
        "confirmed_by": "张经理",
        "confirmed_timezone": "Asia/Shanghai",
        "confirmed_utc_offset_minutes": 480,
        "human_confirmed": True,
    }


def test_approval_post_requires_launcher_session_before_owner(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(
        api,
        "create_report_approval_snapshot",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.post(
        "/api/projects/1/reports/3/approval",
        json=payload(),
        headers={"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 503
    assert called["count"] == 0


def test_approval_post_requires_idempotency_key_before_owner(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(
        api,
        "create_report_approval_snapshot",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    client, headers = make_client()
    response = client.post("/api/projects/1/reports/3/approval", json=payload(), headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_SESSION_IDEMPOTENCY_KEY_REQUIRED"
    assert called["count"] == 0
    local_session_api.invalidate_local_session_guard()


def test_approval_post_forwards_server_guarded_human_confirmation(monkeypatch):
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return {"approval_snapshot": {"approval_snapshot_id": 8, "confirmed_by": kwargs["confirmed_by"]}, "created": True}
    monkeypatch.setattr(api, "create_report_approval_snapshot", create)
    client, headers = make_client()
    headers["local-idempotency-key"] = "approval-idem-1"
    response = client.post("/api/projects/1/reports/3/approval", json=payload(), headers=headers)
    assert response.status_code == 200
    assert response.json()["approval_snapshot"]["confirmed_by"] == "张经理"
    assert captured == {
        "project_id": 1,
        "report_version_id": 3,
        "expected_report_state_version": 1,
        "confirmed_by": "张经理",
        "confirmed_timezone": "Asia/Shanghai",
        "confirmed_utc_offset_minutes": 480,
        "human_confirmed": True,
        "idempotency_key": "approval-idem-1",
        "progress_corrections": [],
    }
    local_session_api.invalidate_local_session_guard()


def test_approval_get_is_read_only_and_needs_no_browser_session(monkeypatch):
    monkeypatch.setattr(
        api,
        "get_report_approval_snapshot",
        lambda **_kwargs: {"approval_snapshot_id": 8, "confirmed_by": "张经理"},
    )
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.get("/api/projects/1/reports/3/approval")
    assert response.status_code == 200
    assert response.json()["approval_snapshot"]["approval_snapshot_id"] == 8
