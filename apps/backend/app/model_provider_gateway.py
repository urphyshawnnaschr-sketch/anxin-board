"""Provider-agnostic Model Gateway for daily report generation.

This production seam preserves the existing Evidence/Context/Redaction/Model Call
identity chain while resolving capability, provider request accounting, and exact-current
authority through the closed-world provider registry. It performs no provider send,
credential read, persistence write, retry, or fallback.

The legacy ``model_gateway`` module remains available for exact historical DeepSeek and
Page07 regenerate compatibility while callers migrate task-by-task to this seam.
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
from app.model_gateway_diagnostics import safe_authority_error
from app import final_context_manifest, model_provider_runtime
from app.ai_contracts import (
    NESTED_OBJECT_SCHEMAS,
    RESULT_ARRAY_ITEM_SCHEMAS,
    RESULT_FIELD_RULES,
    RESULT_FIELDS,
    TASK_TYPES,
)
from app.model_provider_contract import (
    ModelProviderAdapter,
    ProviderCapability,
    ProviderCurrentAuthority,
)


SCHEMA_VERSION = "model_gateway_preflight_v1"
TRANSIENT_SCHEMA_VERSION = "model_gateway_transient_request_v1"
NETWORK_SEND_STATE = "not_attempted"
JSON_INSTRUCTION_VERSION = "deepseek_json_output_instruction_v1"
_GENERATE_TASK_TYPE = "daily_report_generate"

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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSIENT_MATERIAL_SLOT: ContextVar[dict[str, object] | None] = ContextVar(
    "model_provider_gateway_transient_material", default=None
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
        "MODEL_PROVIDER_GATEWAY_UPSTREAM_INCONSISTENT",
        "Provider Gateway 所消费的 Final Context Manifest shape、hash 或 identity 无法闭合。",
    )


def _payload_rebind_failed() -> HTTPException:
    return _error(
        "MODEL_PROVIDER_GATEWAY_PAYLOAD_REBIND_FAILED",
        "临时重建的 context payload 与 Final Context Manifest 记录不一致。",
    )


def _request_plan_inconsistent() -> HTTPException:
    return _error(
        "MODEL_PROVIDER_GATEWAY_REQUEST_PLAN_INCONSISTENT",
        "Provider Gateway request-plan 的 contract、prompt、hash 或 byte accounting 无法闭合。",
    )


def _provider_inconsistent() -> HTTPException:
    return _error(
        "MODEL_PROVIDER_GATEWAY_PROVIDER_INCONSISTENT",
        "Provider adapter capability/current-authority 与 durable Model Call identity 无法闭合。",
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
        raise _request_plan_inconsistent() from exc


def _canonical_text(value: object) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _non_empty(value: object) -> bool:
    return type(value) is str and bool(value.strip())


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
            "MODEL_PROVIDER_GATEWAY_TRANSIENT_NOT_READY",
            "不存在当前可消费的 ready Provider Gateway transient material，或已被消费/撤销。",
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
        or value["task_type"] != _GENERATE_TASK_TYPE
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
    if value["final_context_manifest_hash"] != _stable_hash(
        {field: deepcopy(value[field]) for field in _FINAL_MANIFEST_HASH_KEYS}
    ):
        raise _upstream_inconsistent()
    return value


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
    if (
        hashlib.sha256(payload).hexdigest() != manifest["framed_payload_hash"]
        or len(payload) != manifest["framed_payload_utf8_bytes"]
        or materialized.get("framed_payload_hash") != manifest["framed_payload_hash"]
        or materialized.get("framed_payload_utf8_bytes") != manifest["framed_payload_utf8_bytes"]
    ):
        raise _payload_rebind_failed()
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _payload_rebind_failed() from exc
    return payload


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
        raise _request_plan_inconsistent()
    nested_by_field: dict[str, object] = {}
    for field, schema_name in RESULT_ARRAY_ITEM_SCHEMAS.get(task_type, {}).items():
        schema = NESTED_OBJECT_SCHEMAS.get(schema_name)
        if schema is None:
            raise _request_plan_inconsistent()
        nested_by_field[field] = {
            "schema_name": schema_name,
            "schema": _object_schema_descriptor(schema),
        }
    return {
        "task_type": task_type,
        "result_fields": [
            {"name": name, "rule": _field_rule_descriptor(rule)}
            for name, rule in RESULT_FIELD_RULES[task_type]
        ],
        "required_fields": list(RESULT_FIELDS[task_type]),
        "array_item_schemas": nested_by_field,
        "new_report_contract": None,
    }


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


def _allowed_evidence_ids_for_request(manifest: Mapping[str, object]) -> list[str]:
    from app.report_generation_batches import get_batch_call_selection

    selection = get_batch_call_selection(int(manifest["model_call_id"]))
    if selection is not None:
        ids = selection.get("evidence_ids")
    else:
        admitted = manifest.get("admitted_targets")
        if not isinstance(admitted, list):
            raise _request_plan_inconsistent()
        ids = [
            item.get("target")
            for item in admitted
            if isinstance(item, Mapping) and item.get("target") != "profile"
        ]
    if (
        not isinstance(ids, list)
        or any(type(item) is not str or not item for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise _request_plan_inconsistent()
    return list(ids)


def _bind_evidence_example(value: object, evidence_id: str) -> object:
    if isinstance(value, Mapping):
        return {
            key: ([evidence_id] if key == "evidence_ids" and type(child) is list
                  else _bind_evidence_example(child, evidence_id))
            for key, child in value.items()
        }
    if type(value) is list:
        return [_bind_evidence_example(item, evidence_id) for item in value]
    return deepcopy(value)


def _derive_json_material(
    task_type: str, *, allowed_evidence_ids: list[str]
) -> tuple[dict[str, object], dict[str, object], str]:
    descriptor = _result_contract_descriptor(task_type)
    if task_type != _GENERATE_TASK_TYPE:
        raise _request_plan_inconsistent()
    example = _expected_structural_example(descriptor)
    if allowed_evidence_ids:
        example = _bind_evidence_example(example, allowed_evidence_ids[0])
    instruction = (
        "Return exactly one valid json object and no surrounding text. "
        "The json object must conform exactly to the formal result contract descriptor below; "
        "do not add, remove, rename, or reinterpret fields. "
        f"Formal result contract descriptor: {_canonical_text(descriptor)}\n"
        "Expected structural json example derived from that same descriptor: "
        f"{_canonical_text(example)}\n"
        "Allowed evidence_ids for this exact request are: "
        f"{_canonical_text(allowed_evidence_ids)}. "
        "Every evidence_ids entry must be copied exactly from this list; never invent IDs "
        "and never emit placeholder values such as the literal string 'string'."
    )
    return descriptor, example, instruction


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
    *, manifest: Mapping[str, object], payload: bytes, instruction: str
) -> tuple[Mapping[str, object], ...]:
    try:
        payload_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _payload_rebind_failed() from exc
    system_content = f"{PROMPT_POLICY_TEXT}\n{instruction}"
    # The same fragment marker is used during local planning and actual materialization.
    # Budget estimation therefore includes these instructions before any call is created.
    try:
        fragments = any(
            isinstance(frame.get("body"), dict)
            and frame["body"].get("encoding") == "canonical_json_fragment_v1"
            for frame in (json.loads(line) for line in payload_text.splitlines() if line)
            if isinstance(frame, dict)
        )
    except (ValueError, TypeError):
        fragments = False
    if fragments:
        system_content += (
            "\nThis is one partial batch of a frozen report, not the complete project. "
            "The body text is a canonical JSON fragment at the stated character range. "
            "Use only evidence IDs present in this batch. State only local supported claims; "
            "absence in a fragment does not prove missing implementation. Preserve unknown "
            "when context is insufficient. Do not claim repository-wide completeness. "
            "Other batch conclusions will be combined deterministically without another model."
        )
    user_content = (
        f"Task envelope (canonical json): {_canonical_text(_task_envelope(manifest))}\n"
        f"Frozen context payload:\n{payload_text}"
    )
    return (
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    )


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


def _build_gateway_request_plan(
    *,
    manifest: Mapping[str, object],
    payload: bytes,
    budget_record: Mapping[str, object],
    adapter: ModelProviderAdapter,
) -> dict[str, object]:
    reserved_output, safety_margin = _budget_values(budget_record)
    allowed_evidence_ids = _allowed_evidence_ids_for_request(manifest)
    descriptor, example, instruction = _derive_json_material(
        str(manifest["task_type"]), allowed_evidence_ids=allowed_evidence_ids
    )
    messages = _build_messages(manifest=manifest, payload=payload, instruction=instruction)
    provider_request_bytes = adapter.estimate_request_utf8_bytes(
        messages=messages,
        max_output_tokens=reserved_output,
    )
    if type(provider_request_bytes) is not int or provider_request_bytes <= 0:
        raise _provider_inconsistent()
    messages_list = [dict(item) for item in messages]
    plan: dict[str, object] = {
        "schema_version": "model_gateway_request_plan_v1",
        "prompt_policy_version": PROMPT_POLICY_VERSION,
        "json_instruction_version": JSON_INSTRUCTION_VERSION,
        "formal_result_contract_hash": _stable_hash(descriptor),
        "expected_structural_example_hash": _stable_hash(example),
        "json_instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "messages_hash": _stable_hash(messages_list),
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
        "messages": messages_list,
    }
    plan["request_envelope_hash"] = _stable_hash(
        {field: deepcopy(plan[field]) for field in _REQUEST_PLAN_HASH_KEYS}
    )
    if tuple(plan) != _REQUEST_PLAN_KEYS:
        raise _request_plan_inconsistent()
    return plan


def _validate_capability(
    capability: object,
    *,
    manifest: Mapping[str, object],
) -> bool:
    return (
        isinstance(capability, ProviderCapability)
        and capability.provider == manifest["provider"]
        and capability.model_id == manifest["model_id"]
        and capability.model_version == manifest["model_version"]
        and capability.task_type == manifest["task_type"]
        and capability.output_schema_version == manifest["output_schema_version"]
        and type(capability.context_window_tokens) is int
        and capability.context_window_tokens > 0
        and type(capability.max_output_tokens) is int
        and capability.max_output_tokens > 0
    )


def _expected_data_scope_hash(
    manifest: Mapping[str, object], *, purpose_id: str
) -> str:
    return _stable_hash(
        {
            "provider": manifest["provider"],
            "model_id": manifest["model_id"],
            "model_version": manifest["model_version"],
            "model_call_id": manifest["model_call_id"],
            "call_identity_hash": manifest["call_identity_hash"],
            "final_context_manifest_hash": manifest["final_context_manifest_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
            "task_type": manifest["task_type"],
            "output_schema_version": manifest["output_schema_version"],
            "purpose_id": purpose_id,
        }
    )


def _validate_authority(
    authority: object,
    *,
    manifest: Mapping[str, object],
) -> ProviderCurrentAuthority:
    if not isinstance(authority, ProviderCurrentAuthority):
        raise _provider_inconsistent()
    if (
        authority.model_call_id != manifest["model_call_id"]
        or authority.call_identity_hash != manifest["call_identity_hash"]
        or authority.provider != manifest["provider"]
        or authority.model_id != manifest["model_id"]
        or authority.model_version != manifest["model_version"]
        or type(authority.context_window_tokens) is not int
        or authority.context_window_tokens <= 0
        or type(authority.max_output_tokens) is not int
        or authority.max_output_tokens <= 0
        or not _non_empty(authority.purpose_id)
        or authority.data_scope_hash
        != _expected_data_scope_hash(manifest, purpose_id=authority.purpose_id)
        or not _is_sha256(authority.data_scope_hash)
        or authority.qualification_authority_ref in (None, "", {}, [])
        or not _is_sha256(authority.qualification_evidence_hash)
        or authority.authorization_authority_ref in (None, "", {}, [])
        or not _is_sha256(authority.authorization_evidence_hash)
    ):
        raise _provider_inconsistent()
    return authority


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
        raise _request_plan_inconsistent()
    return result


def _blocked_authority_result(
    *, manifest: Mapping[str, object], plan: Mapping[str, object], failure_code: str
) -> dict[str, object]:
    if failure_code == "MODEL_PROVIDER_AUTHORITY_UNAVAILABLE":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=str(plan["request_envelope_hash"]),
            final_request_fit_state="not_evaluated_current_authority",
            provider_compatibility_state="compatible",
            local_gateway_state="blocked_current_authority_unavailable",
            qualification_authority_state="unavailable",
            authorization_authority_state="not_evaluated",
        )
    if failure_code == "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=str(plan["request_envelope_hash"]),
            final_request_fit_state="not_evaluated_current_authority",
            provider_compatibility_state="compatible",
            local_gateway_state="blocked_current_qualification",
            qualification_authority_state="not_verified",
            authorization_authority_state="not_evaluated",
        )
    if failure_code == "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT":
        return _build_result(
            manifest=manifest,
            request_envelope_hash=str(plan["request_envelope_hash"]),
            final_request_fit_state="not_evaluated_current_authority",
            provider_compatibility_state="compatible",
            local_gateway_state="blocked_current_authorization",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_authorized",
        )
    raise _provider_inconsistent()


def build_model_provider_gateway_preflight(
    *, model_call_id: int, budget_record: Mapping[str, object],
    raise_authority_diagnostics: bool = False,
) -> dict[str, object]:
    """Resolve exact provider-neutral Gateway readiness without provider business send."""
    _revoke_transient_material_for_current_context()
    manifest = _validate_manifest(
        final_context_manifest.build_final_context_manifest(
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
    adapter = model_provider_runtime.resolve_model_provider_adapter(manifest["provider"])
    try:
        capability = adapter.get_capability(
            task_type=str(manifest["task_type"]),
            output_schema_version=str(manifest["output_schema_version"]),
        )
    except HTTPException:
        return _build_result(
            manifest=manifest,
            request_envelope_hash=None,
            final_request_fit_state="not_evaluated",
            provider_compatibility_state="incompatible",
            local_gateway_state="blocked_provider_compatibility",
            qualification_authority_state="not_evaluated",
            authorization_authority_state="not_evaluated",
        )
    if not _validate_capability(capability, manifest=manifest):
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
        adapter=adapter,
    )
    try:
        authority = adapter.resolve_current_authority(
            model_call_id=model_call_id,
            final_context_manifest_hash=str(manifest["final_context_manifest_hash"]),
            framed_payload_hash=str(manifest["framed_payload_hash"]),
            task_type=str(manifest["task_type"]),
        )
    except HTTPException as exc:
        if raise_authority_diagnostics:
            raise safe_authority_error(exc.detail) from None
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, Mapping) else None
        return _blocked_authority_result(
            manifest=manifest,
            plan=plan,
            failure_code=str(code),
        )
    authority = _validate_authority(authority, manifest=manifest)

    reserved_output = plan["reserved_output_tokens"]
    conservative_upper_bound = plan["conservative_local_request_upper_bound"]
    fit = (
        type(reserved_output) is int
        and reserved_output > 0
        and reserved_output <= capability.max_output_tokens
        and reserved_output <= authority.max_output_tokens
        and type(conservative_upper_bound) is int
        and conservative_upper_bound <= capability.context_window_tokens
        and conservative_upper_bound <= authority.context_window_tokens
    )
    common = {
        "manifest": manifest,
        "request_envelope_hash": str(plan["request_envelope_hash"]),
        "provider_compatibility_state": "compatible",
        "qualification_authority_state": "verified",
        "authorization_authority_state": "verified",
        "qualification_authority_ref": authority.qualification_authority_ref,
        "qualification_evidence_hash": authority.qualification_evidence_hash,
        "authorization_authority_ref": authority.authorization_authority_ref,
        "authorization_evidence_hash": authority.authorization_evidence_hash,
        "authorization_data_scope_hash": authority.data_scope_hash,
        "authorization_purpose_id": authority.purpose_id,
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


def materialize_model_provider_gateway_request_transient(
    *, model_call_id: int, budget_record: Mapping[str, object]
) -> dict[str, object]:
    """Atomically consume one provider-neutral ready transient; performs zero I/O."""
    cached = _TRANSIENT_MATERIAL_SLOT.get()
    _TRANSIENT_MATERIAL_SLOT.set(None)
    if not isinstance(cached, Mapping):
        raise _error(
            "MODEL_PROVIDER_GATEWAY_TRANSIENT_NOT_READY",
            "不存在当前 execution context 可见的 ready Provider Gateway transient material。",
        )
    _claim_transient_material(cached)
    if (
        cached.get("model_call_id") != model_call_id
        or cached.get("budget_record_hash") != _stable_hash(budget_record)
    ):
        raise _error(
            "MODEL_PROVIDER_GATEWAY_TRANSIENT_MATERIAL_MISMATCH",
            "Provider transient 的 model-call/budget binding 与 ready Preflight 不一致。",
        )
    manifest = _validate_manifest(deepcopy(cached.get("manifest")), model_call_id=model_call_id)
    payload = cached.get("payload")
    plan = cached.get("plan")
    if type(payload) is not bytes or not isinstance(plan, Mapping):
        raise _request_plan_inconsistent()
    adapter = model_provider_runtime.resolve_model_provider_adapter(manifest["provider"])
    rebuilt = _build_gateway_request_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget_record,
        adapter=adapter,
    )
    if rebuilt != plan:
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
