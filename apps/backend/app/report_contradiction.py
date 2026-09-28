"""Durable exact report/supplement identity for Page07 contradiction checking.

This module is local-only. It never resolves provider authority, reads credentials, or
sends network traffic. Every request is derived from the current server-owned review
bundle and is immutable/idempotent for one exact ReportVersion + SupplementVersion.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException

from app.db import ensure_report_contradiction_schema, get_connection
from app.report_review import get_review_bundle


SCHEMA_VERSION = "report_contradiction_request_v1"
TASK_TYPE = "report_contradiction_check"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_MAX = 2**63 - 1
_COLUMNS = (
    "id, schema_version, project_id, report_version_id, report_content_hash, "
    "source_model_execution_result_id, source_execution_result_hash, evidence_snapshot_id, "
    "evidence_snapshot_hash, supplement_version_id, supplement_content_hash, "
    "supplement_source_type, supplement_provided_by, supplement_provided_at, "
    "supplement_provided_timezone, local_task_id, task_identity_hash, task_type, "
    "created_at, request_hash"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "Contradiction request 输入无效。") -> HTTPException:
    return _error(400, "REPORT_CONTRADICTION_INPUT_INVALID", message)


def _not_ready(message: str = "当前报告没有可校验的 PM supplement。") -> HTTPException:
    return _error(409, "REPORT_CONTRADICTION_NOT_READY", message)


def _stored_invalid(message: str = "Contradiction request 持久化身份无法闭合。") -> HTTPException:
    return _error(409, "REPORT_CONTRADICTION_STORED_INVALID", message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _stored_invalid("Contradiction identity 无法 canonicalize。") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_MAX


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and "\x00" not in value


def _task_identity_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": value["schema_version"],
        "local_task_id": value["local_task_id"],
        "task_type": value["task_type"],
        "project_id": value["project_id"],
        "report_version_id": value["report_version_id"],
        "report_content_hash": value["report_content_hash"],
        "evidence_snapshot_id": value["evidence_snapshot_id"],
        "evidence_snapshot_hash": value["evidence_snapshot_hash"],
        "supplement_version_id": value["supplement_version_id"],
        "supplement_content_hash": value["supplement_content_hash"],
    }


def _request_hash_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": value["schema_version"],
        "project_id": value["project_id"],
        "report_version_id": value["report_version_id"],
        "report_content_hash": value["report_content_hash"],
        "source_model_execution_result_id": value["source_model_execution_result_id"],
        "source_execution_result_hash": value["source_execution_result_hash"],
        "evidence_snapshot_id": value["evidence_snapshot_id"],
        "evidence_snapshot_hash": value["evidence_snapshot_hash"],
        "supplement_version_id": value["supplement_version_id"],
        "supplement_content_hash": value["supplement_content_hash"],
        "supplement_source_type": value["supplement_source_type"],
        "supplement_provided_by": value["supplement_provided_by"],
        "supplement_provided_at": value["supplement_provided_at"],
        "supplement_provided_timezone": value["supplement_provided_timezone"],
        "local_task_id": value["local_task_id"],
        "task_identity_hash": value["task_identity_hash"],
        "task_type": value["task_type"],
    }


def _close_row(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != SCHEMA_VERSION or value.get("task_type") != TASK_TYPE:
        raise _stored_invalid()
    for field in (
        "id", "project_id", "report_version_id", "source_model_execution_result_id",
        "evidence_snapshot_id", "supplement_version_id",
    ):
        if not _positive(value.get(field)):
            raise _stored_invalid()
    for field in (
        "report_content_hash", "source_execution_result_hash", "evidence_snapshot_hash",
        "supplement_content_hash", "task_identity_hash", "request_hash",
    ):
        if not _is_hash(value.get(field)):
            raise _stored_invalid()
    for field in (
        "supplement_source_type", "supplement_provided_by", "supplement_provided_at",
        "supplement_provided_timezone", "local_task_id", "created_at",
    ):
        if not _text(value.get(field)):
            raise _stored_invalid()
    if value["task_identity_hash"] != _hash(_task_identity_payload(value)):
        raise _stored_invalid("Contradiction task_identity_hash 无法重算。")
    if value["request_hash"] != _hash(_request_hash_payload(value)):
        raise _stored_invalid("Contradiction request_hash 无法重算。")
    result = dict(value)
    result["contradiction_request_id"] = result.pop("id")
    result["contradiction_task_id"] = result["contradiction_request_id"]
    return result


def _facts(project_id: int, report_version_id: int) -> tuple[dict[str, object], dict[str, object]]:
    if not _positive(project_id) or not _positive(report_version_id):
        raise _input_invalid("project_id/report_version_id 必须是正整数。")
    bundle = get_review_bundle(project_id=project_id, report_version_id=report_version_id)
    report = bundle.get("report_version")
    supplement = bundle.get("current_supplement")
    if not isinstance(report, Mapping) or not isinstance(supplement, Mapping):
        raise _not_ready()
    if report.get("project_id") != project_id or report.get("report_version_id") != report_version_id:
        raise _stored_invalid("Review bundle report selector 漂移。")
    if supplement.get("report_version_id") != report_version_id:
        raise _stored_invalid("Current supplement 不属于 exact report。")
    return dict(report), dict(supplement)


def create_or_replay_contradiction_request(*, project_id: int, report_version_id: int) -> dict[str, object]:
    """Create/replay one immutable exact-current contradiction request; no provider action."""
    report, supplement = _facts(project_id, report_version_id)
    identity_seed = {
        "project_id": project_id,
        "report_version_id": report_version_id,
        "report_content_hash": report.get("report_content_hash"),
        "evidence_snapshot_id": report.get("evidence_snapshot_id"),
        "evidence_snapshot_hash": report.get("evidence_snapshot_hash"),
        "supplement_version_id": supplement.get("supplement_version_id"),
        "supplement_content_hash": supplement.get("content_hash"),
    }
    if (
        not _is_hash(identity_seed["report_content_hash"])
        or not _positive(identity_seed["evidence_snapshot_id"])
        or not _is_hash(identity_seed["evidence_snapshot_hash"])
        or not _positive(identity_seed["supplement_version_id"])
        or not _is_hash(identity_seed["supplement_content_hash"])
    ):
        raise _stored_invalid("Review bundle contradiction identity 不完整。")
    seed_hash = _hash(identity_seed)
    created_at = _now()
    row: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "report_version_id": report_version_id,
        "report_content_hash": report["report_content_hash"],
        "source_model_execution_result_id": report["model_execution_result_id"],
        "source_execution_result_hash": report["execution_result_hash"],
        "evidence_snapshot_id": report["evidence_snapshot_id"],
        "evidence_snapshot_hash": report["evidence_snapshot_hash"],
        "supplement_version_id": supplement["supplement_version_id"],
        "supplement_content_hash": supplement["content_hash"],
        "supplement_source_type": supplement["source_type"],
        "supplement_provided_by": supplement["provided_by"],
        "supplement_provided_at": supplement["provided_at"],
        "supplement_provided_timezone": supplement["provided_timezone"],
        "local_task_id": f"contradiction-{seed_hash[:32]}",
        "task_type": TASK_TYPE,
        "created_at": created_at,
    }
    row["task_identity_hash"] = _hash(_task_identity_payload(row))
    row["request_hash"] = _hash(_request_hash_payload(row))
    ensure_report_contradiction_schema()
    try:
        with get_connection() as conn:
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM report_contradiction_requests "
                "WHERE report_version_id = ? AND supplement_version_id = ?",
                (report_version_id, supplement["supplement_version_id"]),
            ).fetchone()
            if existing is not None:
                closed = _close_row(existing)
                expected = {k: row[k] for k in row if k != "created_at"}
                actual = {k: closed[k] for k in expected}
                if actual != expected:
                    raise _stored_invalid("Existing contradiction request 与 current exact identity 漂移。")
                return closed
            cursor = conn.execute(
                """
                INSERT INTO report_contradiction_requests (
                    schema_version, project_id, report_version_id, report_content_hash,
                    source_model_execution_result_id, source_execution_result_hash,
                    evidence_snapshot_id, evidence_snapshot_hash, supplement_version_id,
                    supplement_content_hash, supplement_source_type, supplement_provided_by,
                    supplement_provided_at, supplement_provided_timezone, local_task_id,
                    task_identity_hash, task_type, created_at, request_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["schema_version"], row["project_id"], row["report_version_id"],
                    row["report_content_hash"], row["source_model_execution_result_id"],
                    row["source_execution_result_hash"], row["evidence_snapshot_id"],
                    row["evidence_snapshot_hash"], row["supplement_version_id"],
                    row["supplement_content_hash"], row["supplement_source_type"],
                    row["supplement_provided_by"], row["supplement_provided_at"],
                    row["supplement_provided_timezone"], row["local_task_id"],
                    row["task_identity_hash"], row["task_type"], row["created_at"],
                    row["request_hash"],
                ),
            )
            inserted = conn.execute(
                f"SELECT {_COLUMNS} FROM report_contradiction_requests WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if inserted is None:
                raise _stored_invalid("Contradiction request INSERT 后无法回读。")
            return _close_row(inserted)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def get_current_contradiction_request(*, project_id: int, report_version_id: int) -> dict[str, object]:
    """Read the request for the exact current supplement and re-close all source identities."""
    report, supplement = _facts(project_id, report_version_id)
    ensure_report_contradiction_schema()
    try:
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM report_contradiction_requests "
                "WHERE report_version_id = ? AND supplement_version_id = ?",
                (report_version_id, supplement["supplement_version_id"]),
            ).fetchone()
            if row is None:
                raise _error(404, "REPORT_CONTRADICTION_NOT_FOUND", "当前 supplement 尚无 contradiction request。")
            closed = _close_row(row)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
    expected = {
        "project_id": project_id,
        "report_version_id": report_version_id,
        "report_content_hash": report.get("report_content_hash"),
        "source_model_execution_result_id": report.get("model_execution_result_id"),
        "source_execution_result_hash": report.get("execution_result_hash"),
        "evidence_snapshot_id": report.get("evidence_snapshot_id"),
        "evidence_snapshot_hash": report.get("evidence_snapshot_hash"),
        "supplement_version_id": supplement.get("supplement_version_id"),
        "supplement_content_hash": supplement.get("content_hash"),
        "supplement_source_type": supplement.get("source_type"),
        "supplement_provided_by": supplement.get("provided_by"),
        "supplement_provided_at": supplement.get("provided_at"),
        "supplement_provided_timezone": supplement.get("provided_timezone"),
    }
    if any(closed.get(field) != value for field, value in expected.items()):
        raise _stored_invalid("Stored contradiction request 已 stale 或 source drift。")
    return closed
