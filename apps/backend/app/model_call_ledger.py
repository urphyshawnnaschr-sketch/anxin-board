"""Model Call Ledger V1：在任何真实 provider 调用前冻结可审计的 preparation identity。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.ai_contract_validation import _validate_authorization, _validate_qualification
from app.ai_contracts import (
    AUTHORIZATION_FIELDS,
    QUALIFICATION_FIELDS,
    TASK_TYPES,
    ModelQualificationRecord,
)
from app.context_candidate_runtime import build_context_candidate_set
from app.db import get_connection


SCHEMA_VERSION = "model_call_ledger_v1"
_MAX_TASK_ID_LENGTH = 128
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_CALL_COLUMNS = (
    "id, schema_version, project_id, local_task_id, call_prepare_key, snapshot_id, "
    "snapshot_hash, candidate_set_hash, task_type, provider, model_id, model_version, "
    "rule_version, output_schema_version, benchmark_sample_pack_version, "
    "qualification_status, qualification_hash, authorization_provider, "
    "authorization_authorized, authorization_valid, authorization_hash, created_at, "
    "call_identity_hash"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "Model Call preparation 输入无效。") -> HTTPException:
    return _error(400, "MODEL_CALL_INPUT_INVALID", message)


def _idempotency_conflict() -> HTTPException:
    return _error(
        409,
        "MODEL_CALL_IDEMPOTENCY_CONFLICT",
        "同一项目内 call_prepare_key 已绑定不同的不可变 model-call identity。",
    )


def _ledger_invalid(message: str = "Model Call Ledger 冻结身份无法完成自校验。") -> HTTPException:
    return _error(409, "MODEL_CALL_LEDGER_INVALID", message)


def _not_found() -> HTTPException:
    return _error(404, "MODEL_CALL_NOT_FOUND", "指定的 model_call_id 不存在。")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Mapping[str, object] | dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Mapping[str, object] | dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_non_empty_string(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _validate_direct_inputs(
    *,
    local_task_id: object,
    call_prepare_key: object,
    snapshot_id: object,
    task_type: object,
    provider: object,
    model_id: object,
    model_version: object,
    rule_version: object,
    output_schema_version: object,
    benchmark_sample_pack_version: object,
) -> None:
    if type(snapshot_id) is not int or snapshot_id <= 0:
        raise _input_invalid("snapshot_id 必须是正整数。")
    for field_name, value in (
        ("local_task_id", local_task_id),
        ("call_prepare_key", call_prepare_key),
    ):
        if (
            not _is_non_empty_string(value)
            or len(value) > _MAX_TASK_ID_LENGTH
        ):
            raise _input_invalid(f"{field_name} 必须是长度不超过 128 的非空字符串。")
    if type(task_type) is not str or task_type not in TASK_TYPES:
        raise _input_invalid("task_type 未在当前 AI contract 中登记。")
    for field_name, value in (
        ("provider", provider),
        ("model_id", model_id),
        ("model_version", model_version),
        ("rule_version", rule_version),
        ("output_schema_version", output_schema_version),
        ("benchmark_sample_pack_version", benchmark_sample_pack_version),
    ):
        if not _is_non_empty_string(value):
            raise _input_invalid(f"{field_name} 必须是非空字符串。")


def _validated_qualification(
    record: Mapping[str, Any] | ModelQualificationRecord | None,
    *,
    provider: str,
    model_id: str,
    model_version: str,
    rule_version: str,
    output_schema_version: str,
    benchmark_sample_pack_version: str,
) -> dict[str, object]:
    expected = {
        "provider": provider,
        "model_id": model_id,
        "model_version": model_version,
        "rule_version": rule_version,
        "output_schema_version": output_schema_version,
        "benchmark_sample_pack_version": benchmark_sample_pack_version,
        "qualification_status": "qualified",
    }
    # Intentionally reuse the centralized closed-world validator; do not duplicate a second rule set.
    issues = _validate_qualification(record, expected)
    if issues:
        first = sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))[0]
        raise _error(409, "AI_MODEL_NOT_QUALIFIED", first.message)
    if isinstance(record, ModelQualificationRecord):
        mapping = asdict(record)
    else:
        mapping = dict(record or {})
    return {field: mapping[field] for field in QUALIFICATION_FIELDS}


def _validated_authorization(authorization: Any, *, provider: str) -> dict[str, object]:
    # Intentionally reuse the centralized strict-bool/provider validator.
    issues = _validate_authorization(authorization, provider)
    if issues:
        first = sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))[0]
        raise _error(409, "AI_DATA_AUTHORIZATION_REQUIRED", first.message)
    mapping = dict(authorization)
    return {field: mapping[field] for field in AUTHORIZATION_FIELDS}


def _qualification_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "provider": row["provider"],
        "model_id": row["model_id"],
        "model_version": row["model_version"],
        "rule_version": row["rule_version"],
        "output_schema_version": row["output_schema_version"],
        "benchmark_sample_pack_version": row["benchmark_sample_pack_version"],
        "qualification_status": row["qualification_status"],
    }


def _authorization_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "provider": row["authorization_provider"],
        "authorized": row["authorization_authorized"] == 1,
        "valid": row["authorization_valid"] == 1,
    }


def _call_identity_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": row["schema_version"],
        "project_id": row["project_id"],
        "local_task_id": row["local_task_id"],
        "call_prepare_key": row["call_prepare_key"],
        "snapshot_id": row["snapshot_id"],
        "snapshot_hash": row["snapshot_hash"],
        "candidate_set_hash": row["candidate_set_hash"],
        "task_type": row["task_type"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "model_version": row["model_version"],
        "rule_version": row["rule_version"],
        "output_schema_version": row["output_schema_version"],
        "benchmark_sample_pack_version": row["benchmark_sample_pack_version"],
        "qualification_hash": row["qualification_hash"],
        "authorization_hash": row["authorization_hash"],
    }


def _read_by_key(
    conn: sqlite3.Connection, *, project_id: int, call_prepare_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_MODEL_CALL_COLUMNS} FROM model_calls "
        "WHERE project_id = ? AND call_prepare_key = ?",
        (project_id, call_prepare_key),
    ).fetchone()


def _read_by_id(conn: sqlite3.Connection, model_call_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_MODEL_CALL_COLUMNS} FROM model_calls WHERE id = ?",
        (model_call_id,),
    ).fetchone()


def _close_row(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    positive_ints = ("id", "project_id", "snapshot_id")
    bounded_strings = ("local_task_id", "call_prepare_key")
    required_strings = (
        "task_type",
        "provider",
        "model_id",
        "model_version",
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
        "qualification_status",
        "authorization_provider",
        "created_at",
    )
    hashes = (
        "snapshot_hash",
        "candidate_set_hash",
        "qualification_hash",
        "authorization_hash",
        "call_identity_hash",
    )
    if value.get("schema_version") != SCHEMA_VERSION:
        raise _ledger_invalid()
    if any(type(value.get(field)) is not int or value[field] <= 0 for field in positive_ints):
        raise _ledger_invalid()
    if any(
        not _is_non_empty_string(value.get(field))
        or len(value[field]) > _MAX_TASK_ID_LENGTH
        for field in bounded_strings
    ):
        raise _ledger_invalid()
    if any(not _is_non_empty_string(value.get(field)) for field in required_strings):
        raise _ledger_invalid()
    if value.get("task_type") not in TASK_TYPES:
        raise _ledger_invalid()
    if any(not _is_hash(value.get(field)) for field in hashes):
        raise _ledger_invalid()
    if value.get("qualification_status") != "qualified":
        raise _ledger_invalid()
    if value.get("authorization_provider") != value.get("provider"):
        raise _ledger_invalid()
    if value.get("authorization_authorized") != 1 or value.get("authorization_valid") != 1:
        raise _ledger_invalid()

    qualification_hash = _stable_hash(_qualification_payload(value))
    authorization_hash = _stable_hash(_authorization_payload(value))
    if qualification_hash != value["qualification_hash"]:
        raise _ledger_invalid()
    if authorization_hash != value["authorization_hash"]:
        raise _ledger_invalid()
    if _stable_hash(_call_identity_payload(value)) != value["call_identity_hash"]:
        raise _ledger_invalid()

    result = dict(value)
    result["model_call_id"] = result.pop("id")
    result["preparation_state"] = "prepared"
    return result


def _insert_prepared_row(conn: sqlite3.Connection, row: Mapping[str, object]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO model_calls (
            schema_version, project_id, local_task_id, call_prepare_key,
            snapshot_id, snapshot_hash, candidate_set_hash, task_type,
            provider, model_id, model_version, rule_version,
            output_schema_version, benchmark_sample_pack_version,
            qualification_status, qualification_hash,
            authorization_provider, authorization_authorized,
            authorization_valid, authorization_hash, created_at,
            call_identity_hash
        ) VALUES (
            :schema_version, :project_id, :local_task_id, :call_prepare_key,
            :snapshot_id, :snapshot_hash, :candidate_set_hash, :task_type,
            :provider, :model_id, :model_version, :rule_version,
            :output_schema_version, :benchmark_sample_pack_version,
            :qualification_status, :qualification_hash,
            :authorization_provider, :authorization_authorized,
            :authorization_valid, :authorization_hash, :created_at,
            :call_identity_hash
        )
        """,
        dict(row),
    )
    if type(cursor.lastrowid) is not int or cursor.lastrowid <= 0:
        raise _ledger_invalid()
    return cursor.lastrowid


