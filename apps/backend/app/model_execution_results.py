"""Model Execution Result Ledger V1：持久化已验证的 provider success truth。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.ai_contract_validation import validate_ai_response
from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION, ENVELOPE_FIELDS, TASK_TYPES
from app.context_resolver import build_context_candidate_set
from app.db import get_connection
from app.model_call_ledger import get_model_call


SCHEMA_VERSION = "model_execution_result_v1"
_CANDIDATE_SCHEMA_VERSION = "context_candidate_set_v1"
_SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_FIELDS = (
    "provider",
    "provider_response_id",
    "actual_model",
    "provider_runtime_fingerprint",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "result",
)
_RESULT_COLUMNS = (
    "id, schema_version, model_call_id, project_id, snapshot_id, local_task_id, "
    "task_type, call_identity_hash, provider, model_id, model_version, "
    "provider_response_id, actual_model, provider_runtime_fingerprint, finish_reason, "
    "prompt_tokens, completion_tokens, total_tokens, formal_response_json, "
    "formal_response_hash, validated_result_json, validated_result_hash, "
    "execution_result_hash, created_at"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "Model Execution Result 输入无效。") -> HTTPException:
    return _error(400, "MODEL_EXECUTION_RESULT_INPUT_INVALID", message)


def _not_found() -> HTTPException:
    return _error(404, "MODEL_EXECUTION_RESULT_NOT_FOUND", "指定的模型执行结果不存在。")


def _conflict() -> HTTPException:
    return _error(
        409,
        "MODEL_EXECUTION_RESULT_CONFLICT",
        "同一 model_call_id 已绑定不同的 verified execution result。",
    )


def _receipt_invalid(message: str = "Normalized transport receipt 无效。") -> HTTPException:
    return _error(409, "MODEL_EXECUTION_RESULT_RECEIPT_INVALID", message)


def _evidence_invalid(message: str = "Candidate Set / Evidence-ID closure 无效。") -> HTTPException:
    return _error(409, "MODEL_EXECUTION_RESULT_EVIDENCE_INVALID", message)


_VALIDATION_ISSUE_LIMIT = 12
_VALIDATION_CODE_RE = re.compile(r"^AI_[A-Z0-9_]{2,92}$")
_VALIDATION_PATH_RE = re.compile(r"^\$[A-Za-z0-9_.\[\]<>-]{0,191}$")


def _safe_validation_issues(issues: object) -> list[dict[str, str]]:
    safe_issues: list[dict[str, str]] = []
    if isinstance(issues, (tuple, list)):
        for issue in issues:
            if getattr(issue, "severity", None) != "error":
                continue
            code = getattr(issue, "code", None)
            path = getattr(issue, "path", None)
            if (
                type(code) is str
                and _VALIDATION_CODE_RE.fullmatch(code) is not None
                and type(path) is str
                and _VALIDATION_PATH_RE.fullmatch(path) is not None
            ):
                safe_issues.append({"code": code, "path": path})
                if len(safe_issues) >= _VALIDATION_ISSUE_LIMIT:
                    break
    return safe_issues


def _output_invalid(
    issues: object = (),
    message: str = "Formal AI response 未通过正式输出契约。",
    failure_receipt_id: int | None = None,
) -> HTTPException:
    error = _error(409, "MODEL_EXECUTION_RESULT_OUTPUT_INVALID", message)
    safe_issues = _safe_validation_issues(issues)
    error.detail.update(stage="output_contract", cause_code="AI_OUTPUT_CONTRACT_INVALID")
    if safe_issues:
        error.detail["validation_issues"] = safe_issues
    if type(failure_receipt_id) is int and failure_receipt_id > 0:
        error.detail["failure_receipt_id"] = failure_receipt_id
        error.detail["message"] = f"{message} Provider 回执已安全留存为失败回执 #{failure_receipt_id}。"
    return error


def _stored_invalid(message: str = "Model Execution Result 持久化事实无法完成自校验。") -> HTTPException:
    return _error(409, "MODEL_EXECUTION_RESULT_STORED_INVALID", message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_non_empty_string(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _is_positive_sqlite_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_SIGNED_INTEGER_MAX


def _is_nonnegative_sqlite_int(value: object) -> bool:
    return type(value) is int and 0 <= value <= _SQLITE_SIGNED_INTEGER_MAX


def _require_positive_id(value: object, field_name: str) -> int:
    if not _is_positive_sqlite_int(value):
        raise _input_invalid(f"{field_name} 必须是 SQLite signed 范围内的正整数。")
    return value


def _canonical_json(value: object, *, stored: bool = False) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        if stored:
            raise _stored_invalid() from exc
        raise _receipt_invalid("Result 必须是可 canonical JSON 序列化的对象。") from exc


def _stable_hash(value: object, *, stored: bool = False) -> str:
    return hashlib.sha256(_canonical_json(value, stored=stored).encode("utf-8")).hexdigest()


def _normalized_json_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _receipt_invalid("receipt.result 必须是对象。")
    raw = _canonical_json(value)
    try:
        normalized = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - canonical serializer guarantees JSON
        raise _receipt_invalid() from exc
    if type(normalized) is not dict:
        raise _receipt_invalid("receipt.result 必须是 JSON object。")
    return normalized


def _validate_receipt(receipt: object, call: Mapping[str, object]) -> dict[str, object]:
    if type(receipt) is not dict or set(receipt) != set(_RECEIPT_FIELDS):
        raise _receipt_invalid("receipt 必须使用冻结 exact key set。")
    value = dict(receipt)
    for field in (
        "provider",
        "provider_response_id",
        "actual_model",
        "provider_runtime_fingerprint",
        "finish_reason",
    ):
        if not _is_non_empty_string(value.get(field)):
            raise _receipt_invalid(f"receipt.{field} 必须是非空字符串。")
    if value["provider"] != call.get("provider"):
        raise _receipt_invalid("receipt.provider 与 formal Model Call 不一致。")
    if value["actual_model"] != call.get("model_id"):
        raise _receipt_invalid("receipt.actual_model 与 prepared model_id 不一致。")
    if value["finish_reason"] != "stop":
        raise _receipt_invalid("V1 verified result 只接受 finish_reason=stop。")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if not _is_nonnegative_sqlite_int(value.get(field)):
            raise _receipt_invalid(f"receipt.{field} 必须是 SQLite signed 范围内的非负整数。")
    if value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]:
        raise _receipt_invalid("receipt token usage 无法完成 total closure。")
    value["result"] = _normalized_json_mapping(value["result"])
    return value



_FAILURE_RECEIPT_SCHEMA_VERSION = "model_execution_failure_receipt_v1"

def _ensure_failure_receipt_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_execution_failure_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version TEXT NOT NULL,
            model_call_id INTEGER NOT NULL UNIQUE,
            project_id INTEGER NOT NULL,
            snapshot_id INTEGER NOT NULL,
            local_task_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            call_identity_hash TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            provider_response_id TEXT NOT NULL,
            actual_model TEXT NOT NULL,
            provider_runtime_fingerprint TEXT NOT NULL,
            finish_reason TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            result_hash TEXT NOT NULL,
            failure_code TEXT NOT NULL,
            failure_stage TEXT NOT NULL,
            validation_issues_json TEXT NOT NULL,
            receipt_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_model_execution_failure_receipts_no_update
        BEFORE UPDATE ON model_execution_failure_receipts
        BEGIN
            SELECT RAISE(ABORT, 'model_execution_failure_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_model_execution_failure_receipts_no_delete
        BEFORE DELETE ON model_execution_failure_receipts
        BEGIN
            SELECT RAISE(ABORT, 'model_execution_failure_receipts is append-only');
        END;
        """
    )


