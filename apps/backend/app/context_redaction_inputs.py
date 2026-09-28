"""Context Redaction Input Closure V1: exact historical raw-body closure, never sendable."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app.context_candidate_runtime import build_context_candidate_set
from app.context_manifest import build_context_manifest_core
from app.context_resolver import resolve_git_file_fact, resolve_prd_block


SCHEMA_VERSION = "context_redaction_input_v1"
RAW_BODY_STATE = "local_unredacted_ephemeral"
MODEL_SEND_STATE = "not_admitted"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CORE_REQUIRED_FIELDS = (
    "schema_version",
    "manifest_stage",
    "model_call_id",
    "call_identity_hash",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "candidate_set_hash",
    "profile",
    "items",
    "excluded",
    "compression",
    "unsupported_context_sources",
    "redaction_state",
    "token_count_state",
    "budget_fit_state",
    "framing_state",
    "model_send_state",
    "final_manifest_state",
    "manifest_core_hash",
)
_PROFILE_IDENTITY_FIELDS = (
    "profile_id",
    "profile_content_hash",
    "source_prd_id",
)
_PROFILE_CLOSURE_FIELDS = (
    "project_id",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
)
_PRD_IDENTITY_FIELDS = (
    "evidence_id",
    "type",
    "source_ref",
    "content_hash",
    "prd_id",
    "ordinal",
    "kind",
    "heading_level",
    "page_no",
    "prd_structured_hash",
    "prd_document_fingerprint",
)
_GIT_IDENTITY_FIELDS = (
    "evidence_id",
    "type",
    "source_ref",
    "content_hash",
    "git_snapshot_id",
    "ordinal",
    "path",
    "added_lines",
    "deleted_lines",
    "is_binary",
    "from_commit",
    "to_commit",
    "git_facts_hash",
    "file_manifest_hash",
    "resolved_content_hash",
    "resolved_diff_bytes",
    "content_kind",
)


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _target_invalid() -> HTTPException:
    return _error(
        "CONTEXT_REDACTION_INPUT_TARGET_INVALID",
        "目标不是当前 formal Context Manifest Core 中唯一可闭合的 redaction input target。",
    )


def _body_drift() -> HTTPException:
    return _error(
        "CONTEXT_REDACTION_INPUT_BODY_DRIFT",
        "重新解析的 historical raw body identity 与 formal Context Manifest Core 不一致。",
    )


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "CONTEXT_REDACTION_INPUT_UPSTREAM_INCONSISTENT",
        "formal Context Manifest Core 的固定形状或状态无法闭合。",
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _upstream_inconsistent()
    return value


def _validate_core(core: object) -> Mapping[str, object]:
    mapping = _require_mapping(core)
    if any(field not in mapping for field in _CORE_REQUIRED_FIELDS):
        raise _upstream_inconsistent()
    if (
        mapping["schema_version"] != "context_manifest_core_v1"
        or mapping["manifest_stage"] != "pre_redaction_selection_core"
        or mapping["redaction_state"] != "pending"
        or mapping["token_count_state"] != "not_counted"
        or mapping["budget_fit_state"] != "not_evaluated"
        or mapping["framing_state"] != "not_defined"
        or mapping["model_send_state"] != MODEL_SEND_STATE
        or mapping["final_manifest_state"] != "not_final"
        or mapping["excluded"] != []
        or mapping["compression"] != []
    ):
        raise _upstream_inconsistent()
    if (
        type(mapping["model_call_id"]) is not int
        or mapping["model_call_id"] <= 0
        or type(mapping["snapshot_id"]) is not int
        or mapping["snapshot_id"] <= 0
        or type(mapping["project_id"]) is not int
        or mapping["project_id"] <= 0
        or not _is_sha256(mapping["call_identity_hash"])
        or not _is_sha256(mapping["snapshot_hash"])
        or not _is_sha256(mapping["candidate_set_hash"])
        or not _is_sha256(mapping["manifest_core_hash"])
    ):
        raise _upstream_inconsistent()
    if not isinstance(mapping["items"], list):
        raise _upstream_inconsistent()
    unsupported = mapping["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()

    profile = _require_mapping(mapping["profile"])
    if (
        any(field not in profile for field in (*_PROFILE_IDENTITY_FIELDS, "resolved_content_redaction_state", "model_send_state", "selection_state"))
        or profile["resolved_content_redaction_state"] != "pending"
        or profile["model_send_state"] != MODEL_SEND_STATE
        or profile["selection_state"] != "preserved_pending_processing"
        or type(profile["profile_id"]) is not int
        or profile["profile_id"] <= 0
        or type(profile["source_prd_id"]) is not int
        or profile["source_prd_id"] <= 0
        or not _is_sha256(profile["profile_content_hash"])
    ):
        raise _upstream_inconsistent()

    for item in mapping["items"]:
        item_mapping = _require_mapping(item)
        if (
            item_mapping.get("model_send_state") != MODEL_SEND_STATE
            or item_mapping.get("selection_state") != "preserved_pending_processing"
            or not isinstance(item_mapping.get("evidence_id"), str)
            or not item_mapping["evidence_id"]
        ):
            raise _upstream_inconsistent()

    payload = dict(mapping)
    declared_hash = payload.pop("manifest_core_hash")
    if _stable_hash(payload) != declared_hash:
        raise _upstream_inconsistent()
    return mapping


def _target_item(core: Mapping[str, object], target: str) -> Mapping[str, object]:
    matches: list[Mapping[str, object]] = []
    for value in core["items"]:
        item = _require_mapping(value)
        if item.get("evidence_id") == target:
            matches.append(item)
    if len(matches) != 1:
        raise _target_invalid()
    if matches[0].get("type") not in {"prd_block", "git_file_fact"}:
        raise _target_invalid()
    return matches[0]


def _identity_equal(
    left: Mapping[str, object],
    right: Mapping[str, object],
    fields: tuple[str, ...],
) -> bool:
    return all(field in left and field in right and left[field] == right[field] for field in fields)


def _result(
    *,
    core: Mapping[str, object],
    target: str,
    target_type: str,
    source_identity: dict[str, object],
    raw_body_kind: str,
    raw_body: object,
    raw_body_state: str,
    resolved_content_redaction_state: str,
) -> dict[str, object]:
    safe_payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_core_hash": core["manifest_core_hash"],
        "model_call_id": core["model_call_id"],
        "call_identity_hash": core["call_identity_hash"],
        "snapshot_id": core["snapshot_id"],
        "snapshot_hash": core["snapshot_hash"],
        "project_id": core["project_id"],
        "candidate_set_hash": core["candidate_set_hash"],
        "target": target,
        "target_type": target_type,
        "source_identity": source_identity,
        "raw_body_kind": raw_body_kind,
        "raw_body_state": raw_body_state,
        "resolved_content_redaction_state": resolved_content_redaction_state,
        "model_send_state": MODEL_SEND_STATE,
        "unsupported_context_sources": list(core["unsupported_context_sources"]),
    }
    result = dict(safe_payload)
    result["raw_body"] = deepcopy(raw_body)
    result["redaction_input_hash"] = _stable_hash(safe_payload)
    return result


def _resolve_profile(core: Mapping[str, object]) -> dict[str, object]:
    candidate = _require_mapping(build_context_candidate_set(core["snapshot_id"]))
    if not _identity_equal(candidate, core, _PROFILE_CLOSURE_FIELDS):
        raise _body_drift()
    profile = _require_mapping(candidate.get("profile"))
    core_profile = _require_mapping(core["profile"])
    if (
        not _identity_equal(profile, core_profile, _PROFILE_IDENTITY_FIELDS)
        or profile.get("resolved_content_redaction_state") != "pending"
        or profile.get("model_send_state") != MODEL_SEND_STATE
        or "content" not in profile
    ):
        raise _body_drift()
    content = profile["content"]
    if not isinstance(content, Mapping):
        raise _body_drift()
    source_identity = {field: profile[field] for field in _PROFILE_IDENTITY_FIELDS}
    raw_content = dict(content)
    if "progress_context_hash" in profile or "progress_context_hash" in core_profile:
        progress_hash = profile.get("progress_context_hash")
        frozen = profile.get("progress_context")
        if (not _is_sha256(progress_hash) or progress_hash != core_profile.get("progress_context_hash")
                or not isinstance(frozen, Mapping) or frozen.get("context_hash") != progress_hash):
            raise _body_drift()
        frozen_payload = {key: value for key, value in frozen.items() if key != "context_hash"}
        if _stable_hash(frozen_payload) != progress_hash:
            raise _body_drift()
        source_identity["progress_context_hash"] = progress_hash
        # The original profile remains immutable. This additional, hash-bound source
        # passes through the same redaction and token budgeting as the profile.
        raw_content["approved_project_progress"] = deepcopy(frozen_payload)
    return _result(
        core=core,
        target="profile",
        target_type="profile",
        source_identity=source_identity,
        raw_body_kind="profile_json",
        raw_body=raw_content,
        raw_body_state=RAW_BODY_STATE,
        resolved_content_redaction_state="pending",
    )


def _resolve_prd(core: Mapping[str, object], item: Mapping[str, object], target: str) -> dict[str, object]:
    if (
        item.get("resolved_content_redaction_state") != "pending"
        or item.get("model_send_state") != MODEL_SEND_STATE
    ):
        raise _upstream_inconsistent()
    resolved = _require_mapping(resolve_prd_block(core["snapshot_id"], target))
    if not _identity_equal(resolved, item, _PRD_IDENTITY_FIELDS):
        raise _body_drift()
    source_identity = {field: resolved[field] for field in _PRD_IDENTITY_FIELDS}
    raw_body = {
        "kind": resolved.get("kind"),
        "text": resolved.get("text"),
        "table_rows": deepcopy(resolved.get("table_rows")),
        "heading_level": resolved.get("heading_level"),
        "page_no": resolved.get("page_no"),
    }
    return _result(
        core=core,
        target=target,
        target_type="prd_block",
        source_identity=source_identity,
        raw_body_kind="prd_structured_block",
        raw_body=raw_body,
        raw_body_state=RAW_BODY_STATE,
        resolved_content_redaction_state="pending",
    )


def _resolve_git(core: Mapping[str, object], item: Mapping[str, object], target: str) -> dict[str, object]:
    resolved = _require_mapping(resolve_git_file_fact(core["snapshot_id"], target))
    if not _identity_equal(resolved, item, _GIT_IDENTITY_FIELDS):
        raise _body_drift()
    metadata_kind = resolved.get("content_kind")
    if metadata_kind in {"binary_metadata_only", "sensitive_path_metadata_only"}:
        if (
            resolved.get("diff_text") is not None
            or resolved.get("resolved_content_hash") is not None
            or resolved.get("resolved_diff_bytes") != 0
            or resolved.get("resolved_content_redaction_state") != "not_applicable"
            or (resolved.get("is_binary") is not (metadata_kind == "binary_metadata_only"))
            or item.get("resolved_content_redaction_state") != "not_applicable"
            or item.get("model_send_state") != MODEL_SEND_STATE
        ):
            raise _body_drift()
        raw_body = None
        raw_body_state = "not_applicable"
        redaction_state = "not_applicable"
    elif resolved.get("is_binary") is False:
        diff_text = resolved.get("diff_text")
        if (
            not isinstance(diff_text, str)
            or resolved.get("resolved_content_redaction_state") != "pending"
            or resolved.get("content_kind") != "text_unified_diff"
            or item.get("resolved_content_redaction_state") != "pending"
            or item.get("model_send_state") != MODEL_SEND_STATE
        ):
            raise _body_drift()
        raw = diff_text.encode("utf-8")
        if (
            hashlib.sha256(raw).hexdigest() != resolved.get("resolved_content_hash")
            or len(raw) != resolved.get("resolved_diff_bytes")
        ):
            raise _body_drift()
        raw_body = diff_text
        raw_body_state = RAW_BODY_STATE
        redaction_state = "pending"
    else:
        raise _body_drift()

    source_identity = {field: resolved[field] for field in _GIT_IDENTITY_FIELDS}
    return _result(
        core=core,
        target=target,
        target_type="git_file_fact",
        source_identity=source_identity,
        raw_body_kind=(
            resolved["content_kind"]
        ),
        raw_body=raw_body,
        raw_body_state=raw_body_state,
        resolved_content_redaction_state=redaction_state,
    )


def resolve_context_redaction_input(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
    target: str,
) -> dict[str, object]:
    """Resolve exactly one frozen historical target for an in-process redaction consumer.

    The returned raw body is explicitly unredacted, ephemeral, non-persisted and never model-send
    admitted. This entry point does not define redaction policy, sensitive paths, token accounting,
    provider qualification or provider transport.
    """
    if not isinstance(target, str) or not target:
        raise _target_invalid()

    core = _validate_core(
        build_context_manifest_core(
            model_call_id=model_call_id,
            budget_record=budget_record,
        )
    )
    if core["model_call_id"] != model_call_id:
        raise _upstream_inconsistent()

    if target == "profile":
        return _resolve_profile(core)

    item = _target_item(core, target)
    if item["type"] == "prd_block":
        return _resolve_prd(core, item, target)
    if item["type"] == "git_file_fact":
        return _resolve_git(core, item, target)
    raise _target_invalid()
