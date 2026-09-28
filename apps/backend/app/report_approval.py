"""Immutable Page07 ApprovalSnapshot owner.

Final approval is a local, explicit Human action. It reuses the current server-side
ValidationResult authority, re-closes the same candidate/current-authority facts while
holding a SQLite write reservation, and atomically appends one ApprovalSnapshot plus
``report_versions.lifecycle=approved``.

Page07 does not own, copy, validate, render, or select the Anxin Board template.
Formal Page08 rendering is a downstream responsibility that consumes a valid approval.
This module does not send mail, move the formal Git checkpoint, call a provider, read
credentials, or mutate AI raw / supplement bytes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping

from fastapi import HTTPException

from app import report_validation
from app import report_validation_runtime
from app import report_progress_corrections as progress_corrections_store
from app.anxin_board_v3_materialization import (
    AnxinBoardV3MaterializationError,
    materialize_approved_anxin_board_v3_in_transaction,
)
from app.db import get_connection
from app.plain_language_change_summary_source import (
    build_plain_language_change_summary_from_snapshot,
)
from app.report_review_durable_read import get_review_bundle


SCHEMA_VERSION = "approval_snapshot_v1"
_TABLE = "report_approval_snapshots"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_APPROVABLE_LIFECYCLES = {"pending_review", "blocked"}

_COLUMNS = (
    "id, schema_version, project_id, report_version_id, report_version_no, "
    "report_state_version_before, report_state_version_after, validation_result_id, validation_result_hash, "
    "candidate_hash, current_authority_hash, report_content_hash, supplement_version_id, "
    "supplement_content_hash, supplement_provided_by, supplement_provided_at, "
    "supplement_provided_timezone, supplement_source_type, evidence_snapshot_id, evidence_snapshot_hash, "
    "git_snapshot_id, git_facts_hash, git_branch, git_from_commit, git_to_commit, "
    "project_repository_url, prd_id, prd_source_hash, prd_parsed_hash, "
    "prd_structured_hash, prd_document_fingerprint, profile_id, profile_version_no, profile_content_hash, "
    "model_execution_result_id, execution_result_hash, model_call_id, call_identity_hash, "
    "provider, model_id, model_version, actual_model, provider_runtime_fingerprint, rule_version, "
    "output_schema_version, benchmark_sample_pack_version, qualification_hash, authorization_hash, "
    "confirmed_by, confirmed_at, confirmed_timezone, confirmed_utc_offset_minutes, human_acknowledged, "
    "idempotency_key, snapshot_json, approval_snapshot_hash, created_at"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str) -> HTTPException:
    return _error(400, "REPORT_APPROVAL_INPUT_INVALID", message)


def _blocked(message: str) -> HTTPException:
    return _error(409, "REPORT_APPROVAL_BLOCKED", message)


def _stale(message: str = "报告或其确认依据已经变化，请刷新后重新校验。") -> HTTPException:
    return _error(409, "REPORT_APPROVAL_STALE", message)


def _stored_invalid(message: str = "ApprovalSnapshot 持久化事实无法完成自校验。") -> HTTPException:
    return _error(409, "REPORT_APPROVAL_STORED_INVALID", message)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _stored_invalid("ApprovalSnapshot canonical JSON 无法生成。") from exc


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _positive_id(value: object, field: str) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise _input_invalid(f"{field} 必须是 SQLite signed 范围内的正整数。")
    return value


def _confirmed_by(value: object) -> str:
    if type(value) is not str:
        raise _input_invalid("最终确认人必须填写姓名。")
    text = value.strip()
    if not text or len(text) > 120:
        raise _input_invalid("最终确认人姓名不能为空，且不得超过 120 个字符。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _input_invalid("最终确认人姓名包含非法控制字符。")
    if not any(ch.isalnum() for ch in text):
        raise _input_invalid("最终确认人不能只填写空格或无意义符号；该姓名会显示在正式报告中。")
    return text


def _timezone_name(value: object) -> str:
    if type(value) is not str:
        raise _input_invalid("确认时区无效。")
    text = value.strip()
    if not text or len(text) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _input_invalid("确认时区无效。")
    return text


def _utc_offset(value: object) -> int:
    if type(value) is not int or value < -840 or value > 840:
        raise _input_invalid("UTC 偏移必须在 -840 到 840 分钟之间。")
    return value


def _idempotency_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise _input_invalid("idempotency_key 无效。")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema() -> None:
    try:
        with get_connection() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    report_version_id INTEGER NOT NULL UNIQUE,
                    report_version_no INTEGER NOT NULL,
                    report_state_version_before INTEGER NOT NULL,
                    report_state_version_after INTEGER NOT NULL,
                    validation_result_id INTEGER NOT NULL,
                    validation_result_hash TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    current_authority_hash TEXT NOT NULL,
                    report_content_hash TEXT NOT NULL,
                    supplement_version_id INTEGER,
                    supplement_content_hash TEXT,
                    supplement_provided_by TEXT,
                    supplement_provided_at TEXT,
                    supplement_provided_timezone TEXT,
                    supplement_source_type TEXT,
                    evidence_snapshot_id INTEGER NOT NULL,
                    evidence_snapshot_hash TEXT NOT NULL,
                    git_snapshot_id INTEGER NOT NULL,
                    git_facts_hash TEXT NOT NULL,
                    git_branch TEXT NOT NULL,
                    git_from_commit TEXT NOT NULL,
                    git_to_commit TEXT NOT NULL,
                    project_repository_url TEXT NOT NULL,
                    prd_id INTEGER NOT NULL,
                    prd_source_hash TEXT NOT NULL,
                    prd_parsed_hash TEXT NOT NULL,
                    prd_structured_hash TEXT NOT NULL,
                    prd_document_fingerprint TEXT NOT NULL,
                    profile_id INTEGER NOT NULL,
                    profile_version_no INTEGER NOT NULL,
                    profile_content_hash TEXT NOT NULL,
                    model_execution_result_id INTEGER NOT NULL,
                    execution_result_hash TEXT NOT NULL,
                    model_call_id INTEGER NOT NULL,
                    call_identity_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    actual_model TEXT NOT NULL,
                    provider_runtime_fingerprint TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    output_schema_version TEXT NOT NULL,
                    benchmark_sample_pack_version TEXT NOT NULL,
                    qualification_hash TEXT NOT NULL,
                    authorization_hash TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    confirmed_timezone TEXT NOT NULL,
                    confirmed_utc_offset_minutes INTEGER NOT NULL,
                    human_acknowledged INTEGER NOT NULL CHECK (human_acknowledged = 1),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL,
                    approval_snapshot_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_update
                BEFORE UPDATE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'APPROVAL_SNAPSHOT_APPEND_ONLY'); END;
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_delete
                BEFORE DELETE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'APPROVAL_SNAPSHOT_APPEND_ONLY'); END;
                """
            )
            conn.commit()
    except sqlite3.Error as exc:
        raise _stored_invalid("ApprovalSnapshot schema 初始化失败。") from exc