def _record_output_failure_receipt(
    *, call: Mapping[str, object], receipt: Mapping[str, object], issues: object
) -> dict[str, object]:
    safe_issues = _safe_validation_issues(issues)
    issues_json = _canonical_json(safe_issues)
    result_hash = hashlib.sha256(_canonical_json(receipt["result"]).encode("utf-8")).hexdigest()
    row = {
        "schema_version": _FAILURE_RECEIPT_SCHEMA_VERSION,
        "model_call_id": call["model_call_id"],
        "project_id": call["project_id"],
        "snapshot_id": call["snapshot_id"],
        "local_task_id": call["local_task_id"],
        "task_type": call["task_type"],
        "call_identity_hash": call["call_identity_hash"],
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "provider_response_id": receipt["provider_response_id"],
        "actual_model": receipt["actual_model"],
        "provider_runtime_fingerprint": receipt["provider_runtime_fingerprint"],
        "finish_reason": receipt["finish_reason"],
        "prompt_tokens": receipt["prompt_tokens"],
        "completion_tokens": receipt["completion_tokens"],
        "total_tokens": receipt["total_tokens"],
        "result_hash": result_hash,
        "failure_code": "MODEL_EXECUTION_RESULT_OUTPUT_INVALID",
        "failure_stage": "output_contract",
        "validation_issues_json": issues_json,
        "created_at": _now(),
    }
    row["receipt_hash"] = _stable_hash({k: v for k, v in row.items() if k != "created_at"})
    with get_connection() as conn:
        _ensure_failure_receipt_schema(conn)
        existing = conn.execute(
            "SELECT id, receipt_hash FROM model_execution_failure_receipts WHERE model_call_id = ?",
            (row["model_call_id"],),
        ).fetchone()
        if existing is not None:
            if existing["receipt_hash"] != row["receipt_hash"]:
                raise _stored_invalid("同一 Model Call 已绑定不同失败回执。")
            return {"failure_receipt_id": int(existing["id"]), **row}
        cursor = conn.execute(
            """
            INSERT INTO model_execution_failure_receipts (
                schema_version,model_call_id,project_id,snapshot_id,local_task_id,task_type,
                call_identity_hash,provider,model_id,model_version,provider_response_id,
                actual_model,provider_runtime_fingerprint,finish_reason,prompt_tokens,
                completion_tokens,total_tokens,result_hash,failure_code,failure_stage,
                validation_issues_json,receipt_hash,created_at
            ) VALUES (
                :schema_version,:model_call_id,:project_id,:snapshot_id,:local_task_id,:task_type,
                :call_identity_hash,:provider,:model_id,:model_version,:provider_response_id,
                :actual_model,:provider_runtime_fingerprint,:finish_reason,:prompt_tokens,
                :completion_tokens,:total_tokens,:result_hash,:failure_code,:failure_stage,
                :validation_issues_json,:receipt_hash,:created_at
            )
            """,
            row,
        )
        failure_receipt_id = cursor.lastrowid
        if type(failure_receipt_id) is not int or failure_receipt_id <= 0:
            raise _stored_invalid("失败回执写入后没有合法 id。")
        return {"failure_receipt_id": failure_receipt_id, **row}