SEND_CLAIM_SCHEMA_VERSION = "report_contradiction_send_claim_v1"
_SEND_CLAIM_COLUMNS = (
    "id, schema_version, project_id, report_version_id, contradiction_request_id, "
    "local_task_id, task_identity_hash, model_call_id, call_identity_hash, claimed_at, claim_hash"
)


def _send_claim_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": value["schema_version"],
        "project_id": value["project_id"],
        "report_version_id": value["report_version_id"],
        "contradiction_request_id": value["contradiction_request_id"],
        "local_task_id": value["local_task_id"],
        "task_identity_hash": value["task_identity_hash"],
        "model_call_id": value["model_call_id"],
        "call_identity_hash": value["call_identity_hash"],
        "claimed_at": value["claimed_at"],
    }


def _close_send_claim(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != SEND_CLAIM_SCHEMA_VERSION:
        raise _stored_invalid("Contradiction durable send claim schema 无效。")
    for field in ("id", "project_id", "report_version_id", "contradiction_request_id", "model_call_id"):
        if not _positive(value.get(field)):
            raise _stored_invalid("Contradiction durable send claim id 无效。")
    for field in ("task_identity_hash", "call_identity_hash", "claim_hash"):
        if not _is_hash(value.get(field)):
            raise _stored_invalid("Contradiction durable send claim hash 无效。")
    for field in ("local_task_id", "claimed_at"):
        if not _text(value.get(field)):
            raise _stored_invalid("Contradiction durable send claim text identity 无效。")
    if value["claim_hash"] != _hash(_send_claim_payload(value)):
        raise _stored_invalid("Contradiction durable send claim hash 无法重算。")
    result = dict(value)
    result["send_claim_id"] = result.pop("id")
    return result


def claim_contradiction_send_once(*, request: Mapping[str, object], call: Mapping[str, object]) -> dict[str, object]:
    """Durably consume one exact contradiction provider-send right before transport.

    A pre-existing exact claim with no durable ModelExecutionResult is treated as
    an ambiguous external outcome. It is never auto-retried across process restarts.
    """
    request = dict(request)
    call = dict(call)
    if (
        not _positive(request.get("contradiction_request_id"))
        or not _positive(request.get("project_id"))
        or not _positive(request.get("report_version_id"))
        or request.get("task_type") != TASK_TYPE
        or not _text(request.get("local_task_id"))
        or not _is_hash(request.get("task_identity_hash"))
        or not _positive(call.get("model_call_id"))
        or call.get("project_id") != request.get("project_id")
        or call.get("local_task_id") != request.get("local_task_id")
        or call.get("snapshot_id") != request.get("evidence_snapshot_id")
        or call.get("task_type") != TASK_TYPE
        or not _is_hash(call.get("call_identity_hash"))
    ):
        raise _stored_invalid("Contradiction request / ModelCall durable send identity 无法闭合。")
    ensure_report_contradiction_schema()
    row: dict[str, object] = {
        "schema_version": SEND_CLAIM_SCHEMA_VERSION,
        "project_id": request["project_id"],
        "report_version_id": request["report_version_id"],
        "contradiction_request_id": request["contradiction_request_id"],
        "local_task_id": request["local_task_id"],
        "task_identity_hash": request["task_identity_hash"],
        "model_call_id": call["model_call_id"],
        "call_identity_hash": call["call_identity_hash"],
        "claimed_at": _now(),
    }
    row["claim_hash"] = _hash(_send_claim_payload(row))
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"SELECT {_SEND_CLAIM_COLUMNS} FROM report_contradiction_send_claims "
                "WHERE model_call_id = ? OR contradiction_request_id = ?",
                (call["model_call_id"], request["contradiction_request_id"]),
            ).fetchall()
            if existing:
                if len(existing) != 1:
                    raise _stored_invalid("Contradiction durable send claim uniqueness 已损坏。")
                closed = _close_send_claim(existing[0])
                expected = {
                    "project_id": request["project_id"],
                    "report_version_id": request["report_version_id"],
                    "contradiction_request_id": request["contradiction_request_id"],
                    "local_task_id": request["local_task_id"],
                    "task_identity_hash": request["task_identity_hash"],
                    "model_call_id": call["model_call_id"],
                    "call_identity_hash": call["call_identity_hash"],
                }
                if any(closed.get(field) != expected_value for field, expected_value in expected.items()):
                    raise _stored_invalid("Existing contradiction send claim 与 exact request/call 漂移。")
                raise _error(
                    409,
                    "REPORT_CONTRADICTION_SEND_OUTCOME_UNKNOWN",
                    "该 exact contradiction ModelCall 已存在 durable send claim 且没有可在此处证明的结果；禁止自动重发。",
                )
            cursor = conn.execute(
                """
                INSERT INTO report_contradiction_send_claims (
                    schema_version, project_id, report_version_id, contradiction_request_id,
                    local_task_id, task_identity_hash, model_call_id, call_identity_hash,
                    claimed_at, claim_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["schema_version"], row["project_id"], row["report_version_id"],
                    row["contradiction_request_id"], row["local_task_id"], row["task_identity_hash"],
                    row["model_call_id"], row["call_identity_hash"], row["claimed_at"], row["claim_hash"],
                ),
            )
            inserted = conn.execute(
                f"SELECT {_SEND_CLAIM_COLUMNS} FROM report_contradiction_send_claims WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if inserted is None:
                raise _stored_invalid("Contradiction durable send claim INSERT 后无法回读。")
            return _close_send_claim(inserted)
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise _error(
            409,
            "REPORT_CONTRADICTION_SEND_OUTCOME_UNKNOWN",
            "Contradiction durable send claim 并发冲突；禁止自动重发。",
        ) from exc
    except sqlite3.Error as exc:
        raise _stored_invalid("Contradiction durable send claim 保存失败。") from exc