def _snapshot_hash_payload(snapshot: Mapping[str, object]) -> dict[str, object]:
    return {key: snapshot[key] for key in (
        "schema_version", "project_id", "report_version_id", "report_version_no",
        "report_state_version_before", "report_state_version_after", "validation_result_id",
        "validation_result_hash", "candidate_hash", "current_authority_hash", "report_content_hash",
        "supplement_version_id", "supplement_content_hash", "supplement_provided_by",
        "supplement_provided_at", "supplement_provided_timezone", "supplement_source_type",
        "evidence_snapshot_id", "evidence_snapshot_hash", "git_snapshot_id", "git_facts_hash",
        "git_branch", "git_from_commit", "git_to_commit", "project_repository_url", "prd_id",
        "prd_source_hash", "prd_parsed_hash", "prd_structured_hash", "prd_document_fingerprint",
        "profile_id", "profile_version_no", "profile_content_hash", "model_execution_result_id",
        "execution_result_hash", "model_call_id", "call_identity_hash", "provider", "model_id",
        "model_version", "actual_model", "provider_runtime_fingerprint", "rule_version",
        "output_schema_version", "benchmark_sample_pack_version", "qualification_hash",
        "authorization_hash", "confirmed_by", "confirmed_at", "confirmed_timezone",
        "confirmed_utc_offset_minutes", "human_acknowledged"
    )}


