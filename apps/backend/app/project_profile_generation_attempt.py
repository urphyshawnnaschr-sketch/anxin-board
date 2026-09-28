"""Durable at-most-once guard for pre-profile AI generation.

Project Profile generation necessarily happens before the formal EvidenceSnapshot/ModelCall
chain exists. This small ledger closes the same transport safety invariant at that earlier
boundary: one Local-Idempotency-Key can consume provider-send authority at most once, a
successful response is replayable, and an ambiguous transport outcome is durably UNKNOWN.
It does not own provider transport, credentials, prompt construction, or candidate storage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import TypeVar

from fastapi import HTTPException
from app.profile_response_errors import PROFILE_RESPONSE_ERROR_CODES

from app.db import get_connection
from app.local_session_guard import LocalSessionGuardError, validate_idempotency_key
from app.profile_generation_failure import ProfileGenerationPhaseError


SCHEMA_VERSION = "project_profile_generation_attempt_v1"
_STATUS_CLAIMED = "claimed"
_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED_PRE_SEND = "failed_pre_send"
_STATUS_FAILED_AFTER_SEND = "failed_after_send"
_STATUS_UNKNOWN = "unknown"
_TERMINAL = {
    _STATUS_SUCCEEDED,
    _STATUS_FAILED_PRE_SEND,
    _STATUS_FAILED_AFTER_SEND,
    _STATUS_UNKNOWN,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
T = TypeVar("T")

_SAFE_PRE_SEND_CODES = {
    "PROFILE_GENERATION_AUTHORIZATION_REQUIRED",
    "PROJECT_NOT_FOUND",
    "PROFILE_GENERATION_PRD_REQUIRED",
    "PROFILE_GENERATION_INPUT_READ_FAILED",
    "PROFILE_GENERATION_GIT_CHECK_REQUIRED",
    "PROFILE_GENERATION_PRD_INVALID",
    "PROFILE_GENERATION_GIT_WORKSPACE_REQUIRED",
    "PROFILE_GENERATION_CONTEXT_INVALID",
    "PROFILE_GENERATION_PROVIDER_INVALID",
    "PROFILE_GENERATION_AUTHORIZATION_SAVE_FAILED",
    "PROFILE_GENERATION_CREDENTIAL_REQUIRED",
    "PROFILE_GENERATION_CREDENTIAL_UNSUPPORTED",
    "PROFILE_GENERATION_CREDENTIAL_ACCESS_DENIED",
    "PROFILE_GENERATION_CREDENTIAL_READ_FAILED",
}

_KNOWN_AFTER_SEND_CODES = {
    "PROFILE_GENERATION_MODEL_IDENTITY_MISMATCH",
    "PROFILE_GENERATION_AI_RESULT_INVALID",
    "PROFILE_GENERATION_SOURCE_CHANGED",
    "PROFILE_GENERATION_SAVE_FAILED",
} | PROFILE_RESPONSE_ERROR_CODES

_UNKNOWN_MESSAGE = (
    "本次模型请求结果不确定，系统不会自动重发。请先检查项目档案是否已经出现候选；"
    "若仍需重新生成，请由项目经理明确发起一次新的调用。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_hash(*, project_id: int, key_hash: str, created_at: str) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "idempotency_key_hash": key_hash,
                "created_at": created_at,
            }
        )
    )


def ensure_profile_generation_attempt_schema() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile_generation_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                idempotency_key_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (
                    status IN ('claimed', 'succeeded', 'failed_pre_send', 'failed_after_send', 'unknown')
                ),
                response_json TEXT,
                response_hash TEXT,
                error_status INTEGER,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                attempt_identity_hash TEXT NOT NULL UNIQUE
            );

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_attempt_identity_immutable
            BEFORE UPDATE ON profile_generation_attempts
            WHEN
                OLD.schema_version <> NEW.schema_version
                OR OLD.project_id <> NEW.project_id
                OR OLD.idempotency_key_hash <> NEW.idempotency_key_hash
                OR OLD.created_at <> NEW.created_at
                OR OLD.attempt_identity_hash <> NEW.attempt_identity_hash
            BEGIN
                SELECT RAISE(ABORT, 'profile generation attempt identity is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_attempt_terminal_once
            BEFORE UPDATE ON profile_generation_attempts
            WHEN
                OLD.status <> 'claimed'
                OR NEW.status NOT IN ('succeeded', 'failed_pre_send', 'failed_after_send', 'unknown')
            BEGIN
                SELECT RAISE(ABORT, 'profile generation attempt has one terminal transition');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_attempt_no_delete
            BEFORE DELETE ON profile_generation_attempts
            BEGIN
                SELECT RAISE(ABORT, 'profile generation attempts are durable');
            END;
            """
        )


