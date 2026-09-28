import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import local_session_api, page07_send_authorization as send_auth, report_review_api as api


def _client_headers():
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.invalidate_local_session_guard()
    local_session_api.configure_local_session_guard("page07-auth-bootstrap")
    client = TestClient(app)
    base = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "page07-auth-bootstrap"},
        headers=base,
    )
    assert exchanged.status_code == 200
    session = exchanged.json()["session_token"]
    return client, {**base, "x-anxin-session": session, "x-request-id": "page07-auth-req-1"}


def _reset_send_permit(monkeypatch):
    for name in (
        "ANXIN_DEEPSEEK_SEND_AUTHORIZED",
        "ANXIN_DEEPSEEK_SEND_PURPOSE_ID",
        "ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    send_auth.revoke_page07_send_authorization()


def _preview(scope: str, purpose: str) -> dict[str, object]:
    return {
        "data_scope_hash": scope,
        "authorization_state": "awaiting_human_confirmation",
        "purpose_id": purpose,
    }


def test_reanalysis_execute_requires_exact_scope_after_transport_guard(monkeypatch):
    monkeypatch.setattr(
        api,
        "build_reanalysis_send_authorization_preview",
        lambda **_kw: {"data_scope_hash": "a" * 64, "purpose_id": "p"},
    )
    client, headers = _client_headers()
    headers["local-idempotency-key"] = "rean-exec-1"
    response = client.post("/api/projects/1/reports/3/reanalysis/execute", json={"model_call_id": 7}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PAGE07_SEND_AUTHORIZATION_INVALID"
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_authorize_rebuilds_preview_and_requires_human(monkeypatch):
    captured = {}
    monkeypatch.setattr(api, "build_reanalysis_send_authorization_preview", lambda **kw: {"data_scope_hash": "a"*64, "authorization_state": "awaiting_human_confirmation", "purpose_id": "p"})
    monkeypatch.setattr(api, "authorize_page07_send_scope", lambda **kw: captured.update(kw) or {"authorization_state": "authorized_once"})
    client, headers = _client_headers(); headers["local-idempotency-key"] = "rean-auth-1"
    response = client.post("/api/projects/1/reports/3/reanalysis/authorize-send", json={"model_call_id": 7, "data_scope_hash": "a"*64, "human_confirmed": True}, headers=headers)
    assert response.status_code == 200
    assert captured["human_confirmed"] is True
    local_session_api.invalidate_local_session_guard()


def test_contradiction_execute_always_finishes_permit(monkeypatch):
    events = []
    monkeypatch.setattr(api, "build_contradiction_send_authorization_preview", lambda **kw: {"data_scope_hash": "b"*64, "purpose_id": "p"})
    monkeypatch.setattr(api, "begin_page07_authorized_execution", lambda **kw: events.append("begin"))
    monkeypatch.setattr(api, "finish_page07_authorized_execution", lambda **kw: events.append("finish"))
    monkeypatch.setattr(api, "get_model_call", lambda _id: {"project_id": 1, "task_type": "report_contradiction_check", "provider": "deepseek"})
    monkeypatch.setattr(api, "get_report_contradiction_budget_record", lambda **kw: {})
    monkeypatch.setattr(api, "execute_report_contradiction_model_call", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    client, headers = _client_headers(); headers["local-idempotency-key"] = "contra-exec-1"
    try:
        client.post("/api/projects/1/reports/3/contradiction/execute", json={"model_call_id": 8, "data_scope_hash": "b"*64}, headers=headers)
    except RuntimeError:
        pass
    assert events == ["begin", "finish"]
    local_session_api.invalidate_local_session_guard()


def test_reanalysis_lookup_failure_after_begin_still_finishes_permit(monkeypatch):
    events = []
    monkeypatch.setattr(api, "build_reanalysis_send_authorization_preview", lambda **kw: {"data_scope_hash": "c"*64, "purpose_id": "p"})
    monkeypatch.setattr(api, "begin_page07_authorized_execution", lambda **kw: events.append("begin"))
    monkeypatch.setattr(api, "finish_page07_authorized_execution", lambda **kw: events.append("finish"))
    monkeypatch.setattr(api, "get_model_call", lambda _id: (_ for _ in ()).throw(RuntimeError("lookup-boom")))
    client, headers = _client_headers(); headers["local-idempotency-key"] = "rean-lookup-fail-1"
    try:
        client.post("/api/projects/1/reports/3/reanalysis/execute", json={"model_call_id": 7, "data_scope_hash": "c"*64}, headers=headers)
    except RuntimeError:
        pass
    assert events == ["begin", "finish"]
    local_session_api.invalidate_local_session_guard()


def test_contradiction_lookup_failure_after_begin_still_finishes_permit(monkeypatch):
    events = []
    monkeypatch.setattr(api, "build_contradiction_send_authorization_preview", lambda **kw: {"data_scope_hash": "d"*64, "purpose_id": "p"})
    monkeypatch.setattr(api, "begin_page07_authorized_execution", lambda **kw: events.append("begin"))
    monkeypatch.setattr(api, "finish_page07_authorized_execution", lambda **kw: events.append("finish"))
    monkeypatch.setattr(api, "get_model_call", lambda _id: (_ for _ in ()).throw(RuntimeError("lookup-boom")))
    client, headers = _client_headers(); headers["local-idempotency-key"] = "contra-lookup-fail-1"
    try:
        client.post("/api/projects/1/reports/3/contradiction/execute", json={"model_call_id": 8, "data_scope_hash": "d"*64}, headers=headers)
    except RuntimeError:
        pass
    assert events == ["begin", "finish"]
    local_session_api.invalidate_local_session_guard()


def test_stale_finalizer_cannot_clear_newer_in_flight_permit(monkeypatch):
    _reset_send_permit(monkeypatch)
    scope_a = "a" * 64
    scope_b = "b" * 64
    scope_c = "c" * 64
    preview_a = _preview(scope_a, "purpose-a")
    preview_b = _preview(scope_b, "purpose-b")
    preview_c = _preview(scope_c, "purpose-c")

    send_auth.authorize_page07_send_scope(preview=preview_a, expected_data_scope_hash=scope_a, human_confirmed=True)
    send_auth.begin_page07_authorized_execution(preview=preview_a, expected_data_scope_hash=scope_a)
    send_auth.revoke_page07_send_authorization()
    send_auth.authorize_page07_send_scope(preview=preview_b, expected_data_scope_hash=scope_b, human_confirmed=True)
    send_auth.begin_page07_authorized_execution(preview=preview_b, expected_data_scope_hash=scope_b)

    try:
        send_auth.finish_page07_authorized_execution(expected_data_scope_hash=scope_a)
        with pytest.raises(Exception) as exc_info:
            send_auth.authorize_page07_send_scope(preview=preview_c, expected_data_scope_hash=scope_c, human_confirmed=True)
        detail = getattr(exc_info.value, "detail", {})
        assert detail.get("code") == "PAGE07_SEND_AUTHORIZATION_BUSY"
    finally:
        send_auth.finish_page07_authorized_execution(expected_data_scope_hash=scope_b)


def test_stale_begin_cannot_revoke_newer_authorized_permit(monkeypatch):
    _reset_send_permit(monkeypatch)
    scope_a = "a" * 64
    scope_b = "b" * 64
    preview_a = _preview(scope_a, "purpose-a")
    preview_b = _preview(scope_b, "purpose-b")

    send_auth.authorize_page07_send_scope(preview=preview_a, expected_data_scope_hash=scope_a, human_confirmed=True)
    send_auth.authorize_page07_send_scope(preview=preview_b, expected_data_scope_hash=scope_b, human_confirmed=True)

    with pytest.raises(Exception) as exc_info:
        send_auth.begin_page07_authorized_execution(preview=preview_a, expected_data_scope_hash=scope_a)
    detail = getattr(exc_info.value, "detail", {})
    assert detail.get("code") == "PAGE07_SEND_AUTHORIZATION_NOT_READY"

    try:
        send_auth.begin_page07_authorized_execution(preview=preview_b, expected_data_scope_hash=scope_b)
    finally:
        send_auth.finish_page07_authorized_execution(expected_data_scope_hash=scope_b)


def test_duplicate_begin_cannot_clear_in_flight_permit(monkeypatch):
    _reset_send_permit(monkeypatch)
    scope_b = "b" * 64
    scope_c = "c" * 64
    preview_b = _preview(scope_b, "purpose-b")
    preview_c = _preview(scope_c, "purpose-c")

    send_auth.authorize_page07_send_scope(preview=preview_b, expected_data_scope_hash=scope_b, human_confirmed=True)
    send_auth.begin_page07_authorized_execution(preview=preview_b, expected_data_scope_hash=scope_b)
    try:
        with pytest.raises(Exception) as duplicate_exc:
            send_auth.begin_page07_authorized_execution(preview=preview_b, expected_data_scope_hash=scope_b)
        assert getattr(duplicate_exc.value, "detail", {}).get("code") == "PAGE07_SEND_AUTHORIZATION_BUSY"

        with pytest.raises(Exception) as replacement_exc:
            send_auth.authorize_page07_send_scope(preview=preview_c, expected_data_scope_hash=scope_c, human_confirmed=True)
        assert getattr(replacement_exc.value, "detail", {}).get("code") == "PAGE07_SEND_AUTHORIZATION_BUSY"
    finally:
        send_auth.finish_page07_authorized_execution(expected_data_scope_hash=scope_b)

    authorized = send_auth.authorize_page07_send_scope(preview=preview_c, expected_data_scope_hash=scope_c, human_confirmed=True)
    assert authorized["authorization_state"] == "authorized_once"
    send_auth.revoke_page07_send_authorization()
