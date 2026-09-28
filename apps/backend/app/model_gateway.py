"""Model Gateway Preflight V1: deterministic pre-transport readiness closure.

The Gateway may resolve the already-formal Current Authority seam, but it never sends
provider business payloads, reads credentials directly, writes persistence, or grants
send permission. Transient request material remains process-local and non-authorizing.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re
from threading import Lock

from fastapi import HTTPException

from app.report_language_policy import PROMPT_POLICY_TEXT, PROMPT_POLICY_VERSION

from app import context_token_framing as token_framing
from app import deepseek_current_authority, deepseek_transport
from app.deepseek_model_catalog import REPORT_MODEL_ID, REPORT_MODEL_VERSION
from app.ai_contracts import (
    NESTED_OBJECT_SCHEMAS,
    RESULT_ARRAY_ITEM_SCHEMAS,
    RESULT_FIELD_RULES,
    RESULT_FIELDS,
    TASK_TYPES,
)
from app.final_context_manifest import build_final_context_manifest


SCHEMA_VERSION = "model_gateway_preflight_v1"
TRANSIENT_SCHEMA_VERSION = "model_gateway_transient_request_v1"
NETWORK_SEND_STATE = "not_attempted"
JSON_INSTRUCTION_VERSION = "deepseek_json_output_instruction_v1"

_ACTIVATION_PROVIDER = "deepseek"
_ACTIVATION_MODEL_ID = REPORT_MODEL_ID
_ACTIVATION_MODEL_VERSION = REPORT_MODEL_VERSION
_ACTIVATION_TASK_TYPE = "daily_report_generate"
_ACTIVATION_OUTPUT_SCHEMA_VERSION = "daily-report/1.0"
_ACTIVATION_PURPOSE_ID = "anxin_board_daily_report_v1"

_CAPABILITY_KEYS = (
    "schema_version",
    "provider",
    "model_id",
    "model_version",
    "endpoint_origin",
    "endpoint_path",
    "task_type",
    "output_schema_version",
    "response_format",
    "stream",
    "thinking",
    "reasoning_effort",
    "max_output_tokens",
)
_EXPECTED_CAPABILITY = {
    "schema_version": "deepseek_transport_capability_v1",
    "provider": _ACTIVATION_PROVIDER,
    "model_id": _ACTIVATION_MODEL_ID,
    "model_version": _ACTIVATION_MODEL_VERSION,
    "endpoint_origin": "https://api.deepseek.com",
    "endpoint_path": "/chat/completions",
    "task_type": _ACTIVATION_TASK_TYPE,
    "output_schema_version": _ACTIVATION_OUTPUT_SCHEMA_VERSION,
    "response_format": "json_object",
    "stream": False,
    "thinking": "enabled",
    "reasoning_effort": "high",
    "max_output_tokens": 384_000,
}

_FINAL_MANIFEST_KEYS = (
    "schema_version",
    "manifest_stage",
    "model_call_id",
    "call_identity_hash",
    "project_id",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
    "task_type",
    "provider",
    "model_id",
    "model_version",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_hash",
    "authorization_hash",
    "manifest_core_hash",
    "budget_profile_hash",
    "token_framing_accounting_hash",
    "counting_policy_version",
    "framing_policy_version",
    "framing_scope",
    "context_admission_state",
    "coverage_state",
    "admitted_target_count",
    "denied_target_count",
    "admitted_targets",
    "denied_targets",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "conservative_input_token_upper_bound",
    "exact_tokens",
    "budget_fit_state",
    "final_request_fit_state",
    "unsupported_context_sources",
    "local_request_readiness_state",
    "final_manifest_state",
    "gateway_send_state",
    "final_context_manifest_hash",
)
_FINAL_MANIFEST_HASH_KEYS = tuple(
    key for key in _FINAL_MANIFEST_KEYS if key != "final_context_manifest_hash"
)
_ADMITTED_TARGET_KEYS = (
    "ordinal",
    "target",
    "target_type",
    "model_send_admission_hash",
    "redaction_result_hash",
    "frame_hash",
    "frame_utf8_bytes",
)
_DENIED_TARGET_KEYS = (
    "ordinal",
    "target",
    "target_type",
    "admission_reason",
    "matched_rule_id",
    "model_send_admission_hash",
)
_RESULT_KEYS = (
    "schema_version",
    "model_call_id",
    "call_identity_hash",
    "final_context_manifest_hash",
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "output_schema_version",
    "current_qualification_authority_state",
    "current_qualification_authority_ref",
    "current_qualification_evidence_hash",
    "current_authorization_authority_state",
    "current_authorization_authority_ref",
    "current_authorization_evidence_hash",
    "current_authorization_data_scope_hash",
    "current_authorization_purpose_id",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "request_envelope_hash",
    "final_request_fit_state",
    "provider_compatibility_state",
    "local_gateway_state",
    "network_send_state",
    "gateway_preflight_hash",
)
_RESULT_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "gateway_preflight_hash")
_REQUEST_PLAN_KEYS = (
    "schema_version",
    "prompt_policy_version",
    "json_instruction_version",
    "formal_result_contract_hash",
    "expected_structural_example_hash",
    "json_instruction_hash",
    "messages_hash",
    "task_envelope",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "budget_profile_hash",
    "reserved_output_tokens",
    "safety_margin_tokens",
    "provider_request_utf8_bytes",
    "conservative_local_request_upper_bound",
    "messages",
    "request_envelope_hash",
)
_REQUEST_PLAN_HASH_KEYS = tuple(
    key for key in _REQUEST_PLAN_KEYS if key != "request_envelope_hash"
)
_QUALIFICATION_KEYS = (
    "schema_version",
    "authority_source_id",
    "authority_source_version",
    "authority_ref",
    "provider",
    "model_id",
    "model_version",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_status",
    "context_window_tokens",
    "max_output_tokens",
    "checked_at",
    "models_source_hash",
    "metadata_source_hash",
    "evidence_hash",
)
_AUTHORIZATION_KEYS = (
    "schema_version",
    "authority_source_id",
    "authority_source_version",
    "authority_ref",
    "provider",
    "authorized",
    "valid",
    "purpose_id",
    "data_scope_hash",
    "evidence_hash",
)
_AUTHORITY_KEYS = (
    "schema_version",
    "model_call_id",
    "call_identity_hash",
    "provider",
    "model_id",
    "model_version",
    "context_window_tokens",
    "max_output_tokens",
    "purpose_id",
    "data_scope_hash",
    "qualification",
    "authorization",
    "authority_hash",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSIENT_MATERIAL_SLOT: ContextVar[dict[str, object] | None] = ContextVar(
    "model_gateway_transient_material", default=None
)


class _TransientClaim:
    __slots__ = ("_active", "_lock")

    def __init__(self) -> None:
        self._active = True
        self._lock = Lock()

    def revoke(self) -> None:
        with self._lock:
            self._active = False

    def consume(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._active = False
            return True


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "MODEL_GATEWAY_UPSTREAM_INCONSISTENT",
        "Gateway 所消费的 Final Context Manifest shape、hash 或 identity 无法闭合。",
    )


def _payload_rebind_failed() -> HTTPException:
    return _error(
        "MODEL_GATEWAY_PAYLOAD_REBIND_FAILED",
        "临时重建的 context payload 与 Final Context Manifest 记录不一致。",
    )


def _internal_inconsistent() -> HTTPException:
    return _error(
        "MODEL_GATEWAY_INTERNAL_INCONSISTENT",
        "Gateway 内部 canonical hash 或 schema 构造发生不一致。",
    )


def _request_plan_inconsistent() -> HTTPException:
    return _error(
        "MODEL_GATEWAY_REQUEST_PLAN_INCONSISTENT",
        "Gateway request-plan 的 formal contract、prompt/example、hash 或 byte accounting 无法闭合。",
    )


def _current_authority_inconsistent() -> HTTPException:
    return _error(
        "MODEL_GATEWAY_CURRENT_AUTHORITY_INCONSISTENT",
        "DeepSeek Current Authority 返回的 identity、hash、constraint 或 evidence 无法闭合。",
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _internal_inconsistent() from exc


def _canonical_text(value: object) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _revoke_transient_material_for_current_context() -> None:
    cached = _TRANSIENT_MATERIAL_SLOT.get()
    _TRANSIENT_MATERIAL_SLOT.set(None)
    if isinstance(cached, Mapping):
        claim = cached.get("_claim")
        if isinstance(claim, _TransientClaim):
            claim.revoke()


def _claim_transient_material(cached: Mapping[str, object]) -> None:
    claim = cached.get("_claim")
    if not isinstance(claim, _TransientClaim) or not claim.consume():
        raise _error(
            "MODEL_GATEWAY_TRANSIENT_NOT_READY",
            "不存在当前可消费的 ready Gateway transient material，或该 material 已被其他 execution context 消费或撤销。",
        )


def _validate_target_metadata(
    items: object,
    *,
    fields: tuple[str, ...],
    admitted: bool,
) -> tuple[set[int], set[str]]:
    if not isinstance(items, list):
        raise _upstream_inconsistent()
    ordinals: set[int] = set()
    targets: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or tuple(item) != fields:
            raise _upstream_inconsistent()
        ordinal = item.get("ordinal")
        target = item.get("target")
        target_type = item.get("target_type")
        if type(ordinal) is not int or ordinal <= 0 or ordinal in ordinals:
            raise _upstream_inconsistent()
        if type(target) is not str or not target or target in targets:
            raise _upstream_inconsistent()
        if type(target_type) is not str or not target_type:
            raise _upstream_inconsistent()
        if not _is_sha256(item.get("model_send_admission_hash")):
            raise _upstream_inconsistent()
        if admitted:
            if (
                not _is_sha256(item.get("redaction_result_hash"))
                or not _is_sha256(item.get("frame_hash"))
                or type(item.get("frame_utf8_bytes")) is not int
                or item["frame_utf8_bytes"] < 0
            ):
                raise _upstream_inconsistent()
        else:
            if item.get("admission_reason") not in {
                "sensitive_path_hard_deny",
                "binary_metadata_only_no_sendable_body",
            "credential_boundary_quarantined",
            "no_analyzable_source_after_redaction",
            }:
                raise _upstream_inconsistent()
            matched_rule_id = item.get("matched_rule_id")
            if matched_rule_id is not None and (
                type(matched_rule_id) is not str or not matched_rule_id
            ):
                raise _upstream_inconsistent()
        ordinals.add(ordinal)
        targets.add(target)
    return ordinals, targets


def _validate_manifest(value: object, *, model_call_id: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _FINAL_MANIFEST_KEYS:
        raise _upstream_inconsistent()
    if (
        value["schema_version"] != "final_context_manifest_v1"
        or value["manifest_stage"] != "local_pre_gateway_final"
        or value["final_manifest_state"] != "finalized_local_metadata"
        or value["gateway_send_state"] != "not_evaluated"
        or value["framing_scope"] != "context_payload_only"
        or value["final_request_fit_state"] != "not_evaluated"
        or value["exact_tokens"] is not None
        or type(value["model_call_id"]) is not int
        or value["model_call_id"] != model_call_id
        or model_call_id <= 0
        or type(value["project_id"]) is not int
        or value["project_id"] <= 0
        or type(value["snapshot_id"]) is not int
        or value["snapshot_id"] <= 0
        or type(value["framed_payload_utf8_bytes"]) is not int
        or value["framed_payload_utf8_bytes"] < 0
        or type(value["conservative_input_token_upper_bound"]) is not int
        or value["conservative_input_token_upper_bound"] < 0
        or value["conservative_input_token_upper_bound"]
        != value["framed_payload_utf8_bytes"]
        or type(value["admitted_target_count"]) is not int
        or value["admitted_target_count"] < 0
        or type(value["denied_target_count"]) is not int
        or value["denied_target_count"] < 0
        or value["context_admission_state"]
        not in {"all_targets_admitted", "contains_denied_targets"}
        or value["coverage_state"]
        not in {"supported_context_complete", "partial_fail_visible"}
        or value["budget_fit_state"]
        not in {
            "fit_by_conservative_upper_bound",
            "not_fit_by_conservative_upper_bound",
        }
    ):
        raise _upstream_inconsistent()

    for field in (
        "call_identity_hash",
        "snapshot_hash",
        "candidate_set_hash",
        "qualification_hash",
        "authorization_hash",
        "manifest_core_hash",
        "budget_profile_hash",
        "token_framing_accounting_hash",
        "framed_payload_hash",
        "final_context_manifest_hash",
    ):
        if not _is_sha256(value[field]):
            raise _upstream_inconsistent()
    for field in (
        "task_type",
        "provider",
        "model_id",
        "model_version",
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
        "counting_policy_version",
        "framing_policy_version",
    ):
        if type(value[field]) is not str or not value[field].strip():
            raise _upstream_inconsistent()

    unsupported = value["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()
    # Historical manifests used complete for known-but-excluded targets.
    # New producers mark those exclusions partial; preserve their old hashes.
    if value["coverage_state"] == "supported_context_complete" and unsupported:
        raise _upstream_inconsistent()
    if value["coverage_state"] == "partial_fail_visible" and not (unsupported or value["denied_targets"]):
        raise _upstream_inconsistent()

    admitted_ordinals, admitted_targets = _validate_target_metadata(
        value["admitted_targets"], fields=_ADMITTED_TARGET_KEYS, admitted=True
    )
    denied_ordinals, denied_targets = _validate_target_metadata(
        value["denied_targets"], fields=_DENIED_TARGET_KEYS, admitted=False
    )
    if value["admitted_target_count"] != len(admitted_ordinals):
        raise _upstream_inconsistent()
    if value["denied_target_count"] != len(denied_ordinals):
        raise _upstream_inconsistent()
    if admitted_targets & denied_targets:
        raise _upstream_inconsistent()
    all_ordinals = admitted_ordinals | denied_ordinals
    total_targets = value["admitted_target_count"] + value["denied_target_count"]
    if total_targets <= 0 or all_ordinals != set(range(1, total_targets + 1)):
        raise _upstream_inconsistent()

    has_denied = bool(denied_ordinals)
    if has_denied != (value["context_admission_state"] == "contains_denied_targets"):
        raise _upstream_inconsistent()
    expected_readiness = (
        "blocked_context_denied"
        if has_denied and not any(
            item["target_type"] == "git_file_fact" for item in value["admitted_targets"]
        )
        else (
            "blocked_context_budget"
            if value["budget_fit_state"] == "not_fit_by_conservative_upper_bound"
            else "ready_for_gateway_evaluation"
        )
    )
    if value["local_request_readiness_state"] != expected_readiness:
        raise _upstream_inconsistent()

    expected_hash = _stable_hash(
        {field: deepcopy(value[field]) for field in _FINAL_MANIFEST_HASH_KEYS}
    )
    if expected_hash != value["final_context_manifest_hash"]:
        raise _upstream_inconsistent()
    return value


def _field_rule_descriptor(rule: object) -> dict[str, object]:
    return {
        "kind": getattr(rule, "kind"),
        "non_empty": getattr(rule, "non_empty"),
        "min_items": getattr(rule, "min_items"),
        "allowed_values": list(getattr(rule, "allowed_values")),
        "constant": deepcopy(getattr(rule, "constant")),
    }


def _object_schema_descriptor(schema: object) -> dict[str, object]:
    return {
        "fields": [
            {"name": name, "rule": _field_rule_descriptor(rule)}
            for name, rule in getattr(schema, "fields")
        ],
        "required_fields": list(getattr(schema, "required_fields")),
        "evidence_field": getattr(schema, "evidence_field"),
    }


def _result_contract_descriptor(task_type: str) -> dict[str, object]:
    if task_type not in TASK_TYPES or task_type not in RESULT_FIELDS or task_type not in RESULT_FIELD_RULES:
        raise _upstream_inconsistent()
    nested_by_field: dict[str, object] = {}
    for field, schema_name in RESULT_ARRAY_ITEM_SCHEMAS.get(task_type, {}).items():
        schema = NESTED_OBJECT_SCHEMAS.get(schema_name)
        if schema is None:
            raise _upstream_inconsistent()
        nested_by_field[field] = {
            "schema_name": schema_name,
            "schema": _object_schema_descriptor(schema),
        }
    descriptor: dict[str, object] = {
        "task_type": task_type,
        "result_fields": [
            {"name": name, "rule": _field_rule_descriptor(rule)}
            for name, rule in RESULT_FIELD_RULES[task_type]
        ],
        "required_fields": list(RESULT_FIELDS[task_type]),
        "array_item_schemas": nested_by_field,
        "new_report_contract": None,
    }
    if task_type == "daily_report_regenerate":
        descriptor["new_report_contract"] = _result_contract_descriptor(
            "daily_report_generate"
        )
    return descriptor


def _example_from_object_schema(schema: object) -> dict[str, object]:
    if not isinstance(schema, Mapping) or not isinstance(schema.get("fields"), list):
        raise _request_plan_inconsistent()
    result: dict[str, object] = {}
    for field in schema["fields"]:
        if not isinstance(field, Mapping) or type(field.get("name")) is not str:
            raise _request_plan_inconsistent()
        result[field["name"]] = _example_from_rule(field.get("rule"), nested=None)
    if list(result) != list(schema.get("required_fields", ())):
        raise _request_plan_inconsistent()
    return result


def _example_from_rule(rule: object, *, nested: object) -> object:
    if not isinstance(rule, Mapping):
        raise _request_plan_inconsistent()
    kind = rule.get("kind")
    constant = rule.get("constant")
    allowed_values = rule.get("allowed_values")
    min_items = rule.get("min_items")
    if constant is not None:
        return deepcopy(constant)
    if kind == "string":
        if isinstance(allowed_values, list) and allowed_values:
            return deepcopy(allowed_values[0])
        return "string"
    if kind == "bool":
        return False
    if kind == "string_list":
        count = min_items if type(min_items) is int and min_items > 0 else 1
        return ["string" for _ in range(count)]
    if kind == "array":
        if isinstance(nested, Mapping):
            return [_example_from_object_schema(nested)]
        return []
    if kind == "object":
        return {}
    raise _request_plan_inconsistent()


def _expected_structural_example(descriptor: object) -> dict[str, object]:
    if not isinstance(descriptor, Mapping):
        raise _request_plan_inconsistent()
    fields = descriptor.get("result_fields")
    required = descriptor.get("required_fields")
    nested_by_field = descriptor.get("array_item_schemas")
    if not isinstance(fields, list) or not isinstance(required, list) or not isinstance(nested_by_field, Mapping):
        raise _request_plan_inconsistent()
    result: dict[str, object] = {}
    for field in fields:
        if not isinstance(field, Mapping) or type(field.get("name")) is not str:
            raise _request_plan_inconsistent()
        name = field["name"]
        nested_record = nested_by_field.get(name)
        nested_schema = nested_record.get("schema") if isinstance(nested_record, Mapping) else None
        result[name] = _example_from_rule(field.get("rule"), nested=nested_schema)
    if list(result) != required:
        raise _request_plan_inconsistent()
    return result


def _derive_json_material(task_type: str) -> tuple[dict[str, object], dict[str, object], str]:
    descriptor = _result_contract_descriptor(task_type)
    if task_type != _ACTIVATION_TASK_TYPE:
        raise _request_plan_inconsistent()
    example = _expected_structural_example(descriptor)
    instruction = (
        "Return exactly one valid json object and no surrounding text. "
        "The json object must conform exactly to the formal result contract descriptor below; "
        "do not add, remove, rename, or reinterpret fields. "
        f"Formal result contract descriptor: {_canonical_text(descriptor)}\n"
        "Expected structural json example derived from that same descriptor: "
        f"{_canonical_text(example)}"
    )
    return descriptor, example, instruction


def _budget_values(budget_record: object) -> tuple[int, int]:
    if not isinstance(budget_record, Mapping):
        raise _upstream_inconsistent()
    reserved_output = budget_record.get("reserved_output_tokens")
    safety_margin = budget_record.get("safety_margin_tokens")
    if (
        type(reserved_output) is not int
        or reserved_output < 0
        or type(safety_margin) is not int
        or safety_margin < 0
    ):
        raise _upstream_inconsistent()
    return reserved_output, safety_margin


def _task_envelope(manifest: Mapping[str, object]) -> dict[str, object]:
    return {
        "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"],
        "provider": manifest["provider"],
        "model_id": manifest["model_id"],
        "model_version": manifest["model_version"],
        "task_type": manifest["task_type"],
        "output_schema_version": manifest["output_schema_version"],
    }


def _build_messages(
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    instruction: str,
) -> list[dict[str, str]]:
    try:
        payload_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _payload_rebind_failed() from exc
    system_content = f"{PROMPT_POLICY_TEXT}\n{instruction}"
    user_content = (
        f"Task envelope (canonical json): {_canonical_text(_task_envelope(manifest))}\n"
        f"Frozen context payload:\n{payload_text}"
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _provider_request_shape(
    *,
    manifest: Mapping[str, object],
    messages: list[dict[str, str]],
    reserved_output_tokens: int,
) -> dict[str, object]:
    return {
        "model": manifest["model_id"],
        "messages": messages,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": reserved_output_tokens,
    }


def _build_gateway_request_plan(
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    reserved_output, safety_margin = _budget_values(budget_record)
    descriptor, example, instruction = _derive_json_material(str(manifest["task_type"]))
    messages = _build_messages(manifest=manifest, payload=payload, instruction=instruction)
    provider_request = _provider_request_shape(
        manifest=manifest,
        messages=messages,
        reserved_output_tokens=reserved_output,
    )
    provider_request_bytes = len(_canonical_bytes(provider_request))
    plan: dict[str, object] = {
        "schema_version": "model_gateway_request_plan_v1",
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "json_instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "messages_hash": _stable_hash(messages),
        "task_envelope": _task_envelope(manifest),
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": len(payload),
        "budget_profile_hash": manifest["budget_profile_hash"],
        "reserved_output_tokens": reserved_output,
        "safety_margin_tokens": safety_margin,
        "provider_request_utf8_bytes": provider_request_bytes,
        "conservative_local_request_upper_bound": (
            provider_request_bytes + reserved_output + safety_margin
        ),
        "messages": messages,
    }
    plan["request_envelope_hash"] = _stable_hash(
        {field: deepcopy(plan[field]) for field in _REQUEST_PLAN_HASH_KEYS}
    )
    _validate_request_plan(
        plan,
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
    )
    return plan


def _validate_request_plan(
    plan: object,
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    budget_record: Mapping[str, object],
) -> None:
    if not isinstance(plan, Mapping) or tuple(plan) != _REQUEST_PLAN_KEYS:
        raise _request_plan_inconsistent()
    reserved_output, safety_margin = _budget_values(budget_record)
    descriptor, example, instruction = _derive_json_material(str(manifest["task_type"]))
    messages = _build_messages(manifest=manifest, payload=payload, instruction=instruction)
    provider_request = _provider_request_shape(
        manifest=manifest,
        messages=messages,
        reserved_output_tokens=reserved_output,
    )
    provider_request_bytes = len(_canonical_bytes(provider_request))
    expected_values = {
        "schema_version": "model_gateway_request_plan_v1",
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "json_instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "messages_hash": _stable_hash(messages),
        "task_envelope": _task_envelope(manifest),
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": len(payload),
        "budget_profile_hash": manifest["budget_profile_hash"],
        "reserved_output_tokens": reserved_output,
        "safety_margin_tokens": safety_margin,
        "provider_request_utf8_bytes": provider_request_bytes,
        "conservative_local_request_upper_bound": (
            provider_request_bytes + reserved_output + safety_margin
        ),
        "messages": messages,
    }
    if any(plan.get(key) != value for key, value in expected_values.items()):
        raise _request_plan_inconsistent()
    if plan["request_envelope_hash"] != _stable_hash(
        {field: deepcopy(plan[field]) for field in _REQUEST_PLAN_HASH_KEYS}
    ):
        raise _request_plan_inconsistent()


def _materialize_payload_rebound(
    *,
    manifest: Mapping[str, object],
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> bytes:
    materialized = token_framing._materialize_context_payload_transient(
        model_call_id=model_call_id,
        budget_record=budget_record,
    )
    if not isinstance(materialized, Mapping):
        raise _payload_rebind_failed()
    payload = materialized.get("payload")
    if type(payload) is not bytes:
        raise _payload_rebind_failed()
    payload_hash = hashlib.sha256(payload).hexdigest()
    payload_bytes = len(payload)
    if (
        materialized.get("framed_payload_hash") != payload_hash
        or materialized.get("framed_payload_utf8_bytes") != payload_bytes
        or payload_hash != manifest["framed_payload_hash"]
        or payload_bytes != manifest["framed_payload_utf8_bytes"]
    ):
        raise _payload_rebind_failed()
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _payload_rebind_failed() from exc
    return payload


def _validate_capability(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or tuple(value) != _CAPABILITY_KEYS:
        return None
    if any(value.get(key) != expected for key, expected in _EXPECTED_CAPABILITY.items()):
        return None
    return value


def _manifest_matches_activation(
    manifest: Mapping[str, object], capability: Mapping[str, object]
) -> bool:
    return (
        manifest["provider"] == _ACTIVATION_PROVIDER == capability["provider"]
        and manifest["model_id"] == _ACTIVATION_MODEL_ID == capability["model_id"]
        and manifest["model_version"] == _ACTIVATION_MODEL_VERSION == capability["model_version"]
        and manifest["task_type"] == _ACTIVATION_TASK_TYPE == capability["task_type"]
        and manifest["output_schema_version"]
        == _ACTIVATION_OUTPUT_SCHEMA_VERSION
        == capability["output_schema_version"]
    )


def _evidence_hash_valid(value: Mapping[str, object], *, hash_field: str) -> bool:
    if not _is_sha256(value.get(hash_field)):
        return False
    unsigned = {key: deepcopy(value[key]) for key in value if key != hash_field}
    return value[hash_field] == _stable_hash(unsigned)


def _expected_data_scope_hash(manifest: Mapping[str, object]) -> str:
    return _stable_hash(
        {
            "provider": _ACTIVATION_PROVIDER,
            "model_id": _ACTIVATION_MODEL_ID,
            "model_version": _ACTIVATION_MODEL_VERSION,
            "model_call_id": manifest["model_call_id"],
            "call_identity_hash": manifest["call_identity_hash"],
            "final_context_manifest_hash": manifest["final_context_manifest_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
            "task_type": manifest["task_type"],
            "output_schema_version": manifest["output_schema_version"],
            "purpose_id": _ACTIVATION_PURPOSE_ID,
        }
    )


def _validate_current_authority(
    value: object,
    *,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _AUTHORITY_KEYS:
        raise _current_authority_inconsistent()
    qualification = value.get("qualification")
    authorization = value.get("authorization")
    if (
        not isinstance(qualification, Mapping)
        or tuple(qualification) != _QUALIFICATION_KEYS
        or not isinstance(authorization, Mapping)
        or tuple(authorization) != _AUTHORIZATION_KEYS
    ):
        raise _current_authority_inconsistent()

    expected_scope_hash = _expected_data_scope_hash(manifest)
    top_level_ok = (
        value["schema_version"] == "deepseek_current_authority_v1"
        and value["model_call_id"] == manifest["model_call_id"]
        and value["call_identity_hash"] == manifest["call_identity_hash"]
        and value["provider"] == _ACTIVATION_PROVIDER
        and value["model_id"] == _ACTIVATION_MODEL_ID
        and value["model_version"] == _ACTIVATION_MODEL_VERSION
        and value["context_window_tokens"] == 1_000_000
        and value["max_output_tokens"] == 384_000
        and value["purpose_id"] == _ACTIVATION_PURPOSE_ID
        and value["data_scope_hash"] == expected_scope_hash
        and _is_sha256(value["authority_hash"])
    )
    qualification_ok = (
        qualification["schema_version"] == "deepseek_current_model_qualification_v1"
        and qualification["authority_source_id"] == "deepseek_official_models_and_metadata"
        and qualification["authority_source_version"] == "v1"
        and qualification["authority_ref"]
        == {
            "model_list": "deepseek_api_models",
            "model_metadata": "deepseek_api_docs_models_pricing",
        }
        and qualification["provider"] == _ACTIVATION_PROVIDER
        and qualification["model_id"] == _ACTIVATION_MODEL_ID
        and qualification["model_version"] == _ACTIVATION_MODEL_VERSION
        and qualification["rule_version"] == manifest["rule_version"]
        and qualification["output_schema_version"] == manifest["output_schema_version"]
        and qualification["benchmark_sample_pack_version"]
        == manifest["benchmark_sample_pack_version"]
        and qualification["qualification_status"] == "qualified"
        and qualification["context_window_tokens"] == value["context_window_tokens"]
        and qualification["max_output_tokens"] == value["max_output_tokens"]
        and type(qualification["checked_at"]) is str
        and bool(qualification["checked_at"].strip())
        and _is_sha256(qualification["models_source_hash"])
        and _is_sha256(qualification["metadata_source_hash"])
        and _evidence_hash_valid(qualification, hash_field="evidence_hash")
    )
    authorization_ok = (
        authorization["schema_version"] == "deepseek_current_data_authorization_v1"
        and authorization["authority_source_id"] == "windows_process_env_human_permit"
        and authorization["authority_source_version"] == "v1"
        and authorization["authority_ref"] == "process-env/exact-scope"
        and authorization["provider"] == _ACTIVATION_PROVIDER
        and authorization["authorized"] is True
        and authorization["valid"] is True
        and authorization["purpose_id"] == _ACTIVATION_PURPOSE_ID
        and authorization["data_scope_hash"] == expected_scope_hash
        and _evidence_hash_valid(authorization, hash_field="evidence_hash")
    )
    authority_hash_ok = value["authority_hash"] == _stable_hash(
        {key: deepcopy(value[key]) for key in _AUTHORITY_KEYS if key != "authority_hash"}
    )
    if not (top_level_ok and qualification_ok and authorization_ok and authority_hash_ok):
        raise _current_authority_inconsistent()
    return {
        "qualification_ref": deepcopy(qualification["authority_ref"]),
        "qualification_evidence_hash": qualification["evidence_hash"],
        "authorization_ref": deepcopy(authorization["authority_ref"]),
        "authorization_evidence_hash": authorization["evidence_hash"],
        "data_scope_hash": value["data_scope_hash"],
        "purpose_id": value["purpose_id"],
        "context_window_tokens": value["context_window_tokens"],
        "max_output_tokens": value["max_output_tokens"],
    }


def _map_current_authority_failure(exc: HTTPException) -> str:
    detail = exc.detail
    code = detail.get("code") if isinstance(detail, Mapping) else None
    if code in {
        "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE",
        "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE",
    }:
        return "unavailable"
    if code in {
        "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED",
        "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED",
        "DEEPSEEK_AUTHORITY_PROVIDER_CONSTRAINT_CHANGED",
    }:
        return "qualification"
    if code == "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED":
        return "authorization"
    raise _current_authority_inconsistent() from exc


def _build_result(
    *,
    manifest: Mapping[str, object],
    request_envelope_hash: str | None,
    final_request_fit_state: str,
    provider_compatibility_state: str,
    local_gateway_state: str,
    qualification_authority_state: str,
    authorization_authority_state: str,
    qualification_authority_ref: object = None,
    qualification_evidence_hash: object = None,
    authorization_authority_ref: object = None,
    authorization_evidence_hash: object = None,
    authorization_data_scope_hash: object = None,
    authorization_purpose_id: object = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"],
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "provider": manifest["provider"],
        "model_id": manifest["model_id"],
        "model_version": manifest["model_version"],
        "task_type": manifest["task_type"],
        "output_schema_version": manifest["output_schema_version"],
        "current_qualification_authority_state": qualification_authority_state,
        "current_qualification_authority_ref": deepcopy(qualification_authority_ref),
        "current_qualification_evidence_hash": qualification_evidence_hash,
        "current_authorization_authority_state": authorization_authority_state,
        "current_authorization_authority_ref": deepcopy(authorization_authority_ref),
        "current_authorization_evidence_hash": authorization_evidence_hash,
        "current_authorization_data_scope_hash": authorization_data_scope_hash,
        "current_authorization_purpose_id": authorization_purpose_id,
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": manifest["framed_payload_utf8_bytes"],
        "request_envelope_hash": request_envelope_hash,
        "final_request_fit_state": final_request_fit_state,
        "provider_compatibility_state": provider_compatibility_state,
        "local_gateway_state": local_gateway_state,
        "network_send_state": NETWORK_SEND_STATE,
    }
    result["gateway_preflight_hash"] = _stable_hash(
        {field: deepcopy(result[field]) for field in _RESULT_HASH_KEYS}
    )
    if tuple(result) != _RESULT_KEYS:
        raise _internal_inconsistent()
    return result


def _blocked_authority_result(
    *,
    manifest: Mapping[str, object],
    plan: Mapping[str, object],
    failure: str,
) -> dict[str, object]:
    if failure == "unavailable":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=str(plan["request_envelope_hash"]),
            final_request_fit_state="not_evaluated_current_authority",
            provider_compatibility_state="compatible",
            local_gateway_state="blocked_current_authority_unavailable",
            qualification_authority_state="unavailable",
            authorization_authority_state="not_evaluated",
        )
    if failure == "qualification":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=str(plan["request_envelope_hash"]),
            final_request_fit_state="not_evaluated_current_authority",
            provider_compatibility_state="compatible",
            local_gateway_state="blocked_current_qualification",
            qualification_authority_state="not_verified",
            authorization_authority_state="not_evaluated",
        )
    if failure == "authorization":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=str(plan["request_envelope_hash"]),
            final_request_fit_state="not_evaluated_current_authority",
            provider_compatibility_state="compatible",
            local_gateway_state="blocked_current_authorization",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_authorized",
        )
    raise _internal_inconsistent()


def build_model_gateway_preflight(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Resolve Gateway readiness without sending provider business payloads."""
    _revoke_transient_material_for_current_context()
    manifest = _validate_manifest(
        build_final_context_manifest(
            model_call_id=model_call_id,
            budget_record=budget_record,
        ),
        model_call_id=model_call_id,
    )

    if manifest["local_request_readiness_state"] != "ready_for_gateway_evaluation":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=None,
            final_request_fit_state="not_evaluated",
            provider_compatibility_state="not_evaluated",
            local_gateway_state="blocked_final_manifest",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_evaluated",
        )

    payload = _materialize_payload_rebound(
        manifest=manifest,
        model_call_id=model_call_id,
        budget_record=budget_record,
    )

    capability = _validate_capability(
        deepseek_transport.get_deepseek_transport_capability()
    )
    if capability is None or not _manifest_matches_activation(manifest, capability):
        return _build_result(
            manifest=manifest,
            request_envelope_hash=None,
            final_request_fit_state="not_evaluated",
            provider_compatibility_state="incompatible",
            local_gateway_state="blocked_provider_compatibility",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_evaluated",
        )

    plan = _build_gateway_request_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
    )

    try:
        raw_authority = deepseek_current_authority.resolve_deepseek_current_authority(
            model_call_id=model_call_id,
            final_context_manifest_hash=str(manifest["final_context_manifest_hash"]),
            framed_payload_hash=str(manifest["framed_payload_hash"]),
        )
    except HTTPException as exc:
        return _blocked_authority_result(
            manifest=manifest,
            plan=plan,
            failure=_map_current_authority_failure(exc),
        )

    authority = _validate_current_authority(raw_authority, manifest=manifest)
    reserved_output = plan["reserved_output_tokens"]
    conservative_upper_bound = plan["conservative_local_request_upper_bound"]
    fit = (
        type(reserved_output) is int
        and reserved_output > 0
        and reserved_output <= capability["max_output_tokens"]
        and reserved_output <= authority["max_output_tokens"]
        and type(conservative_upper_bound) is int
        and conservative_upper_bound <= authority["context_window_tokens"]
    )
    common = {
        "manifest": manifest,
        "request_envelope_hash": str(plan["request_envelope_hash"]),
        "provider_compatibility_state": "compatible",
        "qualification_authority_state": "verified",
        "authorization_authority_state": "verified",
        "qualification_authority_ref": authority["qualification_ref"],
        "qualification_evidence_hash": authority["qualification_evidence_hash"],
        "authorization_authority_ref": authority["authorization_ref"],
        "authorization_evidence_hash": authority["authorization_evidence_hash"],
        "authorization_data_scope_hash": authority["data_scope_hash"],
        "authorization_purpose_id": authority["purpose_id"],
    }
    if not fit:
        return _build_result(
            **common,
            final_request_fit_state="not_fit_by_conservative_upper_bound",
            local_gateway_state="blocked_request_budget",
        )
    ready_result = _build_result(
        **common,
        final_request_fit_state="fit_by_conservative_upper_bound",
        local_gateway_state="ready_for_provider_transport",
    )
    _TRANSIENT_MATERIAL_SLOT.set(
        {
            "_claim": _TransientClaim(),
            "model_call_id": model_call_id,
            "budget_record_hash": _stable_hash(budget_record),
            "manifest": deepcopy(manifest),
            "payload": payload,
            "plan": deepcopy(plan),
        }
    )
    return ready_result


