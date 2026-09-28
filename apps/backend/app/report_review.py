"""Page 07 Report Review Slice 1/2: immutable review facts and reanalysis replacement admission."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException

from app.context_resolver import build_context_candidate_set
from app.db import get_connection
from app.git_workspace_locks import GitOperationInProgress, project_workspace_lock
from app.model_execution_results import get_model_execution_result
from app import report_generation_tasks


REPORT_VERSION_SCHEMA_VERSION = "report_version_v1"
SUPPLEMENT_VERSION_SCHEMA_VERSION = "report_supplement_version_v1"
REANALYSIS_REQUEST_SCHEMA_VERSION = "report_reanalysis_request_v1"
REVIEW_BUNDLE_SCHEMA_VERSION = "report_review_bundle_v1"

_REPORT_LIFECYCLES = ("pending_review", "blocked", "approved", "superseded", "voided")
_WRITABLE_SUPPLEMENT_LIFECYCLES = {"pending_review", "blocked"}
_REANALYSIS_SOURCE_LIFECYCLES = {"pending_review", "blocked"}
_SUPPLEMENT_SOURCE_TYPES = {"pm_correction", "pm_external_fact"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1

_REPORT_COLUMNS = (
    "id, schema_version, project_id, version_no, parent_report_version_id, "
    "model_execution_result_id, execution_result_hash, formal_response_hash, "
    "validated_result_hash, model_call_id, call_identity_hash, "
    "evidence_snapshot_id, evidence_snapshot_hash, report_content_hash, "
    "lifecycle, state_version, created_at"
)
_SUPPLEMENT_COLUMNS = (
    "id, schema_version, report_version_id, version_no, expected_previous_version_no, "
    "content, content_hash, source_type, provided_by, provided_at, provided_timezone, "
    "idempotency_key"
)
_REANALYSIS_COLUMNS = (
    "id, schema_version, project_id, report_version_id, evidence_snapshot_id, "
    "evidence_snapshot_hash, source_report_state_version, source_report_lifecycle, "
    "error_location, error_location_hash, corrected_truth, corrected_truth_hash, "
    "correction_basis, correction_basis_hash, correction_source, correction_source_hash, "
    "requested_by, requested_at, "
    "requested_timezone, idempotency_key, replacement_task_id, "
    "replacement_local_task_id, replacement_task_identity_hash, request_hash"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "报告审阅输入无效。") -> HTTPException:
    return _error(400, "REPORT_REVIEW_INPUT_INVALID", message)


def _report_not_found() -> HTTPException:
    return _error(404, "REPORT_VERSION_NOT_FOUND", "指定的报告版本不存在。")


def _source_not_reviewable(message: str = "该模型执行结果不能物化为报告审阅版本。") -> HTTPException:
    return _error(409, "REPORT_VERSION_SOURCE_NOT_REVIEWABLE", message)


def _stored_invalid(message: str = "报告审阅持久化事实无法完成自校验。") -> HTTPException:
    return _error(409, "REPORT_REVIEW_STORED_INVALID", message)


def _supplement_stale() -> HTTPException:
    return _error(
        409,
        "REPORT_SUPPLEMENT_STALE",
        "人工补充版本已变化，请刷新后基于最新版本重新提交。",
    )


def _supplement_conflict() -> HTTPException:
    return _error(
        409,
        "REPORT_SUPPLEMENT_IDEMPOTENCY_CONFLICT",
        "该幂等键已绑定不同的人工补充请求。",
    )


def _supplement_not_writable() -> HTTPException:
    return _error(
        409,
        "REPORT_SUPPLEMENT_NOT_WRITABLE",
        "当前报告版本不再允许追加人工补充。",
    )


def _reanalysis_stale() -> HTTPException:
    return _error(
        409,
        "REPORT_REANALYSIS_STALE",
        "报告审阅状态已变化，请刷新后重新提交更正。",
    )


def _reanalysis_conflict() -> HTTPException:
    return _error(
        409,
        "REPORT_REANALYSIS_IDEMPOTENCY_CONFLICT",
        "该更正幂等身份已绑定不同的 ReanalysisRequest。",
    )


def _reanalysis_not_allowed() -> HTTPException:
    return _error(
        409,
        "REPORT_REANALYSIS_NOT_ALLOWED",
        "当前报告版本不允许创建新的重分析替换链。",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_positive_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_SIGNED_INTEGER_MAX


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and 0 <= value <= _SQLITE_SIGNED_INTEGER_MAX


def _require_positive_id(value: object, field_name: str) -> int:
    if not _is_positive_int(value):
        raise _input_invalid(f"{field_name} 必须是 SQLite signed 范围内的正整数。")
    return value


def _require_nonnegative_version(value: object, field_name: str) -> int:
    if not _is_nonnegative_int(value):
        raise _input_invalid(f"{field_name} 必须是 SQLite signed 范围内的非负整数。")
    return value


def _require_identity_text(
    value: object,
    field_name: str,
    *,
    max_length: int,
) -> str:
    if type(value) is not str:
        raise _input_invalid(f"{field_name} 必须是字符串。")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise _input_invalid(f"{field_name} 长度无效。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise _input_invalid(f"{field_name} 不得包含控制字符。")
    return normalized


def _require_content(value: object, field_name: str = "content") -> str:
    if type(value) is not str:
        raise _input_invalid(f"{field_name} 必须是字符串。")
    if not value.strip() or len(value) > 20000:
        raise _input_invalid(f"{field_name} 必须非空且不超过 20000 个字符。")
    if "\x00" in value:
        raise _input_invalid(f"{field_name} 不得包含 NUL。")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _input_invalid(f"{field_name} 必须可编码为 UTF-8。") from exc
    return value


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _stable_hash(value: Mapping[str, object]) -> str:
    raw = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _authority_error(exc: HTTPException, message: str) -> HTTPException:
    detail = exc.detail if isinstance(exc.detail, Mapping) else None
    code = detail.get("code") if detail is not None else None
    if isinstance(code, str) and (
        code.startswith("CONTEXT_")
        or code.startswith("GIT_WORKSPACE_")
        or code == "GIT_OPERATION_IN_PROGRESS"
    ):
        return _stored_invalid(message)
    return exc


def _verified_candidate(snapshot_id: int, *, message: str) -> dict[str, object]:
    try:
        candidate = build_context_candidate_set(snapshot_id)
    except HTTPException as exc:
        raise _authority_error(exc, message) from exc
    if not isinstance(candidate, Mapping):
        raise _stored_invalid(message)
    return dict(candidate)


def _validate_report_source(
    *,
    model_result: Mapping[str, object],
    candidate: Mapping[str, object],
    project_id: int,
) -> None:
    if model_result.get("project_id") != project_id:
        raise _source_not_reviewable("ModelExecutionResult 不属于当前项目。")
    formal_response = model_result.get("formal_response")
    if (
        not isinstance(formal_response, Mapping)
        or formal_response.get("status") != "succeeded"
        or model_result.get("task_type") != "daily_report_generate"
        or formal_response.get("task_type") != "daily_report_generate"
        or formal_response.get("output_schema_version") != "daily-report/1.0"
        or not isinstance(model_result.get("validated_result"), Mapping)
    ):
        raise _source_not_reviewable(
            "当前物化入口只接受 verified succeeded daily_report_generate / daily-report/1.0 结果。"
        )
    if (
        candidate.get("project_id") != project_id
        or candidate.get("snapshot_id") != model_result.get("snapshot_id")
        or not _is_hash(candidate.get("snapshot_hash"))
    ):
        raise _stored_invalid("Context Candidate Set 与 ModelExecutionResult 冻结身份不一致。")


def init_report_review_schema() -> None:
    """Idempotently install Slice 1/2 review tables and immutable-history guards."""
    try:
        with get_connection() as conn:
            from app.report_reanalysis_cancellation import init_schema as init_cancellation_schema
            init_cancellation_schema(conn)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS report_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL CHECK (schema_version = '{REPORT_VERSION_SCHEMA_VERSION}'),
                    project_id INTEGER NOT NULL,
                    version_no INTEGER NOT NULL CHECK (version_no > 0),
                    parent_report_version_id INTEGER NULL,
                    model_execution_result_id INTEGER NOT NULL UNIQUE,
                    execution_result_hash TEXT NOT NULL,
                    formal_response_hash TEXT NOT NULL,
                    validated_result_hash TEXT NOT NULL,
                    model_call_id INTEGER NOT NULL,
                    call_identity_hash TEXT NOT NULL,
                    evidence_snapshot_id INTEGER NOT NULL,
                    evidence_snapshot_hash TEXT NOT NULL,
                    report_content_hash TEXT NOT NULL,
                    lifecycle TEXT NOT NULL CHECK (
                        lifecycle IN ('pending_review', 'blocked', 'approved', 'superseded', 'voided')
                    ),
                    state_version INTEGER NOT NULL CHECK (state_version > 0),
                    created_at TEXT NOT NULL,
                    UNIQUE (project_id, version_no)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS report_supplement_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL CHECK (
                        schema_version = '{SUPPLEMENT_VERSION_SCHEMA_VERSION}'
                    ),
                    report_version_id INTEGER NOT NULL,
                    version_no INTEGER NOT NULL CHECK (version_no > 0),
                    expected_previous_version_no INTEGER NOT NULL CHECK (
                        expected_previous_version_no >= 0
                    ),
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK (
                        source_type IN ('pm_correction', 'pm_external_fact')
                    ),
                    provided_by TEXT NOT NULL,
                    provided_at TEXT NOT NULL,
                    provided_timezone TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    UNIQUE (report_version_id, version_no),
                    UNIQUE (report_version_id, idempotency_key)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS report_reanalysis_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL CHECK (
                        schema_version = '{REANALYSIS_REQUEST_SCHEMA_VERSION}'
                    ),
                    project_id INTEGER NOT NULL,
                    report_version_id INTEGER NOT NULL UNIQUE,
                    evidence_snapshot_id INTEGER NOT NULL,
                    evidence_snapshot_hash TEXT NOT NULL,
                    source_report_state_version INTEGER NOT NULL CHECK (
                        source_report_state_version > 0
                    ),
                    source_report_lifecycle TEXT NOT NULL CHECK (
                        source_report_lifecycle IN ('pending_review', 'blocked')
                    ),
                    error_location TEXT NOT NULL,
                    error_location_hash TEXT NOT NULL,
                    corrected_truth TEXT NOT NULL,
                    corrected_truth_hash TEXT NOT NULL,
                    correction_basis TEXT NOT NULL,
                    correction_basis_hash TEXT NOT NULL,
                    correction_source TEXT NOT NULL,
                    correction_source_hash TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    requested_timezone TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    replacement_task_id INTEGER NOT NULL UNIQUE,
                    replacement_local_task_id TEXT NOT NULL,
                    replacement_task_identity_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL UNIQUE,
                    UNIQUE (project_id, idempotency_key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_report_versions_project_current
                ON report_versions(project_id, version_no DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_report_supplement_versions_latest
                ON report_supplement_versions(report_version_id, version_no DESC)
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS tr_report_versions_no_delete
                BEFORE DELETE ON report_versions
                BEGIN
                    SELECT RAISE(ABORT, 'report_versions are append-preserved');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS tr_report_versions_immutable_identity
                BEFORE UPDATE ON report_versions
                WHEN
                    NEW.schema_version IS NOT OLD.schema_version
                    OR NEW.project_id IS NOT OLD.project_id
                    OR NEW.version_no IS NOT OLD.version_no
                    OR NEW.parent_report_version_id IS NOT OLD.parent_report_version_id
                    OR NEW.model_execution_result_id IS NOT OLD.model_execution_result_id
                    OR NEW.execution_result_hash IS NOT OLD.execution_result_hash
                    OR NEW.formal_response_hash IS NOT OLD.formal_response_hash
                    OR NEW.validated_result_hash IS NOT OLD.validated_result_hash
                    OR NEW.model_call_id IS NOT OLD.model_call_id
                    OR NEW.call_identity_hash IS NOT OLD.call_identity_hash
                    OR NEW.evidence_snapshot_id IS NOT OLD.evidence_snapshot_id
                    OR NEW.evidence_snapshot_hash IS NOT OLD.evidence_snapshot_hash
                    OR NEW.report_content_hash IS NOT OLD.report_content_hash
                    OR NEW.created_at IS NOT OLD.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'report_version immutable identity cannot change');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS tr_report_supplement_versions_no_update
                BEFORE UPDATE ON report_supplement_versions
                BEGIN
                    SELECT RAISE(ABORT, 'report_supplement_versions are append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS tr_report_supplement_versions_no_delete
                BEFORE DELETE ON report_supplement_versions
                BEGIN
                    SELECT RAISE(ABORT, 'report_supplement_versions are append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS tr_report_reanalysis_requests_no_update
                BEFORE UPDATE ON report_reanalysis_requests
                BEGIN
                    SELECT RAISE(ABORT, 'report_reanalysis_requests are append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS tr_report_reanalysis_requests_no_delete
                BEFORE DELETE ON report_reanalysis_requests
                BEGIN
                    SELECT RAISE(ABORT, 'report_reanalysis_requests are append-only');
                END
                """
            )
    except sqlite3.Error as exc:
        raise _stored_invalid("无法初始化报告审阅 Slice 1/2 持久化结构。") from exc