def _candidate_evidence_ids(
    candidate: object,
    *,
    call: Mapping[str, object],
) -> set[str]:
    if not isinstance(candidate, Mapping):
        raise _evidence_invalid()
    if (
        candidate.get("schema_version") != _CANDIDATE_SCHEMA_VERSION
        or candidate.get("project_id") != call.get("project_id")
        or candidate.get("snapshot_id") != call.get("snapshot_id")
        or candidate.get("snapshot_hash") != call.get("snapshot_hash")
        or candidate.get("candidate_set_hash") != call.get("candidate_set_hash")
    ):
        raise _evidence_invalid("Candidate Set 与 formal Model Call 冻结身份不一致。")
    items = candidate.get("items")
    if type(items) is not list:
        raise _evidence_invalid("Candidate Set items 无效。")
    evidence_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise _evidence_invalid("Candidate Set item 必须是对象。")
        evidence_id = item.get("evidence_id")
        if not _is_non_empty_string(evidence_id) or evidence_id in seen:
            raise _evidence_invalid("Candidate Set evidence_id 必须 non-empty 且 unique。")
        seen.add(evidence_id)
        evidence_ids.append(evidence_id)
    if len(evidence_ids) != len(seen):  # defensive closure; duplicates are rejected above
        raise _evidence_invalid()
    return set(evidence_ids)


