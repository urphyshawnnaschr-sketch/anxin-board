"""Token Counter + Framing Accounting V1: deterministic local context-payload framing."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app.context_manifest import build_context_manifest_core
from app.context_redaction_runtime import build_context_redaction_result
from app.model_send_admission import build_model_send_admission


SCHEMA_VERSION = "token_framing_accounting_v1"
FRAME_SCHEMA_VERSION = "context_payload_frame_v1"
FRAMING_POLICY_VERSION = "context_payload_framing_v1"
COUNTING_POLICY_VERSION = "utf8_byte_upper_bound_v1"
FRAMING_SCOPE = "context_payload_only"
FINAL_REQUEST_FIT_STATE = "not_evaluated"

_RESULT_KEYS = (
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
_RESULT_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "token_framing_accounting_hash")

_BUDGET_FIELDS = (
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
    "budget_profile_hash",
)
_ADMISSION_FIELDS = (
    "schema_version",
    "sensitive_path_policy_identity_hash",
    "redaction_result_hash",
    "model_call_id",
    "call_identity_hash",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "candidate_set_hash",
    "target",
    "target_type",
    "source_identity",
    "path_subject_state",
    "path_identity_hash",
    "sensitive_path_policy_id",
    "sensitive_path_policy_hash",
    "policy_evaluation_state",
    "sensitive_path_decision",
    "matched_rule_id",
    "model_send_state",
    "admission_reason",
    "unsupported_context_sources",
    "model_send_admission_hash",
)
_ADMISSION_HASH_FIELDS = tuple(
    key for key in _ADMISSION_FIELDS if key != "model_send_admission_hash"
)
_REDACTION_FIELDS = (
    "schema_version",
    "redaction_input_hash",
    "manifest_core_hash",
    "model_call_id",
    "call_identity_hash",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "candidate_set_hash",
    "target",
    "target_type",
    "source_identity",
    "raw_body_kind",
    "redaction_policy_id",
    "redaction_policy_hash",
    "redaction_state",
    "redacted_body_state",
    "redacted_body",
    "redacted_body_hash",
    "redaction_stats",
    "redaction_match_count",
    "model_send_state",
    "unsupported_context_sources",
    "redaction_result_hash",
)
_REDACTION_HASH_FIELDS = tuple(
    key for key in _REDACTION_FIELDS if key not in {"redacted_body", "redaction_result_hash"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "TOKEN_FRAMING_UPSTREAM_INCONSISTENT",
        "Token + Framing 所消费的正式上游形状、状态或 identity 无法闭合。",
    )


def _counting_policy_unsupported() -> HTTPException:
    return _error(
        "TOKEN_FRAMING_COUNTING_POLICY_UNSUPPORTED",
        "当前 V1 只支持 utf8_byte_upper_bound_v1。",
    )


def _internal_inconsistent() -> HTTPException:
    return _error(
        "TOKEN_FRAMING_INTERNAL_INCONSISTENT",
        "Token + Framing 内部 canonical framing 或 hash 构造发生不一致。",
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
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_exact_mapping(value: object, fields: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != fields:
        raise _upstream_inconsistent()
    return value


def _validate_core(value: object, *, model_call_id: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _upstream_inconsistent()
    required = {
        "schema_version",
        "manifest_core_hash",
        "model_call_id",
        "call_identity_hash",
        "snapshot_id",
        "snapshot_hash",
        "project_id",
        "candidate_set_hash",
        "budget_profile",
        "profile",
        "items",
        "unsupported_context_sources",
        "redaction_state",
        "token_count_state",
        "budget_fit_state",
        "framing_state",
        "model_send_state",
        "final_manifest_state",
    }
    if not required.issubset(value):
        raise _upstream_inconsistent()
    if (
        value["schema_version"] != "context_manifest_core_v1"
        or value["model_call_id"] != model_call_id
        or type(value["model_call_id"]) is not int
        or value["model_call_id"] <= 0
        or type(value["snapshot_id"]) is not int
        or value["snapshot_id"] <= 0
        or type(value["project_id"]) is not int
        or value["project_id"] <= 0
        or value["redaction_state"] != "pending"
        or value["token_count_state"] != "not_counted"
        or value["budget_fit_state"] != "not_evaluated"
        or value["framing_state"] != "not_defined"
        or value["model_send_state"] != "not_admitted"
        or value["final_manifest_state"] != "not_final"
        or not isinstance(value["profile"], Mapping)
        or not isinstance(value["items"], list)
    ):
        raise _upstream_inconsistent()
    for field in ("manifest_core_hash", "call_identity_hash", "snapshot_hash", "candidate_set_hash"):
        if not _is_sha256(value[field]):
            raise _upstream_inconsistent()
    unsupported = value["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()
    for item in value["items"]:
        if not isinstance(item, Mapping) or type(item.get("evidence_id")) is not str or not item["evidence_id"]:
            raise _upstream_inconsistent()
    payload = {key: deepcopy(item) for key, item in value.items() if key != "manifest_core_hash"}
    if _stable_hash(payload) != value["manifest_core_hash"]:
        raise _upstream_inconsistent()
    return value


def _validate_budget(value: object, core: Mapping[str, object]) -> Mapping[str, object]:
    budget = _require_exact_mapping(value, _BUDGET_FIELDS)
    if (
        budget["schema_version"] != "model_budget_profile_v1"
        or budget["model_call_id"] != core["model_call_id"]
        or budget["call_identity_hash"] != core["call_identity_hash"]
        or budget["budget_authority_state"] != "assertion_only"
        or budget["token_count_state"] != "not_counted"
        or budget["model_send_state"] != "not_admitted"
        or type(budget["max_input_tokens"]) is not int
        or budget["max_input_tokens"] <= 0
        or not _is_sha256(budget["budget_profile_hash"])
    ):
        raise _upstream_inconsistent()
    if budget["counting_policy_version"] != COUNTING_POLICY_VERSION:
        raise _counting_policy_unsupported()
    payload = {
        field: deepcopy(budget[field])
        for field in _BUDGET_FIELDS
        if field != "budget_profile_hash"
    }
    if _stable_hash(payload) != budget["budget_profile_hash"]:
        raise _upstream_inconsistent()
    return budget


def _validate_admission(
    value: object,
    *,
    core: Mapping[str, object],
    target: str,
) -> Mapping[str, object]:
    admission = _require_exact_mapping(value, _ADMISSION_FIELDS)
    if (
        admission["schema_version"] != "model_send_admission_v1"
        or admission["model_call_id"] != core["model_call_id"]
        or admission["call_identity_hash"] != core["call_identity_hash"]
        or admission["snapshot_id"] != core["snapshot_id"]
        or admission["snapshot_hash"] != core["snapshot_hash"]
        or admission["project_id"] != core["project_id"]
        or admission["candidate_set_hash"] != core["candidate_set_hash"]
        or admission["target"] != target
        or admission["model_send_state"] not in {"admitted", "denied"}
        or admission["admission_reason"]
        not in {
            "content_policy_passed",
            "sensitive_path_hard_deny",
            "binary_metadata_only_no_sendable_body",
            "credential_boundary_quarantined",
            "no_analyzable_source_after_redaction",
        }
        or not _is_sha256(admission["redaction_result_hash"])
        or not _is_sha256(admission["model_send_admission_hash"])
        or not isinstance(admission["source_identity"], Mapping)
    ):
        raise _upstream_inconsistent()
    if admission["unsupported_context_sources"] != core["unsupported_context_sources"]:
        raise _upstream_inconsistent()
    payload = {field: deepcopy(admission[field]) for field in _ADMISSION_HASH_FIELDS}
    if _stable_hash(payload) != admission["model_send_admission_hash"]:
        raise _upstream_inconsistent()
    if admission["model_send_state"] == "admitted":
        if admission["admission_reason"] != "content_policy_passed":
            raise _upstream_inconsistent()
    elif admission["admission_reason"] == "content_policy_passed":
        raise _upstream_inconsistent()
    return admission


def _expected_redacted_body_hash(redaction: Mapping[str, object]) -> str:
    kind = redaction["raw_body_kind"]
    body = redaction["redacted_body"]
    if kind in {"profile_json", "prd_structured_block"}:
        if not isinstance(body, Mapping):
            raise _upstream_inconsistent()
        return _stable_hash(body)
    if kind == "text_unified_diff":
        if not isinstance(body, str):
            raise _upstream_inconsistent()
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    raise _upstream_inconsistent()


def _validate_redaction(
    value: object,
    *,
    core: Mapping[str, object],
    admission: Mapping[str, object],
) -> Mapping[str, object]:
    redaction = _require_exact_mapping(value, _REDACTION_FIELDS)
    if (
        redaction["schema_version"] != "context_redaction_result_v1"
        or redaction["manifest_core_hash"] != core["manifest_core_hash"]
        or redaction["model_call_id"] != core["model_call_id"]
        or redaction["call_identity_hash"] != core["call_identity_hash"]
        or redaction["snapshot_id"] != core["snapshot_id"]
        or redaction["snapshot_hash"] != core["snapshot_hash"]
        or redaction["project_id"] != core["project_id"]
        or redaction["candidate_set_hash"] != core["candidate_set_hash"]
        or redaction["target"] != admission["target"]
        or redaction["target_type"] != admission["target_type"]
        or redaction["source_identity"] != admission["source_identity"]
        or redaction["redaction_state"] != "completed"
        or redaction["redacted_body_state"] != "local_redacted_ephemeral"
        or redaction["model_send_state"] != "not_admitted"
        or redaction["unsupported_context_sources"] != core["unsupported_context_sources"]
        or redaction["redaction_result_hash"] != admission["redaction_result_hash"]
        or not _is_sha256(redaction["redaction_result_hash"])
        or not _is_sha256(redaction["redacted_body_hash"])
    ):
        raise _upstream_inconsistent()
    if redaction["redacted_body_hash"] != _expected_redacted_body_hash(redaction):
        raise _upstream_inconsistent()
    payload = {field: deepcopy(redaction[field]) for field in _REDACTION_HASH_FIELDS}
    if _stable_hash(payload) != redaction["redaction_result_hash"]:
        raise _upstream_inconsistent()
    return redaction


def _targets(core: Mapping[str, object]) -> list[str]:
    targets = ["profile"]
    seen = {"profile"}
    for item in core["items"]:
        if not isinstance(item, Mapping):
            raise _upstream_inconsistent()
        target = item.get("evidence_id")
        if type(target) is not str or not target or target in seen:
            raise _upstream_inconsistent()
        seen.add(target)
        targets.append(target)
    return targets


def _batch_selection(model_call_id: int):
    # Lazy import keeps the normal single-call path independent of the planner.
    from app.report_generation_batches import get_batch_call_selection
    return get_batch_call_selection(model_call_id)


def _selected_targets(core):
    targets = _targets(core)
    selection = _batch_selection(core["model_call_id"])
    if selection is None:
        return [(target, None) for target in targets]
    chunks = selection.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise _upstream_inconsistent()
    seen = set()
    selected = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {"target", "start", "end", "body_hash"}:
            raise _upstream_inconsistent()
        target = chunk["target"]
        if not isinstance(target, str) or target not in targets or target in seen:
            raise _upstream_inconsistent()
        if (type(chunk["start"]) is not int or type(chunk["end"]) is not int
                or not 0 <= chunk["start"] < chunk["end"] or not _is_sha256(chunk["body_hash"])):
            raise _upstream_inconsistent()
        seen.add(target)
        selected.append((target, chunk))
    if (not isinstance(selection.get("evidence_ids"), list)
            or any(type(item) is not str for item in selection["evidence_ids"])
            or len(set(selection["evidence_ids"])) != len(selection["evidence_ids"])
            or set(selection["evidence_ids"]) != seen - {"profile"}):
        raise _upstream_inconsistent()
    return selected


def _selected_body(redaction, chunk):
    if chunk is None:
        return deepcopy(redaction["redacted_body"])
    complete = _canonical_bytes(redaction["redacted_body"])
    text = complete.decode("utf-8")
    if hashlib.sha256(complete).hexdigest() != chunk["body_hash"] or chunk["end"] > len(text):
        raise _upstream_inconsistent()
    return {"encoding": "canonical_json_fragment_v1", "start": chunk["start"],
            "end": chunk["end"], "source_body_hash": chunk["body_hash"],
            "text": text[chunk["start"]:chunk["end"]]}


def _frame(
    *,
    ordinal: int,
    admission: Mapping[str, object],
    redaction: Mapping[str, object],
    chunk=None,
) -> tuple[bytes, dict[str, object]]:
    frame = {
        "frame_schema_version": FRAME_SCHEMA_VERSION,
        "ordinal": ordinal,
        "target": admission["target"],
        "target_type": admission["target_type"],
        "redaction_result_hash": redaction["redaction_result_hash"],
        "body": _selected_body(redaction, chunk),
    }
    encoded = _canonical_bytes(frame)
    summary = {
        "ordinal": ordinal,
        "target": admission["target"],
        "target_type": admission["target_type"],
        "model_send_admission_hash": admission["model_send_admission_hash"],
        "redaction_result_hash": redaction["redaction_result_hash"],
        "frame_hash": hashlib.sha256(encoded).hexdigest(),
        "frame_utf8_bytes": len(encoded),
    }
    return encoded, summary


def build_context_token_framing_accounting(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Build local context framing and a conservative budget assertion; never authorize send."""
    core = _validate_core(
        build_context_manifest_core(model_call_id=model_call_id, budget_record=budget_record),
        model_call_id=model_call_id,
    )
    budget = _validate_budget(core["budget_profile"], core)

    frame_bytes: list[bytes] = []
    admitted_targets: list[dict[str, object]] = []
    denied_targets: list[dict[str, object]] = []

    for ordinal, (target, chunk) in enumerate(_selected_targets(core), start=1):
        admission = _validate_admission(
            build_model_send_admission(
                model_call_id=model_call_id,
                budget_record=budget_record,
                target=target,
            ),
            core=core,
            target=target,
        )
        if admission["model_send_state"] == "denied":
            denied_targets.append(
                {
                    "ordinal": ordinal,
                    "target": admission["target"],
                    "target_type": admission["target_type"],
                    "admission_reason": admission["admission_reason"],
                    "matched_rule_id": admission["matched_rule_id"],
                    "model_send_admission_hash": admission["model_send_admission_hash"],
                }
            )
            continue

        redaction = _validate_redaction(
            build_context_redaction_result(
                model_call_id=model_call_id,
                budget_record=budget_record,
                target=target,
            ),
            core=core,
            admission=admission,
        )
        encoded, summary = _frame(
            ordinal=ordinal,
            admission=admission,
            redaction=redaction,
            chunk=chunk,
        )
        frame_bytes.append(encoded)
        admitted_targets.append(summary)

    payload = b"\n".join(frame_bytes)
    payload_bytes = len(payload)
    max_input_tokens = budget["max_input_tokens"]
    if type(max_input_tokens) is not int or max_input_tokens <= 0:
        raise _upstream_inconsistent()

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_core_hash": core["manifest_core_hash"],
        "budget_profile_hash": budget["budget_profile_hash"],
        "model_call_id": core["model_call_id"],
        "call_identity_hash": core["call_identity_hash"],
        "snapshot_id": core["snapshot_id"],
        "snapshot_hash": core["snapshot_hash"],
        "project_id": core["project_id"],
        "candidate_set_hash": core["candidate_set_hash"],
        "counting_policy_version": COUNTING_POLICY_VERSION,
        "framing_policy_version": FRAMING_POLICY_VERSION,
        "framing_scope": FRAMING_SCOPE,
        "context_admission_state": (
            "contains_denied_targets" if denied_targets else "all_targets_admitted"
        ),
        "coverage_state": (
            "partial_fail_visible"
            if core["unsupported_context_sources"] or denied_targets
            else "supported_context_complete"
        ),
        "admitted_targets": admitted_targets,
        "denied_targets": denied_targets,
        "framed_payload_hash": hashlib.sha256(payload).hexdigest(),
        "framed_payload_utf8_bytes": payload_bytes,
        "conservative_input_token_upper_bound": payload_bytes,
        "exact_tokens": None,
        "budget_fit_state": (
            "fit_by_conservative_upper_bound"
            if payload_bytes <= max_input_tokens
            else "not_fit_by_conservative_upper_bound"
        ),
        "final_request_fit_state": FINAL_REQUEST_FIT_STATE,
        "unsupported_context_sources": list(core["unsupported_context_sources"]),
    }
    result["token_framing_accounting_hash"] = _stable_hash(
        {field: deepcopy(result[field]) for field in _RESULT_HASH_KEYS}
    )
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("token_framing_accounting_v1 key construction drift")
    return result