def _close_row(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成尝试记录无法自校验。")
    project_id = value.get("project_id")
    key_hash = value.get("idempotency_key_hash")
    created_at = value.get("created_at")
    if type(project_id) is not int or project_id <= 0:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成尝试记录无法自校验。")
    if type(key_hash) is not str or _SHA256_RE.fullmatch(key_hash) is None:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成尝试记录无法自校验。")
    if type(created_at) is not str or not created_at:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成尝试记录无法自校验。")
    if value.get("attempt_identity_hash") != _identity_hash(
        project_id=project_id, key_hash=key_hash, created_at=created_at
    ):
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成尝试身份哈希不一致。")

    status = value.get("status")
    if status == _STATUS_CLAIMED:
        if any(
            value.get(field) is not None
            for field in (
                "response_json",
                "response_hash",
                "error_status",
                "error_code",
                "error_message",
                "finished_at",
            )
        ):
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成 claimed 记录包含终态字段。")
        return value

    if status not in _TERMINAL or type(value.get("finished_at")) is not str or not value["finished_at"]:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成尝试终态无效。")

    if status == _STATUS_SUCCEEDED:
        raw = value.get("response_json")
        digest = value.get("response_hash")
        if type(raw) is not str or type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成成功回执无效。")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成成功回执无效。") from exc
        if type(parsed) is not dict or _canonical_json(parsed) != raw or _sha256_text(raw) != digest:
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成成功回执完整性失败。")
        if any(value.get(field) is not None for field in ("error_status", "error_code", "error_message")):
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成成功记录混入错误字段。")
        value["_response"] = parsed
        return value

    if value.get("response_json") is not None or value.get("response_hash") is not None:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成失败记录混入成功回执。")
    error_status = value.get("error_status")
    if type(error_status) is not int or not 400 <= error_status <= 599:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成失败状态码无效。")
    for field in ("error_code", "error_message"):
        if type(value.get(field)) is not str or not value[field]:
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_STORED_INVALID", "候选生成失败记录不完整。")
    return value


def _read_by_key(conn: sqlite3.Connection, key_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, schema_version, project_id, idempotency_key_hash, status,
               response_json, response_hash, error_status, error_code, error_message,
               created_at, finished_at, attempt_identity_hash
        FROM profile_generation_attempts
        WHERE idempotency_key_hash = ?
        """,
        (key_hash,),
    ).fetchone()


def _claim(project_id: int, key_hash: str) -> tuple[dict[str, object], bool]:
    created_at = _now()
    attempt_hash = _identity_hash(
        project_id=project_id, key_hash=key_hash, created_at=created_at
    )
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO profile_generation_attempts (
                schema_version, project_id, idempotency_key_hash, status,
                created_at, attempt_identity_hash
            ) VALUES (?, ?, ?, 'claimed', ?, ?)
            """,
            (SCHEMA_VERSION, project_id, key_hash, created_at, attempt_hash),
        )
        inserted = cursor.rowcount == 1
        row = _read_by_key(conn, key_hash)
        conn.commit()
    if row is None:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_CLAIM_FAILED", "无法形成候选生成一次性执行记录。")
    return _close_row(row), inserted