def _close_row(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise _stored_invalid()
    for field in (
        "id", "project_id", "report_version_id", "report_version_no", "report_state_version_before",
        "report_state_version_after", "validation_result_id", "evidence_snapshot_id",
        "git_snapshot_id", "prd_id", "profile_id", "profile_version_no", "model_execution_result_id",
        "model_call_id",
    ):
        if type(value.get(field)) is not int or value[field] <= 0:
            raise _stored_invalid()
    if value["report_state_version_after"] != value["report_state_version_before"] + 1:
        raise _stored_invalid("ApprovalSnapshot report state version 无法闭合。")
    if value.get("human_acknowledged") != 1:
        raise _stored_invalid("ApprovalSnapshot Human acknowledgement 无效。")
    if (
        type(value.get("confirmed_utc_offset_minutes")) is not int
        or value["confirmed_utc_offset_minutes"] < -840
        or value["confirmed_utc_offset_minutes"] > 840
    ):
        raise _stored_invalid("ApprovalSnapshot UTC 偏移无效。")
    for field in (
        "validation_result_hash", "candidate_hash", "current_authority_hash",
        "report_content_hash", "evidence_snapshot_hash", "git_facts_hash",
        "prd_source_hash", "prd_parsed_hash", "prd_structured_hash",
        "prd_document_fingerprint", "profile_content_hash", "execution_result_hash",
        "call_identity_hash", "qualification_hash", "authorization_hash",
        "approval_snapshot_hash",
    ):
        if not _is_hash(value.get(field)):
            raise _stored_invalid()
    supplement_fields = (
        "supplement_content_hash", "supplement_provided_by", "supplement_provided_at",
        "supplement_provided_timezone", "supplement_source_type",
    )
    if value.get("supplement_version_id") is None:
        if any(value.get(field) is not None for field in supplement_fields):
            raise _stored_invalid()
    elif (
        type(value.get("supplement_version_id")) is not int
        or value["supplement_version_id"] <= 0
        or not _is_hash(value.get("supplement_content_hash"))
        or any(
            type(value.get(field)) is not str or not value[field]
            for field in (
                "supplement_provided_by", "supplement_provided_at",
                "supplement_provided_timezone", "supplement_source_type",
            )
        )
    ):
        raise _stored_invalid()
    for field in (
        "project_repository_url", "git_branch", "git_from_commit", "git_to_commit",
        "provider", "model_id", "model_version", "actual_model", "provider_runtime_fingerprint",
        "rule_version", "output_schema_version", "benchmark_sample_pack_version", "confirmed_by",
        "confirmed_at", "confirmed_timezone", "idempotency_key", "created_at",
    ):
        if type(value.get(field)) is not str or not value[field]:
            raise _stored_invalid()
    try:
        payload = json.loads(value["snapshot_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise _stored_invalid() from exc
    if not isinstance(payload, Mapping) or _canonical_json(payload) != value["snapshot_json"]:
        raise _stored_invalid()
    normalized = dict(value)
    normalized["human_acknowledged"] = value["human_acknowledged"] == 1
    expected_payload = _snapshot_hash_payload(normalized)
    if dict(payload) != expected_payload:
        raise _stored_invalid("ApprovalSnapshot row 与 snapshot_json identity 发生漂移。")
    if _stable_hash(payload) != value["approval_snapshot_hash"]:
        raise _stored_invalid("ApprovalSnapshot hash 无法闭合。")
    result = dict(payload)
    result["approval_snapshot_id"] = value["id"]
    result["approval_snapshot_hash"] = value["approval_snapshot_hash"]
    result["idempotency_key"] = value["idempotency_key"]
    result["created_at"] = value["created_at"]
    return result


def _existing_by_report(conn: sqlite3.Connection, project_id: int, report_version_id: int):
    return conn.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} WHERE project_id = ? AND report_version_id = ?",
        (project_id, report_version_id),
    ).fetchone()


def _close_existing_with_report(
    conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, object]
) -> dict[str, object]:
    closed = _close_row(row)
    report = conn.execute(
        "SELECT lifecycle, state_version, report_content_hash, version_no FROM report_versions "
        "WHERE id = ? AND project_id = ?",
        (closed["report_version_id"], closed["project_id"]),
    ).fetchone()
    if (
        report is None
        or report["lifecycle"] != "approved"
        or report["state_version"] != closed["report_state_version_after"]
        or report["report_content_hash"] != closed["report_content_hash"]
        or report["version_no"] != closed["report_version_no"]
    ):
        raise _stored_invalid("ApprovalSnapshot 与当前 approved ReportVersion 镜像无法闭合。")
    return closed