def _batch_evidence_ids(call, allowed):
    from app.report_generation_batches import get_batch_call_selection
    selection = get_batch_call_selection(call["model_call_id"])
    if selection is None:
        return allowed
    ids = selection.get("evidence_ids")
    if (not isinstance(ids, list) or any(type(item) is not str for item in ids)
            or len(set(ids)) != len(ids) or not set(ids) <= allowed):
        raise _evidence_invalid()
    return set(ids)


def _formal_response(
    call: Mapping[str, object], receipt: Mapping[str, object]
) -> dict[str, object]:
    return {
        "local_task_id": call["local_task_id"],
        "provider_request_id": receipt["provider_response_id"],
        "task_type": call["task_type"],
        "status": "succeeded",
        "schema_version": AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": call["output_schema_version"],
        "actual_model": {
            "provider": call["provider"],
            "id": receipt["actual_model"],
            "version": call["model_version"],
        },
        "usage": {
            "input_tokens": receipt["prompt_tokens"],
            "output_tokens": receipt["completion_tokens"],
        },
        "cost": "unknown",
        "billing_status": "unknown",
        "warnings": [],
        "result": receipt["result"],
    }


def _execution_identity_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": row["schema_version"],
        "model_call_id": row["model_call_id"],
        "project_id": row["project_id"],
        "snapshot_id": row["snapshot_id"],
        "local_task_id": row["local_task_id"],
        "task_type": row["task_type"],
        "call_identity_hash": row["call_identity_hash"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "model_version": row["model_version"],
        "provider_response_id": row["provider_response_id"],
        "actual_model": row["actual_model"],
        "provider_runtime_fingerprint": row["provider_runtime_fingerprint"],
        "finish_reason": row["finish_reason"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "formal_response_hash": row["formal_response_hash"],
        "validated_result_hash": row["validated_result_hash"],
    }


def _read_by_id(conn: sqlite3.Connection, model_result_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_RESULT_COLUMNS} FROM model_execution_results WHERE id = ?",
        (model_result_id,),
    ).fetchone()


def _read_by_call(conn: sqlite3.Connection, model_call_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_RESULT_COLUMNS} FROM model_execution_results WHERE model_call_id = ?",
        (model_call_id,),
    ).fetchone()


