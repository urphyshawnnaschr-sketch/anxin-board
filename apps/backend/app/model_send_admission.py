"""Model-Send Admission V1: local content gate only; never provider/network authorization."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app.context_redaction_runtime import build_context_redaction_result

from app.context_sensitive_path_policy import (
    POLICY_DESCRIPTOR,
    SENSITIVE_PATH_POLICY_HASH,
    build_sensitive_path_policy_identity,
)


SCHEMA_VERSION = "model_send_admission_v1"
_EXPECTED_POLICY_ID = "builtin_sensitive_path_v1"
_EXPECTED_MATCH_SEMANTICS_ID = "ascii-ci-git-path-exact-basename-suffix-v1"
_PATH_NAMESPACE = "historical_git_repository_relative_path"
_UPSTREAM_SCHEMA_VERSION = "sensitive_path_policy_identity_result_v1"
_UPSTREAM_POLICY_EVALUATION_STATE = "not_evaluated"
_UPSTREAM_MODEL_SEND_STATE = "not_admitted"

_EXPECTED_RULE_ROWS = (
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
_EXPECTED_RULE_ORDER = tuple(row[0] for row in _EXPECTED_RULE_ROWS)
_EXPECTED_POLICY_DESCRIPTOR = {
    "policy_schema_version": "sensitive_path_policy_v1",
    "policy_id": _EXPECTED_POLICY_ID,
    "policy_version": "1.0",
    "policy_origin": "governance_frozen_builtin",
    "path_namespace": _PATH_NAMESPACE,
    "path_subject_identity_version": "historical-git-path-identity-1.0",
    "match_semantics_id": _EXPECTED_MATCH_SEMANTICS_ID,
    "rule_schema_version": "sensitive-path-rule-v1",
    "rules": [
        {"rule_id": rule_id, "rule_type": rule_type, "value": value}
        for rule_id, rule_type, value in _EXPECTED_RULE_ROWS
    ],
    "rule_order": list(_EXPECTED_RULE_ORDER),
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

_UPSTREAM_KEYS = (
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
_RESULT_KEYS = (
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
_RESULT_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "model_send_admission_hash")
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


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "MODEL_SEND_ADMISSION_UPSTREAM_INCONSISTENT",
        "formal Sensitive-Path Identity 的固定形状、状态或 identity 无法闭合。",
    )


def _internal_policy_inconsistent() -> HTTPException:
    return _error(
        "MODEL_SEND_ADMISSION_INTERNAL_POLICY_INCONSISTENT",
        "内置 Sensitive-Path policy 与正式冻结 catalog 或 identity 不一致。",
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _validate_internal_policy() -> None:
    if (
        tuple(POLICY_DESCRIPTOR) != _POLICY_DESCRIPTOR_KEYS
        or POLICY_DESCRIPTOR != _EXPECTED_POLICY_DESCRIPTOR
        or SENSITIVE_PATH_POLICY_HASH != _stable_hash(_EXPECTED_POLICY_DESCRIPTOR)
    ):
        raise _internal_policy_inconsistent()

    rules = POLICY_DESCRIPTOR.get("rules")
    order = POLICY_DESCRIPTOR.get("rule_order")
    if not isinstance(rules, list) or not isinstance(order, list) or len(rules) != 26:
        raise _internal_policy_inconsistent()
    if order != list(_EXPECTED_RULE_ORDER):
        raise _internal_policy_inconsistent()

    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or tuple(rule) != _RULE_KEYS:
            raise _internal_policy_inconsistent()
        expected_id, expected_type, expected_value = _EXPECTED_RULE_ROWS[index]
        if (
            rule["rule_id"] != expected_id
            or rule["rule_type"] != expected_type
            or rule["value"] != expected_value
            or rule["rule_type"] not in _ALLOWED_RULE_TYPES
        ):
            raise _internal_policy_inconsistent()


def _validate_upstream(
    value: object, *, model_call_id: int, target: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _UPSTREAM_KEYS:
        raise _upstream_inconsistent()

    if (
        value["schema_version"] != _UPSTREAM_SCHEMA_VERSION
        or type(value["model_call_id"]) is not int
        or value["model_call_id"] <= 0
        or value["model_call_id"] != model_call_id
        or type(value["snapshot_id"]) is not int
        or value["snapshot_id"] <= 0
        or type(value["project_id"]) is not int
        or value["project_id"] <= 0
        or type(value["target"]) is not str
        or not value["target"]
        or value["target"] != target
        or type(value["target_type"]) is not str
        or not isinstance(value["source_identity"], Mapping)
        or value["sensitive_path_policy_id"] != _EXPECTED_POLICY_ID
        or value["sensitive_path_policy_hash"] != SENSITIVE_PATH_POLICY_HASH
        or value["policy_evaluation_state"] != _UPSTREAM_POLICY_EVALUATION_STATE
        or value["model_send_state"] != _UPSTREAM_MODEL_SEND_STATE
    ):
        raise _upstream_inconsistent()

    for field in (
        "sensitive_path_policy_identity_hash",
        "redaction_result_hash",
        "call_identity_hash",
        "snapshot_hash",
        "candidate_set_hash",
        "sensitive_path_policy_hash",
    ):
        if not _is_sha256(value[field]):
            raise _upstream_inconsistent()

    unsupported = value["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()

    identity_payload = {
        key: deepcopy(value[key])
        for key in _UPSTREAM_KEYS
        if key != "sensitive_path_policy_identity_hash"
    }
    try:
        if _stable_hash(identity_payload) != value["sensitive_path_policy_identity_hash"]:
            raise _upstream_inconsistent()
    except (TypeError, ValueError):
        raise _upstream_inconsistent() from None

    target_type = value["target_type"]
    source_identity = value["source_identity"]
    if target_type == "git_file_fact":
        path = source_identity.get("path")
        is_binary = source_identity.get("is_binary")
        if (
            value["path_subject_state"] != "available"
            or type(path) is not str
            or not path
            or type(is_binary) is not bool
            or not _is_sha256(value["path_identity_hash"])
        ):
            raise _upstream_inconsistent()
        expected_path_hash = _stable_hash({"path_namespace": _PATH_NAMESPACE, "path": path})
        if value["path_identity_hash"] != expected_path_hash:
            raise _upstream_inconsistent()
    elif target_type in {"profile", "prd_block"}:
        if (
            value["path_subject_state"] != "not_applicable"
            or value["path_identity_hash"] is not None
            or "path" in source_identity
        ):
            raise _upstream_inconsistent()
    else:
        raise _upstream_inconsistent()

    return value


def _ascii_ci(value: str) -> str:
    return "".join(chr(ord(char) + 32) if "A" <= char <= "Z" else char for char in value)


def _matches_rule(path: str, rule: Mapping[str, object]) -> bool:
    rule_type = rule["rule_type"]
    rule_value = rule["value"]
    if type(rule_type) is not str or type(rule_value) is not str:
        raise _internal_policy_inconsistent()

    expected = _ascii_ci(rule_value)
    if rule_type == "exact_relative_path":
        return _ascii_ci(path) == expected

    basename = _ascii_ci(path.rsplit("/", 1)[-1])
    if rule_type == "exact_basename":
        return basename == expected
    if rule_type == "basename_suffix":
        return basename.endswith(expected)
    raise _internal_policy_inconsistent()


def _first_match(path: str) -> str | None:
    rules = POLICY_DESCRIPTOR["rules"]
    order = POLICY_DESCRIPTOR["rule_order"]
    if not isinstance(rules, list) or not isinstance(order, list):
        raise _internal_policy_inconsistent()
    by_id = {rule["rule_id"]: rule for rule in rules if isinstance(rule, Mapping)}
    if set(by_id) != set(order):
        raise _internal_policy_inconsistent()
    for rule_id in order:
        rule = by_id.get(rule_id)
        if rule is None:
            raise _internal_policy_inconsistent()
        if _matches_rule(path, rule):
            return rule_id
    return None


def _decision(upstream: Mapping[str, object]) -> tuple[str, str, str | None, str, str]:
    target_type = upstream["target_type"]
    source_identity = upstream["source_identity"]
    if not isinstance(source_identity, Mapping):
        raise _upstream_inconsistent()

    if target_type in {"profile", "prd_block"}:
        return (
            "not_applicable",
            "not_applicable",
            None,
            "admitted",
            "content_policy_passed",
        )

    if target_type != "git_file_fact":
        raise _upstream_inconsistent()

    path = source_identity.get("path")
    is_binary = source_identity.get("is_binary")
    if type(path) is not str or not path or type(is_binary) is not bool:
        raise _upstream_inconsistent()

    matched_rule_id = _first_match(path)
    if matched_rule_id is not None:
        return (
            "evaluated",
            "hard_deny",
            matched_rule_id,
            "denied",
            "sensitive_path_hard_deny",
        )

    if is_binary:
        return (
            "evaluated",
            "not_blocked",
            None,
            "denied",
            "binary_metadata_only_no_sendable_body",
        )

    return (
        "evaluated",
        "not_blocked",
        None,
        "admitted",
        "content_policy_passed",
    )


def _build_result(upstream: Mapping[str, object]) -> dict[str, object]:
    (
        policy_evaluation_state,
        sensitive_path_decision,
        matched_rule_id,
        model_send_state,
        admission_reason,
    ) = _decision(upstream)

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sensitive_path_policy_identity_hash": upstream["sensitive_path_policy_identity_hash"],
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
        "path_subject_state": upstream["path_subject_state"],
        "path_identity_hash": upstream["path_identity_hash"],
        "sensitive_path_policy_id": upstream["sensitive_path_policy_id"],
        "sensitive_path_policy_hash": upstream["sensitive_path_policy_hash"],
        "policy_evaluation_state": policy_evaluation_state,
        "sensitive_path_decision": sensitive_path_decision,
        "matched_rule_id": matched_rule_id,
        "model_send_state": model_send_state,
        "admission_reason": admission_reason,
        "unsupported_context_sources": list(upstream["unsupported_context_sources"]),
    }
    hash_payload = {key: deepcopy(result[key]) for key in _RESULT_HASH_KEYS}
    result["model_send_admission_hash"] = _stable_hash(hash_payload)
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("model_send_admission_v1 key construction drift")
    return result


def _has_analyzable_diff_body(text: str) -> bool:
    """Ignore framing and redaction-only lines when deciding source sufficiency."""
    for line in text.splitlines():
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@", "\\ No newline")):
            continue
        if not line or line[0] not in {"+", "-", " "}:
            continue
        content = line[1:].strip()
        if re.fullmatch(r"-----(?:BEGIN|END) [A-Z ]*PRIVATE KEY-----", content):
            continue
        if "[REDACTED:" in content:
            remainder = re.sub(r"\[REDACTED:[^\]]+\]", "", content).strip()
            # A credential assignment with its entire value removed is not source evidence.
            if re.fullmatch(r"[\w.\-\s\"']*[:=]?[\s\"',;{}()\[\]]*", remainder):
                continue
            content = remainder
        if re.search(r"[A-Za-z0-9_\u0080-\uffff]", content):
            return True
    return False


def build_model_send_admission(
    *, model_call_id: int, budget_record: Mapping[str, object], target: str
) -> dict[str, object]:
    """Evaluate one formal target for local request preparation; never authorize network send."""
    upstream = _validate_upstream(
        build_sensitive_path_policy_identity(
            model_call_id=model_call_id,
            budget_record=budget_record,
            target=target,
        ),
        model_call_id=model_call_id,
        target=target,
    )
    _validate_internal_policy()
    result = _build_result(upstream)
    if result["model_send_state"] == "denied":
        return result
    # Bind quarantine to the same formal redaction identity, never a raw exception.
    redaction = build_context_redaction_result(
        model_call_id=model_call_id, budget_record=budget_record, target=target,
    )
    if redaction.get("redaction_result_hash") != upstream["redaction_result_hash"]:
        raise _upstream_inconsistent()
    no_source = (
        redaction.get("raw_body_kind") == "text_unified_diff"
        and not _has_analyzable_diff_body(str(redaction.get("redacted_body", "")))
    )
    if redaction.get("redaction_state") == "quarantined" or no_source:
        result["model_send_state"] = "denied"
        result["admission_reason"] = (
            "credential_boundary_quarantined" if redaction.get("redaction_state") == "quarantined"
            else "no_analyzable_source_after_redaction"
        )
        result["model_send_admission_hash"] = _stable_hash(
            {key: deepcopy(result[key]) for key in _RESULT_HASH_KEYS}
        )
    return result
