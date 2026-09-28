"""Page 07 Report Review HTTP boundary tests."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from app import (  # noqa: E402
    context_resolver,
    db,
    local_session_api,
    main,
    model_execution_results,
    report_review,
)
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_model_execution_results as result_tests  # noqa: E402


@pytest.fixture()
def api_state(tmp_path, monkeypatch):
    state = candidate_tests._make_state(tmp_path, monkeypatch)
    db.init_db()
    report_review.init_report_review_schema()
    candidate = context_resolver.build_context_candidate_set(state["snapshot_id"])
    state["evidence_id"] = candidate["items"][0]["evidence_id"]
    call = result_tests._prepare(
        state,
        task_type="daily_report_generate",
        local_task_id="page07-api-report",
        call_prepare_key="page07-api-prepare",
    )
    result = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"],
        receipt=result_tests._receipt(state, call),
    )
    materialized = report_review.materialize_report_version(
        project_id=state["project_id"],
        model_execution_result_id=result["model_result_id"],
    )
    state["model_result"] = result
    state["report"] = materialized["report_version"]
    importlib.reload(main)
    local_session_api.invalidate_local_session_guard()
    monkeypatch.setenv("ANXINBOARD_LOCAL_BOOTSTRAP_SECRET", "page07-api-bootstrap")
    with TestClient(main.app) as client:
        base = {"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}
        exchanged = client.post(
            "/api/local-session/exchange",
            json={"bootstrap_secret": "page07-api-bootstrap"},
            headers=base,
        )
        assert exchanged.status_code == 200
        state["client"] = client
        state["write_headers"] = {
            **base,
            "x-anxin-session": exchanged.json()["session_token"],
            "x-request-id": "page07-api-write-1",
        }
        yield state
    local_session_api.invalidate_local_session_guard()


def _model_row(state: dict) -> tuple:
    with sqlite3.connect(state["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT formal_response_json, formal_response_hash,
                   validated_result_json, validated_result_hash,
                   execution_result_hash
            FROM model_execution_results
            WHERE id = ?
            """,
            (state["model_result"]["model_result_id"],),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_t01_exact_and_current_get_return_same_read_only_bundle(api_state):
    client = api_state["client"]
    report_id = api_state["report"]["report_version_id"]
    before_model = _model_row(api_state)
    with sqlite3.connect(api_state["db_path"]) as conn:
        before_counts = (
            conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM report_supplement_versions").fetchone()[0],
        )

    exact = client.get(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/review"
    )
    current = client.get(
        f"/api/projects/{api_state['project_id']}/report-review/current"
    )
    assert exact.status_code == 200
    assert current.status_code == 200
    assert exact.json() == current.json()
    body = exact.json()
    assert body["report_version"]["report_version_id"] == report_id
    assert body["ai_raw"]["content"] == api_state["model_result"]["validated_result"]
    assert body["current_supplement"] is None
    assert body["latest_validation_result"] is None
    assert _model_row(api_state) == before_model
    with sqlite3.connect(api_state["db_path"]) as conn:
        after_counts = (
            conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM report_supplement_versions").fetchone()[0],
        )
    assert after_counts == before_counts


def test_t02_current_get_never_materializes_missing_project_report(api_state):
    client = api_state["client"]
    with sqlite3.connect(api_state["db_path"]) as conn:
        before = conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0]
    response = client.get(
        f"/api/projects/{api_state['project_id'] + 999}/report-review/current"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REPORT_VERSION_NOT_FOUND"
    with sqlite3.connect(api_state["db_path"]) as conn:
        after = conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0]
    assert after == before


def test_t03_supplement_post_appends_provenance_and_exact_replay(api_state):
    client = api_state["client"]
    report_id = api_state["report"]["report_version_id"]
    payload = {
        "content": "项目经理补充：联调窗口待甲方确认。",
        "source_type": "pm_external_fact",
        "provided_by": "pm-user",
        "provided_timezone": "Asia/Shanghai",
        "idempotency_key": "api-supplement-1",
        "expected_latest_supplement_version": 0,
    }
    before_model = _model_row(api_state)
    first = client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/supplements",
        json=payload,
        headers=api_state["write_headers"],
    )
    replay = client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/supplements",
        json=payload,
        headers=api_state["write_headers"],
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json()["created"] is True
    assert replay.json()["created"] is False
    assert replay.json()["supplement_version"] == first.json()["supplement_version"]
    supplement = first.json()["supplement_version"]
    assert supplement["provided_by"] == "pm-user"
    assert supplement["provided_at"]
    assert supplement["provided_timezone"] == "Asia/Shanghai"
    assert _model_row(api_state) == before_model

    bundle = client.get(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/review"
    ).json()
    assert bundle["current_supplement"] == supplement
    assert bundle["latest_validation_result"] is None
    assert bundle["ai_raw"]["content"] == api_state["model_result"]["validated_result"]