def _insert_result_row(conn: sqlite3.Connection, row: Mapping[str, object]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO model_execution_results (
            schema_version, model_call_id, project_id, snapshot_id, local_task_id,
            task_type, call_identity_hash, provider, model_id, model_version,
            provider_response_id, actual_model, provider_runtime_fingerprint,
            finish_reason, prompt_tokens, completion_tokens, total_tokens,
            formal_response_json, formal_response_hash, validated_result_json,
            validated_result_hash, execution_result_hash, created_at
        ) VALUES (
            :schema_version, :model_call_id, :project_id, :snapshot_id, :local_task_id,
            :task_type, :call_identity_hash, :provider, :model_id, :model_version,
            :provider_response_id, :actual_model, :provider_runtime_fingerprint,
            :finish_reason, :prompt_tokens, :completion_tokens, :total_tokens,
            :formal_response_json, :formal_response_hash, :validated_result_json,
            :validated_result_hash, :execution_result_hash, :created_at
        )
        """,
        dict(row),
    )
    if not _is_positive_sqlite_int(cursor.lastrowid):
        raise _stored_invalid("Result row 写入后没有合法 id。")
    return cursor.lastrowid


def _stored_json_mapping(raw: object) -> tuple[dict[str, object], str]:
    if type(raw) is not str or not raw:
        raise _stored_invalid()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _stored_invalid() from exc
    if type(value) is not dict:
        raise _stored_invalid()
    canonical = _canonical_json(value, stored=True)
    if canonical != raw:
        raise _stored_invalid("持久化 JSON 不是 canonical bytes。")
    return value, canonical


def _assert_call_binding(value: Mapping[str, object], call: Mapping[str, object]) -> None:
    expected = {
        "model_call_id": call.get("model_call_id"),
        "project_id": call.get("project_id"),
        "snapshot_id": call.get("snapshot_id"),
        "local_task_id": call.get("local_task_id"),
        "task_type": call.get("task_type"),
        "call_identity_hash": call.get("call_identity_hash"),
        "provider": call.get("provider"),
        "model_id": call.get("model_id"),
        "model_version": call.get("model_version"),
    }
    if any(value.get(field) != expected_value for field, expected_value in expected.items()):
        raise _stored_invalid("Result row 与 durable Model Call identity 发生漂移。")


def _close_row(
    row: sqlite3.Row | Mapping[str, object], *, call: Mapping[str, object]
) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") == "model_execution_aggregate_v1":
        return _close_aggregate_row(value, call=call)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise _stored_invalid()
    for field in ("id", "model_call_id", "project_id", "snapshot_id"):
        if not _is_positive_sqlite_int(value.get(field)):
            raise _stored_invalid()
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if not _is_nonnegative_sqlite_int(value.get(field)):
            raise _stored_invalid()
    if value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]:
        raise _stored_invalid()
    for field in (
        "local_task_id",
        "task_type",
        "provider",
        "model_id",
        "model_version",
        "provider_response_id",
        "actual_model",
        "provider_runtime_fingerprint",
        "finish_reason",
        "created_at",
    ):
        if not _is_non_empty_string(value.get(field)):
            raise _stored_invalid()
    if value["task_type"] not in TASK_TYPES or value["finish_reason"] != "stop":
        raise _stored_invalid()
    if value["actual_model"] != value["model_id"]:
        raise _stored_invalid()
    for field in (
        "call_identity_hash",
        "formal_response_hash",
        "validated_result_hash",
        "execution_result_hash",
    ):
        if not _is_hash(value.get(field)):
            raise _stored_invalid()

    _assert_call_binding(value, call)

    formal_response, formal_json = _stored_json_mapping(value.get("formal_response_json"))
    validated_result, result_json = _stored_json_mapping(value.get("validated_result_json"))
    if hashlib.sha256(formal_json.encode("utf-8")).hexdigest() != value["formal_response_hash"]:
        raise _stored_invalid()
    if hashlib.sha256(result_json.encode("utf-8")).hexdigest() != value["validated_result_hash"]:
        raise _stored_invalid()
    if set(formal_response) != set(ENVELOPE_FIELDS):
        raise _stored_invalid()
    if (
        formal_response.get("local_task_id") != value["local_task_id"]
        or formal_response.get("provider_request_id") != value["provider_response_id"]
        or formal_response.get("task_type") != value["task_type"]
        or formal_response.get("status") != "succeeded"
        or formal_response.get("schema_version") != AI_CONTRACT_SCHEMA_VERSION
        or formal_response.get("output_schema_version") != call.get("output_schema_version")
        or formal_response.get("cost") != "unknown"
        or formal_response.get("billing_status") != "unknown"
        or formal_response.get("warnings") != []
        or formal_response.get("result") != validated_result
    ):
        raise _stored_invalid()
    if formal_response.get("actual_model") != {
        "provider": value["provider"],
        "id": value["actual_model"],
        "version": value["model_version"],
    }:
        raise _stored_invalid()
    if formal_response.get("usage") != {
        "input_tokens": value["prompt_tokens"],
        "output_tokens": value["completion_tokens"],
    }:
        raise _stored_invalid()
    if _stable_hash(_execution_identity_payload(value), stored=True) != value["execution_result_hash"]:
        raise _stored_invalid()

    from app.report_generation_batches import get_batch_call_selection
    selection = get_batch_call_selection(call["model_call_id"])
    if selection is not None:
        # The write path already verified snapshot membership. Durable reads close
        # against the hash-bound plan, without replaying current Git for each child.
        allowed = set(selection["evidence_ids"])
        if not validate_ai_response(formal_response, allowed).is_valid:
            raise _stored_invalid("Batch result contains evidence outside its frozen batch.")

    result = dict(value)
    result["model_result_id"] = result.pop("id")
    result.pop("formal_response_json")
    result.pop("validated_result_json")
    result["formal_response"] = formal_response
    result["validated_result"] = validated_result
    return result


def _read_call_for_stored_row(model_call_id: object) -> dict[str, object]:
    if not _is_positive_sqlite_int(model_call_id):
        raise _stored_invalid()
    try:
        return get_model_call(model_call_id)
    except HTTPException as exc:
        raise _stored_invalid("Result row 无法重新绑定 durable Model Call。") from exc


def record_model_execution_result(
    *, model_call_id: int, receipt: Mapping[str, object]
) -> dict[str, object]:
    """Persist one formally validated provider success result; never sends network traffic."""
    model_call_id = _require_positive_id(model_call_id, "model_call_id")

    # Formal authority reads: exactly once each on the record path.
    call = get_model_call(model_call_id)
    candidate = build_context_candidate_set(call["snapshot_id"])
    allowed_evidence_ids = _batch_evidence_ids(call, _candidate_evidence_ids(candidate, call=call))
    normalized_receipt = _validate_receipt(receipt, call)
    formal_response = _formal_response(call, normalized_receipt)

    validation = validate_ai_response(formal_response, allowed_evidence_ids)
    if not validation.is_valid:
        failure_receipt = _record_output_failure_receipt(
            call=call, receipt=normalized_receipt, issues=validation.issues
        )
        raise _output_invalid(
            validation.issues, failure_receipt_id=failure_receipt["failure_receipt_id"]
        )

    formal_response_json = _canonical_json(formal_response)
    validated_result_json = _canonical_json(normalized_receipt["result"])
    row: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "model_call_id": model_call_id,
        "project_id": call["project_id"],
        "snapshot_id": call["snapshot_id"],
        "local_task_id": call["local_task_id"],
        "task_type": call["task_type"],
        "call_identity_hash": call["call_identity_hash"],
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "provider_response_id": normalized_receipt["provider_response_id"],
        "actual_model": normalized_receipt["actual_model"],
        "provider_runtime_fingerprint": normalized_receipt["provider_runtime_fingerprint"],
        "finish_reason": normalized_receipt["finish_reason"],
        "prompt_tokens": normalized_receipt["prompt_tokens"],
        "completion_tokens": normalized_receipt["completion_tokens"],
        "total_tokens": normalized_receipt["total_tokens"],
        "formal_response_json": formal_response_json,
        "formal_response_hash": hashlib.sha256(formal_response_json.encode("utf-8")).hexdigest(),
        "validated_result_json": validated_result_json,
        "validated_result_hash": hashlib.sha256(validated_result_json.encode("utf-8")).hexdigest(),
        "created_at": _now(),
    }
    row["execution_result_hash"] = _stable_hash(_execution_identity_payload(row))

    try:
        with get_connection() as conn:
            existing = _read_by_call(conn, model_call_id)
            if existing is not None:
                closed = _close_row(existing, call=call)
                if closed["execution_result_hash"] == row["execution_result_hash"]:
                    return closed
                raise _conflict()

            try:
                model_result_id = _insert_result_row(conn, row)
            except sqlite3.IntegrityError as exc:
                winner = _read_by_call(conn, model_call_id)
                if winner is None:
                    raise _stored_invalid("并发写入冲突后无法闭合 winner row。") from exc
                closed = _close_row(winner, call=call)
                if closed["execution_result_hash"] == row["execution_result_hash"]:
                    return closed
                raise _conflict() from exc

            inserted = _read_by_id(conn, model_result_id)
            if inserted is None:
                raise _stored_invalid("Result row 写入后无法回读。")
            return _close_row(inserted, call=call)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def get_model_execution_result(model_result_id: int) -> dict[str, object]:
    """Read one result and rebind it to its durable Model Call identity."""
    model_result_id = _require_positive_id(model_result_id, "model_result_id")
    try:
        with get_connection() as conn:
            row = _read_by_id(conn, model_result_id)
            if row is None:
                raise _not_found()
            raw_model_call_id = row["model_call_id"]
        call = _read_call_for_stored_row(raw_model_call_id)
        return _close_row(row, call=call)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def get_model_execution_result_for_call(model_call_id: int) -> dict[str, object]:
    """Read the single verified result bound to one formal Model Call."""
    model_call_id = _require_positive_id(model_call_id, "model_call_id")
    try:
        with get_connection() as conn:
            row = _read_by_call(conn, model_call_id)
            if row is None:
                raise _not_found()
        call = _read_call_for_stored_row(model_call_id)
        return _close_row(row, call=call)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc



def get_single_model_execution_result_for_task(
    *, project_id: int, local_task_id: str, task_type: str
) -> dict[str, object]:
    """Recover exactly one verified non-batch result by durable task identity."""
    project_id = _require_positive_id(project_id, "project_id")
    if not _is_non_empty_string(local_task_id) or task_type not in TASK_TYPES:
        raise _input_invalid("task identity 无效。")
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT model_call_id FROM model_execution_results "
                "WHERE project_id = ? AND local_task_id = ? AND task_type = ? ORDER BY id",
                (project_id, local_task_id, task_type),
            ).fetchall()
        if not rows:
            raise _not_found()
        if len(rows) != 1:
            raise _stored_invalid("单次任务恢复发现多个 verified result；分批任务必须走分批恢复。")
        return get_model_execution_result_for_call(int(rows[0]["model_call_id"]))
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def _aggregate_facts(call, child_result_ids):
    """Rebuild only from verified provider rows; never invent a provider receipt."""
    from app.report_generation_batches import get_batch_parent_call_ids, get_plan
    expected = get_batch_parent_call_ids(call["model_call_id"])
    if (call["task_type"] != "daily_report_generate" or not expected
            or not isinstance(child_result_ids, list) or not child_result_ids
            or any(not _is_positive_sqlite_int(item) for item in child_result_ids)
            or len(set(child_result_ids)) != len(child_result_ids)):
        raise _stored_invalid("Aggregate requires the complete ordered batch plan.")
    children = []
    for result_id in child_result_ids:
        # Reject aggregate children before recursing, including forged cycles.
        with get_connection() as conn:
            raw = _read_by_id(conn, result_id)
        if raw is None or raw["schema_version"] != SCHEMA_VERSION:
            raise _stored_invalid("Aggregate source must be a real provider result.")
        child = get_model_execution_result(result_id)
        for field in ("project_id", "snapshot_id", "local_task_id", "task_type", "provider", "model_id", "model_version"):
            if child[field] != call[field]:
                raise _stored_invalid("Aggregate child identity differs from parent.")
        children.append(child)
    if [child["model_call_id"] for child in children] != expected:
        raise _stored_invalid("Aggregate child list is incomplete or out of order.")
    source = [{"model_result_id": child["model_result_id"],
               "execution_result_hash": child["execution_result_hash"]} for child in children]
    plan = get_plan(call["model_call_id"])
    if plan is None:
        raise _stored_invalid("Aggregate plan provenance is missing.")
    coverage_notice = (
        "以下为分批证据结论，未由额外模型汇总；局部结论不代表整个项目状态。"
        f"排除 {plan['denied_target_count']} 项，不支持来源 {len(plan['unsupported_context_sources'])} 项。\n\n"
    )
    merged = {"plain_summary": coverage_notice + "\n\n".join(
        f"[批次 {index}] {child['validated_result']['plain_summary']}"
        for index, child in enumerate(children, 1))}
    for field in ("feature_progress", "code_change_summary", "test_evidence", "risks", "unknown_items", "source_warnings"):
        merged[field] = []
        seen = set()
        for child in children:
            for item in child["validated_result"][field]:
                identity = _canonical_json(item) if isinstance(item, dict) else json.dumps(item, ensure_ascii=False, sort_keys=True)
                if identity not in seen:
                    seen.add(identity)
                    merged[field].append(item)
    # Different stages for one feature remain visibly batch-local rather than being collapsed.
    by_feature = {}
    for index, child in enumerate(children, 1):
        for item in child["validated_result"]["feature_progress"]:
            by_feature.setdefault(item["feature"], set()).add(item["stage"])
    conflicting = {name for name, stages in by_feature.items() if len(stages) > 1}
    if conflicting:
        merged["feature_progress"] = []
        for index, child in enumerate(children, 1):
            for item in child["validated_result"]["feature_progress"]:
                annotated = dict(item)
                if item["feature"] in conflicting:
                    annotated["feature"] = f"[批次 {index} 的局部结论] {item['feature']}"
                if annotated not in merged["feature_progress"]:
                    merged["feature_progress"].append(annotated)
    aggregate_hash = _stable_hash({"parent_call_identity_hash": call["call_identity_hash"], "children": source})
    input_tokens = sum(child["prompt_tokens"] for child in children)
    output_tokens = sum(child["completion_tokens"] for child in children)
    formal = {
        "local_task_id": call["local_task_id"],
        "provider_request_id": "local-aggregate:" + aggregate_hash,
        "task_type": call["task_type"], "status": "succeeded",
        "schema_version": AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": call["output_schema_version"],
        "actual_model": {"provider": call["provider"], "id": call["model_id"], "version": call["model_version"]},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "cost": "unknown", "billing_status": "unknown", "warnings": [], "result": merged,
        "aggregation_source": {"kind": "deterministic_local_aggregation", "children": source, "aggregate_hash": aggregate_hash},
    }
    formal_json = _canonical_json(formal)
    result_json = _canonical_json(merged)
    row = {"schema_version": "model_execution_aggregate_v1", "model_call_id": call["model_call_id"],
           **{field: call[field] for field in ("project_id", "snapshot_id", "local_task_id", "task_type", "call_identity_hash", "provider", "model_id", "model_version")},
           "provider_response_id": "local-aggregate:" + aggregate_hash,
           "actual_model": call["model_id"], "provider_runtime_fingerprint": "local-deterministic-aggregate-v1",
           "finish_reason": "stop", "prompt_tokens": input_tokens, "completion_tokens": output_tokens,
           "total_tokens": input_tokens + output_tokens, "formal_response_json": formal_json,
           "formal_response_hash": hashlib.sha256(formal_json.encode("utf-8")).hexdigest(),
           "validated_result_json": result_json, "validated_result_hash": hashlib.sha256(result_json.encode("utf-8")).hexdigest()}
    row["execution_result_hash"] = _stable_hash(_execution_identity_payload(row))
    return row, source, aggregate_hash


def _close_aggregate_row(value, *, call):
    _assert_call_binding(value, call)
    if not _is_positive_sqlite_int(value.get("id")) or not _is_non_empty_string(value.get("created_at")):
        raise _stored_invalid()
    with get_connection() as conn:
        source_row = conn.execute("SELECT children_json, aggregate_hash FROM model_execution_aggregate_sources WHERE parent_result_id = ?", (value["id"],)).fetchone()
    if source_row is None:
        raise _stored_invalid("Aggregate provenance is missing.")
    try:
        sources = json.loads(source_row["children_json"])
        child_ids = [item["model_result_id"] for item in sources]
    except (TypeError, ValueError, KeyError) as exc:
        raise _stored_invalid() from exc
    expected, source, aggregate_hash = _aggregate_facts(call, child_ids)
    if (sources != source or source_row["children_json"] != json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            or source_row["aggregate_hash"] != aggregate_hash
            or any(value.get(key) != item for key, item in expected.items())):
        raise _stored_invalid("Aggregate or child provenance changed.")
    result = dict(value)
    result["model_result_id"] = result.pop("id")
    result["formal_response"] = json.loads(result.pop("formal_response_json"))
    result["validated_result"] = json.loads(result.pop("validated_result_json"))
    result["aggregation_source"] = result["formal_response"]["aggregation_source"]
    return result


def record_aggregate_model_execution_result(parent_call_id: int, child_result_ids: list[int]):
    """Persist deterministic aggregation with explicit child provenance, without provider I/O."""
    parent_call_id = _require_positive_id(parent_call_id, "parent_call_id")
    call = get_model_call(parent_call_id)
    row, source, aggregate_hash = _aggregate_facts(call, child_result_ids)
    row["created_at"] = _now()
    with get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS model_execution_aggregate_sources (
            parent_result_id INTEGER PRIMARY KEY,
            children_json TEXT NOT NULL, aggregate_hash TEXT NOT NULL)""")
        conn.execute("BEGIN IMMEDIATE")
        existing = _read_by_call(conn, parent_call_id)
        if existing is not None:
            existing_id = existing["id"]
            if existing["execution_result_hash"] != row["execution_result_hash"]:
                raise _conflict()
        else:
            existing_id = _insert_result_row(conn, row)
            conn.execute("INSERT INTO model_execution_aggregate_sources VALUES (?, ?, ?)", (
                existing_id, json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")), aggregate_hash))
    return get_model_execution_result(existing_id)