def _reclose_under_lock(
    *, conn: sqlite3.Connection, bundle: Mapping[str, object]
) -> dict[str, object]:
    """Re-close only durable identity and local mutable state; never replay Git/PRD."""
    return report_validation_runtime.build_approval_closure(conn=conn, bundle=bundle)


def _assert_validation_matches(
    *, validation: Mapping[str, object], bundle: Mapping[str, object], closure: Mapping[str, object]
) -> None:
    report = bundle["report_version"]
    supplement = bundle.get("current_supplement")
    expected = {
        "project_id": bundle["project_id"],
        "report_version_id": report["report_version_id"],
        "report_state_version": report["state_version"],
        "report_content_hash": report["report_content_hash"],
        "supplement_version_id": None if supplement is None else supplement["supplement_version_id"],
        "supplement_content_hash": None if supplement is None else supplement["content_hash"],
        "evidence_snapshot_id": report["evidence_snapshot_id"],
        "evidence_snapshot_hash": report["evidence_snapshot_hash"],
        "git_snapshot_id": closure["git_snapshot_id"],
        "git_facts_hash": closure["git_facts_hash"],
        "model_execution_result_id": report["model_execution_result_id"],
        "execution_result_hash": report["execution_result_hash"],
        "model_call_id": report["model_call_id"],
        "call_identity_hash": report["call_identity_hash"],
        "candidate_hash": closure["candidate_hash"],
        "current_authority_hash": closure["current_authority_hash"],
        "state": "passed",
    }
    if any(validation.get(key) != value for key, value in expected.items()):
        raise _stale("确认前校验与当前报告/证据/模型/项目 authority 已不一致，请重新校验。")
    if validation.get("blockers") != [] or not _is_hash(validation.get("result_hash")):
        raise _blocked("只有无 blocker 的 passed ValidationResult 可以生成 ApprovalSnapshot。")


def _require_model_identity(bundle: Mapping[str, object], model_call: Mapping[str, object]) -> dict[str, str]:
    ai_raw = bundle.get("ai_raw")
    if not isinstance(ai_raw, Mapping):
        raise _blocked("AI 原文 identity 不完整，不能最终确认。")
    values = {
        "provider": ai_raw.get("provider"),
        "model_id": ai_raw.get("model_id"),
        "model_version": ai_raw.get("model_version"),
        "actual_model": ai_raw.get("actual_model"),
        "provider_runtime_fingerprint": ai_raw.get("provider_runtime_fingerprint"),
        "rule_version": model_call.get("rule_version"),
        "output_schema_version": model_call.get("output_schema_version"),
        "benchmark_sample_pack_version": model_call.get("benchmark_sample_pack_version"),
        "qualification_hash": model_call.get("qualification_hash"),
        "authorization_hash": model_call.get("authorization_hash"),
    }
    for field in (
        "provider", "model_id", "model_version", "actual_model", "provider_runtime_fingerprint",
        "rule_version", "output_schema_version", "benchmark_sample_pack_version",
    ):
        if type(values[field]) is not str or not values[field].strip():
            raise _blocked(f"{field} 无法确认，不能形成正式 ApprovalSnapshot。")
    for field in ("qualification_hash", "authorization_hash"):
        if not _is_hash(values[field]):
            raise _blocked(f"{field} 无法确认，不能形成正式 ApprovalSnapshot。")
    if (
        values["provider"] != model_call.get("provider")
        or values["model_id"] != model_call.get("model_id")
        or values["model_version"] != model_call.get("model_version")
    ):
        raise _stale("AI 原文实际模型 identity 与冻结 Model Call 不一致。")
    return values  # type: ignore[return-value]


