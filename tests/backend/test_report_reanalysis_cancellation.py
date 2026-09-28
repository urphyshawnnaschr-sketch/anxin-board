"""Cancellation must close the original durable chain without provider traffic."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app import db, report_review, report_generation_tasks, page07_send_authorization as permits
from app import model_execution, report_reanalysis_execution, report_task_subjects
from test_report_review import review_state, _materialize, _reanalyze, _model_row, _report_identity_row
from test_report_reanalysis_api import make_client
import test_model_execution_results as result_tests


@pytest.fixture(autouse=True)
def isolated_permits(monkeypatch):
    permits.revoke_page07_send_authorization()
    for name in (permits._ENV_AUTHORIZED, permits._ENV_PURPOSE, permits._ENV_SCOPE):
        monkeypatch.delenv(name, raising=False)
    yield
    permits.revoke_page07_send_authorization()


def prepared_preview(state, report, bundle):
    task = bundle["replacement_task"]
    call = result_tests._prepare(state, task_type="daily_report_regenerate", local_task_id=task["local_task_id"], call_prepare_key="replacement-call")
    return {"flow": "report_reanalysis", "project_id": state["project_id"],
        "report_version_id": report["report_version_id"], "local_task_id": task["local_task_id"],
        "model_call_id": call["model_call_id"], "data_scope_hash": "b"*64,
        "purpose_id": "anxin_board_daily_report_regenerate_v1", "authorization_state": "awaiting_human_confirmation"}


def authorize(preview):
    return permits.authorize_page07_send_scope(preview=preview, expected_data_scope_hash=preview["data_scope_hash"], human_confirmed=True)


def begin(preview):
    return permits.begin_page07_authorized_execution(preview=preview, expected_data_scope_hash=preview["data_scope_hash"])


def chain(state):
    report = _materialize(state)["report_version"]
    bundle = _reanalyze(state, report["report_version_id"])
    payload = dict(
        expected_report_state_version=2,
        expected_reanalysis_request_hash=bundle["reanalysis_request"]["request_hash"],
        expected_replacement_task_identity_hash=bundle["replacement_task"]["identity_hash"],
        cancelled_by="Codex (authorized acceptance)",
        cancellation_reason="The replacement was never sent; continue reviewing the saved result.",
        idempotency_key="cancel-1",
    )
    return report, bundle, payload


def cancel(state, report, payload):
    from app.report_reanalysis_cancellation import cancel_unstarted_reanalysis
    return cancel_unstarted_reanalysis(project_id=state["project_id"], report_version_id=report["report_version_id"], **payload)


def test_cancel_http_restores_review_without_changing_ai_or_history(review_state):
    report, bundle, payload = chain(review_state)
    before = _model_row(review_state), _report_identity_row(review_state, report["report_version_id"])
    client, headers = make_client()
    headers["local-idempotency-key"] = "cancel-request-1"
    response = client.post(f'/api/projects/{review_state["project_id"]}/reports/{report["report_version_id"]}/reanalysis/cancel', json=payload, headers=headers)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["cancelled"] is True and value["created"] is True
    assert value["cancellation"]["state"] == "cancelled_before_send"
    assert value["source_report_version"]["lifecycle"] == "pending_review"
    assert value["source_report_version"]["state_version"] == 3
    assert value["replacement_task"]["state"] == "voided"
    assert value["replacement_task"]["current_attempt"]["started_at"] is None
    observed = report_generation_tasks.get_report_generation_task(project_id=review_state["project_id"], local_task_id=bundle["replacement_task"]["local_task_id"])
    assert observed["state"] == "voided" and observed["reanalysis_cancelled"] is True
    assert before == (_model_row(review_state), _report_identity_row(review_state, report["report_version_id"]))
    assert value["reanalysis_request"] == bundle["reanalysis_request"]
    replay = cancel(review_state, report, payload)
    assert replay["created"] is False
    assert replay["cancellation"] == value["cancellation"]
    old_request = _reanalyze(review_state, report["report_version_id"])
    assert old_request["cancelled"] is True
    assert old_request["replacement_task"]["state"] == "voided"
    assert report_review.get_current_review_bundle(project_id=review_state["project_id"])["report_version"]["state_version"] == 3
    supplement = report_review.append_supplement_version(project_id=review_state["project_id"], report_version_id=report["report_version_id"], content="Explicit simulation; five checks passed.", source_type="pm_external_fact", provided_by="PM", provided_timezone="UTC", idempotency_key="supplement-after-cancel", expected_latest_supplement_version=0)
    assert supplement["created"] is True
    with pytest.raises(HTTPException):
        report_task_subjects.build_daily_report_regenerate_subject(project_id=review_state["project_id"], report_version_id=report["report_version_id"])


@pytest.mark.parametrize("field,value", [("expected_report_state_version", 1), ("expected_reanalysis_request_hash", "0"*64), ("expected_replacement_task_identity_hash", "0"*64)])
def test_stale_identity_is_zero_write(review_state, field, value):
    report, bundle, payload = chain(review_state)
    with pytest.raises(HTTPException):
        cancel(review_state, report, {**payload, field: value})
    current = report_review.get_reanalysis_request(project_id=review_state["project_id"], report_version_id=report["report_version_id"])
    assert current["source_report_version"]["state_version"] == 2
    assert current["replacement_task"]["state"] == "queued"


@pytest.mark.parametrize("state", ["running", "unknown", "succeeded"])
def test_started_unknown_or_completed_task_cannot_be_restored(review_state, state):
    report, bundle, payload = chain(review_state)
    task = bundle["replacement_task"]
    with db.get_connection() as conn:
        conn.execute("UPDATE report_generation_tasks SET state=? WHERE id=?", (state,task["id"]))
        conn.execute("UPDATE report_generation_task_attempts SET state=?,started_at='started',finished_at=? WHERE task_id=?", (state,"finished" if state == "succeeded" else None,task["id"]))
    with pytest.raises(HTTPException):
        cancel(review_state, report, payload)
    with db.get_connection() as conn:
        assert conn.execute("SELECT lifecycle FROM report_versions WHERE id=?", (report["report_version_id"],)).fetchone()[0] == "superseded"


def test_cancel_releases_active_checkpoint_and_survives_restart(review_state):
    report, bundle, payload = chain(review_state)
    cancel(review_state, report, payload)
    db.init_db(); report_review.init_report_review_schema()
    loaded = report_review.get_reanalysis_request(project_id=review_state["project_id"], report_version_id=report["report_version_id"])
    assert loaded["cancelled"] is True
    assert report_generation_tasks.find_active_report_generation_task(project_id=review_state["project_id"], evidence_snapshot_id=review_state["snapshot_id"]) is None
    with pytest.raises(sqlite3.IntegrityError):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM report_reanalysis_cancellations")


def test_two_sqlite_connections_cancel_once(review_state):
    report, bundle, payload = chain(review_state)
    gate = threading.Barrier(2)
    def invoke():
        gate.wait(timeout=5)
        return cancel(review_state, report, payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    assert sorted(x["created"] for x in results) == [False, True]
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_reanalysis_cancellations").fetchone()[0] == 1


def test_cancellation_revokes_exact_permit_and_rejects_stale_authorize_or_begin(review_state):
    report, bundle, payload = chain(review_state)
    preview = prepared_preview(review_state, report, bundle)
    authorize(preview)
    cancel(review_state, report, payload)
    assert permits._active_scope_hash is None
    for action in (authorize, begin):
        with pytest.raises(HTTPException):
            action(preview)


def test_other_flow_permit_is_not_cleared(review_state):
    report, bundle, payload = chain(review_state)
    preview = {"flow":"report_contradiction", "data_scope_hash":"c"*64, "purpose_id":"contradiction", "authorization_state":"awaiting_human_confirmation"}
    authorize(preview)
    cancel(review_state, report, payload)
    begin(preview)
    assert permits._in_flight is True


def test_commit_failure_rolls_back_and_preserves_exact_permit(review_state, monkeypatch):
    from app import report_reanalysis_cancellation as cancellation
    report, bundle, payload = chain(review_state)
    preview = prepared_preview(review_state, report, bundle)
    authorize(preview)
    class RejectCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")
    def connection():
        conn = sqlite3.connect(review_state["db_path"], factory=RejectCommit)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(cancellation, "get_connection", connection)
    with pytest.raises(HTTPException):
        cancel(review_state, report, payload)
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_reanalysis_cancellations").fetchone()[0] == 0
        assert conn.execute("SELECT lifecycle,state_version FROM report_versions WHERE id=?", (report["report_version_id"],)).fetchone()[:] == ("superseded",2)
    begin(preview)
    assert permits._in_flight is True


def test_begin_competes_with_cancellation_and_only_one_wins(review_state):
    report, bundle, payload = chain(review_state)
    preview = prepared_preview(review_state, report, bundle)
    authorize(preview)
    gate = threading.Barrier(2)
    def competing(action):
        gate.wait(timeout=5)
        try:
            action()
            return True
        except HTTPException:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(competing, lambda: cancel(review_state, report, payload)), pool.submit(competing, lambda: begin(preview))]
        results = [job.result(timeout=30) for job in jobs]
    assert sorted(results) == [False, True]


def test_low_level_stale_transient_cannot_send_after_cancellation(review_state, monkeypatch):
    from app import page07_model_execution as execution
    from types import SimpleNamespace
    report, bundle, payload = chain(review_state)
    preview = prepared_preview(review_state, report, bundle)
    cancel(review_state, report, payload)
    provider_calls = []
    monkeypatch.setattr(execution, "build_ready_daily_report_regenerate_preflight", lambda **kw: {"provider":"fake"})
    monkeypatch.setattr(execution.model_provider_runtime, "resolve_model_provider_adapter", lambda _p: SimpleNamespace(execute=lambda req: provider_calls.append(req)))
    monkeypatch.setattr(execution.model_gateway, "materialize_daily_report_regenerate_gateway_request_transient", lambda **kw: {"task_type":"daily_report_regenerate", "output_schema_version":"daily-report-regenerate/1.0"})
    monkeypatch.setattr(model_execution, "_require_transient", lambda value, **kw: value)
    monkeypatch.setattr(model_execution, "_provider_request", lambda value: value)
    with pytest.raises(HTTPException) as error:
        execution.execute_daily_report_regenerate_model_call(model_call_id=preview["model_call_id"], budget_record={}, report_version_id=report["report_version_id"])
    assert error.value.detail["code"] == "REPORT_REANALYSIS_CANCELLED"
    assert provider_calls == []


def test_running_transition_competes_with_cancellation_in_separate_connections(review_state):
    from app.report_reanalysis_cancellation import transition_regeneration_task
    report, bundle, payload = chain(review_state)
    task = bundle["replacement_task"]
    gate = threading.Barrier(2)
    def run(action):
        gate.wait(timeout=5)
        try:
            action()
            return True
        except HTTPException:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(run, lambda: cancel(review_state, report, payload)),
            pool.submit(run, lambda: transition_regeneration_task(project_id=task["project_id"], local_task_id=task["local_task_id"], expected_state="queued", new_state="running"))]
        assert sorted(job.result(timeout=30) for job in jobs) == [False, True]


def test_finalize_rechecks_source_and_task_after_entering_write_transaction(review_state, monkeypatch):
    report, bundle, payload = chain(review_state)
    stale_task = {**bundle["replacement_task"], "state":"running"}
    cancel(review_state, report, payload)
    monkeypatch.setattr(report_reanalysis_execution, "_assert_result", lambda **kw: None)
    monkeypatch.setattr(report_reanalysis_execution, "get_model_call", lambda _id: {})
    monkeypatch.setattr(report_review, "get_reanalysis_request", lambda **kw: bundle)
    monkeypatch.setattr(report_review, "_verified_candidate", lambda *args, **kw: {"project_id":report["project_id"],"snapshot_hash":report["evidence_snapshot_hash"]})
    with pytest.raises(HTTPException) as error:
        report_reanalysis_execution._materialize_replacement(project_id=report["project_id"], source_report_version_id=report["report_version_id"], task=stale_task,
            result={**review_state["model_result"], "model_call_id":999,"model_result_id":999})
    assert error.value.detail["code"] == "REPORT_REANALYSIS_CANCELLED"
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0] == 1


def test_cross_project_send_claim_result_and_successor_each_block_restoration(review_state):
    from app import model_call_ledger, model_execution_results
    report, bundle, payload = chain(review_state)
    with pytest.raises(HTTPException):
        cancel({**review_state, "project_id":review_state["project_id"]+1}, report, payload)
    preview = prepared_preview(review_state, report, bundle)
    call_id = preview["model_call_id"]
    model_execution._claim_send_once(call_id)
    try:
        with pytest.raises(HTTPException) as claimed:
            cancel(review_state, report, payload)
        assert claimed.value.detail["code"] == "REPORT_REANALYSIS_CANCELLATION_SEND_CLAIMED"
    finally:
        model_execution._release_send_claim_before_transport(call_id)
    call = model_call_ledger.get_model_call(call_id)
    model_execution_results.record_model_execution_result(model_call_id=call_id, receipt=result_tests._receipt(review_state, call))
    with pytest.raises(HTTPException) as result:
        cancel(review_state, report, payload)
    assert result.value.detail["code"] == "REPORT_REANALYSIS_CANCELLATION_RESULT_EXISTS"
    with db.get_connection() as conn:
        conn.execute("""INSERT INTO report_versions (schema_version,project_id,version_no,parent_report_version_id,
            model_execution_result_id,execution_result_hash,formal_response_hash,validated_result_hash,
            model_call_id,call_identity_hash,evidence_snapshot_id,evidence_snapshot_hash,report_content_hash,lifecycle,state_version,created_at)
            SELECT schema_version,project_id,2,id,999,execution_result_hash,formal_response_hash,validated_result_hash,
                model_call_id,call_identity_hash,evidence_snapshot_id,evidence_snapshot_hash,report_content_hash,'pending_review',1,created_at
            FROM report_versions WHERE id=?""", (report["report_version_id"],))
    with pytest.raises(HTTPException) as child:
        cancel(review_state, report, payload)
    assert child.value.detail["code"] == "REPORT_REANALYSIS_CANCELLATION_SUCCESSOR"
