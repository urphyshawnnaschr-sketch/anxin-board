"""Durable Page07 reanalysis execution and replacement ReportVersion closure.

queued replacement task -> exact Gateway ready -> running -> one provider execution ->
verified daily_report_regenerate result -> immutable child ReportVersion -> succeeded.
An ambiguous provider outcome becomes ``unknown`` and is never blindly retried.
"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3

from fastapi import HTTPException

from app import model_execution_results, page07_model_execution, page07_model_preparation, report_review
from app.db import get_connection
from app.git_workspace_locks import GitOperationInProgress, project_workspace_lock
from app.model_call_ledger import get_model_call
from app.report_generation_tasks import get_report_regeneration_task
from app.report_reanalysis_cancellation import assert_active_chain, transition_regeneration_task as transition_report_generation_task

SCHEMA_VERSION = "report_reanalysis_execution_lifecycle_v1"
_TASK_TYPE = "daily_report_regenerate"
_OUTPUT_SCHEMA_VERSION = "daily-report-regenerate/1.0"
_RESULT_NOT_FOUND = "MODEL_EXECUTION_RESULT_NOT_FOUND"


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _code(exc: HTTPException) -> str | None:
    return exc.detail.get("code") if isinstance(exc.detail, Mapping) else None


def _load_bound(*, project_id: int, source_report_version_id: int, local_task_id: str, model_call_id: int) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    bundle = report_review.get_reanalysis_request(project_id=project_id, report_version_id=source_report_version_id)
    source = bundle["source_report_version"]; request = bundle["reanalysis_request"]
    task = get_report_regeneration_task(project_id=project_id, local_task_id=local_task_id)
    call = get_model_call(model_call_id)
    if (
        source.get("report_version_id") != source_report_version_id
        or source.get("lifecycle") != "superseded"
        or request.get("replacement_local_task_id") != local_task_id
        or request.get("replacement_task_id") != task.get("id")
        or request.get("replacement_task_identity_hash") != task.get("identity_hash")
        or task.get("task_type") != _TASK_TYPE
        or task.get("evidence_snapshot_id") != source.get("evidence_snapshot_id")
        or call.get("project_id") != project_id
        or call.get("local_task_id") != local_task_id
        or call.get("snapshot_id") != task.get("evidence_snapshot_id")
        or call.get("task_type") != _TASK_TYPE
        or call.get("output_schema_version") != _OUTPUT_SCHEMA_VERSION
        or call.get("preparation_state") != "prepared"
    ):
        raise _error("REPORT_REANALYSIS_EXECUTION_BINDING_INVALID", "ReanalysisRequest / replacement task / ModelCall 身份无法闭合。")
    return dict(source), dict(request), dict(task), dict(call)


def _read_result(model_call_id: int) -> dict[str, object] | None:
    try:
        return model_execution_results.get_model_execution_result_for_call(model_call_id)
    except HTTPException as exc:
        if _code(exc) == _RESULT_NOT_FOUND:
            return None
        raise


def _assert_result(*, project_id: int, task: Mapping[str, object], call: Mapping[str, object], result: Mapping[str, object]) -> None:
    formal = result.get("formal_response"); validated = result.get("validated_result")
    if (
        result.get("project_id") != project_id
        or result.get("model_call_id") != call.get("model_call_id")
        or result.get("call_identity_hash") != call.get("call_identity_hash")
        or result.get("local_task_id") != task.get("local_task_id")
        or result.get("snapshot_id") != task.get("evidence_snapshot_id")
        or result.get("task_type") != _TASK_TYPE
        or not isinstance(formal, Mapping)
        or formal.get("status") != "succeeded"
        or formal.get("task_type") != _TASK_TYPE
        or formal.get("output_schema_version") != _OUTPUT_SCHEMA_VERSION
        or not isinstance(validated, Mapping)
        or not isinstance(validated.get("new_report"), Mapping)
    ):
        raise _error("REPORT_REANALYSIS_EXECUTION_RESULT_INVALID", "verified regenerate result 未精确绑定 replacement task/call 或缺少 new_report。")
    if type(result.get("model_result_id")) is not int or result["model_result_id"] <= 0:
        raise _error("REPORT_REANALYSIS_EXECUTION_RESULT_INVALID", "regenerate result 缺少合法 model_result_id。")


def _enter_running(task: Mapping[str, object]) -> dict[str, object]:
    if task.get("state") == "running":
        return dict(task)
    if task.get("state") != "queued":
        raise _error("REPORT_REANALYSIS_EXECUTION_STATE_INVALID", "只有 queued/running replacement task 可以进入执行收口。")
    return dict(transition_report_generation_task(project_id=task["project_id"], local_task_id=task["local_task_id"], expected_state="queued", new_state="running"))


def _materialize_replacement(*, project_id: int, source_report_version_id: int, task: Mapping[str, object], result: Mapping[str, object]) -> dict[str, object]:
    """Create/replay exactly one child ReportVersion from a verified regenerate result."""
    _assert_result(project_id=project_id, task=task, call=get_model_call(int(result["model_call_id"])), result=result)
    bundle = report_review.get_reanalysis_request(project_id=project_id, report_version_id=source_report_version_id)
    source = bundle["source_report_version"]; request = bundle["reanalysis_request"]
    if source.get("lifecycle") != "superseded" or request.get("replacement_local_task_id") != task.get("local_task_id"):
        raise _error("REPORT_REANALYSIS_MATERIALIZATION_BINDING_INVALID", "source report/reanalysis request 已漂移。")
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source_row, request, task = assert_active_chain(conn, project_id=project_id,
                report_version_id=source_report_version_id, local_task_id=task["local_task_id"],
                model_call_id=int(result["model_call_id"]), allowed_states=("running", "succeeded"))
            source = dict(source_row)
            try:
                with project_workspace_lock(project_id):
                    candidate = report_review._verified_candidate(
                        int(result["snapshot_id"]),
                        message="Replacement ReportVersion source Evidence/Git/PRD 无法完成 authoritative verified read。",
                    )
                    if candidate.get("project_id") != project_id or candidate.get("snapshot_hash") != source.get("evidence_snapshot_hash"):
                        raise _error("REPORT_REANALYSIS_MATERIALIZATION_BINDING_INVALID", "replacement Context Candidate Set 与 source snapshot 漂移。")
                    existing = report_review._read_report_by_result(conn, int(result["model_result_id"]))
                    if existing is not None:
                        closed = report_review._close_report_row(existing, model_result=result, candidate=candidate)
                        if closed.get("parent_report_version_id") != source_report_version_id or closed.get("version_no") != source["version_no"] + 1:
                            raise _error("REPORT_REANALYSIS_MATERIALIZATION_BINDING_INVALID", "existing replacement ReportVersion parent/version 链无效。")
                        conn.commit(); return {"report_version": closed, "created": False}
                    if task.get("state") != "running":
                        raise _error("REPORT_REANALYSIS_EXECUTION_STATE_INVALID", "replacement ReportVersion 只允许从 running task 物化。")
                    latest = conn.execute("SELECT MAX(version_no) AS max_version FROM report_versions WHERE project_id = ?", (project_id,)).fetchone()
                    expected_version = int(source["version_no"]) + 1
                    if latest is None or latest["max_version"] != source["version_no"]:
                        raise _error("REPORT_REANALYSIS_MATERIALIZATION_CONFLICT", "当前项目已存在其他新版报告，禁止旁路插入 replacement。")
                    created_at = report_review._now()
                    cursor = conn.execute(
                        """
                        INSERT INTO report_versions (
                            schema_version, project_id, version_no, parent_report_version_id,
                            model_execution_result_id, execution_result_hash, formal_response_hash,
                            validated_result_hash, model_call_id, call_identity_hash,
                            evidence_snapshot_id, evidence_snapshot_hash, report_content_hash,
                            lifecycle, state_version, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', 1, ?)
                        """,
                        (
                            report_review.REPORT_VERSION_SCHEMA_VERSION, project_id, expected_version,
                            source_report_version_id, result["model_result_id"], result["execution_result_hash"],
                            result["formal_response_hash"], result["validated_result_hash"], result["model_call_id"],
                            result["call_identity_hash"], result["snapshot_id"], candidate["snapshot_hash"],
                            result["validated_result_hash"], created_at,
                        ),
                    )
                    if type(cursor.lastrowid) is not int or cursor.lastrowid <= 0:
                        raise _error("REPORT_REANALYSIS_MATERIALIZATION_FAILED", "replacement ReportVersion 写入后缺少合法 id。")
                    row = report_review._read_report_by_id(conn, cursor.lastrowid)
                    if row is None:
                        raise _error("REPORT_REANALYSIS_MATERIALIZATION_FAILED", "replacement ReportVersion 写入后无法回读。")
                    closed = report_review._close_report_row(row, model_result=result, candidate=candidate)
                    conn.commit(); return {"report_version": closed, "created": True}
            except GitOperationInProgress as exc:
                raise _error("REPORT_REANALYSIS_MATERIALIZATION_BUSY", "source Git workspace 正忙，未物化 replacement ReportVersion。") from exc
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise _error("REPORT_REANALYSIS_MATERIALIZATION_CONFLICT", "replacement ReportVersion 并发写入冲突。") from exc
    except sqlite3.Error as exc:
        raise _error("REPORT_REANALYSIS_MATERIALIZATION_FAILED", "replacement ReportVersion 保存失败，数据库原状态未改变。") from exc


def _finish(*, source_report_version_id: int, task: Mapping[str, object], call: Mapping[str, object], result: Mapping[str, object], execution_source: str) -> dict[str, object]:
    _assert_result(project_id=int(task["project_id"]), task=task, call=call, result=result)
    materialized = _materialize_replacement(project_id=int(task["project_id"]), source_report_version_id=source_report_version_id, task=task, result=result)
    report = materialized["report_version"]
    transitioned = transition_report_generation_task(project_id=task["project_id"], local_task_id=task["local_task_id"], expected_state="running", new_state="succeeded")
    if transitioned.get("state") != "succeeded":
        raise _error("REPORT_REANALYSIS_STATE_RECONCILIATION_FAILED", "replacement ReportVersion 已存在，但 task 未能收口为 succeeded。")
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": task["project_id"],
        "source_report_version_id": source_report_version_id,
        "replacement_report_version_id": report["report_version_id"],
        "local_task_id": task["local_task_id"],
        "model_call_id": call["model_call_id"],
        "model_result_id": result["model_result_id"],
        "task_state": "succeeded",
        "execution_source": execution_source,
        "provider_retry_state": "not_retried",
    }


def execute_prepared_report_reanalysis(*, project_id: int, source_report_version_id: int, local_task_id: str, model_call_id: int) -> dict[str, object]:
    source, _request, task, call = _load_bound(project_id=project_id, source_report_version_id=source_report_version_id, local_task_id=local_task_id, model_call_id=model_call_id)
    if task.get("state") != "queued":
        raise _error("REPORT_REANALYSIS_EXECUTION_STATE_INVALID", "只有 queued replacement task 可以消费新的 provider send 权利。")
    existing = _read_result(model_call_id)
    if existing is not None:
        _assert_result(project_id=project_id, task=task, call=call, result=existing)
        return _finish(source_report_version_id=source_report_version_id, task=_enter_running(task), call=call, result=existing, execution_source="existing_verified_result")
    budget = page07_model_preparation.get_daily_report_regenerate_budget_record(provider=str(call["provider"]))
    for field in ("provider", "model_id", "model_version"):
        if budget.get(field) != call.get(field):
            raise _error("REPORT_REANALYSIS_EXECUTION_BINDING_INVALID", "regenerate execution budget 与 ModelCall provider identity 漂移。")
    preflight = page07_model_execution.build_ready_daily_report_regenerate_preflight(model_call_id=model_call_id, budget_record=budget, report_version_id=source_report_version_id)
    for field in ("call_identity_hash", "provider", "model_id", "model_version", "task_type", "output_schema_version"):
        if preflight.get(field) != call.get(field):
            raise _error("REPORT_REANALYSIS_EXECUTION_BINDING_INVALID", "regenerate Gateway preflight 与 durable ModelCall 漂移。")
    running = _enter_running(task)
    try:
        result = page07_model_execution.execute_daily_report_regenerate_model_call(model_call_id=model_call_id, budget_record=budget, report_version_id=source_report_version_id)
        _assert_result(project_id=project_id, task=running, call=call, result=result)
    except Exception:
        durable = None
        try:
            durable = _read_result(model_call_id)
            if durable is not None:
                _assert_result(project_id=project_id, task=running, call=call, result=durable)
        except Exception:
            durable = None
        if durable is not None:
            return _finish(source_report_version_id=source_report_version_id, task=running, call=call, result=durable, execution_source="recovered_verified_result")
        try:
            transition_report_generation_task(project_id=project_id, local_task_id=local_task_id, expected_state="running", new_state="unknown")
        except Exception as exc:
            raise _error("REPORT_REANALYSIS_STATE_RECONCILIATION_FAILED", "外部执行结果未知且 task 未能收口，禁止自动重试。") from exc
        raise
    return _finish(source_report_version_id=source_report_version_id, task=running, call=call, result=result, execution_source="new_provider_execution")


def finalize_report_reanalysis_from_existing_result(*, project_id: int, source_report_version_id: int, local_task_id: str, model_call_id: int) -> dict[str, object]:
    _source, _request, task, call = _load_bound(project_id=project_id, source_report_version_id=source_report_version_id, local_task_id=local_task_id, model_call_id=model_call_id)
    if task.get("state") not in {"queued", "running"}:
        raise _error("REPORT_REANALYSIS_EXECUTION_STATE_INVALID", "只有 queued/running task 可以从已有 verified result 收口。")
    result = _read_result(model_call_id)
    if result is None:
        raise _error("REPORT_REANALYSIS_EXECUTION_RESULT_UNAVAILABLE", "没有 durable verified regenerate result；不会自动重发模型。")
    _assert_result(project_id=project_id, task=task, call=call, result=result)
    return _finish(source_report_version_id=source_report_version_id, task=_enter_running(task), call=call, result=result, execution_source="existing_verified_result")