def get_model_call(model_call_id: int) -> dict[str, object]:
    """Read one durable preparation row and verify only its persisted self-closure."""
    if type(model_call_id) is not int or model_call_id <= 0:
        raise _input_invalid("model_call_id 必须是正整数。")
    try:
        with get_connection() as conn:
            row = _read_by_id(conn, model_call_id)
            if row is None:
                raise _not_found()
            return _close_row(row)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _ledger_invalid() from exc


def prepare_model_call(
    *,
    local_task_id: str,
    call_prepare_key: str,
    snapshot_id: int,
    task_type: str,
    provider: str,
    model_id: str,
    model_version: str,
    rule_version: str,
    output_schema_version: str,
    benchmark_sample_pack_version: str,
    qualification_record: Mapping[str, object] | ModelQualificationRecord,
    data_sending_authorization: Mapping[str, object],
) -> dict[str, object]:
    """Freeze one model-call preparation identity; never contacts a model/provider."""
    _validate_direct_inputs(
        local_task_id=local_task_id,
        call_prepare_key=call_prepare_key,
        snapshot_id=snapshot_id,
        task_type=task_type,
        provider=provider,
        model_id=model_id,
        model_version=model_version,
        rule_version=rule_version,
        output_schema_version=output_schema_version,
        benchmark_sample_pack_version=benchmark_sample_pack_version,
    )

    # Formal frozen identity closure. Upstream HTTPException codes intentionally propagate unchanged.
    candidate = build_context_candidate_set(snapshot_id)
    try:
        project_id = candidate["project_id"]
        frozen_snapshot_id = candidate["snapshot_id"]
        snapshot_hash = candidate["snapshot_hash"]
        candidate_set_hash = candidate["candidate_set_hash"]
    except (KeyError, TypeError) as exc:
        raise _ledger_invalid("Candidate Set 未返回完整冻结身份。") from exc
    if (
        type(project_id) is not int
        or project_id <= 0
        or frozen_snapshot_id != snapshot_id
        or not _is_hash(snapshot_hash)
        or not _is_hash(candidate_set_hash)
    ):
        raise _ledger_invalid("Candidate Set 返回的冻结身份无效。")

    qualification = _validated_qualification(
        qualification_record,
        provider=provider,
        model_id=model_id,
        model_version=model_version,
        rule_version=rule_version,
        output_schema_version=output_schema_version,
        benchmark_sample_pack_version=benchmark_sample_pack_version,
    )
    authorization = _validated_authorization(
        data_sending_authorization,
        provider=provider,
    )
    qualification_hash = _stable_hash(qualification)
    authorization_hash = _stable_hash(authorization)

    row: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "local_task_id": local_task_id,
        "call_prepare_key": call_prepare_key,
        "snapshot_id": frozen_snapshot_id,
        "snapshot_hash": snapshot_hash,
        "candidate_set_hash": candidate_set_hash,
        "task_type": task_type,
        "provider": provider,
        "model_id": model_id,
        "model_version": model_version,
        "rule_version": rule_version,
        "output_schema_version": output_schema_version,
        "benchmark_sample_pack_version": benchmark_sample_pack_version,
        "qualification_status": qualification["qualification_status"],
        "qualification_hash": qualification_hash,
        "authorization_provider": authorization["provider"],
        "authorization_authorized": 1 if authorization["authorized"] is True else 0,
        "authorization_valid": 1 if authorization["valid"] is True else 0,
        "authorization_hash": authorization_hash,
        "created_at": _now(),
    }
    row["call_identity_hash"] = _stable_hash(_call_identity_payload(row))

    try:
        with get_connection() as conn:
            existing = _read_by_key(
                conn,
                project_id=project_id,
                call_prepare_key=call_prepare_key,
            )
            if existing is not None:
                closed = _close_row(existing)
                if closed["call_identity_hash"] == row["call_identity_hash"]:
                    return closed
                raise _idempotency_conflict()

            try:
                model_call_id = _insert_prepared_row(conn, row)
            except sqlite3.IntegrityError as exc:
                # UNIQUE collision is an implementation detail. Re-read the winner and
                # resolve exact identity vs drift deterministically; never blind-retry INSERT.
                winner = _read_by_key(
                    conn,
                    project_id=project_id,
                    call_prepare_key=call_prepare_key,
                )
                if winner is None:
                    raise _ledger_invalid("并发写入冲突后无法闭合 winner row。") from exc
                closed = _close_row(winner)
                if closed["call_identity_hash"] == row["call_identity_hash"]:
                    return closed
                raise _idempotency_conflict() from exc

            inserted = _read_by_id(conn, model_call_id)
            if inserted is None:
                raise _ledger_invalid("Model Call row 写入后无法回读。")
            return _close_row(inserted)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _ledger_invalid() from exc
