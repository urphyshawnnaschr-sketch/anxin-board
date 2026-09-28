"""Model Budget Profile V1: validate and freeze a model-call budget assertion without side effects."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json

from fastapi import HTTPException

from app.model_call_ledger import get_model_call


SCHEMA_VERSION = "model_budget_profile_v1"
BUDGET_AUTHORITY_STATE = "assertion_only"
TOKEN_COUNT_STATE = "not_counted"
MODEL_SEND_STATE = "not_admitted"
_MAX_CONTEXT_WINDOW_TOKENS = 100_000_000

_BUDGET_FIELDS = (
    "provider",
    "model_id",
    "model_version",
    "budget_policy_version",
    "context_window_tokens",
    "max_output_tokens",
    "reserved_output_tokens",
    "safety_margin_tokens",
    "tokenizer_family",
    "tokenizer_version",
    "counting_policy_version",
)
_STRING_FIELDS = (
    "provider",
    "model_id",
    "model_version",
    "budget_policy_version",
    "tokenizer_family",
    "tokenizer_version",
    "counting_policy_version",
)
_INTEGER_FIELDS = (
    "context_window_tokens",
    "max_output_tokens",
    "reserved_output_tokens",
    "safety_margin_tokens",
)
_MODEL_IDENTITY_FIELDS = ("provider", "model_id", "model_version")
_HASH_FIELDS = (
    "schema_version",
    "model_call_id",
    "call_identity_hash",
    "task_type",
    "provider",
    "model_id",
    "model_version",
    "budget_policy_version",
    "context_window_tokens",
    "max_output_tokens",
    "reserved_output_tokens",
    "safety_margin_tokens",
    "max_input_tokens",
    "tokenizer_family",
    "tokenizer_version",
    "counting_policy_version",
    "budget_authority_state",
    "token_count_state",
    "model_send_state",
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "Model Budget Profile 输入无效。") -> HTTPException:
    return _error(400, "MODEL_BUDGET_INPUT_INVALID", message)


def _model_mismatch() -> HTTPException:
    return _error(
        409,
        "MODEL_BUDGET_MODEL_MISMATCH",
        "budget_record 的模型身份与 Model Call Ledger 冻结身份不一致。",
    )


def _limit_invalid() -> HTTPException:
    return _error(
        400,
        "MODEL_BUDGET_LIMIT_INVALID",
        "budget_record 的 token 预算范围或关系无效。",
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_non_blank_utf8_string(value: object) -> bool:
    if type(value) is not str or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validated_budget_record(budget_record: object) -> dict[str, object]:
    if not isinstance(budget_record, Mapping):
        raise _input_invalid("budget_record 必须是 closed-world Mapping。")
    try:
        keys = set(budget_record.keys())
    except (TypeError, ValueError) as exc:
        raise _input_invalid("budget_record 字段集合无效。") from exc
    expected = set(_BUDGET_FIELDS)
    if keys != expected:
        raise _input_invalid("budget_record 必须 exact 包含 V1 固定字段，且不得缺失或追加字段。")
    try:
        value = {field: budget_record[field] for field in _BUDGET_FIELDS}
    except (KeyError, TypeError, ValueError) as exc:
        raise _input_invalid("budget_record 无法按固定字段读取。") from exc

    # Validate every string structurally before any identity mismatch classification.
    for field in _STRING_FIELDS:
        if not _is_non_blank_utf8_string(value[field]):
            raise _input_invalid(f"{field} 必须是非空、非纯空白 UTF-8 字符串。")
    for field in _INTEGER_FIELDS:
        if type(value[field]) is not int:
            raise _input_invalid(f"{field} 必须是 strict integer，bool 不接受。")
    return value


def _validate_limits(value: Mapping[str, object]) -> int:
    context_window = value["context_window_tokens"]
    max_output = value["max_output_tokens"]
    reserved_output = value["reserved_output_tokens"]
    safety_margin = value["safety_margin_tokens"]
    assert type(context_window) is int
    assert type(max_output) is int
    assert type(reserved_output) is int
    assert type(safety_margin) is int

    if not (0 < context_window <= _MAX_CONTEXT_WINDOW_TOKENS):
        raise _limit_invalid()
    if not (0 < max_output <= context_window):
        raise _limit_invalid()
    if not (0 < reserved_output <= max_output):
        raise _limit_invalid()
    if not (0 <= safety_margin < context_window):
        raise _limit_invalid()

    # safety_margin_tokens is one-time profile headroom: it is subtracted exactly once here.
    max_input_tokens = context_window - reserved_output - safety_margin
    if max_input_tokens <= 0:
        raise _limit_invalid()
    return max_input_tokens


def _hash_payload(profile: Mapping[str, object]) -> dict[str, object]:
    return {field: profile[field] for field in _HASH_FIELDS}


def build_model_budget_profile(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Build a deterministic, non-persisted budget assertion bound to a durable Model Call Ledger row.

    This function does not count tokens, contact a provider, grant send permission, or write a
    Budget Profile row. Future send-time gates must independently revalidate provider/model limits,
    authorization, redaction, admission, and final request size.
    """
    # Formal Ledger input validation / not-found / tamper errors intentionally propagate unchanged.
    ledger = get_model_call(model_call_id)
    value = _validated_budget_record(budget_record)

    if any(value[field] != ledger[field] for field in _MODEL_IDENTITY_FIELDS):
        raise _model_mismatch()

    max_input_tokens = _validate_limits(value)
    profile: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "model_call_id": ledger["model_call_id"],
        "call_identity_hash": ledger["call_identity_hash"],
        "task_type": ledger["task_type"],
        "provider": value["provider"],
        "model_id": value["model_id"],
        "model_version": value["model_version"],
        "budget_policy_version": value["budget_policy_version"],
        "context_window_tokens": value["context_window_tokens"],
        "max_output_tokens": value["max_output_tokens"],
        "reserved_output_tokens": value["reserved_output_tokens"],
        "safety_margin_tokens": value["safety_margin_tokens"],
        "max_input_tokens": max_input_tokens,
        "tokenizer_family": value["tokenizer_family"],
        "tokenizer_version": value["tokenizer_version"],
        "counting_policy_version": value["counting_policy_version"],
        "budget_authority_state": BUDGET_AUTHORITY_STATE,
        "token_count_state": TOKEN_COUNT_STATE,
        "model_send_state": MODEL_SEND_STATE,
    }
    profile["budget_profile_hash"] = _stable_hash(_hash_payload(profile))
    return profile