def materialize_model_gateway_request_transient(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Atomically consume one shared ready request material record; never performs I/O."""
    cached = _TRANSIENT_MATERIAL_SLOT.get()
    _TRANSIENT_MATERIAL_SLOT.set(None)
    if not isinstance(cached, Mapping):
        raise _error(
            "MODEL_GATEWAY_TRANSIENT_NOT_READY",
            "不存在当前 execution context 可见的 ready Gateway transient material。",
        )
    _claim_transient_material(cached)
    if (
        cached.get("model_call_id") != model_call_id
        or cached.get("budget_record_hash") != _stable_hash(budget_record)
    ):
        raise _error(
            "MODEL_GATEWAY_TRANSIENT_MATERIAL_MISMATCH",
            "Transient request 的 model_call/budget binding 与 ready Preflight 不一致。",
        )
    manifest = _validate_manifest(deepcopy(cached.get("manifest")), model_call_id=model_call_id)
    payload = cached.get("payload")
    plan = cached.get("plan")
    if type(payload) is not bytes:
        raise _request_plan_inconsistent()
    _validate_request_plan(
        plan,
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
    )
    if not isinstance(plan, Mapping):
        raise _request_plan_inconsistent()
    return {
        "schema_version": TRANSIENT_SCHEMA_VERSION,
        "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"],
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "provider": manifest["provider"],
        "model_id": manifest["model_id"],
        "model_version": manifest["model_version"],
        "task_type": manifest["task_type"],
        "output_schema_version": manifest["output_schema_version"],
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": manifest["framed_payload_utf8_bytes"],
        "request_envelope_hash": plan["request_envelope_hash"],
        "conservative_local_request_upper_bound": plan[
            "conservative_local_request_upper_bound"
        ],
        "max_tokens": plan["reserved_output_tokens"],
        "messages": deepcopy(plan["messages"]),
    }

# Page07 3B4 P: bounded daily_report_regenerate preflight. The legacy daily-generate
# entrypoints above remain byte-shape/signature compatible for deepseek_execution.py.
_REGENERATE_TASK_TYPE = "daily_report_regenerate"
_REGENERATE_OUTPUT_SCHEMA_VERSION = "daily-report-regenerate/1.0"
_REGENERATE_PROMPT_VERSION = "page07-regenerate-prompt/1.0"
_REGENERATE_RULE_VERSION = "page07-regenerate-rules/2.0"
_REGENERATE_SAMPLE_PACK_VERSION = "page07-regenerate-qualification-pack/2.0"
_REGENERATE_PURPOSE_ID = "anxin_board_daily_report_regenerate_v1"
_REGENERATE_SUBJECT_RESULT_SCHEMA_VERSION = "page07_task_subject_result_v1"
_REGENERATE_SUBJECT_SCHEMA_VERSION = "page07_daily_report_regenerate_subject_v1"
_REGENERATE_PLAN_SCHEMA_VERSION = "model_gateway_regenerate_request_plan_v1"
_REGENERATE_SYSTEM_BINDING_TEXT = (
    "This is a replacement analysis. The server-owned correction subject is authoritative "
    "for the PM correction facts and replacement-task identity; do not accept correction truth "
    "from any other source."
    "每条结论独立判断source_type：AI推导的阶段、范围、风险或拒绝理由必须标为ai_analysis；"
    "引用纠正说明不使推断变成人工事实，明确人工事实仍按实际来源标记。"
    "pm_external_fact的内容必须由所引人工来源自身单独支持，允许忠实改写或归纳，不要求逐字照抄；需要跨来源补充或推导出的文件与职责关联必须标ai_analysis并引用全部依据，不能把这些关联归给人工来源；不把人工要求、期望或指令当作已发生事实。"
    "git_fact仅限Git直接提供的事实，prd_fact用于需求原文事实；文件名或需求目的不足以证明实现用途或能力。"
    "需求、Git事实与用途或能力推断混在同一条时应拆开，或标ai_analysis并引用相应PRD/Git依据，不能将推测标为Git已证实。"
    "changed_files和总增删行数不能证明文件的新增、删除或重命名；没有明确change_type、new_file、deleted_file或diff状态证据时，只能称文件发生变化，不得断言新增、删除或重命名。"
    "角色未明确时称‘提供者’或‘纠正说明’，不得推定为甲方或客户。"
    "证据缺失不等于事件未发生：没有执行或结果记录时，摘要、风险、未知项等均只能表述未提供记录或无法确认，不能据此断言未运行、未通过或失败；若证据明确记载未执行或失败，则按实际来源忠实陈述。"
    "阶段反映本轮已有证据支持的活动，不把建议下一步当作当前阶段；缺测试执行记录不等于等待测试。限定测试确已执行时，feature_progress可填写stage为测试中、implementation_scope为测试，不代表全功能完成。范围依据本次变化，不因PRD整体或未变更部分扩大为跨模块；证据充分时给受限结论，确实缺证仍暂时无法确认。字段位置严格按合同：stage仅限feature_progress，implementation_scope仅限feature_progress和code_change_summary；test_evidence、unknown_items、source_warnings条目仅含content/source_type/evidence_ids，不添加stage或implementation_scope；risks仍只使用合同规定的content/risk_level/source_type/evidence_ids。"
)
_REGENERATE_USER_TEMPLATE = (
    "Task envelope (canonical json): {task_envelope}\n"
    "Server-owned correction subject (canonical json): {task_subject}\n"
    "Frozen context payload:\n{payload_text}"
)
_EXPECTED_REGENERATE_CAPABILITY = {
    "schema_version": "deepseek_transport_capability_v1",
    "provider": _ACTIVATION_PROVIDER,
    "model_id": _ACTIVATION_MODEL_ID,
    "model_version": _ACTIVATION_MODEL_VERSION,
    "endpoint_origin": "https://api.deepseek.com",
    "endpoint_path": "/chat/completions",
    "task_type": _REGENERATE_TASK_TYPE,
    "output_schema_version": _REGENERATE_OUTPUT_SCHEMA_VERSION,
    "response_format": "json_object",
    "stream": False,
    "thinking": "enabled",
    "reasoning_effort": "high",
    "max_output_tokens": 384_000,
}
_REGENERATE_REQUEST_PLAN_KEYS = (
    "schema_version",
    "prompt_version",
    "prompt_policy_version",
    "json_instruction_version",
    "formal_result_contract_hash",
    "expected_structural_example_hash",
    "json_instruction_hash",
    "prompt_contract_hash",
    "sampling_parameters_hash",
    "task_subject_hash",
    "effective_input_hash",
    "messages_hash",
    "task_envelope",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "budget_profile_hash",
    "reserved_output_tokens",
    "safety_margin_tokens",
    "provider_request_utf8_bytes",
    "conservative_local_request_upper_bound",
    "messages",
    "request_envelope_hash",
)
_REGENERATE_REQUEST_PLAN_HASH_KEYS = tuple(
    key for key in _REGENERATE_REQUEST_PLAN_KEYS if key != "request_envelope_hash"
)
_REGISTRY_QUALIFICATION_KEYS = (
    "schema_version",
    "authority_source_id",
    "authority_source_version",
    "authority_ref",
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "ai_contract_schema_version",
    "output_schema_version",
    "prompt_version",
    "prompt_contract_hash",
    "rule_version",
    "sample_pack_version",
    "sampling_parameters_hash",
    "qualification_status",
    "sample_manifest_hash",
    "qualification_harness_commit",
    "evidence_manifest_hash",
    "review_ref",
    "record_hash",
    "context_window_tokens",
    "max_output_tokens",
    "evidence_hash",
)


def _regenerate_selector_invalid() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "code": "MODEL_GATEWAY_REGENERATE_SELECTOR_INVALID",
            "message": "report_version_id selector 必须是 SQLite signed 范围内的正整数。",
        },
    )


def _require_regenerate_report_selector(value: object) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise _regenerate_selector_invalid()
    return value


def _validate_regenerate_model_call(
    value: object, *, manifest: Mapping[str, object], model_call_id: int
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _upstream_inconsistent()
    expected = {
        "model_call_id": model_call_id,
        "project_id": manifest["project_id"],
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_hash": manifest["snapshot_hash"],
        "candidate_set_hash": manifest["candidate_set_hash"],
        "task_type": _REGENERATE_TASK_TYPE,
        "provider": _ACTIVATION_PROVIDER,
        "model_id": _ACTIVATION_MODEL_ID,
        "model_version": _ACTIVATION_MODEL_VERSION,
        "rule_version": _REGENERATE_RULE_VERSION,
        "output_schema_version": _REGENERATE_OUTPUT_SCHEMA_VERSION,
        "benchmark_sample_pack_version": _REGENERATE_SAMPLE_PACK_VERSION,
        "qualification_hash": manifest["qualification_hash"],
        "authorization_hash": manifest["authorization_hash"],
        "call_identity_hash": manifest["call_identity_hash"],
    }
    if any(value.get(field) != expected_value for field, expected_value in expected.items()):
        raise _upstream_inconsistent()
    local_task_id = value.get("local_task_id")
    if type(local_task_id) is not str or not local_task_id.strip():
        raise _upstream_inconsistent()
    return dict(value)


def _read_regenerate_model_call(
    *, manifest: Mapping[str, object], model_call_id: int
) -> dict[str, object]:
    from app.model_call_ledger import get_model_call

    try:
        raw = get_model_call(model_call_id)
    except HTTPException as exc:
        raise _upstream_inconsistent() from exc
    return _validate_regenerate_model_call(
        raw,
        manifest=manifest,
        model_call_id=model_call_id,
    )


def _validate_regenerate_subject(
    value: object,
    *,
    call: Mapping[str, object],
    manifest: Mapping[str, object],
    report_version_id: int,
) -> dict[str, object]:
    from app import context_redaction

    outer_keys = {
        "schema_version",
        "subject_schema_version",
        "task_type",
        "subject",
        "task_subject_hash",
        "redaction_policy_id",
        "redaction_policy_hash",
        "redaction_stats",
        "redaction_match_count",
    }
    if not isinstance(value, Mapping) or set(value) != outer_keys:
        raise _upstream_inconsistent()
    stats = value.get("redaction_stats")
    match_count = value.get("redaction_match_count")
    if (
        value.get("schema_version") != _REGENERATE_SUBJECT_RESULT_SCHEMA_VERSION
        or value.get("subject_schema_version") != _REGENERATE_SUBJECT_SCHEMA_VERSION
        or value.get("task_type") != _REGENERATE_TASK_TYPE
        or not _is_sha256(value.get("task_subject_hash"))
        or value.get("redaction_policy_id") != context_redaction.POLICY_ID
        or value.get("redaction_policy_hash") != context_redaction.REDACTION_POLICY_HASH
        or not isinstance(stats, Mapping)
        or type(match_count) is not int
        or match_count < 0
        or any(rule_id not in {"R1", "R2", "R3", "R4", "R5", "R6"} for rule_id in stats)
        or any(type(count) is not int or count <= 0 for count in stats.values())
        or sum(stats.values()) != match_count
    ):
        raise _upstream_inconsistent()
    subject = value.get("subject")
    if not isinstance(subject, Mapping) or _stable_hash(subject) != value["task_subject_hash"]:
        raise _upstream_inconsistent()
    if (
        subject.get("schema_version") != _REGENERATE_SUBJECT_SCHEMA_VERSION
        or subject.get("task_type") != _REGENERATE_TASK_TYPE
        or subject.get("project_id") != call["project_id"]
        or subject.get("redaction_policy_id") != value["redaction_policy_id"]
        or subject.get("redaction_policy_hash") != value["redaction_policy_hash"]
    ):
        raise _upstream_inconsistent()
    source_report = subject.get("source_report")
    request = subject.get("reanalysis_request")
    replacement = subject.get("replacement_task")
    snapshot = subject.get("evidence_snapshot")
    if not all(isinstance(item, Mapping) for item in (source_report, request, replacement, snapshot)):
        raise _upstream_inconsistent()
    if (
        source_report.get("report_version_id") != report_version_id
        or type(source_report.get("model_execution_result_id")) is not int
        or source_report["model_execution_result_id"] <= 0
        or type(source_report.get("model_call_id")) is not int
        or source_report["model_call_id"] <= 0
        or not _is_sha256(source_report.get("report_content_hash"))
        or not _is_sha256(source_report.get("execution_result_hash"))
        or not _is_sha256(source_report.get("call_identity_hash"))
    ):
        raise _upstream_inconsistent()
    if (
        type(request.get("reanalysis_request_id")) is not int
        or request["reanalysis_request_id"] <= 0
        or not _is_sha256(request.get("request_hash"))
    ):
        raise _upstream_inconsistent()
    for text_field, hash_field in (
        ("error_location", "error_location_hash"),
        ("corrected_truth", "corrected_truth_hash"),
        ("correction_basis", "correction_basis_hash"),
        ("correction_source", "correction_source_hash"),
    ):
        text = request.get(text_field)
        if type(text) is not str or not text or not _is_sha256(request.get(hash_field)):
            raise _upstream_inconsistent()
    for field in ("requested_by", "requested_at", "requested_timezone"):
        if type(request.get(field)) is not str or not request[field].strip():
            raise _upstream_inconsistent()
    if (
        type(replacement.get("replacement_task_id")) is not int
        or replacement["replacement_task_id"] <= 0
        or replacement.get("replacement_local_task_id") != call["local_task_id"]
        or not _is_sha256(replacement.get("replacement_task_identity_hash"))
    ):
        raise _upstream_inconsistent()
    if (
        snapshot.get("evidence_snapshot_id") != call["snapshot_id"]
        or snapshot.get("evidence_snapshot_id") != manifest["snapshot_id"]
        or snapshot.get("evidence_snapshot_hash") != manifest["snapshot_hash"]
        or not _is_sha256(snapshot.get("evidence_snapshot_hash"))
    ):
        raise _upstream_inconsistent()
    return deepcopy(dict(value))


def _read_regenerate_subject(
    *,
    call: Mapping[str, object],
    manifest: Mapping[str, object],
    report_version_id: int,
) -> dict[str, object]:
    from app import report_task_subjects

    try:
        raw = report_task_subjects.build_daily_report_regenerate_subject(
            project_id=int(call["project_id"]),
            report_version_id=report_version_id,
        )
    except HTTPException as exc:
        raise _upstream_inconsistent() from exc
    return _validate_regenerate_subject(
        raw,
        call=call,
        manifest=manifest,
        report_version_id=report_version_id,
    )


def _derive_regenerate_json_material() -> tuple[dict[str, object], dict[str, object], str]:
    descriptor = _result_contract_descriptor(_REGENERATE_TASK_TYPE)
    example = _expected_structural_example(descriptor)
    instruction = (
        "Return exactly one valid json object and no surrounding text. "
        "The json object must conform exactly to the formal result contract descriptor below; "
        "do not add, remove, rename, or reinterpret fields. "
        f"Formal result contract descriptor: {_canonical_text(descriptor)}\n"
        "Expected structural json example derived from that same descriptor: "
        f"{_canonical_text(example)}"
    )
    return descriptor, example, instruction


def _regenerate_prompt_contract() -> tuple[dict[str, object], str, str]:
    descriptor, example, instruction = _derive_regenerate_json_material()
    contract = {
        "schema_version": "page07_regenerate_prompt_contract_v1",
        "prompt_version": _REGENERATE_PROMPT_VERSION,
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "prompt_policy_text": PROMPT_POLICY_TEXT,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "json_instruction": instruction,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "subject_schema_version": _REGENERATE_SUBJECT_SCHEMA_VERSION,
        "system_binding_text": _REGENERATE_SYSTEM_BINDING_TEXT,
        "user_template": _REGENERATE_USER_TEMPLATE,
    }
    return contract, _stable_hash(contract), instruction


def _regenerate_sampling_policy() -> tuple[dict[str, object], str]:
    policy = {
        "schema_version": "page07_regenerate_sampling_request_policy_v1",
        "provider": _ACTIVATION_PROVIDER,
        "model_id": _ACTIVATION_MODEL_ID,
        "model_version": _ACTIVATION_MODEL_VERSION,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens_source": "budget_record.reserved_output_tokens",
        "max_tokens_min": 1,
        "max_tokens_max": 384_000,
    }
    return policy, _stable_hash(policy)


def _build_regenerate_messages(
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    instruction: str,
    task_subject: Mapping[str, object],
) -> list[dict[str, str]]:
    try:
        payload_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _payload_rebind_failed() from exc
    system_content = f"{PROMPT_POLICY_TEXT}\n{_REGENERATE_SYSTEM_BINDING_TEXT}\n{instruction}"
    user_content = _REGENERATE_USER_TEMPLATE.format(
        task_envelope=_canonical_text(_task_envelope(manifest)),
        task_subject=_canonical_text(task_subject["subject"]),
        payload_text=payload_text,
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _build_regenerate_gateway_request_plan(
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    budget_record: Mapping[str, object],
    task_subject: Mapping[str, object],
) -> dict[str, object]:
    reserved_output, safety_margin = _budget_values(budget_record)
    prompt_contract, prompt_contract_hash, instruction = _regenerate_prompt_contract()
    _sampling_policy, sampling_parameters_hash = _regenerate_sampling_policy()
    descriptor, example, _ = _derive_regenerate_json_material()
    messages = _build_regenerate_messages(
        manifest=manifest,
        payload=payload,
        instruction=instruction,
        task_subject=task_subject,
    )
    provider_request = _provider_request_shape(
        manifest=manifest,
        messages=messages,
        reserved_output_tokens=reserved_output,
    )
    provider_request_bytes = len(_canonical_bytes(provider_request))
    effective_input_hash = _stable_hash(
        {
            "messages": messages,
            "task_subject_hash": task_subject["task_subject_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
        }
    )
    plan: dict[str, object] = {
        "schema_version": _REGENERATE_PLAN_SCHEMA_VERSION,
        "prompt_version": prompt_contract["prompt_version"],
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "json_instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "prompt_contract_hash": prompt_contract_hash,
        "sampling_parameters_hash": sampling_parameters_hash,
        "task_subject_hash": task_subject["task_subject_hash"],
        "effective_input_hash": effective_input_hash,
        "messages_hash": _stable_hash(messages),
        "task_envelope": _task_envelope(manifest),
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": len(payload),
        "budget_profile_hash": manifest["budget_profile_hash"],
        "reserved_output_tokens": reserved_output,
        "safety_margin_tokens": safety_margin,
        "provider_request_utf8_bytes": provider_request_bytes,
        "conservative_local_request_upper_bound": provider_request_bytes
        + reserved_output
        + safety_margin,
        "messages": messages,
    }
    plan["request_envelope_hash"] = _stable_hash(
        {field: deepcopy(plan[field]) for field in _REGENERATE_REQUEST_PLAN_HASH_KEYS}
    )
    _validate_regenerate_request_plan(
        plan,
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
        task_subject=task_subject,
    )
    return plan


def _validate_regenerate_request_plan(
    plan: object,
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    budget_record: Mapping[str, object],
    task_subject: Mapping[str, object],
) -> None:
    if not isinstance(plan, Mapping) or tuple(plan) != _REGENERATE_REQUEST_PLAN_KEYS:
        raise _request_plan_inconsistent()
    reserved_output, safety_margin = _budget_values(budget_record)
    prompt_contract, prompt_contract_hash, instruction = _regenerate_prompt_contract()
    _sampling_policy, sampling_parameters_hash = _regenerate_sampling_policy()
    descriptor, example, _ = _derive_regenerate_json_material()
    messages = _build_regenerate_messages(
        manifest=manifest,
        payload=payload,
        instruction=instruction,
        task_subject=task_subject,
    )
    provider_request = _provider_request_shape(
        manifest=manifest,
        messages=messages,
        reserved_output_tokens=reserved_output,
    )
    provider_request_bytes = len(_canonical_bytes(provider_request))
    expected_values = {
        "schema_version": _REGENERATE_PLAN_SCHEMA_VERSION,
        "prompt_version": prompt_contract["prompt_version"],
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "json_instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "prompt_contract_hash": prompt_contract_hash,
        "sampling_parameters_hash": sampling_parameters_hash,
        "task_subject_hash": task_subject["task_subject_hash"],
        "effective_input_hash": _stable_hash(
            {
                "messages": messages,
                "task_subject_hash": task_subject["task_subject_hash"],
                "framed_payload_hash": manifest["framed_payload_hash"],
            }
        ),
        "messages_hash": _stable_hash(messages),
        "task_envelope": _task_envelope(manifest),
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": len(payload),
        "budget_profile_hash": manifest["budget_profile_hash"],
        "reserved_output_tokens": reserved_output,
        "safety_margin_tokens": safety_margin,
        "provider_request_utf8_bytes": provider_request_bytes,
        "conservative_local_request_upper_bound": provider_request_bytes
        + reserved_output
        + safety_margin,
        "messages": messages,
    }
    if any(plan.get(field) != expected for field, expected in expected_values.items()):
        raise _request_plan_inconsistent()
    if plan["request_envelope_hash"] != _stable_hash(
        {field: deepcopy(plan[field]) for field in _REGENERATE_REQUEST_PLAN_HASH_KEYS}
    ):
        raise _request_plan_inconsistent()


def _validate_regenerate_capability(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or tuple(value) != _CAPABILITY_KEYS:
        return None
    if any(value.get(key) != expected for key, expected in _EXPECTED_REGENERATE_CAPABILITY.items()):
        return None
    return value


def _load_regenerate_capability() -> Mapping[str, object] | None:
    try:
        raw = deepseek_transport.get_deepseek_transport_capability(
            task_type=_REGENERATE_TASK_TYPE,
            output_schema_version=_REGENERATE_OUTPUT_SCHEMA_VERSION,
        )
    except HTTPException:
        return None
    return _validate_regenerate_capability(raw)


def _expected_regenerate_data_scope_hash(
    *, manifest: Mapping[str, object], plan: Mapping[str, object]
) -> str:
    return _stable_hash(
        {
            "provider": _ACTIVATION_PROVIDER,
            "model_id": _ACTIVATION_MODEL_ID,
            "model_version": _ACTIVATION_MODEL_VERSION,
            "model_call_id": manifest["model_call_id"],
            "call_identity_hash": manifest["call_identity_hash"],
            "final_context_manifest_hash": manifest["final_context_manifest_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
            "request_envelope_hash": plan["request_envelope_hash"],
            "prompt_contract_hash": plan["prompt_contract_hash"],
            "sampling_parameters_hash": plan["sampling_parameters_hash"],
            "task_type": _REGENERATE_TASK_TYPE,
            "output_schema_version": _REGENERATE_OUTPUT_SCHEMA_VERSION,
            "purpose_id": _REGENERATE_PURPOSE_ID,
        }
    )


def _validate_regenerate_current_authority(
    value: object,
    *,
    manifest: Mapping[str, object],
    plan: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _AUTHORITY_KEYS:
        raise _current_authority_inconsistent()
    qualification = value.get("qualification")
    authorization = value.get("authorization")
    if (
        not isinstance(qualification, Mapping)
        or tuple(qualification) != _REGISTRY_QUALIFICATION_KEYS
        or not isinstance(authorization, Mapping)
        or tuple(authorization) != _AUTHORIZATION_KEYS
    ):
        raise _current_authority_inconsistent()
    expected_scope_hash = _expected_regenerate_data_scope_hash(
        manifest=manifest,
        plan=plan,
    )
    top_level_ok = (
        value["schema_version"] == "deepseek_current_authority_v1"
        and value["model_call_id"] == manifest["model_call_id"]
        and value["call_identity_hash"] == manifest["call_identity_hash"]
        and value["provider"] == _ACTIVATION_PROVIDER
        and value["model_id"] == _ACTIVATION_MODEL_ID
        and value["model_version"] == _ACTIVATION_MODEL_VERSION
        and value["context_window_tokens"] == 1_000_000
        and value["max_output_tokens"] == 384_000
        and value["purpose_id"] == _REGENERATE_PURPOSE_ID
        and value["data_scope_hash"] == expected_scope_hash
        and _is_sha256(value["authority_hash"])
    )
    ref = qualification.get("authority_ref")
    qualification_ok = (
        qualification["schema_version"] == "deepseek_current_registry_qualification_v1"
        and qualification["authority_source_id"] == "product_model_qualification_registry"
        and qualification["authority_source_version"] == "v1"
        and isinstance(ref, Mapping)
        and set(ref) == {"record_hash", "review_ref"}
        and ref["record_hash"] == qualification["record_hash"]
        and ref["review_ref"] == qualification["review_ref"]
        and qualification["provider"] == _ACTIVATION_PROVIDER
        and qualification["model_id"] == _ACTIVATION_MODEL_ID
        and qualification["model_version"] == _ACTIVATION_MODEL_VERSION
        and qualification["task_type"] == _REGENERATE_TASK_TYPE
        and qualification["ai_contract_schema_version"] == "ai-agent-contract/1.0"
        and qualification["output_schema_version"] == _REGENERATE_OUTPUT_SCHEMA_VERSION
        and qualification["prompt_version"] == _REGENERATE_PROMPT_VERSION
        and qualification["prompt_contract_hash"] == plan["prompt_contract_hash"]
        and qualification["rule_version"] == manifest["rule_version"] == _REGENERATE_RULE_VERSION
        and qualification["sample_pack_version"]
        == manifest["benchmark_sample_pack_version"]
        == _REGENERATE_SAMPLE_PACK_VERSION
        and qualification["sampling_parameters_hash"] == plan["sampling_parameters_hash"]
        and qualification["qualification_status"] == "qualified"
        and qualification["context_window_tokens"] == value["context_window_tokens"]
        and qualification["max_output_tokens"] == value["max_output_tokens"]
        and all(
            _is_sha256(qualification[field])
            for field in (
                "prompt_contract_hash",
                "sampling_parameters_hash",
                "sample_manifest_hash",
                "evidence_manifest_hash",
                "record_hash",
            )
        )
        and type(qualification["qualification_harness_commit"]) is str
        and bool(qualification["qualification_harness_commit"])
        and type(qualification["review_ref"]) is str
        and bool(qualification["review_ref"])
        and _evidence_hash_valid(qualification, hash_field="evidence_hash")
    )
    authorization_ok = (
        authorization["schema_version"] == "deepseek_current_data_authorization_v1"
        and authorization["authority_source_id"] == "windows_process_env_human_permit"
        and authorization["authority_source_version"] == "v1"
        and authorization["authority_ref"] == "process-env/exact-scope"
        and authorization["provider"] == _ACTIVATION_PROVIDER
        and authorization["authorized"] is True
        and authorization["valid"] is True
        and authorization["purpose_id"] == _REGENERATE_PURPOSE_ID
        and authorization["data_scope_hash"] == expected_scope_hash
        and _evidence_hash_valid(authorization, hash_field="evidence_hash")
    )
    authority_hash_ok = value["authority_hash"] == _stable_hash(
        {key: deepcopy(value[key]) for key in _AUTHORITY_KEYS if key != "authority_hash"}
    )
    if not (top_level_ok and qualification_ok and authorization_ok and authority_hash_ok):
        raise _current_authority_inconsistent()
    return {
        "qualification_ref": deepcopy(qualification["authority_ref"]),
        "qualification_evidence_hash": qualification["evidence_hash"],
        "authorization_ref": deepcopy(authorization["authority_ref"]),
        "authorization_evidence_hash": authorization["evidence_hash"],
        "data_scope_hash": value["data_scope_hash"],
        "purpose_id": value["purpose_id"],
        "context_window_tokens": value["context_window_tokens"],
        "max_output_tokens": value["max_output_tokens"],
    }


def _map_regenerate_current_authority_failure(exc: HTTPException) -> str:
    detail = exc.detail
    code = detail.get("code") if isinstance(detail, Mapping) else None
    if code == "DEEPSEEK_AUTHORITY_QUALIFICATION_NOT_ADMITTED":
        return "qualification"
    if code == "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED":
        return "authorization"
    raise _current_authority_inconsistent() from exc


def build_daily_report_regenerate_gateway_preflight(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
    report_version_id: int,
) -> dict[str, object]:
    """Resolve Page07 regenerate readiness from one selector and server-owned durable truth."""
    report_version_id = _require_regenerate_report_selector(report_version_id)
    _revoke_transient_material_for_current_context()
    manifest = _validate_manifest(
        build_final_context_manifest(
            model_call_id=model_call_id,
            budget_record=budget_record,
        ),
        model_call_id=model_call_id,
    )
    if (
        manifest["task_type"] != _REGENERATE_TASK_TYPE
        or manifest["output_schema_version"] != _REGENERATE_OUTPUT_SCHEMA_VERSION
        or manifest["provider"] != _ACTIVATION_PROVIDER
        or manifest["model_id"] != _ACTIVATION_MODEL_ID
        or manifest["model_version"] != _ACTIVATION_MODEL_VERSION
        or manifest["rule_version"] != _REGENERATE_RULE_VERSION
        or manifest["benchmark_sample_pack_version"] != _REGENERATE_SAMPLE_PACK_VERSION
    ):
        return _build_result(
            manifest=manifest,
            request_envelope_hash=None,
            final_request_fit_state="not_evaluated",
            provider_compatibility_state="incompatible",
            local_gateway_state="blocked_provider_compatibility",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_evaluated",
        )
    if manifest["local_request_readiness_state"] != "ready_for_gateway_evaluation":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=None,
            final_request_fit_state="not_evaluated",
            provider_compatibility_state="not_evaluated",
            local_gateway_state="blocked_final_manifest",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_evaluated",
        )
    payload = _materialize_payload_rebound(
        manifest=manifest,
        model_call_id=model_call_id,
        budget_record=budget_record,
    )
    call = _read_regenerate_model_call(manifest=manifest, model_call_id=model_call_id)
    task_subject = _read_regenerate_subject(
        call=call,
        manifest=manifest,
        report_version_id=report_version_id,
    )
    capability = _load_regenerate_capability()
    if capability is None:
        return _build_result(
            manifest=manifest,
            request_envelope_hash=None,
            final_request_fit_state="not_evaluated",
            provider_compatibility_state="incompatible",
            local_gateway_state="blocked_provider_compatibility",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_evaluated",
        )
    plan = _build_regenerate_gateway_request_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
        task_subject=task_subject,
    )
    try:
        raw_authority = deepseek_current_authority.resolve_deepseek_regenerate_current_authority(
            model_call_id=model_call_id,
            final_context_manifest_hash=str(manifest["final_context_manifest_hash"]),
            framed_payload_hash=str(manifest["framed_payload_hash"]),
            request_envelope_hash=str(plan["request_envelope_hash"]),
            prompt_contract_hash=str(plan["prompt_contract_hash"]),
            sampling_parameters_hash=str(plan["sampling_parameters_hash"]),
        )
    except HTTPException as exc:
        return _blocked_authority_result(
            manifest=manifest,
            plan=plan,
            failure=_map_regenerate_current_authority_failure(exc),
        )
    authority = _validate_regenerate_current_authority(
        raw_authority,
        manifest=manifest,
        plan=plan,
    )
    reserved_output = plan["reserved_output_tokens"]
    conservative_upper_bound = plan["conservative_local_request_upper_bound"]
    fit = (
        type(reserved_output) is int
        and reserved_output > 0
        and reserved_output <= capability["max_output_tokens"]
        and reserved_output <= authority["max_output_tokens"]
        and type(conservative_upper_bound) is int
        and conservative_upper_bound <= authority["context_window_tokens"]
    )
    common = {
        "manifest": manifest,
        "request_envelope_hash": str(plan["request_envelope_hash"]),
        "provider_compatibility_state": "compatible",
        "qualification_authority_state": "verified",
        "authorization_authority_state": "verified",
        "qualification_authority_ref": authority["qualification_ref"],
        "qualification_evidence_hash": authority["qualification_evidence_hash"],
        "authorization_authority_ref": authority["authorization_ref"],
        "authorization_evidence_hash": authority["authorization_evidence_hash"],
        "authorization_data_scope_hash": authority["data_scope_hash"],
        "authorization_purpose_id": authority["purpose_id"],
    }
    if not fit:
        return _build_result(
            **common,
            final_request_fit_state="not_fit_by_conservative_upper_bound",
            local_gateway_state="blocked_request_budget",
        )
    ready_result = _build_result(
        **common,
        final_request_fit_state="fit_by_conservative_upper_bound",
        local_gateway_state="ready_for_provider_transport",
    )
    _TRANSIENT_MATERIAL_SLOT.set(
        {
            "_claim": _TransientClaim(),
            "request_kind": _REGENERATE_TASK_TYPE,
            "model_call_id": model_call_id,
            "report_version_id": report_version_id,
            "budget_record_hash": _stable_hash(budget_record),
            "manifest": deepcopy(manifest),
            "payload": payload,
            "task_subject": deepcopy(task_subject),
            "plan": deepcopy(plan),
        }
    )
    return ready_result


def materialize_daily_report_regenerate_gateway_request_transient(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
    report_version_id: int,
) -> dict[str, object]:
    """Atomically consume one regenerate-ready transient; performs no upstream re-read or I/O."""
    report_version_id = _require_regenerate_report_selector(report_version_id)
    cached = _TRANSIENT_MATERIAL_SLOT.get()
    _TRANSIENT_MATERIAL_SLOT.set(None)
    if not isinstance(cached, Mapping):
        raise _error(
            "MODEL_GATEWAY_TRANSIENT_NOT_READY",
            "不存在当前 execution context 可见的 ready Gateway transient material。",
        )
    _claim_transient_material(cached)
    if (
        cached.get("request_kind") != _REGENERATE_TASK_TYPE
        or cached.get("model_call_id") != model_call_id
        or cached.get("report_version_id") != report_version_id
        or cached.get("budget_record_hash") != _stable_hash(budget_record)
    ):
        raise _error(
            "MODEL_GATEWAY_TRANSIENT_MATERIAL_MISMATCH",
            "Regenerate transient request selector/model-call/budget binding 与 ready Preflight 不一致。",
        )
    manifest = _validate_manifest(deepcopy(cached.get("manifest")), model_call_id=model_call_id)
    if (
        manifest["task_type"] != _REGENERATE_TASK_TYPE
        or manifest["output_schema_version"] != _REGENERATE_OUTPUT_SCHEMA_VERSION
    ):
        raise _request_plan_inconsistent()
    payload = cached.get("payload")
    task_subject = cached.get("task_subject")
    plan = cached.get("plan")
    if type(payload) is not bytes or not isinstance(task_subject, Mapping):
        raise _request_plan_inconsistent()
    _validate_regenerate_request_plan(
        plan,
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
        task_subject=task_subject,
    )
    if not isinstance(plan, Mapping):
        raise _request_plan_inconsistent()
    return {
        "schema_version": TRANSIENT_SCHEMA_VERSION,
        "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"],
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "provider": manifest["provider"],
        "model_id": manifest["model_id"],
        "model_version": manifest["model_version"],
        "task_type": manifest["task_type"],
        "output_schema_version": manifest["output_schema_version"],
        "framed_payload_hash": manifest["framed_payload_hash"],
        "framed_payload_utf8_bytes": manifest["framed_payload_utf8_bytes"],
        "request_envelope_hash": plan["request_envelope_hash"],
        "conservative_local_request_upper_bound": plan[
            "conservative_local_request_upper_bound"
        ],
        "max_tokens": plan["reserved_output_tokens"],
        "messages": deepcopy(plan["messages"]),
    }

# Page07 contradiction-check preflight. Uses the same FinalContext/ModelGateway transient
# claim and provider adapter authority seam as generation/regeneration; single-attempt transport only; alternate-provider routing is not implemented.
_CONTRADICTION_TASK_TYPE = "report_contradiction_check"
_CONTRADICTION_OUTPUT_SCHEMA_VERSION = "report-contradiction-check/1.0"
_CONTRADICTION_PROMPT_VERSION = "page07-contradiction-prompt/1.0"
_CONTRADICTION_RULE_VERSION = "page07-contradiction-rules/1.0"
_CONTRADICTION_SAMPLE_PACK_VERSION = "page07-contradiction-qualification-pack/1.0"
_CONTRADICTION_PLAN_SCHEMA_VERSION = "model_gateway_contradiction_request_plan_v1"
_CONTRADICTION_SYSTEM_BINDING_TEXT = (
    "Compare only the exact server-owned AI raw report and PM supplement subject. "
    "Any unresolved or conflicting statement must remain visible; never infer agreement."
)
_CONTRADICTION_REQUEST_PLAN_KEYS = (
    "schema_version", "prompt_version", "prompt_policy_version",
    "json_instruction_version", "formal_result_contract_hash",
    "expected_structural_example_hash", "json_instruction_hash",
    "prompt_contract_hash", "sampling_parameters_hash", "task_subject_hash",
    "effective_input_hash", "messages_hash", "task_envelope",
    "framed_payload_hash", "framed_payload_utf8_bytes", "budget_profile_hash",
    "reserved_output_tokens", "safety_margin_tokens", "provider_request_utf8_bytes",
    "conservative_local_request_upper_bound", "messages", "request_envelope_hash",
)
_CONTRADICTION_REQUEST_PLAN_HASH_KEYS = tuple(k for k in _CONTRADICTION_REQUEST_PLAN_KEYS if k != "request_envelope_hash")


def _contradiction_prompt_contract() -> tuple[dict[str, object], str, str]:
    descriptor = _result_contract_descriptor(_CONTRADICTION_TASK_TYPE)
    example = _expected_structural_example(descriptor)
    instruction = (
        "Return exactly one valid json object and no surrounding text. "
        "Conform exactly to the formal result contract; do not add/remove/rename fields. "
        f"Formal result contract descriptor: {_canonical_text(descriptor)}\n"
        f"Expected structural json example: {_canonical_text(example)}"
    )
    contract = {
        "schema_version": "page07_contradiction_prompt_contract_v1",
        "prompt_version": _CONTRADICTION_PROMPT_VERSION,
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "prompt_policy_text": PROMPT_POLICY_TEXT,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "json_instruction": instruction,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "system_binding_text": _CONTRADICTION_SYSTEM_BINDING_TEXT,
    }
    return contract, _stable_hash(contract), instruction


def _contradiction_sampling_policy(manifest: Mapping[str, object]) -> tuple[dict[str, object], str]:
    policy = {
        "schema_version": "page07_contradiction_sampling_request_policy_v1",
        "provider": manifest["provider"], "model_id": manifest["model_id"],
        "model_version": manifest["model_version"],
        "response_format": {"type": "json_object"}, "stream": False,
        "thinking": {"type": "enabled"}, "reasoning_effort": "high",
        "max_tokens_source": "budget_record.reserved_output_tokens",
    }
    return policy, _stable_hash(policy)


def _build_contradiction_plan(*, manifest: Mapping[str, object], payload: bytes, budget_record: Mapping[str, object], task_subject: Mapping[str, object]) -> dict[str, object]:
    reserved_output, safety_margin = _budget_values(budget_record)
    prompt_contract, prompt_hash, instruction = _contradiction_prompt_contract()
    _sampling, sampling_hash = _contradiction_sampling_policy(manifest)
    descriptor = _result_contract_descriptor(_CONTRADICTION_TASK_TYPE)
    example = _expected_structural_example(descriptor)
    try:
        payload_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _payload_rebind_failed() from exc
    messages = [
        {"role": "system", "content": f"{PROMPT_POLICY_TEXT}\n{_CONTRADICTION_SYSTEM_BINDING_TEXT}\n{instruction}"},
        {"role": "user", "content": (
            "Task envelope (canonical json): " + _canonical_text(_task_envelope(manifest)) +
            "\nExact contradiction subject (canonical json): " + _canonical_text(task_subject["subject"]) +
            "\nFrozen evidence context payload:\n" + payload_text
        )},
    ]
    provider_request = _provider_request_shape(manifest=manifest, messages=messages, reserved_output_tokens=reserved_output)
    provider_request_bytes = len(_canonical_bytes(provider_request))
    plan: dict[str, object] = {
        "schema_version": _CONTRADICTION_PLAN_SCHEMA_VERSION,
        "prompt_version": prompt_contract["prompt_version"],
        "prompt_policy_version": PROMPT_POLICY_VERSION, "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "json_instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "prompt_contract_hash": prompt_hash, "sampling_parameters_hash": sampling_hash,
        "task_subject_hash": task_subject["task_subject_hash"],
        "effective_input_hash": _stable_hash({"messages": messages, "task_subject_hash": task_subject["task_subject_hash"], "framed_payload_hash": manifest["framed_payload_hash"]}),
        "messages_hash": _stable_hash(messages), "task_envelope": _task_envelope(manifest),
        "framed_payload_hash": manifest["framed_payload_hash"], "framed_payload_utf8_bytes": len(payload),
        "budget_profile_hash": manifest["budget_profile_hash"], "reserved_output_tokens": reserved_output,
        "safety_margin_tokens": safety_margin, "provider_request_utf8_bytes": provider_request_bytes,
        "conservative_local_request_upper_bound": provider_request_bytes + reserved_output + safety_margin,
        "messages": messages,
    }
    plan["request_envelope_hash"] = _stable_hash({k: deepcopy(plan[k]) for k in _CONTRADICTION_REQUEST_PLAN_HASH_KEYS})
    return plan


def build_report_contradiction_gateway_preflight(*, model_call_id: int, budget_record: Mapping[str, object], report_version_id: int) -> dict[str, object]:
    from app import model_provider_runtime, report_task_subjects
    from app.model_call_ledger import get_model_call

    _revoke_transient_material_for_current_context()
    manifest = _validate_manifest(build_final_context_manifest(model_call_id=model_call_id, budget_record=budget_record), model_call_id=model_call_id)
    expected = {
        "task_type": _CONTRADICTION_TASK_TYPE, "output_schema_version": _CONTRADICTION_OUTPUT_SCHEMA_VERSION,
        "rule_version": _CONTRADICTION_RULE_VERSION, "benchmark_sample_pack_version": _CONTRADICTION_SAMPLE_PACK_VERSION,
    }
    if any(manifest.get(field) != value for field, value in expected.items()):
        return _build_result(manifest=manifest, request_envelope_hash=None, final_request_fit_state="not_evaluated", provider_compatibility_state="incompatible", local_gateway_state="blocked_provider_compatibility", qualification_authority_state="not_evaluated", authorization_authority_state="not_evaluated")
    if manifest["local_request_readiness_state"] != "ready_for_gateway_evaluation":
        return _build_result(manifest=manifest, request_envelope_hash=None, final_request_fit_state="not_evaluated", provider_compatibility_state="not_evaluated", local_gateway_state="blocked_final_manifest", qualification_authority_state="not_evaluated", authorization_authority_state="not_evaluated")
    call = get_model_call(model_call_id)
    subject = report_task_subjects.build_report_contradiction_subject(project_id=int(call["project_id"]), report_version_id=report_version_id)
    subject_value = subject.get("subject")
    durable_request = subject_value.get("contradiction_request") if isinstance(subject_value, Mapping) else None
    source_report = subject_value.get("source_report") if isinstance(subject_value, Mapping) else None
    if (
        subject.get("task_type") != _CONTRADICTION_TASK_TYPE
        or not isinstance(subject_value, Mapping)
        or subject_value.get("project_id") != call.get("project_id")
        or not isinstance(durable_request, Mapping)
        or durable_request.get("local_task_id") != call.get("local_task_id")
        or not isinstance(source_report, Mapping)
        or source_report.get("report_version_id") != report_version_id
    ):
        raise _upstream_inconsistent()
    payload = _materialize_payload_rebound(manifest=manifest, model_call_id=model_call_id, budget_record=budget_record)
    plan = _build_contradiction_plan(manifest=manifest, payload=payload, budget_record=budget_record, task_subject=subject)
    try:
        adapter = model_provider_runtime.resolve_model_provider_adapter(str(manifest["provider"]))
        capability = adapter.get_capability(task_type=_CONTRADICTION_TASK_TYPE, output_schema_version=_CONTRADICTION_OUTPUT_SCHEMA_VERSION)
    except HTTPException:
        return _build_result(manifest=manifest, request_envelope_hash=None, final_request_fit_state="not_evaluated", provider_compatibility_state="incompatible", local_gateway_state="blocked_provider_compatibility", qualification_authority_state="not_evaluated", authorization_authority_state="not_evaluated")
    if (
        capability.provider != manifest["provider"] or capability.model_id != manifest["model_id"]
        or capability.model_version != manifest["model_version"] or capability.task_type != _CONTRADICTION_TASK_TYPE
        or capability.output_schema_version != _CONTRADICTION_OUTPUT_SCHEMA_VERSION
    ):
        return _build_result(manifest=manifest, request_envelope_hash=None, final_request_fit_state="not_evaluated", provider_compatibility_state="incompatible", local_gateway_state="blocked_provider_compatibility", qualification_authority_state="not_evaluated", authorization_authority_state="not_evaluated")
    try:
        authority = adapter.resolve_current_authority(
            model_call_id=model_call_id, final_context_manifest_hash=str(manifest["final_context_manifest_hash"]),
            framed_payload_hash=str(manifest["framed_payload_hash"]), task_type=_CONTRADICTION_TASK_TYPE,
            request_envelope_hash=str(plan["request_envelope_hash"]), prompt_contract_hash=str(plan["prompt_contract_hash"]),
            sampling_parameters_hash=str(plan["sampling_parameters_hash"]),
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        code = detail.get("code")
        failure = "qualification" if "QUALIFICATION" in str(code) else "authorization" if "AUTHORIZATION" in str(code) or "AUTHORIZED" in str(code) else None
        if failure is None:
            raise _current_authority_inconsistent() from exc
        return _blocked_authority_result(manifest=manifest, plan=plan, failure=failure)
    reserved_output = plan["reserved_output_tokens"]
    upper = plan["conservative_local_request_upper_bound"]
    fit = (type(reserved_output) is int and 0 < reserved_output <= capability.max_output_tokens and reserved_output <= authority.max_output_tokens and type(upper) is int and upper <= authority.context_window_tokens)
    common = {
        "manifest": manifest, "request_envelope_hash": str(plan["request_envelope_hash"]),
        "provider_compatibility_state": "compatible", "qualification_authority_state": "verified",
        "authorization_authority_state": "verified", "qualification_authority_ref": authority.qualification_authority_ref,
        "qualification_evidence_hash": authority.qualification_evidence_hash, "authorization_authority_ref": authority.authorization_authority_ref,
        "authorization_evidence_hash": authority.authorization_evidence_hash, "authorization_data_scope_hash": authority.data_scope_hash,
        "authorization_purpose_id": authority.purpose_id,
    }
    if not fit:
        return _build_result(**common, final_request_fit_state="not_fit_by_conservative_upper_bound", local_gateway_state="blocked_request_budget")
    ready = _build_result(**common, final_request_fit_state="fit_by_conservative_upper_bound", local_gateway_state="ready_for_provider_transport")
    _TRANSIENT_MATERIAL_SLOT.set({
        "_claim": _TransientClaim(), "request_kind": _CONTRADICTION_TASK_TYPE, "model_call_id": model_call_id,
        "report_version_id": report_version_id, "budget_record_hash": _stable_hash(budget_record),
        "manifest": deepcopy(manifest), "payload": payload, "task_subject": deepcopy(subject), "plan": deepcopy(plan),
    })
    return ready


def materialize_report_contradiction_gateway_request_transient(*, model_call_id: int, budget_record: Mapping[str, object], report_version_id: int) -> dict[str, object]:
    cached = _TRANSIENT_MATERIAL_SLOT.get(); _TRANSIENT_MATERIAL_SLOT.set(None)
    if not isinstance(cached, Mapping):
        raise _error("MODEL_GATEWAY_TRANSIENT_NOT_READY", "不存在当前 execution context 可见的 ready contradiction transient material。")
    _claim_transient_material(cached)
    if (cached.get("request_kind") != _CONTRADICTION_TASK_TYPE or cached.get("model_call_id") != model_call_id or cached.get("report_version_id") != report_version_id or cached.get("budget_record_hash") != _stable_hash(budget_record)):
        raise _error("MODEL_GATEWAY_TRANSIENT_MATERIAL_MISMATCH", "Contradiction transient selector/model-call/budget binding 漂移。")
    manifest = _validate_manifest(deepcopy(cached.get("manifest")), model_call_id=model_call_id)
    plan = cached.get("plan")
    if not isinstance(plan, Mapping) or plan.get("schema_version") != _CONTRADICTION_PLAN_SCHEMA_VERSION or plan.get("request_envelope_hash") != _stable_hash({k: deepcopy(plan[k]) for k in _CONTRADICTION_REQUEST_PLAN_HASH_KEYS}):
        raise _request_plan_inconsistent()
    return {
        "schema_version": TRANSIENT_SCHEMA_VERSION, "model_call_id": manifest["model_call_id"],
        "call_identity_hash": manifest["call_identity_hash"], "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "provider": manifest["provider"], "model_id": manifest["model_id"], "model_version": manifest["model_version"],
        "task_type": manifest["task_type"], "output_schema_version": manifest["output_schema_version"],
        "framed_payload_hash": manifest["framed_payload_hash"], "framed_payload_utf8_bytes": manifest["framed_payload_utf8_bytes"],
        "request_envelope_hash": plan["request_envelope_hash"], "conservative_local_request_upper_bound": plan["conservative_local_request_upper_bound"],
        "max_tokens": plan["reserved_output_tokens"], "messages": deepcopy(plan["messages"]),
    }

