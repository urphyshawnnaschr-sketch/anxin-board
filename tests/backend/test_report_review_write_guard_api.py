from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import local_session_api  # noqa: E402
from app import report_review_api as api  # noqa: E402


def supplement_payload() -> dict[str, object]:
    return {
        "content": "项目经理补充：当前测试状态仍待确认。",
        "source_type": "pm_external_fact",
        "provided_by": "张经理",
        "provided_timezone": "Asia/Shanghai",
        "idempotency_key": "supplement-domain-idem-1",
        "expected_latest_supplement_version": 0,
    }


def validation_payload() -> dict[str, object]:
    return {"expected_report_state_version": 1}


def make_guarded_client() -> tuple[TestClient, dict[str, str]]:
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.invalidate_local_session_guard()
    local_session_api.configure_local_session_guard("page07-write-bootstrap")
    client = TestClient(app)
    base = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "page07-write-bootstrap"},
        headers=base,
    )
    assert exchanged.status_code == 200
    return client, {
        **base,
        "x-anxin-session": exchanged.json()["session_token"],
        "x-request-id": "page07-write-req-1",
    }


def test_supplement_write_requires_launcher_session_before_owner(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(
        api,
        "append_supplement_version",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    local_session_api.invalidate_local_session_guard()
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).post(
        "/api/projects/1/reports/3/supplements",
        json=supplement_payload(),
        headers={"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LOCAL_SESSION_UNAVAILABLE"
    assert called["count"] == 0


def test_validation_write_requires_launcher_session_before_owner(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(
        api,
        "create_validation_result",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    local_session_api.invalidate_local_session_guard()
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).post(
        "/api/projects/1/reports/3/validations",
        json=validation_payload(),
        headers={"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LOCAL_SESSION_UNAVAILABLE"
    assert called["count"] == 0


def test_guarded_supplement_reuses_domain_idempotency_without_transport_key(monkeypatch):
    captured: dict[str, object] = {}

    def append(**kwargs):
        captured.update(kwargs)
        return {"supplement_version": {"supplement_version_id": 4}, "created": True}

    monkeypatch.setattr(api, "append_supplement_version", append)
    client, headers = make_guarded_client()
    assert "local-idempotency-key" not in headers
    response = client.post(
        "/api/projects/1/reports/3/supplements",
        json=supplement_payload(),
        headers=headers,
    )
    assert response.status_code == 201
    assert captured["idempotency_key"] == "supplement-domain-idem-1"
    assert captured["expected_latest_supplement_version"] == 0
    local_session_api.invalidate_local_session_guard()


def test_guarded_validation_uses_deterministic_owner_without_transport_key(monkeypatch):
    captured: dict[str, object] = {}

    def create(**kwargs):
        captured.update(kwargs)
        return {"validation_result": {"validation_result_id": 5}, "created": False}

    monkeypatch.setattr(api, "create_validation_result", create)
    client, headers = make_guarded_client()
    assert "local-idempotency-key" not in headers
    response = client.post(
        "/api/projects/1/reports/3/validations",
        json=validation_payload(),
        headers=headers,
    )
    assert response.status_code == 201
    assert captured == {
        "project_id": 1,
        "report_version_id": 3,
        "expected_report_state_version": 1,
    }
    local_session_api.invalidate_local_session_guard()


def test_wrong_origin_blocks_both_legacy_writes_before_owner(monkeypatch):
    calls = {"supplement": 0, "validation": 0}
    monkeypatch.setattr(
        api,
        "append_supplement_version",
        lambda **_kwargs: calls.__setitem__("supplement", calls["supplement"] + 1) or {},
    )
    monkeypatch.setattr(
        api,
        "create_validation_result",
        lambda **_kwargs: calls.__setitem__("validation", calls["validation"] + 1) or {},
    )
    client, headers = make_guarded_client()
    bad = {**headers, "origin": "http://localhost:5173"}
    supplement = client.post(
        "/api/projects/1/reports/3/supplements",
        json=supplement_payload(),
        headers=bad,
    )
    validation = client.post(
        "/api/projects/1/reports/3/validations",
        json=validation_payload(),
        headers=bad,
    )
    assert supplement.status_code == 403
    assert validation.status_code == 403
    assert supplement.json()["detail"]["code"] == "LOCAL_SESSION_ORIGIN_INVALID"
    assert validation.json()["detail"]["code"] == "LOCAL_SESSION_ORIGIN_INVALID"
    assert calls == {"supplement": 0, "validation": 0}
    local_session_api.invalidate_local_session_guard()