def _finish_success(attempt_id: int, response: Mapping[str, object]) -> dict[str, object]:
    try:
        normalized = json.loads(_canonical_json(dict(response)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_RESPONSE_INVALID", "候选生成成功响应无法安全持久化。") from exc
    raw = _canonical_json(normalized)
    digest = _sha256_text(raw)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE profile_generation_attempts
            SET status = 'succeeded', response_json = ?, response_hash = ?, finished_at = ?
            WHERE id = ? AND status = 'claimed'
            """,
            (raw, digest, _now(), attempt_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_FINALIZE_FAILED", "候选生成执行记录无法安全收口。")
        row = conn.execute(
            """
            SELECT id, schema_version, project_id, idempotency_key_hash, status,
                   response_json, response_hash, error_status, error_code, error_message,
                   created_at, finished_at, attempt_identity_hash
            FROM profile_generation_attempts WHERE id = ?
            """,
            (attempt_id,),
        ).fetchone()
        conn.commit()
    if row is None:
        raise _error(500, "PROFILE_GENERATION_ATTEMPT_FINALIZE_FAILED", "候选生成执行记录无法安全收口。")
    return _close_row(row)["_response"]


def _finish_error(
    attempt_id: int,
    *,
    status: str,
    error_status: int,
    error_code: str,
    error_message: str,
) -> None:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE profile_generation_attempts
            SET status = ?, error_status = ?, error_code = ?, error_message = ?, finished_at = ?
            WHERE id = ? AND status = 'claimed'
            """,
            (status, error_status, error_code, error_message, _now(), attempt_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise _error(500, "PROFILE_GENERATION_ATTEMPT_FINALIZE_FAILED", "候选生成执行记录无法安全收口。")
        conn.commit()


def _stored_error(attempt: Mapping[str, object]) -> HTTPException:
    status = attempt["status"]
    if status in {_STATUS_CLAIMED, _STATUS_UNKNOWN}:
        stored_message = attempt.get("error_message")
        message = stored_message if status == _STATUS_UNKNOWN and type(stored_message) is str and stored_message else _UNKNOWN_MESSAGE
        return _error(409, "PROFILE_GENERATION_RESULT_UNKNOWN", message)
    return _error(
        int(attempt["error_status"]),
        str(attempt["error_code"]),
        str(attempt["error_message"]),
    )


def execute_profile_generation_once(
    *,
    project_id: int,
    idempotency_key: str,
    operation: Callable[[], dict[str, object]],
) -> dict[str, object]:
    """Run *operation* at most once for one durable local idempotency key."""

    if type(project_id) is not int or project_id <= 0:
        raise _error(400, "PROFILE_GENERATION_ATTEMPT_INPUT_INVALID", "project_id 必须是正整数。")
    try:
        canonical_key = validate_idempotency_key(idempotency_key, required=True)
    except LocalSessionGuardError as exc:
        raise _error(400, "PROFILE_GENERATION_IDEMPOTENCY_KEY_INVALID", "本次候选生成缺少合法的一次性请求标识。") from exc
    if canonical_key is None:
        raise _error(400, "PROFILE_GENERATION_IDEMPOTENCY_KEY_INVALID", "本次候选生成缺少合法的一次性请求标识。")

    ensure_profile_generation_attempt_schema()
    key_hash = _sha256_text(canonical_key)
    attempt, inserted = _claim(project_id, key_hash)
    if attempt["project_id"] != project_id:
        raise _error(409, "PROFILE_GENERATION_IDEMPOTENCY_CONFLICT", "一次性请求标识已绑定其他项目。")
    if not inserted:
        if attempt["status"] == _STATUS_SUCCEEDED:
            return dict(attempt["_response"])
        raise _stored_error(attempt)

    attempt_id = int(attempt["id"])
    try:
        response = operation()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        code = detail.get("code")
        message = detail.get("message")
        safe_code = code if type(code) is str and code else "PROFILE_GENERATION_FAILED"
        safe_message = message if type(message) is str and message else "候选生成失败。"
        if isinstance(exc, ProfileGenerationPhaseError) or safe_code in _SAFE_PRE_SEND_CODES:
            _finish_error(
                attempt_id,
                status=_STATUS_FAILED_PRE_SEND,
                error_status=exc.status_code,
                error_code=safe_code,
                error_message=safe_message,
            )
            raise
        if safe_code in _KNOWN_AFTER_SEND_CODES:
            _finish_error(
                attempt_id,
                status=_STATUS_FAILED_AFTER_SEND,
                error_status=exc.status_code,
                error_code=safe_code,
                error_message=safe_message,
            )
            raise
        unknown_message = (
            safe_message
            if safe_code == "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN"
            else _UNKNOWN_MESSAGE
        )
        _finish_error(
            attempt_id,
            status=_STATUS_UNKNOWN,
            error_status=409,
            error_code="PROFILE_GENERATION_RESULT_UNKNOWN",
            error_message=unknown_message,
        )
        raise _error(409, "PROFILE_GENERATION_RESULT_UNKNOWN", unknown_message) from exc
    except Exception as exc:
        _finish_error(
            attempt_id,
            status=_STATUS_UNKNOWN,
            error_status=409,
            error_code="PROFILE_GENERATION_RESULT_UNKNOWN",
            error_message=_UNKNOWN_MESSAGE,
        )
        raise _error(409, "PROFILE_GENERATION_RESULT_UNKNOWN", _UNKNOWN_MESSAGE) from exc

    if type(response) is not dict:
        _finish_error(
            attempt_id,
            status=_STATUS_UNKNOWN,
            error_status=409,
            error_code="PROFILE_GENERATION_RESULT_UNKNOWN",
            error_message=_UNKNOWN_MESSAGE,
        )
        raise _error(409, "PROFILE_GENERATION_RESULT_UNKNOWN", _UNKNOWN_MESSAGE)
    return _finish_success(attempt_id, response)
