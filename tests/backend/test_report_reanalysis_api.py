from __future__ import annotations

from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import local_session_api  # noqa: E402
from app import report_review_api as api  # noqa: E402


def make_client():
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.invalidate_local_session_guard()
    local_session_api.configure_local_session_guard("reanalyze-bootstrap")
    client = TestClient(app)
    base = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "reanalyze-bootstrap"},
        headers=base,
    )
    assert exchanged.status_code == 200
    session = exchanged.json()["session_token"]
    return client, {
        **base,
        "x-anxin-session": session,
        "x-request-id": "reanalyze-req-1",
    }


def payload():
    return {
        "expected_report_state_version": 1,
        "error_location": "支付模块 / 测试状态",
        "corrected_truth": "当前测试结论尚未形成，不能写成已完成。",
        "correction_basis": "项目经理核对冻结证据后确认。",
        "correction_source": "项目经理人工核对",
        "requested_by": "张经理",
        "requested_timezone": "Asia/Shanghai",
    }


def test_reanalysis_post_requires_launcher_session_before_core(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(
        api,
        "create_reanalysis_request",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.post(
        "/api/projects/1/reports/3/reanalysis",
        json=payload(),
        headers={"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 503
    assert called["count"] == 0


def test_reanalysis_post_requires_idempotency_key_before_core(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(
        api,
        "create_reanalysis_request",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    client, headers = make_client()
    response = client.post("/api/projects/1/reports/3/reanalysis", json=payload(), headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_SESSION_IDEMPOTENCY_KEY_REQUIRED"
    assert called["count"] == 0
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_post_forwards_exact_human_correction_without_execution(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return {
            "created": True,
            "reanalysis_request": {
                "reanalysis_request_id": 8,
                "report_version_id": 3,
                "replacement_local_task_id": "report-reanalysis-3-demo",
            },
            "replacement_task": {
                "id": 9,
                "local_task_id": "report-reanalysis-3-demo",
                "task_type": "daily_report_regenerate",
                "state": "queued",
            },
            "source_report_version": {
                "report_version_id": 3,
                "lifecycle": "superseded",
                "state_version": 2,
            },
        }

    monkeypatch.setattr(api, "create_reanalysis_request", create)
    client, headers = make_client()
    headers["local-idempotency-key"] = "reanalyze-idem-1"
    response = client.post("/api/projects/1/reports/3/reanalysis", json=payload(), headers=headers)
    assert response.status_code == 201
    assert response.json()["replacement_task"]["task_type"] == "daily_report_regenerate"
    assert response.json()["replacement_task"]["state"] == "queued"
    assert captured == {
        "project_id": 1,
        "report_version_id": 3,
        "error_location": "支付模块 / 测试状态",
        "corrected_truth": "当前测试结论尚未形成，不能写成已完成。",
        "correction_basis": "项目经理核对冻结证据后确认。",
        "correction_source": "项目经理人工核对",
        "requested_by": "张经理",
        "requested_timezone": "Asia/Shanghai",
        "idempotency_key": "reanalyze-idem-1",
        "expected_report_state_version": 1,
    }
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_get_is_read_only_and_needs_no_browser_session(monkeypatch):
    monkeypatch.setattr(
        api,
        "get_reanalysis_request",
        lambda **_kwargs: {
            "reanalysis_request": {"reanalysis_request_id": 8},
            "replacement_task": {"local_task_id": "report-reanalysis-3-demo", "state": "queued"},
            "source_report_version": {"lifecycle": "superseded"},
        },
    )
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.get("/api/projects/1/reports/3/reanalysis")
    assert response.status_code == 200
    assert response.json()["source_report_version"]["lifecycle"] == "superseded"
def test_reanalysis_prepare_uses_server_owned_replacement_task_and_never_executes(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        api,
        "get_reanalysis_request",
        lambda **_kwargs: {"replacement_task": {"local_task_id": "report-reanalysis-3-exact"}},
    )
    monkeypatch.setattr(
        api,
        "prepare_daily_report_regenerate_model_call",
        lambda **kwargs: captured.update(kwargs) or {"preparation_state": "prepared", "provider_send_state": "not_attempted"},
    )
    client, headers = make_client()
    response = client.post(
        "/api/projects/1/reports/3/reanalysis/prepare",
        json={"preparation_authorized": True},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["provider_send_state"] == "not_attempted"
    assert captured == {
        "project_id": 1,
        "local_task_id": "report-reanalysis-3-exact",
        "preparation_authorized": True,
    }
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_execute_requires_transport_idempotency_before_core(monkeypatch):
    called = {"count": 0}
    monkeypatch.setattr(api, "get_model_call", lambda _id: {"local_task_id": "task-x"})
    monkeypatch.setattr(
        api,
        "execute_prepared_report_reanalysis",
        lambda **_kwargs: called.__setitem__("count", called["count"] + 1) or {},
    )
    client, headers = make_client()
    response = client.post(
        "/api/projects/1/reports/3/reanalysis/execute",
        json={"model_call_id": 7},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_SESSION_IDEMPOTENCY_KEY_REQUIRED"
    assert called["count"] == 0
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_execute_binds_route_to_durable_call_local_task(monkeypatch):
    captured = {}
    events = []
    monkeypatch.setattr(
        api,
        "get_model_call",
        lambda model_call_id: {"model_call_id": model_call_id, "local_task_id": "report-reanalysis-3-exact"},
    )
    monkeypatch.setattr(
        api,
        "build_reanalysis_send_authorization_preview",
        lambda **_kwargs: {"data_scope_hash": "a" * 64, "purpose_id": "p"},
    )
    monkeypatch.setattr(api, "begin_page07_authorized_execution", lambda **_kwargs: events.append("begin"))
    monkeypatch.setattr(api, "finish_page07_authorized_execution", lambda **_kwargs: events.append("finish"))
    monkeypatch.setattr(
        api,
        "execute_prepared_report_reanalysis",
        lambda **kwargs: captured.update(kwargs) or {"task_state": "succeeded", "execution_source": "new_provider_execution"},
    )
    client, headers = make_client()
    headers["local-idempotency-key"] = "execute-reanalysis-7"
    response = client.post(
        "/api/projects/1/reports/3/reanalysis/execute",
        json={"model_call_id": 7, "data_scope_hash": "a" * 64},
        headers=headers,
    )
    assert response.status_code == 201
    assert events == ["begin", "finish"]
    assert captured == {
        "project_id": 1,
        "source_report_version_id": 3,
        "local_task_id": "report-reanalysis-3-exact",
        "model_call_id": 7,
    }
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_finalize_existing_calls_recovery_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(api, "get_model_call", lambda model_call_id: {"model_call_id": model_call_id, "local_task_id": "task-existing"})
    monkeypatch.setattr(
        api,
        "finalize_report_reanalysis_from_existing_result",
        lambda **kwargs: captured.update(kwargs) or {"execution_source": "existing_verified_result"},
    )
    client, headers = make_client()
    headers["local-idempotency-key"] = "finalize-existing-9"
    response = client.post(
        "/api/projects/1/reports/3/reanalysis/finalize-existing",
        json={"model_call_id": 9},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["execution_source"] == "existing_verified_result"
    assert captured["model_call_id"] == 9
    local_session_api.invalidate_local_session_guard()