def _materialize_context_payload_transient(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Rebuild the canonical admitted payload for one in-stack Gateway rebind only."""
    core = _validate_core(
        build_context_manifest_core(model_call_id=model_call_id, budget_record=budget_record),
        model_call_id=model_call_id,
    )
    _validate_budget(core["budget_profile"], core)

    frame_bytes: list[bytes] = []
    for ordinal, (target, chunk) in enumerate(_selected_targets(core), start=1):
        admission = _validate_admission(
            build_model_send_admission(
                model_call_id=model_call_id,
                budget_record=budget_record,
                target=target,
            ),
            core=core,
            target=target,
        )
        if admission["model_send_state"] == "denied":
            continue
        redaction = _validate_redaction(
            build_context_redaction_result(
                model_call_id=model_call_id,
                budget_record=budget_record,
                target=target,
            ),
            core=core,
            admission=admission,
        )
        encoded, _ = _frame(
            ordinal=ordinal,
            admission=admission,
            redaction=redaction,
            chunk=chunk,
        )
        frame_bytes.append(encoded)

    payload = b"\n".join(frame_bytes)
    return {
        "payload": payload,
        "framed_payload_hash": hashlib.sha256(payload).hexdigest(),
        "framed_payload_utf8_bytes": len(payload),
    }