def _read_report_by_id(conn: sqlite3.Connection, report_version_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_REPORT_COLUMNS} FROM report_versions WHERE id = ?",
        (report_version_id,),
    ).fetchone()


def _read_report_by_result(
    conn: sqlite3.Connection, model_execution_result_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_REPORT_COLUMNS} FROM report_versions WHERE model_execution_result_id = ?",
        (model_execution_result_id,),
    ).fetchone()


def _read_current_report_id(conn: sqlite3.Connection, project_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM report_versions
        WHERE project_id = ?
        ORDER BY version_no DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else row["id"]


def _collect_evidence_ids(value: object) -> set[str]:
    found: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if key in {"evidence_ids", "evidence_refs"} and type(child) is list:
                    for item in child:
                        if type(item) is str and item:
                            found.add(item)
                visit(child)
        elif type(node) is list:
            for child in node:
                visit(child)

    visit(value)
    return found


def _candidate_evidence_refs(
    *, candidate: Mapping[str, object], ai_content: object
) -> list[dict[str, object]]:
    referenced = _collect_evidence_ids(ai_content)
    if not referenced:
        return []
    items = candidate.get("items")
    if type(items) is not list:
        raise _stored_invalid("Context Candidate Set items 无效。")
    by_id: dict[str, Mapping[str, object]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise _stored_invalid("Context Candidate Set item 无效。")
        evidence_id = item.get("evidence_id")
        if type(evidence_id) is not str or not evidence_id or evidence_id in by_id:
            raise _stored_invalid("Context Candidate Set evidence identity 无效。")
        by_id[evidence_id] = item
    if not referenced.issubset(by_id):
        raise _stored_invalid("AI 原文引用无法闭合到 authoritative Context Candidate Set。")
    result: list[dict[str, object]] = []
    for evidence_id in sorted(referenced):
        item = by_id[evidence_id]
        if item.get("type") not in {"git_file_fact", "prd_block"}:
            raise _stored_invalid("AI 原文引用了 Slice 1 不支持的 EvidenceItem 类型。")
        if type(item.get("source_ref")) is not str or not _is_hash(item.get("content_hash")):
            raise _stored_invalid("Context Candidate Set EvidenceItem 投影无效。")
        redaction_state = item.get("resolved_content_redaction_state")
        if type(redaction_state) is not str or not redaction_state:
            raise _stored_invalid("Context Candidate Set EvidenceItem redaction 投影无效。")
        result.append(
            {
                "evidence_id": evidence_id,
                "type": item["type"],
                "source_ref": item["source_ref"],
                "content_hash": item["content_hash"],
                "redaction_state": redaction_state,
            }
        )
    return result


def _candidate_git_facts(candidate: Mapping[str, object]) -> dict[str, object]:
    range_value = candidate.get("range")
    items = candidate.get("items")
    if not isinstance(range_value, Mapping) or type(items) is not list:
        raise _stored_invalid("Context Candidate Set Git 投影无效。")
    commits = range_value.get("commits")
    if type(commits) is not list or range_value.get("commit_count") != len(commits):
        raise _stored_invalid("Context Candidate Set Git commit 投影无效。")
    if any(not isinstance(item, Mapping) for item in items):
        raise _stored_invalid("Context Candidate Set item 投影无效。")
    git_items = [item for item in items if item.get("type") == "git_file_fact"]
    added_lines = sum(
        item["added_lines"]
        for item in git_items
        if type(item.get("added_lines")) is int
    )
    deleted_lines = sum(
        item["deleted_lines"]
        for item in git_items
        if type(item.get("deleted_lines")) is int
    )
    manifest_hashes = {
        item.get("file_manifest_hash")
        for item in git_items
        if item.get("file_manifest_hash") is not None
    }
    if len(manifest_hashes) > 1 or any(not _is_hash(value) for value in manifest_hashes):
        raise _stored_invalid("Context Candidate Set file manifest 投影无效。")
    return {
        "git_snapshot_id": range_value.get("git_snapshot_id"),
        "branch": range_value.get("branch"),
        "from_commit": range_value.get("from_commit"),
        "to_commit": range_value.get("to_commit"),
        "commits": list(commits),
        "commit_count": range_value.get("commit_count"),
        "changed_file_count": len(git_items),
        "added_lines": added_lines,
        "deleted_lines": deleted_lines,
        "git_facts_hash": range_value.get("git_facts_hash"),
        "file_manifest_hash": next(iter(manifest_hashes), None),
    }


def _close_report_row(
    row: sqlite3.Row | Mapping[str, object],
    *,
    model_result: Mapping[str, object],
    candidate: Mapping[str, object],
) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != REPORT_VERSION_SCHEMA_VERSION:
        raise _stored_invalid("ReportVersion schema_version 无效。")
    for field in (
        "id",
        "project_id",
        "version_no",
        "model_execution_result_id",
        "model_call_id",
        "evidence_snapshot_id",
        "state_version",
    ):
        if not _is_positive_int(value.get(field)):
            raise _stored_invalid("ReportVersion 身份字段无效。")
    if value.get("parent_report_version_id") is not None and not _is_positive_int(
        value["parent_report_version_id"]
    ):
        raise _stored_invalid("ReportVersion parent identity 无效。")
    if value.get("lifecycle") not in _REPORT_LIFECYCLES:
        raise _stored_invalid("ReportVersion lifecycle 无效。")
    for field in (
        "execution_result_hash",
        "formal_response_hash",
        "validated_result_hash",
        "call_identity_hash",
        "evidence_snapshot_hash",
        "report_content_hash",
    ):
        if not _is_hash(value.get(field)):
            raise _stored_invalid("ReportVersion 哈希字段无效。")
    if type(value.get("created_at")) is not str or not value["created_at"]:
        raise _stored_invalid("ReportVersion created_at 无效。")

    bindings = {
        "project_id": model_result.get("project_id"),
        "model_execution_result_id": model_result.get("model_result_id"),
        "execution_result_hash": model_result.get("execution_result_hash"),
        "formal_response_hash": model_result.get("formal_response_hash"),
        "validated_result_hash": model_result.get("validated_result_hash"),
        "model_call_id": model_result.get("model_call_id"),
        "call_identity_hash": model_result.get("call_identity_hash"),
        "evidence_snapshot_id": model_result.get("snapshot_id"),
        "evidence_snapshot_hash": candidate.get("snapshot_hash"),
        "report_content_hash": model_result.get("validated_result_hash"),
    }
    if any(value.get(field) != expected for field, expected in bindings.items()):
        raise _stored_invalid(
            "ReportVersion 与 verified ModelExecutionResult/Context Candidate Set 绑定漂移。"
        )
    return {
        "report_version_id": value["id"],
        "schema_version": value["schema_version"],
        "project_id": value["project_id"],
        "version_no": value["version_no"],
        "parent_report_version_id": value["parent_report_version_id"],
        "model_execution_result_id": value["model_execution_result_id"],
        "execution_result_hash": value["execution_result_hash"],
        "formal_response_hash": value["formal_response_hash"],
        "validated_result_hash": value["validated_result_hash"],
        "model_call_id": value["model_call_id"],
        "call_identity_hash": value["call_identity_hash"],
        "evidence_snapshot_id": value["evidence_snapshot_id"],
        "evidence_snapshot_hash": value["evidence_snapshot_hash"],
        "report_content_hash": value["report_content_hash"],
        "lifecycle": value["lifecycle"],
        "state_version": value["state_version"],
        "created_at": value["created_at"],
    }


def _load_closed_report(
    report_version_id: int,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    try:
        with get_connection() as conn:
            row = _read_report_by_id(conn, report_version_id)
            if row is None:
                raise _report_not_found()
            project_id = row["project_id"]
            model_execution_result_id = row["model_execution_result_id"]
            snapshot_id = row["evidence_snapshot_id"]
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc

    try:
        model_result = get_model_execution_result(model_execution_result_id)
    except HTTPException as exc:
        raise _stored_invalid(
            "ReportVersion 引用的 ModelExecutionResult 无法完成 verified read。"
        ) from exc
    candidate = _verified_candidate(
        snapshot_id,
        message="ReportVersion 引用的 Evidence/Git/PRD source 无法完成 authoritative verified read。",
    )
    if candidate.get("project_id") != project_id:
        raise _stored_invalid("ReportVersion 引用的 Context Candidate Set 项目身份漂移。")
    report = _close_report_row(row, model_result=model_result, candidate=candidate)
    return report, model_result, candidate


def materialize_report_version(
    *,
    project_id: int,
    model_execution_result_id: int,
) -> dict[str, object]:
    """Materialize one ReportVersion under one DB + Git no-drift critical section."""
    project_id = _require_positive_id(project_id, "project_id")
    model_execution_result_id = _require_positive_id(
        model_execution_result_id, "model_execution_result_id"
    )

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                model_result = get_model_execution_result(model_execution_result_id)
            except HTTPException:
                raise
            if model_result.get("project_id") != project_id:
                raise _source_not_reviewable("ModelExecutionResult 不属于当前项目。")

            try:
                with project_workspace_lock(project_id):
                    candidate = _verified_candidate(
                        model_result["snapshot_id"],
                        message="ReportVersion source Evidence/Git/PRD 无法完成 authoritative verified read。",
                    )
                    _validate_report_source(
                        model_result=model_result,
                        candidate=candidate,
                        project_id=project_id,
                    )

                    existing = _read_report_by_result(conn, model_execution_result_id)
                    if existing is not None:
                        closed = _close_report_row(
                            existing,
                            model_result=model_result,
                            candidate=candidate,
                        )
                        conn.commit()
                        return {"report_version": closed, "created": False}

                    next_row = conn.execute(
                        "SELECT MAX(version_no) AS max_version "
                        "FROM report_versions WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                    max_version = next_row["max_version"] if next_row is not None else None
                    if max_version is not None and not _is_positive_int(max_version):
                        raise _stored_invalid("已有 ReportVersion version_no 无效。")
                    version_no = 1 if max_version is None else max_version + 1
                    if not _is_positive_int(version_no):
                        raise _stored_invalid("ReportVersion version_no 溢出。")
                    created_at = _now()
                    cursor = conn.execute(
                        """
                        INSERT INTO report_versions (
                            schema_version, project_id, version_no, parent_report_version_id,
                            model_execution_result_id, execution_result_hash,
                            formal_response_hash, validated_result_hash, model_call_id,
                            call_identity_hash, evidence_snapshot_id, evidence_snapshot_hash,
                            report_content_hash, lifecycle, state_version, created_at
                        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'pending_review', 1, ?)
                        """,
                        (
                            REPORT_VERSION_SCHEMA_VERSION,
                            project_id,
                            version_no,
                            model_execution_result_id,
                            model_result["execution_result_hash"],
                            model_result["formal_response_hash"],
                            model_result["validated_result_hash"],
                            model_result["model_call_id"],
                            model_result["call_identity_hash"],
                            model_result["snapshot_id"],
                            candidate["snapshot_hash"],
                            model_result["validated_result_hash"],
                            created_at,
                        ),
                    )
                    report_version_id = cursor.lastrowid
                    if not _is_positive_int(report_version_id):
                        raise _stored_invalid("ReportVersion 写入后没有合法 id。")
                    row = _read_report_by_id(conn, report_version_id)
                    if row is None:
                        raise _stored_invalid("ReportVersion 写入后无法回读。")
                    closed = _close_report_row(
                        row,
                        model_result=model_result,
                        candidate=candidate,
                    )
                    conn.commit()
                    return {"report_version": closed, "created": True}
            except GitOperationInProgress as exc:
                raise _stored_invalid(
                    "ReportVersion source Git workspace 正在被另一操作使用，未执行 durable admission。"
                ) from exc
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise _stored_invalid("ReportVersion 并发写入冲突无法闭合。") from exc
    except sqlite3.Error as exc:
        raise _stored_invalid("ReportVersion 保存失败，数据库原状态未改变。") from exc


def _close_supplement(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != SUPPLEMENT_VERSION_SCHEMA_VERSION:
        raise _stored_invalid("SupplementVersion schema_version 无效。")
    for field in ("id", "report_version_id", "version_no"):
        if not _is_positive_int(value.get(field)):
            raise _stored_invalid("SupplementVersion 身份字段无效。")
    if not _is_nonnegative_int(value.get("expected_previous_version_no")):
        raise _stored_invalid("SupplementVersion expected_previous_version_no 无效。")
    if value["version_no"] != value["expected_previous_version_no"] + 1:
        raise _stored_invalid("SupplementVersion 版本链无法闭合。")
    content = value.get("content")
    if type(content) is not str or not content.strip() or "\x00" in content:
        raise _stored_invalid("SupplementVersion content 无效。")
    if _content_hash(content) != value.get("content_hash"):
        raise _stored_invalid("SupplementVersion content_hash 无法闭合。")
    if value.get("source_type") not in _SUPPLEMENT_SOURCE_TYPES:
        raise _stored_invalid("SupplementVersion source_type 无效。")
    for field in ("provided_by", "provided_at", "provided_timezone", "idempotency_key"):
        if type(value.get(field)) is not str or not value[field]:
            raise _stored_invalid("SupplementVersion provenance/idempotency 无效。")
    return {
        "supplement_version_id": value["id"],
        "schema_version": value["schema_version"],
        "report_version_id": value["report_version_id"],
        "version_no": value["version_no"],
        "expected_previous_version_no": value["expected_previous_version_no"],
        "content": value["content"],
        "content_hash": value["content_hash"],
        "source_type": value["source_type"],
        "provided_by": value["provided_by"],
        "provided_at": value["provided_at"],
        "provided_timezone": value["provided_timezone"],
        "idempotency_key": value["idempotency_key"],
    }


def _read_latest_supplement(
    conn: sqlite3.Connection, report_version_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT {_SUPPLEMENT_COLUMNS}
        FROM report_supplement_versions
        WHERE report_version_id = ?
        ORDER BY version_no DESC
        LIMIT 1
        """,
        (report_version_id,),
    ).fetchone()


def append_supplement_version(
    *,
    project_id: int,
    report_version_id: int,
    content: str,
    source_type: str,
    provided_by: str,
    provided_timezone: str,
    idempotency_key: str,
    expected_latest_supplement_version: int,
) -> dict[str, object]:
    """Append/replay one immutable PM supplement version; never updates AI/model ledgers."""
    project_id = _require_positive_id(project_id, "project_id")
    report_version_id = _require_positive_id(report_version_id, "report_version_id")
    content = _require_content(content)
    source_type = _require_identity_text(source_type, "source_type", max_length=64)
    if source_type not in _SUPPLEMENT_SOURCE_TYPES:
        raise _input_invalid("source_type 仅允许 pm_correction / pm_external_fact。")
    provided_by = _require_identity_text(provided_by, "provided_by", max_length=200)
    provided_timezone = _require_identity_text(
        provided_timezone, "provided_timezone", max_length=64
    )
    idempotency_key = _require_identity_text(
        idempotency_key, "idempotency_key", max_length=128
    )
    expected_latest_supplement_version = _require_nonnegative_version(
        expected_latest_supplement_version,
        "expected_latest_supplement_version",
    )
    report, _model_result, _candidate = _load_closed_report(report_version_id)
    if report["project_id"] != project_id:
        raise _report_not_found()
    if report["lifecycle"] not in _WRITABLE_SUPPLEMENT_LIFECYCLES:
        raise _supplement_not_writable()

    request_identity = {
        "content": content,
        "content_hash": _content_hash(content),
        "source_type": source_type,
        "provided_by": provided_by,
        "provided_timezone": provided_timezone,
        "idempotency_key": idempotency_key,
        "expected_previous_version_no": expected_latest_supplement_version,
    }

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            report_row = _read_report_by_id(conn, report_version_id)
            if report_row is None or report_row["project_id"] != project_id:
                raise _report_not_found()
            if report_row["lifecycle"] not in _WRITABLE_SUPPLEMENT_LIFECYCLES:
                raise _supplement_not_writable()

            replay = conn.execute(
                f"""
                SELECT {_SUPPLEMENT_COLUMNS}
                FROM report_supplement_versions
                WHERE report_version_id = ? AND idempotency_key = ?
                """,
                (report_version_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                closed = _close_supplement(replay)
                expected_replay = {
                    "content": closed["content"],
                    "content_hash": closed["content_hash"],
                    "source_type": closed["source_type"],
                    "provided_by": closed["provided_by"],
                    "provided_timezone": closed["provided_timezone"],
                    "idempotency_key": closed["idempotency_key"],
                    "expected_previous_version_no": closed[
                        "expected_previous_version_no"
                    ],
                }
                if expected_replay != request_identity:
                    raise _supplement_conflict()
                conn.commit()
                return {"supplement_version": closed, "created": False}

            latest = _read_latest_supplement(conn, report_version_id)
            latest_version = 0 if latest is None else latest["version_no"]
            if not _is_nonnegative_int(latest_version):
                raise _stored_invalid("当前 SupplementVersion version_no 无效。")
            if latest is not None:
                _close_supplement(latest)
            if latest_version != expected_latest_supplement_version:
                raise _supplement_stale()
            version_no = latest_version + 1
            if not _is_positive_int(version_no):
                raise _stored_invalid("SupplementVersion version_no 溢出。")
            provided_at = _now()
            cursor = conn.execute(
                """
                INSERT INTO report_supplement_versions (
                    schema_version, report_version_id, version_no,
                    expected_previous_version_no, content, content_hash, source_type,
                    provided_by, provided_at, provided_timezone, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SUPPLEMENT_VERSION_SCHEMA_VERSION,
                    report_version_id,
                    version_no,
                    expected_latest_supplement_version,
                    content,
                    request_identity["content_hash"],
                    source_type,
                    provided_by,
                    provided_at,
                    provided_timezone,
                    idempotency_key,
                ),
            )
            supplement_id = cursor.lastrowid
            if not _is_positive_int(supplement_id):
                raise _stored_invalid("SupplementVersion 写入后没有合法 id。")
            inserted = conn.execute(
                f"SELECT {_SUPPLEMENT_COLUMNS} "
                "FROM report_supplement_versions WHERE id = ?",
                (supplement_id,),
            ).fetchone()
            if inserted is None:
                raise _stored_invalid("SupplementVersion 写入后无法回读。")
            closed = _close_supplement(inserted)
            conn.commit()
            return {"supplement_version": closed, "created": True}
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise _stored_invalid("SupplementVersion append-only 写入失败。") from exc
    except sqlite3.Error as exc:
        raise _stored_invalid("SupplementVersion 保存失败，数据库原状态未改变。") from exc


def list_supplement_versions(
    *, project_id: int, report_version_id: int
) -> list[dict[str, object]]:
    """Read the complete append-only human supplement chain for one report."""
    project_id = _require_positive_id(project_id, "project_id")
    report_version_id = _require_positive_id(report_version_id, "report_version_id")
    report, _model_result, _candidate = _load_closed_report(report_version_id)
    if report["project_id"] != project_id:
        raise _report_not_found()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_SUPPLEMENT_COLUMNS}
                FROM report_supplement_versions
                WHERE report_version_id = ?
                ORDER BY version_no
                """,
                (report_version_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
    result = [_close_supplement(row) for row in rows]
    for expected, item in enumerate(result, start=1):
        if item["version_no"] != expected:
            raise _stored_invalid("SupplementVersion 历史链存在缺口。")
    return result


def _read_reanalysis_by_report(
    conn: sqlite3.Connection, report_version_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_REANALYSIS_COLUMNS} FROM report_reanalysis_requests "
        "WHERE report_version_id = ?",
        (report_version_id,),
    ).fetchone()


def _read_reanalysis_by_key(
    conn: sqlite3.Connection, project_id: int, idempotency_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_REANALYSIS_COLUMNS} FROM report_reanalysis_requests "
        "WHERE project_id = ? AND idempotency_key = ?",
        (project_id, idempotency_key),
    ).fetchone()


def _reanalysis_hash_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": value["schema_version"],
        "project_id": value["project_id"],
        "report_version_id": value["report_version_id"],
        "evidence_snapshot_id": value["evidence_snapshot_id"],
        "evidence_snapshot_hash": value["evidence_snapshot_hash"],
        "source_report_state_version": value["source_report_state_version"],
        "source_report_lifecycle": value["source_report_lifecycle"],
        "error_location_hash": value["error_location_hash"],
        "corrected_truth_hash": value["corrected_truth_hash"],
        "correction_basis_hash": value["correction_basis_hash"],
        "correction_source_hash": value["correction_source_hash"],
        "requested_by": value["requested_by"],
        "requested_at": value["requested_at"],
        "requested_timezone": value["requested_timezone"],
        "idempotency_key": value["idempotency_key"],
        "replacement_task_id": value["replacement_task_id"],
        "replacement_local_task_id": value["replacement_local_task_id"],
        "replacement_task_identity_hash": value["replacement_task_identity_hash"],
    }


def _close_reanalysis_request(
    row: sqlite3.Row | Mapping[str, object],
) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != REANALYSIS_REQUEST_SCHEMA_VERSION:
        raise _stored_invalid("ReanalysisRequest schema_version 无效。")
    for field in (
        "id",
        "project_id",
        "report_version_id",
        "evidence_snapshot_id",
        "source_report_state_version",
        "replacement_task_id",
    ):
        if not _is_positive_int(value.get(field)):
            raise _stored_invalid("ReanalysisRequest 身份字段无效。")
    if value.get("source_report_lifecycle") not in _REANALYSIS_SOURCE_LIFECYCLES:
        raise _stored_invalid("ReanalysisRequest source lifecycle 无效。")
    for text_field, hash_field in (
        ("error_location", "error_location_hash"),
        ("corrected_truth", "corrected_truth_hash"),
        ("correction_basis", "correction_basis_hash"),
        ("correction_source", "correction_source_hash"),
    ):
        text = value.get(text_field)
        if type(text) is not str or not text.strip() or "\x00" in text:
            raise _stored_invalid("ReanalysisRequest correction 内容无效。")
        if _content_hash(text) != value.get(hash_field):
            raise _stored_invalid("ReanalysisRequest correction hash 无法闭合。")
    for field in (
        "evidence_snapshot_hash",
        "replacement_task_identity_hash",
        "request_hash",
    ):
        if not _is_hash(value.get(field)):
            raise _stored_invalid("ReanalysisRequest 哈希字段无效。")
    for field in (
        "requested_by",
        "requested_at",
        "requested_timezone",
        "idempotency_key",
        "replacement_local_task_id",
    ):
        if type(value.get(field)) is not str or not value[field]:
            raise _stored_invalid("ReanalysisRequest provenance/task identity 无效。")
    try:
        expected_hash = _stable_hash(_reanalysis_hash_payload(value))
    except (KeyError, TypeError, ValueError) as exc:
        raise _stored_invalid("ReanalysisRequest request_hash 无法重算。") from exc
    if expected_hash != value["request_hash"]:
        raise _stored_invalid("ReanalysisRequest request_hash 无法闭合。")
    return {
        "reanalysis_request_id": value["id"],
        "schema_version": value["schema_version"],
        "project_id": value["project_id"],
        "report_version_id": value["report_version_id"],
        "evidence_snapshot_id": value["evidence_snapshot_id"],
        "evidence_snapshot_hash": value["evidence_snapshot_hash"],
        "source_report_state_version": value["source_report_state_version"],
        "source_report_lifecycle": value["source_report_lifecycle"],
        "error_location": value["error_location"],
        "error_location_hash": value["error_location_hash"],
        "corrected_truth": value["corrected_truth"],
        "corrected_truth_hash": value["corrected_truth_hash"],
        "correction_basis": value["correction_basis"],
        "correction_basis_hash": value["correction_basis_hash"],
        "correction_source": value["correction_source"],
        "correction_source_hash": value["correction_source_hash"],
        "requested_by": value["requested_by"],
        "requested_at": value["requested_at"],
        "requested_timezone": value["requested_timezone"],
        "idempotency_key": value["idempotency_key"],
        "replacement_task_id": value["replacement_task_id"],
        "replacement_local_task_id": value["replacement_local_task_id"],
        "replacement_task_identity_hash": value["replacement_task_identity_hash"],
        "request_hash": value["request_hash"],
    }


def _reanalysis_input_identity(
    *,
    report_version_id: int,
    evidence_snapshot_id: int,
    evidence_snapshot_hash: str,
    expected_report_state_version: int,
    error_location: str,
    corrected_truth: str,
    correction_basis: str,
    correction_source: str,
    requested_by: str,
    requested_timezone: str,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "report_version_id": report_version_id,
        "evidence_snapshot_id": evidence_snapshot_id,
        "evidence_snapshot_hash": evidence_snapshot_hash,
        "source_report_state_version": expected_report_state_version,
        "error_location": error_location,
        "error_location_hash": _content_hash(error_location),
        "corrected_truth": corrected_truth,
        "corrected_truth_hash": _content_hash(corrected_truth),
        "correction_basis": correction_basis,
        "correction_basis_hash": _content_hash(correction_basis),
        "correction_source": correction_source,
        "correction_source_hash": _content_hash(correction_source),
        "requested_by": requested_by,
        "requested_timezone": requested_timezone,
        "idempotency_key": idempotency_key,
    }


def _assert_reanalysis_replay(
    *,
    request: Mapping[str, object],
    expected: Mapping[str, object],
    current_report: Mapping[str, object],
) -> None:
    for field, expected_value in expected.items():
        if request.get(field) != expected_value:
            raise _reanalysis_conflict()
    source_state = request["source_report_state_version"]
    from app.report_reanalysis_cancellation import read_cancellation
    with get_connection() as conn:
        cancellation = read_cancellation(conn, request["report_version_id"])
    if cancellation is not None:
        if (cancellation["request_hash"] != request["request_hash"]
                or cancellation["replacement_task_identity_hash"] != request["replacement_task_identity_hash"]
                or cancellation["source_report_state_version_before"] != source_state + 1
                or cancellation["source_report_state_version_after"] != source_state + 2
                or current_report.get("state_version", 0) < source_state + 2
                or current_report.get("evidence_snapshot_id") != request["evidence_snapshot_id"]
                or current_report.get("evidence_snapshot_hash") != request["evidence_snapshot_hash"]):
            raise _stored_invalid("撤销后的 ReanalysisRequest 历史链无法闭合。")
        return
    if (
        current_report.get("lifecycle") != "superseded"
        or current_report.get("state_version") != source_state + 1
        or current_report.get("evidence_snapshot_id") != request["evidence_snapshot_id"]
        or current_report.get("evidence_snapshot_hash")
        != request["evidence_snapshot_hash"]
    ):
        raise _stored_invalid("ReanalysisRequest 与 superseded ReportVersion 状态链无法闭合。")


def create_reanalysis_request(
    *,
    project_id: int,
    report_version_id: int,
    error_location: str,
    corrected_truth: str,
    correction_basis: str,
    correction_source: str,
    requested_by: str,
    requested_timezone: str,
    idempotency_key: str,
    expected_report_state_version: int,
) -> dict[str, object]:
    """Atomically persist correction + regenerate logical task + explicit supersede transition.

    This Slice 2 function never executes the queued task and never calls a model/provider.
    """
    project_id = _require_positive_id(project_id, "project_id")
    report_version_id = _require_positive_id(report_version_id, "report_version_id")
    error_location = _require_content(error_location, "error_location")
    corrected_truth = _require_content(corrected_truth, "corrected_truth")
    correction_basis = _require_content(correction_basis, "correction_basis")
    correction_source = _require_content(correction_source, "correction_source")
    requested_by = _require_identity_text(requested_by, "requested_by", max_length=200)
    requested_timezone = _require_identity_text(
        requested_timezone, "requested_timezone", max_length=64
    )
    idempotency_key = _require_identity_text(
        idempotency_key, "idempotency_key", max_length=128
    )
    expected_report_state_version = _require_positive_id(
        expected_report_state_version, "expected_report_state_version"
    )

    report, model_result, initial_candidate = _load_closed_report(report_version_id)
    if report["project_id"] != project_id:
        raise _report_not_found()
    expected_input = _reanalysis_input_identity(
        report_version_id=report_version_id,
        evidence_snapshot_id=report["evidence_snapshot_id"],
        evidence_snapshot_hash=report["evidence_snapshot_hash"],
        expected_report_state_version=expected_report_state_version,
        error_location=error_location,
        corrected_truth=corrected_truth,
        correction_basis=correction_basis,
        correction_source=correction_source,
        requested_by=requested_by,
        requested_timezone=requested_timezone,
        idempotency_key=idempotency_key,
    )

    replay_request: dict[str, object] | None = None
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                with project_workspace_lock(project_id):
                    candidate = _verified_candidate(
                        report["evidence_snapshot_id"],
                        message="ReanalysisRequest source EvidenceSnapshot 无法完成 authoritative verified read。",
                    )
                    if (
                        candidate.get("project_id") != project_id
                        or candidate.get("snapshot_id") != report["evidence_snapshot_id"]
                        or candidate.get("snapshot_hash") != report["evidence_snapshot_hash"]
                        or candidate.get("snapshot_hash") != initial_candidate.get("snapshot_hash")
                    ):
                        raise _stored_invalid(
                            "ReanalysisRequest 必须严格绑定 source ReportVersion 的同一 EvidenceSnapshot。"
                        )

                    report_row = _read_report_by_id(conn, report_version_id)
                    if report_row is None or report_row["project_id"] != project_id:
                        raise _report_not_found()
                    current_report = _close_report_row(
                        report_row,
                        model_result=model_result,
                        candidate=candidate,
                    )

                    by_report = _read_reanalysis_by_report(conn, report_version_id)
                    by_key = _read_reanalysis_by_key(conn, project_id, idempotency_key)
                    if by_report is not None or by_key is not None:
                        if by_report is None or by_key is None or by_report["id"] != by_key["id"]:
                            raise _reanalysis_conflict()
                        replay_request = _close_reanalysis_request(by_report)
                        _assert_reanalysis_replay(
                            request=replay_request,
                            expected=expected_input,
                            current_report=current_report,
                        )
                        conn.commit()
                    else:
                        if current_report["lifecycle"] not in _REANALYSIS_SOURCE_LIFECYCLES:
                            raise _reanalysis_not_allowed()
                        if current_report["state_version"] != expected_report_state_version:
                            raise _reanalysis_stale()

                        digest = hashlib.sha256(
                            (
                                f"{project_id}:{report_version_id}:"
                                f"{idempotency_key}:{report['evidence_snapshot_hash']}"
                            ).encode("utf-8")
                        ).hexdigest()
                        local_task_id = f"report-reanalysis-{report_version_id}-{digest[:24]}"
                        create_key = f"reanalyze-{report_version_id}-{digest}"

                        replacement_task = (
                            report_generation_tasks.create_report_regeneration_task_in_transaction(
                                conn=conn,
                                project_id=project_id,
                                local_task_id=local_task_id,
                                evidence_snapshot_id=report["evidence_snapshot_id"],
                                create_key=create_key,
                            )
                        )
                        if (
                            replacement_task.get("task_type")
                            != report_generation_tasks.REGENERATE_TASK_TYPE
                            or replacement_task.get("evidence_snapshot_id")
                            != report["evidence_snapshot_id"]
                            or not _is_hash(replacement_task.get("identity_hash"))
                        ):
                            raise _stored_invalid(
                                "replacement logical task 未闭合到 same EvidenceSnapshot regenerate identity。"
                            )

                        requested_at = _now()
                        stored: dict[str, object] = {
                            "schema_version": REANALYSIS_REQUEST_SCHEMA_VERSION,
                            "project_id": project_id,
                            "report_version_id": report_version_id,
                            "evidence_snapshot_id": report["evidence_snapshot_id"],
                            "evidence_snapshot_hash": report["evidence_snapshot_hash"],
                            "source_report_state_version": current_report["state_version"],
                            "source_report_lifecycle": current_report["lifecycle"],
                            "error_location": error_location,
                            "error_location_hash": expected_input["error_location_hash"],
                            "corrected_truth": corrected_truth,
                            "corrected_truth_hash": expected_input["corrected_truth_hash"],
                            "correction_basis": correction_basis,
                            "correction_basis_hash": expected_input["correction_basis_hash"],
                            "correction_source": correction_source,
                            "correction_source_hash": expected_input["correction_source_hash"],
                            "requested_by": requested_by,
                            "requested_at": requested_at,
                            "requested_timezone": requested_timezone,
                            "idempotency_key": idempotency_key,
                            "replacement_task_id": replacement_task["id"],
                            "replacement_local_task_id": replacement_task["local_task_id"],
                            "replacement_task_identity_hash": replacement_task["identity_hash"],
                        }
                        stored["request_hash"] = _stable_hash(
                            _reanalysis_hash_payload(stored)
                        )
                        cursor = conn.execute(
                            """
                            INSERT INTO report_reanalysis_requests (
                                schema_version, project_id, report_version_id,
                                evidence_snapshot_id, evidence_snapshot_hash,
                                source_report_state_version, source_report_lifecycle,
                                error_location, error_location_hash,
                                corrected_truth, corrected_truth_hash,
                                correction_basis, correction_basis_hash,
                                correction_source, correction_source_hash,
                                requested_by, requested_at, requested_timezone,
                                idempotency_key, replacement_task_id,
                                replacement_local_task_id,
                                replacement_task_identity_hash, request_hash
                            ) VALUES (
                                :schema_version, :project_id, :report_version_id,
                                :evidence_snapshot_id, :evidence_snapshot_hash,
                                :source_report_state_version, :source_report_lifecycle,
                                :error_location, :error_location_hash,
                                :corrected_truth, :corrected_truth_hash,
                                :correction_basis, :correction_basis_hash,
                                :correction_source, :correction_source_hash,
                                :requested_by, :requested_at, :requested_timezone,
                                :idempotency_key, :replacement_task_id,
                                :replacement_local_task_id,
                                :replacement_task_identity_hash, :request_hash
                            )
                            """,
                            stored,
                        )
                        request_id = cursor.lastrowid
                        if not _is_positive_int(request_id):
                            raise _stored_invalid("ReanalysisRequest INSERT 未返回有效 id。")

                        transitioned = conn.execute(
                            """
                            UPDATE report_versions
                            SET lifecycle = 'superseded', state_version = state_version + 1
                            WHERE id = ? AND project_id = ?
                              AND lifecycle IN ('pending_review', 'blocked')
                              AND state_version = ?
                            """,
                            (
                                report_version_id,
                                project_id,
                                expected_report_state_version,
                            ),
                        )
                        if transitioned.rowcount != 1:
                            raise _reanalysis_stale()

                        inserted_request = conn.execute(
                            f"SELECT {_REANALYSIS_COLUMNS} "
                            "FROM report_reanalysis_requests WHERE id = ?",
                            (request_id,),
                        ).fetchone()
                        updated_report_row = _read_report_by_id(conn, report_version_id)
                        if inserted_request is None or updated_report_row is None:
                            raise _stored_invalid(
                                "ReanalysisRequest/ReportVersion transition 后无法回读。"
                            )
                        closed_request = _close_reanalysis_request(inserted_request)
                        updated_report = _close_report_row(
                            updated_report_row,
                            model_result=model_result,
                            candidate=candidate,
                        )
                        _assert_reanalysis_replay(
                            request=closed_request,
                            expected=expected_input,
                            current_report=updated_report,
                        )
                        conn.commit()
                        return {
                            "reanalysis_request": closed_request,
                            "replacement_task": replacement_task,
                            "source_report_version": updated_report,
                            "created": True,
                        }
            except GitOperationInProgress as exc:
                raise _stored_invalid(
                    "ReanalysisRequest source Git workspace 正在被另一操作使用，未执行 durable admission。"
                ) from exc
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise _stored_invalid("ReanalysisRequest 原子写入冲突无法闭合。") from exc
    except sqlite3.Error as exc:
        raise _stored_invalid(
            "ReanalysisRequest 保存失败，task/request/report 原状态已回滚。"
        ) from exc

    if replay_request is None:
        raise _stored_invalid("ReanalysisRequest replay 分类未闭合。")
    replacement_task = report_generation_tasks.get_report_regeneration_task(
        project_id=project_id,
        local_task_id=replay_request["replacement_local_task_id"],
    )
    source_report, _model_result, _candidate = _load_closed_report(report_version_id)
    if (
        replacement_task.get("id") != replay_request["replacement_task_id"]
        or replacement_task.get("identity_hash")
        != replay_request["replacement_task_identity_hash"]
        or replacement_task.get("evidence_snapshot_id")
        != replay_request["evidence_snapshot_id"]
        or replacement_task.get("task_type")
        != report_generation_tasks.REGENERATE_TASK_TYPE
    ):
        raise _stored_invalid("ReanalysisRequest replacement task durable binding 无法闭合。")
    _assert_reanalysis_replay(
        request=replay_request,
        expected=expected_input,
        current_report=source_report,
    )
    return {**get_reanalysis_request(project_id=project_id, report_version_id=report_version_id), "created": False}


def get_reanalysis_request(
    *, project_id: int, report_version_id: int
) -> dict[str, object]:
    """Read one durable ReanalysisRequest and verify its source/task bindings."""
    project_id = _require_positive_id(project_id, "project_id")
    report_version_id = _require_positive_id(report_version_id, "report_version_id")
    source_report, _model_result, _candidate = _load_closed_report(report_version_id)
    if source_report["project_id"] != project_id:
        raise _report_not_found()
    try:
        with get_connection() as conn:
            row = _read_reanalysis_by_report(conn, report_version_id)
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
    if row is None:
        raise _error(404, "REPORT_REANALYSIS_NOT_FOUND", "指定的 ReanalysisRequest 不存在。")
    request = _close_reanalysis_request(row)
    replacement_task = report_generation_tasks.get_report_regeneration_task(
        project_id=project_id,
        local_task_id=request["replacement_local_task_id"],
    )
    if (
        replacement_task.get("id") != request["replacement_task_id"]
        or replacement_task.get("identity_hash")
        != request["replacement_task_identity_hash"]
        or replacement_task.get("evidence_snapshot_id")
        != request["evidence_snapshot_id"]
        or replacement_task.get("task_type")
        != report_generation_tasks.REGENERATE_TASK_TYPE
    ):
        raise _stored_invalid("ReanalysisRequest replacement task durable binding 无法闭合。")
    _assert_reanalysis_replay(
        request=request,
        expected={
            "report_version_id": report_version_id,
            "evidence_snapshot_id": source_report["evidence_snapshot_id"],
            "evidence_snapshot_hash": source_report["evidence_snapshot_hash"],
            "source_report_state_version": request["source_report_state_version"],
            "error_location": request["error_location"],
            "error_location_hash": request["error_location_hash"],
            "corrected_truth": request["corrected_truth"],
            "corrected_truth_hash": request["corrected_truth_hash"],
            "correction_basis": request["correction_basis"],
            "correction_basis_hash": request["correction_basis_hash"],
            "correction_source": request["correction_source"],
            "correction_source_hash": request["correction_source_hash"],
            "requested_by": request["requested_by"],
            "requested_timezone": request["requested_timezone"],
            "idempotency_key": request["idempotency_key"],
        },
        current_report=source_report,
    )
    from app.report_reanalysis_cancellation import read_cancellation
    with get_connection() as conn:
        cancellation = read_cancellation(conn, report_version_id)
    return {
        "reanalysis_request": request,
        "replacement_task": replacement_task,
        "source_report_version": source_report,
        "cancellation": cancellation,
        "cancelled": cancellation is not None,
    }


def get_review_bundle(
    *, project_id: int, report_version_id: int
) -> dict[str, object]:
    """Build one coherent read-only Page 07 bundle from existing verified read authority."""
    project_id = _require_positive_id(project_id, "project_id")
    report_version_id = _require_positive_id(report_version_id, "report_version_id")
    report, model_result, candidate = _load_closed_report(report_version_id)
    if report["project_id"] != project_id:
        raise _report_not_found()
    try:
        with get_connection() as conn:
            latest = _read_latest_supplement(conn, report_version_id)
            current_supplement = None if latest is None else _close_supplement(latest)
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc

    evidence_refs = _candidate_evidence_refs(
        candidate=candidate,
        ai_content=model_result["validated_result"],
    )
    git_facts = _candidate_git_facts(candidate)
    ai_raw = {
        "model_execution_result_id": model_result["model_result_id"],
        "execution_result_hash": model_result["execution_result_hash"],
        "formal_response_hash": model_result["formal_response_hash"],
        "validated_result_hash": model_result["validated_result_hash"],
        "model_call_id": model_result["model_call_id"],
        "call_identity_hash": model_result["call_identity_hash"],
        "snapshot_id": model_result["snapshot_id"],
        "local_task_id": model_result["local_task_id"],
        "task_type": model_result["task_type"],
        "provider": model_result["provider"],
        "model_id": model_result["model_id"],
        "model_version": model_result["model_version"],
        "actual_model": model_result["actual_model"],
        "provider_runtime_fingerprint": model_result["provider_runtime_fingerprint"],
        "content": model_result["validated_result"],
    }
    return {
        "schema_version": REVIEW_BUNDLE_SCHEMA_VERSION,
        "project_id": project_id,
        "report_version": report,
        "ai_raw": ai_raw,
        "evidence_snapshot": {
            "snapshot_id": candidate["snapshot_id"],
            "snapshot_hash": candidate["snapshot_hash"],
        },
        "git_facts": git_facts,
        "evidence_refs": evidence_refs,
        "current_supplement": current_supplement,
    }


def get_current_review_bundle(*, project_id: int) -> dict[str, object]:
    """Read the highest immutable ReportVersion for a project; never materializes on GET."""
    project_id = _require_positive_id(project_id, "project_id")
    try:
        with get_connection() as conn:
            report_version_id = _read_current_report_id(conn, project_id)
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
    if report_version_id is None:
        raise _report_not_found()
    return get_review_bundle(
        project_id=project_id,
        report_version_id=report_version_id,
    )
