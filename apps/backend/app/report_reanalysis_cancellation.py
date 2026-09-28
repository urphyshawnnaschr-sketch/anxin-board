"""Cancel only an unstarted replacement chain, preserving every original fact.

Lock order is Page07 permit lock -> SQLite write reservation -> send-claim lock.
No provider, credentials, or transport is used here. The existing voided state is
accepted for an unstarted task only with this immutable cancellation proof.
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from app.db import get_connection

SCHEMA_VERSION = "report_reanalysis_cancellation_v1"


def _error(code, message):
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS report_reanalysis_cancellations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        report_version_id INTEGER NOT NULL UNIQUE,
        reanalysis_request_id INTEGER NOT NULL UNIQUE,
        replacement_task_id INTEGER NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        cancellation_hash TEXT NOT NULL UNIQUE,
        UNIQUE(project_id, idempotency_key)
    )""")
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS tr_reanalysis_cancellation_no_{operation.lower()}
            BEFORE {operation} ON report_reanalysis_cancellations
            BEGIN SELECT RAISE(ABORT, 'reanalysis cancellation is append-only'); END""")


def read_cancellation(conn, report_version_id):
    from app import report_review
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_reanalysis_cancellations'").fetchone() is None:
        return None
    row = conn.execute("SELECT * FROM report_reanalysis_cancellations WHERE report_version_id=?", (report_version_id,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["snapshot_json"])
        if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
                or value.get("state") != "cancelled_before_send"
                or report_review._stable_hash(value) != row["cancellation_hash"]
                or any(value.get(key) != row[key] for key in ("project_id", "report_version_id", "reanalysis_request_id", "replacement_task_id", "idempotency_key"))):
            raise ValueError("invalid cancellation identity")
    except (ValueError, TypeError, KeyError) as exc:
        raise _error("REPORT_REANALYSIS_CANCELLATION_STORED_INVALID", "撤销审计身份无法闭合。") from exc
    return {**value, "cancellation_id": row["id"], "cancellation_hash": row["cancellation_hash"]}


def cancellation_for_task(conn, task):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_reanalysis_cancellations'").fetchone() is None:
        return None
    row = conn.execute("SELECT report_version_id FROM report_reanalysis_cancellations WHERE replacement_task_id=?", (task["id"],)).fetchone()
    if row is None:
        return None
    value = read_cancellation(conn, row["report_version_id"])
    if (value["project_id"] != task["project_id"]
            or value["replacement_task_identity_hash"] != task["identity_hash"]
            or value["replacement_local_task_id"] != task["local_task_id"]
            or value["attempt_id"] != task["current_attempt_id"]):
        raise _error("REPORT_REANALYSIS_CANCELLATION_STORED_INVALID", "撤销审计与替代任务不一致。")
    return value


def assert_active_chain(conn, *, project_id, report_version_id, local_task_id=None, model_call_id=None, allowed_states=("queued", "running")):
    """Recheck current chain while caller holds BEGIN IMMEDIATE, never a stale preview."""
    from app import report_review, report_generation_tasks
    row = report_review._read_reanalysis_by_report(conn, report_version_id)
    source = report_review._read_report_by_id(conn, report_version_id)
    if row is None or source is None or row["project_id"] != project_id or source["project_id"] != project_id:
        raise _error("REPORT_REANALYSIS_BINDING_INVALID", "重分析项目或来源报告不一致。")
    request = report_review._close_reanalysis_request(row)
    if read_cancellation(conn, report_version_id) is not None:
        raise _error("REPORT_REANALYSIS_CANCELLED", "这次未发送的重分析已撤销；请返回原报告继续审阅。")
    if source["lifecycle"] != "superseded" or source["state_version"] != request["source_report_state_version"] + 1:
        raise _error("REPORT_REANALYSIS_BINDING_INVALID", "原报告已离开该重分析状态链。")
    task_row = report_generation_tasks._read_task_by_id(conn, request["replacement_task_id"])
    if task_row is None:
        raise _error("REPORT_REANALYSIS_BINDING_INVALID", "替代任务不存在。")
    task = report_generation_tasks._close_task_local(conn, task_row)
    if (task["project_id"] != project_id or task["task_type"] != "daily_report_regenerate"
            or task["local_task_id"] != request["replacement_local_task_id"]
            or task["identity_hash"] != request["replacement_task_identity_hash"]
            or task["evidence_snapshot_id"] != request["evidence_snapshot_id"]
            or source["evidence_snapshot_id"] != request["evidence_snapshot_id"]
            or source["evidence_snapshot_hash"] != request["evidence_snapshot_hash"]
            or task["state"] not in allowed_states
            or (local_task_id is not None and task["local_task_id"] != local_task_id)):
        raise _error("REPORT_REANALYSIS_NOT_UNSTARTED", "重分析任务已开始、已结束或身份已变化，不能消费旧请求。")
    if model_call_id is not None:
        call = conn.execute("SELECT * FROM model_calls WHERE id=?", (model_call_id,)).fetchone()
        if (call is None or call["project_id"] != project_id or call["local_task_id"] != task["local_task_id"]
                or call["task_type"] != "daily_report_regenerate" or call["snapshot_id"] != task["evidence_snapshot_id"]):
            raise _error("REPORT_REANALYSIS_BINDING_INVALID", "模型请求与替代任务不一致。")
    return source, request, task


def claim_reanalysis_send_once(*, model_call_id, report_version_id):
    """The final dispatch fence: a committed running task precedes all transport.

    This does not take the permit lock (no inverse lock order). Cancellation's
    SQLite reservation serializes against this check, including direct callers.
    """
    from app import model_execution
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        call = conn.execute("SELECT * FROM model_calls WHERE id=?", (model_call_id,)).fetchone()
        if call is None:
            raise _error("REPORT_REANALYSIS_BINDING_INVALID", "模型请求不存在。")
        assert_active_chain(conn, project_id=call["project_id"], report_version_id=report_version_id,
            local_task_id=call["local_task_id"], model_call_id=model_call_id, allowed_states=("running",))
        model_execution._claim_send_once(model_call_id)


def transition_regeneration_task(*, project_id, local_task_id, expected_state, new_state):
    """Narrow regenerate CAS; original generic task transitions stay unchanged."""
    from app import report_generation_tasks as tasks, report_review
    if (expected_state, new_state) not in tasks.LEGAL_TRANSITIONS:
        raise tasks._transition_invalid()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request = conn.execute("SELECT report_version_id FROM report_reanalysis_requests WHERE project_id=? AND replacement_local_task_id=?", (project_id,local_task_id)).fetchone()
        if request is None:
            raise _error("REPORT_REANALYSIS_BINDING_INVALID", "重分析请求不存在。")
        _source, _request, task = assert_active_chain(conn, project_id=project_id,
            report_version_id=request["report_version_id"], local_task_id=local_task_id,
            allowed_states=(expected_state,))
        now = report_review._now()
        attempt = task["current_attempt"]
        started_at = now if expected_state == "queued" else attempt["started_at"]
        finished_at = now if new_state in tasks.TERMINAL_STATES else attempt["finished_at"]
        changed = conn.execute("UPDATE report_generation_task_attempts SET state=?,started_at=?,finished_at=?,updated_at=? WHERE attempt_id=? AND task_id=? AND state=?", (new_state,started_at,finished_at,now,task["current_attempt_id"],task["id"],expected_state))
        if changed.rowcount != 1:
            raise tasks._transition_invalid()
        changed = conn.execute("UPDATE report_generation_tasks SET state=?,updated_at=? WHERE id=? AND state=? AND current_attempt_id=?", (new_state,now,task["id"],expected_state,task["current_attempt_id"]))
        if changed.rowcount != 1:
            raise tasks._transition_invalid()
        return tasks._close_task_local(conn, tasks._read_task_by_id(conn, task["id"]))


def cancel_unstarted_reanalysis(*, project_id, report_version_id, expected_report_state_version,
        expected_reanalysis_request_hash, expected_replacement_task_identity_hash,
        cancelled_by, cancellation_reason, idempotency_key):
    from app import report_review, page07_send_authorization as permits, model_execution
    project_id = report_review._require_positive_id(project_id, "project_id")
    report_version_id = report_review._require_positive_id(report_version_id, "report_version_id")
    expected_report_state_version = report_review._require_positive_id(expected_report_state_version, "expected_report_state_version")
    for value in (expected_reanalysis_request_hash, expected_replacement_task_identity_hash):
        if not report_review._is_hash(value):
            raise report_review._input_invalid("撤销必须绑定有效的请求与任务哈希。")
    cancelled_by = report_review._require_identity_text(cancelled_by, "cancelled_by", max_length=128)
    idempotency_key = report_review._require_identity_text(idempotency_key, "idempotency_key", max_length=128)
    for key, value, limit in (("cancelled_by", cancelled_by, 128), ("cancellation_reason", cancellation_reason, 2000), ("idempotency_key", idempotency_key, 128)):
        if type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value:
            raise report_review._input_invalid(f"{key} 无效。")
    expected = dict(project_id=project_id, report_version_id=report_version_id,
        source_report_state_version_before=expected_report_state_version,
        request_hash=expected_reanalysis_request_hash,
        replacement_task_identity_hash=expected_replacement_task_identity_hash,
        cancelled_by=cancelled_by, cancellation_reason=cancellation_reason, idempotency_key=idempotency_key)
    created = False
    try:
        with permits._lock:
            report, model_result, candidate = report_review._load_closed_report(report_version_id)
            if report["project_id"] != project_id:
                raise report_review._report_not_found()
            with get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = read_cancellation(conn, report_version_id)
                if previous is not None:
                    if any(previous.get(key) != value for key, value in expected.items()):
                        raise _error("REPORT_REANALYSIS_CANCELLATION_CONFLICT", "已保存的撤销记录与本次请求不一致。")
                else:
                    source, request, task = assert_active_chain(conn, project_id=project_id, report_version_id=report_version_id, allowed_states=("queued",))
                    report_review._close_report_row(source, model_result=model_result, candidate=candidate)
                    if (source["state_version"] != expected_report_state_version
                            or request["request_hash"] != expected_reanalysis_request_hash
                            or task["identity_hash"] != expected_replacement_task_identity_hash):
                        raise _error("REPORT_REANALYSIS_CANCELLATION_STALE", "报告或重分析身份已变化，请刷新后重试。")
                    if report_review._read_current_report_id(conn, project_id) != report_version_id:
                        raise _error("REPORT_REANALYSIS_CANCELLATION_SUCCESSOR", "项目已有后继报告，不能恢复旧报告。")
                    if conn.execute("SELECT 1 FROM report_versions WHERE parent_report_version_id=?", (report_version_id,)).fetchone():
                        raise _error("REPORT_REANALYSIS_CANCELLATION_SUCCESSOR", "重分析已产生后继报告。")
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='report_approval_snapshots'").fetchone() and conn.execute("SELECT 1 FROM report_approval_snapshots WHERE report_version_id=?", (report_version_id,)).fetchone():
                        raise _error("REPORT_REANALYSIS_CANCELLATION_SUCCESSOR", "来源报告已存在正式确认记录。")
                    calls = conn.execute("SELECT id FROM model_calls WHERE project_id=? AND local_task_id=?", (project_id, task["local_task_id"])).fetchall()
                    if conn.execute("SELECT 1 FROM model_execution_results WHERE (project_id=? AND local_task_id=?) OR model_call_id IN (SELECT id FROM model_calls WHERE project_id=? AND local_task_id=?)", (project_id, task["local_task_id"], project_id, task["local_task_id"])).fetchone():
                        raise _error("REPORT_REANALYSIS_CANCELLATION_RESULT_EXISTS", "替代任务已有模型执行结果，不能撤销为未发送。")
                    with model_execution._SEND_CLAIM_LOCK:
                        if any(row["id"] in model_execution._SEND_CLAIMED_MODEL_CALL_IDS for row in calls):
                            raise _error("REPORT_REANALYSIS_CANCELLATION_SEND_CLAIMED", "替代任务已有发送声明，不能按未发送恢复。")
                    revoke_scope = permits._cancellation_permit_locked(project_id=project_id, report_version_id=report_version_id, local_task_id=task["local_task_id"])
                    now = report_review._now()
                    audit = {**expected, "schema_version": SCHEMA_VERSION, "state": "cancelled_before_send",
                        "reanalysis_request_id": request["reanalysis_request_id"], "replacement_task_id": task["id"],
                        "replacement_local_task_id": task["local_task_id"], "attempt_id": task["current_attempt_id"],
                        "source_report_state_version_after": expected_report_state_version + 1,
                        "restored_lifecycle": request["source_report_lifecycle"], "cancelled_at": now}
                    conn.execute("INSERT INTO report_reanalysis_cancellations (project_id,report_version_id,reanalysis_request_id,replacement_task_id,idempotency_key,snapshot_json,cancellation_hash) VALUES (?,?,?,?,?,?,?)",
                        (project_id, report_version_id, request["reanalysis_request_id"], task["id"], idempotency_key,
                         json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")), report_review._stable_hash(audit)))
                    updated = conn.execute("UPDATE report_generation_task_attempts SET state='voided',finished_at=?,updated_at=? WHERE attempt_id=? AND task_id=? AND state='queued' AND started_at IS NULL AND finished_at IS NULL", (now,now,task["current_attempt_id"],task["id"]))
                    if updated.rowcount != 1:
                        raise _error("REPORT_REANALYSIS_CANCELLATION_STALE", "重分析 attempt 已变化。")
                    updated = conn.execute("UPDATE report_generation_tasks SET state='voided',updated_at=? WHERE id=? AND current_attempt_id=? AND state='queued'", (now,task["id"],task["current_attempt_id"]))
                    if updated.rowcount != 1:
                        raise _error("REPORT_REANALYSIS_CANCELLATION_STALE", "重分析任务已变化。")
                    updated = conn.execute("UPDATE report_versions SET lifecycle=?,state_version=state_version+1 WHERE id=? AND project_id=? AND lifecycle='superseded' AND state_version=?", (request["source_report_lifecycle"],report_version_id,project_id,expected_report_state_version))
                    if updated.rowcount != 1:
                        raise _error("REPORT_REANALYSIS_CANCELLATION_STALE", "来源报告已变化。")
                    conn.commit()
                    # Commit first: a failed transaction must not consume a valid permit.
                    if revoke_scope is not None and permits._active_scope_hash == revoke_scope:
                        permits._clear_locked()
                    created = True
            return {**report_review.get_reanalysis_request(project_id=project_id, report_version_id=report_version_id), "created": created}
    except sqlite3.Error as exc:
        raise _error("REPORT_REANALYSIS_CANCELLATION_FAILED", "撤销未能提交，报告、任务和发送授权保持原状态。") from exc