def _supplement_provenance(supplement: object) -> dict[str, object]:
    if supplement is None:
        return {
            "supplement_provided_by": None,
            "supplement_provided_at": None,
            "supplement_provided_timezone": None,
            "supplement_source_type": None,
        }
    if not isinstance(supplement, Mapping):
        raise _stored_invalid("ApprovalSnapshot SupplementVersion provenance 无效。")
    for field in ("provided_by", "provided_at", "provided_timezone", "source_type"):
        if type(supplement.get(field)) is not str or not supplement[field]:
            raise _stored_invalid("ApprovalSnapshot SupplementVersion provenance 不完整。")
    return {
        "supplement_provided_by": supplement["provided_by"],
        "supplement_provided_at": supplement["provided_at"],
        "supplement_provided_timezone": supplement["provided_timezone"],
        "supplement_source_type": supplement["source_type"],
    }


def _profile_version(closure: Mapping[str, object], snapshot: Mapping[str, object]) -> int:
    version = closure.get("profile_version_no")
    if type(version) is int and version > 0:
        return version
    authority = closure.get("current_authority")
    profile = authority.get("current_profile") if isinstance(authority, Mapping) else None
    if (
        not isinstance(profile, Mapping)
        or profile.get("id") != snapshot.get("profile_id")
        or profile.get("content_hash") != snapshot.get("profile_content_hash")
        or type(profile.get("version_no")) is not int
        or profile["version_no"] <= 0
    ):
        raise _stored_invalid("ApprovalSnapshot ProjectProfile version identity 无法闭合。")
    return profile["version_no"]


def _check_progress_replay(conn, approval, values):
    try:
        progress_corrections_store.assert_replay(conn, approval=approval, values=values)
    except progress_corrections_store.ProgressCorrectionError as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": str(exc)}) from exc


