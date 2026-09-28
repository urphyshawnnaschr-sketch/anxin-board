"""Sensitive-Path Policy Identity V1: deterministic policy + historical path identity only."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app.context_redaction import is_quarantined_body
from app.context_redaction_runtime import build_context_redaction_result


SCHEMA_VERSION = "sensitive_path_policy_identity_result_v1"
POLICY_SCHEMA_VERSION = "sensitive_path_policy_v1"
POLICY_ID = "builtin_sensitive_path_v1"
POLICY_VERSION = "1.0"
POLICY_ORIGIN = "governance_frozen_builtin"
PATH_NAMESPACE = "historical_git_repository_relative_path"
PATH_SUBJECT_IDENTITY_VERSION = "historical-git-path-identity-1.0"
MATCH_SEMANTICS_ID = "ascii-ci-git-path-exact-basename-suffix-v1"
RULE_SCHEMA_VERSION = "sensitive-path-rule-v1"
POLICY_EVALUATION_STATE = "not_evaluated"
MODEL_SEND_STATE = "not_admitted"

_RULE_ROWS = (
    ("SP01", "exact_basename", ".env"),
    ("SP02", "exact_basename", ".env.local"),
    ("SP03", "exact_basename", ".env.development.local"),
    ("SP04", "exact_basename", ".env.test.local"),
    ("SP05", "exact_basename", ".env.production.local"),
    ("SP06", "exact_basename", ".git-credentials"),
    ("SP07", "exact_basename", ".netrc"),
    ("SP08", "exact_basename", "_netrc"),
    ("SP09", "exact_basename", ".npmrc"),
    ("SP10", "exact_basename", ".pypirc"),
    ("SP11", "exact_basename", "id_rsa"),
    ("SP12", "exact_basename", "id_dsa"),
    ("SP13", "exact_basename", "id_ecdsa"),
    ("SP14", "exact_basename", "id_ed25519"),
    ("SP15", "exact_basename", "credentials.json"),
    ("SP16", "exact_basename", "secrets.json"),
    ("SP17", "exact_basename", "secrets.yaml"),
    ("SP18", "exact_basename", "secrets.yml"),
    ("SP19", "exact_basename", "secrets.toml"),
    ("SP20", "basename_suffix", ".key"),
    ("SP21", "basename_suffix", ".p12"),
    ("SP22", "basename_suffix", ".pfx"),
    ("SP23", "basename_suffix", ".jks"),
    ("SP24", "basename_suffix", ".keystore"),
    ("SP25", "basename_suffix", ".tfstate"),
    ("SP26", "basename_suffix", ".tfstate.backup"),
)
RULES = tuple(
    {"rule_id": rule_id, "rule_type": rule_type, "value": value}
    for rule_id, rule_type, value in _RULE_ROWS
)
RULE_ORDER = tuple(rule_id for rule_id, _rule_type, _value in _RULE_ROWS)

POLICY_DESCRIPTOR: dict[str, object] = {
    "policy_schema_version": POLICY_SCHEMA_VERSION,
    "policy_id": POLICY_ID,
    "policy_version": POLICY_VERSION,
    "policy_origin": POLICY_ORIGIN,
    "path_namespace": PATH_NAMESPACE,
    "path_subject_identity_version": PATH_SUBJECT_IDENTITY_VERSION,
    "match_semantics_id": MATCH_SEMANTICS_ID,
    "rule_schema_version": RULE_SCHEMA_VERSION,
    "rules": [dict(rule) for rule in RULES],
    "rule_order": list(RULE_ORDER),
}

_POLICY_DESCRIPTOR_KEYS = (
    "policy_schema_version",
    "policy_id",
    "policy_version",
    "policy_origin",
    "path_namespace",
    "path_subject_identity_version",
    "match_semantics_id",
    "rule_schema_version",
    "rules",
    "rule_order",
)
_RULE_KEYS = ("rule_id", "rule_type", "value")
_ALLOWED_RULE_TYPES = {"exact_relative_path", "exact_basename", "basename_suffix"}
_ALLOWED_REDACTION_RULE_IDS = frozenset({"R1", "R2", "R3", "R4", "R5", "R6"})

_UPSTREAM_RESULT_KEYS = (
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
_UPSTREAM_HASH_KEYS = tuple(
    key for key in _UPSTREAM_RESULT_KEYS if key not in {"redacted_body", "redaction_result_hash"}
)

_RESULT_KEYS = (
    "schema_version",
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
    "model_send_state",
    "unsupported_context_sources",
    "sensitive_path_policy_identity_hash",
)
_RESULT_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "sensitive_path_policy_identity_hash")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


SENSITIVE_PATH_POLICY_HASH = _stable_hash(POLICY_DESCRIPTOR)


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "SENSITIVE_PATH_POLICY_UPSTREAM_INCONSISTENT",
        "formal Redaction Transform 的固定形状、状态或安全 identity 无法闭合。",
    )


def _internal_policy_inconsistent() -> HTTPException:
    return _error(
        "SENSITIVE_PATH_POLICY_INTERNAL_POLICY_INCONSISTENT",
        "内置 Sensitive-Path policy 的固定 schema、catalog 或 identity 无法闭合。",
    )


def _unsupported_target() -> HTTPException:
    return _error(
        "SENSITIVE_PATH_POLICY_UNSUPPORTED_TARGET",
        "目标类型不属于 Sensitive-Path Policy Identity V1 支持范围。",
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _expected_policy_descriptor() -> dict[str, object]:
    return {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "policy_origin": POLICY_ORIGIN,
        "path_namespace": PATH_NAMESPACE,
        "path_subject_identity_version": PATH_SUBJECT_IDENTITY_VERSION,
        "match_semantics_id": MATCH_SEMANTICS_ID,
        "rule_schema_version": RULE_SCHEMA_VERSION,
        "rules": [
            {"rule_id": rule_id, "rule_type": rule_type, "value": value}
            for rule_id, rule_type, value in _RULE_ROWS
        ],
        "rule_order": [rule_id for rule_id, _rule_type, _value in _RULE_ROWS],
    }


def _validate_internal_policy() -> None:
    expected = _expected_policy_descriptor()
    if tuple(POLICY_DESCRIPTOR) != _POLICY_DESCRIPTOR_KEYS or POLICY_DESCRIPTOR != expected:
        raise _internal_policy_inconsistent()

    rules = POLICY_DESCRIPTOR["rules"]
    order = POLICY_DESCRIPTOR["rule_order"]
    if not isinstance(rules, list) or not isinstance(order, list) or len(rules) != 26:
        raise _internal_policy_inconsistent()
    if order != list(RULE_ORDER):
        raise _internal_policy_inconsistent()

    seen: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or tuple(rule) != _RULE_KEYS:
            raise _internal_policy_inconsistent()
        rule_id = rule["rule_id"]
        rule_type = rule["rule_type"]
        value = rule["value"]
        if (
            type(rule_id) is not str
            or not rule_id
            or rule_id in seen
            or rule_id != RULE_ORDER[index]
            or type(rule_type) is not str
            or rule_type not in _ALLOWED_RULE_TYPES
            or type(value) is not str
            or not value
        ):
            raise _internal_policy_inconsistent()
        seen.add(rule_id)

    if _stable_hash(POLICY_DESCRIPTOR) != SENSITIVE_PATH_POLICY_HASH:
        raise _internal_policy_inconsistent()


def _validate_upstream(
    value: object, *, model_call_id: int, target: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _UPSTREAM_RESULT_KEYS:
        raise _upstream_inconsistent()

    if (
        value["schema_version"] != "context_redaction_result_v1"
        or value["model_call_id"] != model_call_id
        or value["target"] != target
        or value["redaction_policy_id"] != "credential_redaction_v1"
        or value["model_send_state"] != MODEL_SEND_STATE
        or type(value["model_call_id"]) is not int
        or value["model_call_id"] <= 0
        or type(value["snapshot_id"]) is not int
        or value["snapshot_id"] <= 0
        or type(value["project_id"]) is not int
        or value["project_id"] <= 0
        or type(value["target"]) is not str
        or not value["target"]
        or type(value["target_type"]) is not str
        or not isinstance(value["source_identity"], Mapping)
    ):
        raise _upstream_inconsistent()

    for field in (
        "redaction_input_hash",
        "manifest_core_hash",
        "call_identity_hash",
        "snapshot_hash",
        "candidate_set_hash",
        "redaction_policy_hash",
        "redaction_result_hash",
    ):
        if not _is_sha256(value[field]):
            raise _upstream_inconsistent()

    unsupported = value["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()

    stats = value["redaction_stats"]
    if not isinstance(stats, Mapping) or any(
        type(rule_id) is not str
        or rule_id not in _ALLOWED_REDACTION_RULE_IDS
        or type(count) is not int
        or count <= 0
        for rule_id, count in stats.items()
    ):
        raise _upstream_inconsistent()
    if type(value["redaction_match_count"]) is not int or value["redaction_match_count"] < 0:
        raise _upstream_inconsistent()
    if sum(stats.values()) != value["redaction_match_count"]:
        raise _upstream_inconsistent()

    kind = value["raw_body_kind"]
    target_type = value["target_type"]
    if value["redaction_state"] == "quarantined" and (
        not is_quarantined_body(kind, value["redacted_body"])
        or value["redaction_stats"] or value["redaction_match_count"] != 0
    ):
        raise _upstream_inconsistent()
    if kind == "profile_json":
        if (
            target_type != "profile"
            or value["redaction_state"] not in {"completed", "quarantined"}
            or value["redacted_body_state"] != "local_redacted_ephemeral"
            or not isinstance(value["redacted_body"], Mapping)
            or not _is_sha256(value["redacted_body_hash"])
        ):
            raise _upstream_inconsistent()
        try:
            body_hash = _stable_hash(value["redacted_body"])
        except (TypeError, ValueError):
            raise _upstream_inconsistent() from None
        if body_hash != value["redacted_body_hash"]:
            raise _upstream_inconsistent()
    elif kind == "prd_structured_block":
        if (
            target_type != "prd_block"
            or value["redaction_state"] not in {"completed", "quarantined"}
            or value["redacted_body_state"] != "local_redacted_ephemeral"
            or not isinstance(value["redacted_body"], Mapping)
            or not _is_sha256(value["redacted_body_hash"])
        ):
            raise _upstream_inconsistent()
        try:
            body_hash = _stable_hash(value["redacted_body"])
        except (TypeError, ValueError):
            raise _upstream_inconsistent() from None
        if body_hash != value["redacted_body_hash"]:
            raise _upstream_inconsistent()
    elif kind == "text_unified_diff":
        if (
            target_type != "git_file_fact"
            or value["redaction_state"] not in {"completed", "quarantined"}
            or value["redacted_body_state"] != "local_redacted_ephemeral"
            or not isinstance(value["redacted_body"], str)
            or not _is_sha256(value["redacted_body_hash"])
        ):
            raise _upstream_inconsistent()
        body_hash = hashlib.sha256(value["redacted_body"].encode("utf-8")).hexdigest()
        if body_hash != value["redacted_body_hash"]:
            raise _upstream_inconsistent()
    elif kind in {"binary_metadata_only", "sensitive_path_metadata_only"}:
        if (
            target_type != "git_file_fact"
            or value["redaction_state"] != "not_applicable"
            or value["redacted_body_state"] != "not_applicable"
            or value["redacted_body"] is not None
            or value["redacted_body_hash"] is not None
        ):
            raise _upstream_inconsistent()
    else:
        raise _upstream_inconsistent()

    hash_payload = {key: deepcopy(value[key]) for key in _UPSTREAM_HASH_KEYS}
    try:
        computed = _stable_hash(hash_payload)
    except (TypeError, ValueError):
        raise _upstream_inconsistent() from None
    if computed != value["redaction_result_hash"]:
        raise _upstream_inconsistent()

    return value


def _path_subject(upstream: Mapping[str, object]) -> tuple[str, str | None]:
    target_type = upstream["target_type"]
    source_identity = upstream["source_identity"]
    if not isinstance(source_identity, Mapping):
        raise _upstream_inconsistent()

    if target_type == "git_file_fact":
        path = source_identity.get("path")
        if type(path) is not str or not path:
            raise _upstream_inconsistent()
        payload = {"path_namespace": PATH_NAMESPACE, "path": path}
        return "available", _stable_hash(payload)

    if target_type in {"profile", "prd_block"}:
        if "path" in source_identity:
            raise _upstream_inconsistent()
        return "not_applicable", None

    raise _unsupported_target()


def _build_result(
    upstream: Mapping[str, object], *, path_subject_state: str, path_identity_hash: str | None
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "redaction_result_hash": upstream["redaction_result_hash"],
        "model_call_id": upstream["model_call_id"],
        "call_identity_hash": upstream["call_identity_hash"],
        "snapshot_id": upstream["snapshot_id"],
        "snapshot_hash": upstream["snapshot_hash"],
        "project_id": upstream["project_id"],
        "candidate_set_hash": upstream["candidate_set_hash"],
        "target": upstream["target"],
        "target_type": upstream["target_type"],
        "source_identity": deepcopy(dict(upstream["source_identity"])),
        "path_subject_state": path_subject_state,
        "path_identity_hash": path_identity_hash,
        "sensitive_path_policy_id": POLICY_ID,
        "sensitive_path_policy_hash": SENSITIVE_PATH_POLICY_HASH,
        "policy_evaluation_state": POLICY_EVALUATION_STATE,
        "model_send_state": MODEL_SEND_STATE,
        "unsupported_context_sources": list(upstream["unsupported_context_sources"]),
    }
    hash_payload = {key: deepcopy(result[key]) for key in _RESULT_HASH_KEYS}
    result["sensitive_path_policy_identity_hash"] = _stable_hash(hash_payload)
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("sensitive_path_policy_identity_result_v1 key construction drift")
    return result


def build_sensitive_path_policy_identity(
    *, model_call_id: int, budget_record: Mapping[str, object], target: str
) -> dict[str, object]:
    """Bind one formal redaction result to the frozen policy identity; never evaluate or admit."""
    upstream = _validate_upstream(
        build_context_redaction_result(
            model_call_id=model_call_id,
            budget_record=budget_record,
            target=target,
        ),
        model_call_id=model_call_id,
        target=target,
    )
    _validate_internal_policy()
    path_subject_state, path_identity_hash = _path_subject(upstream)
    return _build_result(
        upstream,
        path_subject_state=path_subject_state,
        path_identity_hash=path_identity_hash,
    )
