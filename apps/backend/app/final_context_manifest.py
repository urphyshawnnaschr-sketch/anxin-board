"""Final Context Manifest V1: the last local metadata closure before Gateway evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app.context_token_framing import build_context_token_framing_accounting
from app.model_call_ledger import get_model_call


SCHEMA_VERSION = "final_context_manifest_v1"
MANIFEST_STAGE = "local_pre_gateway_final"
FINAL_MANIFEST_STATE = "finalized_local_metadata"
GATEWAY_SEND_STATE = "not_evaluated"

_ACCOUNTING_KEYS = (
    "schema_version",
    "manifest_core_hash",
    "budget_profile_hash",
    "model_call_id",
    "call_identity_hash",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "candidate_set_hash",
    "counting_policy_version",
    "framing_policy_version",
    "framing_scope",
    "context_admission_state",
    "coverage_state",
    "admitted_targets",
    "denied_targets",
    "framed_payload_hash",
    "framed_payload_utf8_bytes",
    "conservative_input_token_upper_bound",
    "exact_tokens",
    "budget_fit_state",
    "final_request_fit_state",
    "unsupported_context_sources",
    "token_framing_accounting_hash",
)
_ACCOUNTING_HASH_KEYS = tuple(
    key for key in _ACCOUNTING_KEYS if key != "token_framing_accounting_hash"
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
_SHARED_IDENTITY_FIELDS = (
    "model_call_id",
    "call_identity_hash",
    "project_id",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
)
_LEDGER_BIND_FIELDS = (
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
)
_RESULT_KEYS = (
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
_RESULT_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "final_context_manifest_hash")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "FINAL_CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT",
        "Final Context Manifest 所消费的正式上游 shape、hash、identity 或 state 无法闭合。",
    )


def _internal_inconsistent() -> HTTPException:
    return _error(
        "FINAL_CONTEXT_MANIFEST_INTERNAL_INCONSISTENT",
        "Final Context Manifest 内部 canonical hash 或 schema 构造发生不一致。",
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


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _require_non_empty_string(value: object) -> None:
    if type(value) is not str or not value.strip():
        raise _upstream_inconsistent()


def _validate_ledger(value: object, *, model_call_id: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _upstream_inconsistent()
    if value.get("schema_version") != "model_call_ledger_v1":
        raise _upstream_inconsistent()
    if value.get("preparation_state") != "prepared":
        raise _upstream_inconsistent()
    if type(value.get("model_call_id")) is not int or value["model_call_id"] <= 0:
        raise _upstream_inconsistent()
    if value["model_call_id"] != model_call_id:
        raise _upstream_inconsistent()
    if type(value.get("project_id")) is not int or value["project_id"] <= 0:
        raise _upstream_inconsistent()
    if type(value.get("snapshot_id")) is not int or value["snapshot_id"] <= 0:
        raise _upstream_inconsistent()
    for field in (
        "task_type",
        "provider",
        "model_id",
        "model_version",
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
    ):
        _require_non_empty_string(value.get(field))
    for field in (
        "call_identity_hash",
        "snapshot_hash",
        "candidate_set_hash",
        "qualification_hash",
        "authorization_hash",
    ):
        if not _is_sha256(value.get(field)):
            raise _upstream_inconsistent()
    if value.get("qualification_status") != "qualified":
        raise _upstream_inconsistent()
    if value.get("authorization_provider") != value.get("provider"):
        raise _upstream_inconsistent()
    if value.get("authorization_authorized") != 1 or value.get("authorization_valid") != 1:
        raise _upstream_inconsistent()
    return value


def _validate_target_metadata(
    items: object,
    *,
    fields: tuple[str, ...],
    admitted: bool,
) -> list[dict[str, object]]:
    if not isinstance(items, list):
        raise _upstream_inconsistent()
    result: list[dict[str, object]] = []
    seen_ordinals: set[int] = set()
    seen_targets: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or tuple(item) != fields:
            raise _upstream_inconsistent()
        ordinal = item.get("ordinal")
        target = item.get("target")
        target_type = item.get("target_type")
        if type(ordinal) is not int or ordinal <= 0 or ordinal in seen_ordinals:
            raise _upstream_inconsistent()
        if type(target) is not str or not target or target in seen_targets:
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
            if type(item.get("admission_reason")) is not str or not item["admission_reason"]:
                raise _upstream_inconsistent()
            matched_rule_id = item.get("matched_rule_id")
            if matched_rule_id is not None and type(matched_rule_id) is not str:
                raise _upstream_inconsistent()
        seen_ordinals.add(ordinal)
        seen_targets.add(target)
        result.append({field: deepcopy(item[field]) for field in fields})
    return result


def _validate_accounting(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _ACCOUNTING_KEYS:
        raise _upstream_inconsistent()
    if value["schema_version"] != "token_framing_accounting_v1":
        raise _upstream_inconsistent()
    if value["framing_scope"] != "context_payload_only":
        raise _upstream_inconsistent()
    if value["final_request_fit_state"] != "not_evaluated":
        raise _upstream_inconsistent()
    if value["exact_tokens"] is not None:
        raise _upstream_inconsistent()
    if value["context_admission_state"] not in {
        "all_targets_admitted",
        "contains_denied_targets",
    }:
        raise _upstream_inconsistent()
    if value["coverage_state"] not in {
        "supported_context_complete",
        "partial_fail_visible",
    }:
        raise _upstream_inconsistent()
    if value["budget_fit_state"] not in {
        "fit_by_conservative_upper_bound",
        "not_fit_by_conservative_upper_bound",
    }:
        raise _upstream_inconsistent()
    for field in (
        "manifest_core_hash",
        "budget_profile_hash",
        "call_identity_hash",
        "snapshot_hash",
        "candidate_set_hash",
        "framed_payload_hash",
        "token_framing_accounting_hash",
    ):
        if not _is_sha256(value[field]):
            raise _upstream_inconsistent()
    for field in ("model_call_id", "snapshot_id", "project_id"):
        if type(value[field]) is not int or value[field] <= 0:
            raise _upstream_inconsistent()
    for field in ("counting_policy_version", "framing_policy_version"):
        _require_non_empty_string(value[field])
    for field in ("framed_payload_utf8_bytes", "conservative_input_token_upper_bound"):
        if type(value[field]) is not int or value[field] < 0:
            raise _upstream_inconsistent()
    if value["framed_payload_utf8_bytes"] != value["conservative_input_token_upper_bound"]:
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

    admitted = _validate_target_metadata(
        value["admitted_targets"], fields=_ADMITTED_TARGET_KEYS, admitted=True
    )
    denied = _validate_target_metadata(
        value["denied_targets"], fields=_DENIED_TARGET_KEYS, admitted=False
    )
    admitted_names = {item["target"] for item in admitted}
    if any(item["target"] in admitted_names for item in denied):
        raise _upstream_inconsistent()
    if not admitted and not denied:
        raise _upstream_inconsistent()
    if value["context_admission_state"] == "all_targets_admitted" and denied:
        raise _upstream_inconsistent()
    if value["context_admission_state"] == "contains_denied_targets" and not denied:
        raise _upstream_inconsistent()

    payload = {field: deepcopy(value[field]) for field in _ACCOUNTING_HASH_KEYS}
    if _stable_hash(payload) != value["token_framing_accounting_hash"]:
        raise _upstream_inconsistent()
    return value


def _readiness(accounting: Mapping[str, object]) -> str:
    if accounting["context_admission_state"] == "contains_denied_targets" and not any(
        item["target_type"] == "git_file_fact" for item in accounting["admitted_targets"]
    ):
        return "blocked_context_denied"
    if accounting["budget_fit_state"] == "not_fit_by_conservative_upper_bound":
        return "blocked_context_budget"
    return "ready_for_gateway_evaluation"


def build_final_context_manifest(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Close local metadata for Gateway evaluation; never grants provider/network send permission."""
    ledger = _validate_ledger(get_model_call(model_call_id), model_call_id=model_call_id)
    accounting = _validate_accounting(
        build_context_token_framing_accounting(
            model_call_id=model_call_id,
            budget_record=budget_record,
        )
    )

    for field in _SHARED_IDENTITY_FIELDS:
        if ledger[field] != accounting[field]:
            raise _upstream_inconsistent()

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_stage": MANIFEST_STAGE,
        **{field: deepcopy(ledger[field]) for field in _LEDGER_BIND_FIELDS},
        "manifest_core_hash": accounting["manifest_core_hash"],
        "budget_profile_hash": accounting["budget_profile_hash"],
        "token_framing_accounting_hash": accounting["token_framing_accounting_hash"],
        "counting_policy_version": accounting["counting_policy_version"],
        "framing_policy_version": accounting["framing_policy_version"],
        "framing_scope": accounting["framing_scope"],
        "context_admission_state": accounting["context_admission_state"],
        "coverage_state": accounting["coverage_state"],
        "admitted_target_count": len(accounting["admitted_targets"]),
        "denied_target_count": len(accounting["denied_targets"]),
        "admitted_targets": deepcopy(accounting["admitted_targets"]),
        "denied_targets": deepcopy(accounting["denied_targets"]),
        "framed_payload_hash": accounting["framed_payload_hash"],
        "framed_payload_utf8_bytes": accounting["framed_payload_utf8_bytes"],
        "conservative_input_token_upper_bound": accounting[
            "conservative_input_token_upper_bound"
        ],
        "exact_tokens": accounting["exact_tokens"],
        "budget_fit_state": accounting["budget_fit_state"],
        "final_request_fit_state": accounting["final_request_fit_state"],
        "unsupported_context_sources": deepcopy(accounting["unsupported_context_sources"]),
        "local_request_readiness_state": _readiness(accounting),
        "final_manifest_state": FINAL_MANIFEST_STATE,
        "gateway_send_state": GATEWAY_SEND_STATE,
    }
    result["final_context_manifest_hash"] = _stable_hash(
        {field: deepcopy(result[field]) for field in _RESULT_HASH_KEYS}
    )
    if tuple(result) != _RESULT_KEYS:
        raise _internal_inconsistent()
    return result