def create_report_approval_snapshot(
    *,
    project_id: int,
    report_version_id: int,
    expected_report_state_version: int,
    confirmed_by: str,
    confirmed_timezone: str,
    confirmed_utc_offset_minutes: int,
    human_confirmed: bool,
    idempotency_key: str,
    progress_corrections: list[dict] | None = None,
) -> dict[str, object]:
    """Create/replay one immutable approval and atomically mark the exact report approved."""
    project_id = _positive_id(project_id, "project_id")
    report_version_id = _positive_id(report_version_id, "report_version_id")
    expected_report_state_version = _positive_id(
        expected_report_state_version, "expected_report_state_version"
    )
    if human_confirmed is not True:
        raise _input_invalid("最终确认必须由用户明确提交。")
    confirmed_by = _confirmed_by(confirmed_by)
    confirmed_timezone = _timezone_name(confirmed_timezone)
    confirmed_utc_offset_minutes = _utc_offset(confirmed_utc_offset_minutes)
    idempotency_key = _idempotency_key(idempotency_key)
    try:
        progress_corrections = progress_corrections_store.normalize(progress_corrections)
    except progress_corrections_store.ProgressCorrectionError as exc:
        raise _input_invalid(str(exc)) from exc
    _ensure_schema()

    try:
        with get_connection() as conn:
            repeated = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if repeated is not None:
                closed = _close_existing_with_report(conn, repeated)
                if closed["project_id"] != project_id or closed["report_version_id"] != report_version_id:
                    raise _stored_invalid("idempotency_key 已绑定其它 ApprovalSnapshot。")
                _check_progress_replay(conn, closed, progress_corrections)
                return {"approval_snapshot": closed, "created": False}
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc

    validation_envelope = report_validation_runtime.create_validation_result(
        project_id=project_id,
        report_version_id=report_version_id,
        expected_report_state_version=expected_report_state_version,
    )
    validation = validation_envelope.get("validation_result")
    if not isinstance(validation, Mapping) or validation.get("state") != "passed" or validation.get("blockers") != []:
        raise _blocked("确认前校验未通过；当前报告不会生成 ApprovalSnapshot。")

    try:
        daily_change = build_plain_language_change_summary_from_snapshot(
            project_id=project_id,
            git_snapshot_id=validation["git_snapshot_id"],
        )
    except (HTTPException, RuntimeError, ValueError, KeyError) as exc:
        raise _blocked("冻结 Git 变化摘要无法闭合；本次最终确认和正式安心看板均未写入。") from exc

    bundle = get_review_bundle(project_id=project_id, report_version_id=report_version_id)
    report = bundle["report_version"]
    if report.get("state_version") != expected_report_state_version:
        raise _stale()
    if report.get("lifecycle") not in _APPROVABLE_LIFECYCLES:
        if report.get("lifecycle") == "approved":
            try:
                with get_connection() as conn:
                    existing = _existing_by_report(conn, project_id, report_version_id)
                    if existing is not None:
                        closed = _close_existing_with_report(conn, existing)
                        _check_progress_replay(conn, closed, progress_corrections)
                        return {"approval_snapshot": closed, "created": False}
            except sqlite3.Error as exc:
                raise _stored_invalid() from exc
        raise _blocked("当前 ReportVersion 生命周期不允许最终确认。")

    confirmed_at = _now()
    created_at = confirmed_at
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = _existing_by_report(conn, project_id, report_version_id)
            if existing is not None:
                closed = _close_existing_with_report(conn, existing)
                _check_progress_replay(conn, closed, progress_corrections)
                conn.commit()
                return {"approval_snapshot": closed, "created": False}

            fresh = conn.execute(
                "SELECT lifecycle, state_version, report_content_hash, version_no FROM report_versions "
                "WHERE id = ? AND project_id = ?",
                (report_version_id, project_id),
            ).fetchone()
            if fresh is None:
                raise _stale("当前 ReportVersion 不存在。")
            if fresh["state_version"] != expected_report_state_version:
                raise _stale()
            if fresh["lifecycle"] not in _APPROVABLE_LIFECYCLES:
                raise _blocked("当前 ReportVersion 生命周期已变化，不能最终确认。")
            if fresh["report_content_hash"] != report.get("report_content_hash"):
                raise _stale("报告内容指纹已变化。")
            if fresh["version_no"] != report.get("version_no"):
                raise _stale("报告版本号已变化。")

            closure = _reclose_under_lock(conn=conn, bundle=bundle)
            if closure["state"] != "passed" or closure["blockers"]:
                raise _blocked("确认瞬间重新闭合校验后出现阻断；没有生成 ApprovalSnapshot。")
            _assert_validation_matches(validation=validation, bundle=bundle, closure=closure)

            snapshot = closure["snapshot"]
            model_call = closure["model_call"]
            model_identity = _require_model_identity(bundle, model_call)
            supplement = bundle.get("current_supplement")
            supplement_provenance = _supplement_provenance(supplement)
            snapshot_payload: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "report_version_id": report_version_id,
                "report_version_no": report["version_no"],
                "report_state_version_before": expected_report_state_version,
                "report_state_version_after": expected_report_state_version + 1,
                "validation_result_id": validation["validation_result_id"],
                "validation_result_hash": validation["result_hash"],
                "candidate_hash": validation["candidate_hash"],
                "current_authority_hash": validation["current_authority_hash"],
                "report_content_hash": report["report_content_hash"],
                "supplement_version_id": None if supplement is None else supplement["supplement_version_id"],
                "supplement_content_hash": None if supplement is None else supplement["content_hash"],
                **supplement_provenance,
                "evidence_snapshot_id": report["evidence_snapshot_id"],
                "evidence_snapshot_hash": report["evidence_snapshot_hash"],
                "git_snapshot_id": closure["git_snapshot_id"],
                "git_facts_hash": closure["git_facts_hash"],
                "git_branch": closure["git_branch"],
                "git_from_commit": closure["git_from_commit"],
                "git_to_commit": closure["git_to_commit"],
                "project_repository_url": snapshot["project_repository_url"],
                "prd_id": snapshot["prd_id"],
                "prd_source_hash": snapshot["prd_source_hash"],
                "prd_parsed_hash": snapshot["prd_parsed_hash"],
                "prd_structured_hash": snapshot["prd_structured_hash"],
                "prd_document_fingerprint": snapshot["prd_document_fingerprint"],
                "profile_id": snapshot["profile_id"],
                "profile_version_no": _profile_version(closure, snapshot),
                "profile_content_hash": snapshot["profile_content_hash"],
                "model_execution_result_id": report["model_execution_result_id"],
                "execution_result_hash": report["execution_result_hash"],
                "model_call_id": report["model_call_id"],
                "call_identity_hash": report["call_identity_hash"],
                **model_identity,
                "confirmed_by": confirmed_by,
                "confirmed_at": confirmed_at,
                "confirmed_timezone": confirmed_timezone,
                "confirmed_utc_offset_minutes": confirmed_utc_offset_minutes,
                "human_acknowledged": True,
            }
            approval_hash = _stable_hash(_snapshot_hash_payload(snapshot_payload))
            snapshot_json = _canonical_json(_snapshot_hash_payload(snapshot_payload))
            cursor = conn.execute(
                f"""
                INSERT INTO {_TABLE} (
                    schema_version, project_id, report_version_id, report_version_no,
                    report_state_version_before, report_state_version_after,
                    validation_result_id, validation_result_hash, candidate_hash,
                    current_authority_hash, report_content_hash, supplement_version_id,
                    supplement_content_hash, supplement_provided_by, supplement_provided_at,
                    supplement_provided_timezone, supplement_source_type, evidence_snapshot_id,
                    evidence_snapshot_hash, git_snapshot_id, git_facts_hash, git_branch,
                    git_from_commit, git_to_commit, project_repository_url, prd_id,
                    prd_source_hash, prd_parsed_hash, prd_structured_hash,
                    prd_document_fingerprint, profile_id, profile_version_no, profile_content_hash,
                    model_execution_result_id, execution_result_hash, model_call_id,
                    call_identity_hash, provider, model_id, model_version, actual_model,
                    provider_runtime_fingerprint, rule_version, output_schema_version,
                    benchmark_sample_pack_version, qualification_hash, authorization_hash,
                    confirmed_by, confirmed_at, confirmed_timezone, confirmed_utc_offset_minutes,
                    human_acknowledged, idempotency_key, snapshot_json, approval_snapshot_hash, created_at
                ) VALUES ({','.join('?' for _ in range(56))})
                """,
                (
                    snapshot_payload["schema_version"], snapshot_payload["project_id"],
                    snapshot_payload["report_version_id"], snapshot_payload["report_version_no"],
                    snapshot_payload["report_state_version_before"], snapshot_payload["report_state_version_after"],
                    snapshot_payload["validation_result_id"], snapshot_payload["validation_result_hash"],
                    snapshot_payload["candidate_hash"], snapshot_payload["current_authority_hash"],
                    snapshot_payload["report_content_hash"], snapshot_payload["supplement_version_id"],
                    snapshot_payload["supplement_content_hash"], snapshot_payload["supplement_provided_by"],
                    snapshot_payload["supplement_provided_at"], snapshot_payload["supplement_provided_timezone"],
                    snapshot_payload["supplement_source_type"], snapshot_payload["evidence_snapshot_id"],
                    snapshot_payload["evidence_snapshot_hash"], snapshot_payload["git_snapshot_id"],
                    snapshot_payload["git_facts_hash"], snapshot_payload["git_branch"],
                    snapshot_payload["git_from_commit"], snapshot_payload["git_to_commit"],
                    snapshot_payload["project_repository_url"], snapshot_payload["prd_id"],
                    snapshot_payload["prd_source_hash"], snapshot_payload["prd_parsed_hash"],
                    snapshot_payload["prd_structured_hash"], snapshot_payload["prd_document_fingerprint"],
                    snapshot_payload["profile_id"], snapshot_payload["profile_version_no"],
                    snapshot_payload["profile_content_hash"], snapshot_payload["model_execution_result_id"],
                    snapshot_payload["execution_result_hash"], snapshot_payload["model_call_id"],
                    snapshot_payload["call_identity_hash"], snapshot_payload["provider"],
                    snapshot_payload["model_id"], snapshot_payload["model_version"],
                    snapshot_payload["actual_model"], snapshot_payload["provider_runtime_fingerprint"],
                    snapshot_payload["rule_version"], snapshot_payload["output_schema_version"],
                    snapshot_payload["benchmark_sample_pack_version"], snapshot_payload["qualification_hash"],
                    snapshot_payload["authorization_hash"], snapshot_payload["confirmed_by"],
                    snapshot_payload["confirmed_at"], snapshot_payload["confirmed_timezone"],
                    snapshot_payload["confirmed_utc_offset_minutes"], snapshot_payload["human_acknowledged"],
                    idempotency_key, snapshot_json, approval_hash, created_at,
                ),
            )
            transitioned = conn.execute(
                "UPDATE report_versions SET lifecycle = 'approved', state_version = state_version + 1 "
                "WHERE id = ? AND project_id = ? AND lifecycle IN ('pending_review','blocked') "
                "AND state_version = ? AND report_content_hash = ?",
                (report_version_id, project_id, expected_report_state_version, report["report_content_hash"]),
            )
            if transitioned.rowcount != 1:
                raise _stale("最终确认事务未能原子消费当前 ReportVersion 状态。")
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if row is None:
                raise _stored_invalid("ApprovalSnapshot INSERT 后无法回读。")
            closed = _close_existing_with_report(conn, row)
            try:
                progress_corrections_store.persist(conn, approval=closed, values=progress_corrections)
                materialize_approved_anxin_board_v3_in_transaction(
                    conn=conn,
                    project_id=project_id,
                    review_bundle=bundle,
                    approval_snapshot=closed,
                    daily_change=daily_change,
                    **({"corrections": progress_corrections_store.as_updates(progress_corrections)} if progress_corrections else {}),
                )
            except AnxinBoardV3MaterializationError as exc:
                raise HTTPException(409, detail={
                    "code": getattr(exc, "cause_code", exc.code),
                    "message": str(exc) + " 本次确认未保存，原报告保持不变。",
                }) from exc
            conn.commit()
            return {"approval_snapshot": closed, "created": True}
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        try:
            with get_connection() as conn:
                winner = _existing_by_report(conn, project_id, report_version_id)
                if winner is not None:
                    closed = _close_existing_with_report(conn, winner)
                    _check_progress_replay(conn, closed, progress_corrections)
                    return {"approval_snapshot": closed, "created": False}
        except (HTTPException, sqlite3.Error):
            pass
        raise _stored_invalid("ApprovalSnapshot 并发写入冲突无法闭合。") from exc
    except sqlite3.Error as exc:
        raise _stored_invalid("ApprovalSnapshot 持久化失败。") from exc


def get_report_approval_snapshot(
    *, project_id: int, report_version_id: int
) -> dict[str, object] | None:
    """Pure read: return the immutable ApprovalSnapshot, or None before first approval."""
    project_id = _positive_id(project_id, "project_id")
    report_version_id = _positive_id(report_version_id, "report_version_id")
    try:
        with get_connection() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_TABLE,),
            ).fetchone()
            if table is None:
                return None
            row = _existing_by_report(conn, project_id, report_version_id)
            if row is None:
                return None
            return _close_existing_with_report(conn, row)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