def test_t04_stale_append_is_conflict_and_zero_write(api_state):
    client = api_state["client"]
    report_id = api_state["report"]["report_version_id"]
    first = {
        "content": "v1",
        "source_type": "pm_external_fact",
        "provided_by": "pm",
        "provided_timezone": "UTC",
        "idempotency_key": "api-v1",
        "expected_latest_supplement_version": 0,
    }
    assert client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/supplements",
        json=first,
        headers=api_state["write_headers"],
    ).status_code == 201
    stale = dict(first)
    stale["content"] = "stale"
    stale["idempotency_key"] = "api-stale"
    response = client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/supplements",
        json=stale,
        headers=api_state["write_headers"],
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REPORT_SUPPLEMENT_STALE"
    with sqlite3.connect(api_state["db_path"]) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM report_supplement_versions").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "forbidden",
    [
        {"ai_raw": "caller rewrite"},
        {"validated_result_hash": "0" * 64},
        {"formal_response_hash": "0" * 64},
        {"execution_result_hash": "0" * 64},
        {"model_execution_result_id": 999},
        {"provided_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_t05_supplement_http_has_no_ai_raw_or_hash_write_surface(api_state, forbidden):
    client = api_state["client"]
    report_id = api_state["report"]["report_version_id"]
    payload = {
        "content": "合法补充",
        "source_type": "pm_external_fact",
        "provided_by": "pm",
        "provided_timezone": "UTC",
        "idempotency_key": "forbidden-" + next(iter(forbidden)),
        "expected_latest_supplement_version": 0,
        **forbidden,
    }
    before_model = _model_row(api_state)
    response = client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/supplements",
        json=payload,
        headers=api_state["write_headers"],
    )
    assert response.status_code == 422
    assert _model_row(api_state) == before_model
    with sqlite3.connect(api_state["db_path"]) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM report_supplement_versions").fetchone()[0]
            == 0
        )


def test_t06_current_router_includes_approval_and_reanalysis_without_materialize(api_state):
    paths = {
        route.path
        for route in main.app.routes
        if "report-review" in route.path or "/reports/{report_version_id}" in route.path
    }
    assert paths == {
        "/api/projects/{project_id}/anxin-board/reports/{report_version_id}/narrative",
        "/api/projects/{project_id}/report-review/current",
        "/api/projects/{project_id}/reports/{report_version_id}/approval",
        "/api/projects/{project_id}/reports/{report_version_id}/contradiction/authorize-send",
        "/api/projects/{project_id}/reports/{report_version_id}/contradiction/execute",
        "/api/projects/{project_id}/reports/{report_version_id}/contradiction/prepare",
        "/api/projects/{project_id}/reports/{report_version_id}/contradiction/send-authorization-preview",
        "/api/projects/{project_id}/reports/{report_version_id}/reanalysis",
        "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/authorize-send",
        "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/execute",
        "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/finalize-existing",
        "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/prepare",
        "/api/projects/{project_id}/reports/{report_version_id}/reanalysis/send-authorization-preview",
        "/api/projects/{project_id}/reports/{report_version_id}/review",
        "/api/projects/{project_id}/reports/{report_version_id}/supplements",
        "/api/projects/{project_id}/reports/{report_version_id}/validations",
    }
    assert all("materialize" not in path for path in paths)


def test_t07_validation_post_is_server_derived_and_visible_on_reload(api_state):
    client = api_state["client"]
    report = api_state["report"]
    report_id = report["report_version_id"]
    before_model = _model_row(api_state)

    response = client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/validations",
        json={"expected_report_state_version": report["state_version"]},
        headers=api_state["write_headers"],
    )
    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["validation_result"]["state"] == "passed"
    assert body["validation_result"]["blockers"] == []
    assert _model_row(api_state) == before_model

    reloaded = client.get(
        f"/api/projects/{api_state['project_id']}/reports/{report_id}/review"
    )
    assert reloaded.status_code == 200
    assert reloaded.json()["latest_validation_result"] == body["validation_result"]


def test_t08_validation_http_rejects_client_pass_override(api_state):
    client = api_state["client"]
    report = api_state["report"]
    response = client.post(
        f"/api/projects/{api_state['project_id']}/reports/{report['report_version_id']}/validations",
        json={
            "expected_report_state_version": report["state_version"],
            "validation_passed": True,
        },
        headers=api_state["write_headers"],
    )
    assert response.status_code == 422
