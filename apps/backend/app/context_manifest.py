"""Context Manifest Core V1: deterministic pre-redaction project-profile selection envelope."""

from __future__ import annotations

from app.project_profile_v2 import profile_planned_modules

from collections.abc import Mapping
from fnmatch import fnmatchcase
import hashlib
import json

from fastapi import HTTPException

from app.context_candidate_runtime import build_context_candidate_set
from app.model_budget_profiles import build_model_budget_profile
from app.model_call_ledger import get_model_call
from app.project_profiles import _path_pattern_error


SCHEMA_VERSION = "context_manifest_core_v1"
MANIFEST_STAGE = "pre_redaction_selection_core"
SELECTION_POLICY_VERSION = "context_manifest_core_preserve_all_v1"
PROFILE_EXCLUSION_SELECTION_POLICY_VERSION = "context_manifest_profile_exclusions_v1"
REDACTION_STATE = "pending"
TOKEN_COUNT_STATE = "not_counted"
BUDGET_FIT_STATE = "not_evaluated"
FRAMING_STATE = "not_defined"
MODEL_SEND_STATE = "not_admitted"
FINAL_MANIFEST_STATE = "not_final"

_LEDGER_IDENTITY_FIELDS = (
    "model_call_id",
    "project_id",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
    "task_type",
    "provider",
    "model_id",
    "model_version",
    "call_identity_hash",
)
_BUDGET_OUTPUT_FIELDS = (
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
_BUDGET_LEDGER_FIELDS = (
    "model_call_id",
    "call_identity_hash",
    "task_type",
    "provider",
    "model_id",
    "model_version",
)
_CANDIDATE_TOP_LEVEL_FIELDS = (
    "schema_version",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "profile",
    "range",
    "items",
    "unsupported_context_sources",
    "candidate_set_hash",
)
_CANDIDATE_LEDGER_FIELDS = (
    "project_id",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
)
_PROFILE_FIELDS = (
    "profile_id",
    "profile_content_hash",
    "source_prd_id",
    "resolved_content_redaction_state",
    "model_send_state",
)
_RANGE_FIELDS = (
    "git_snapshot_id",
    "branch",
    "from_commit",
    "to_commit",
    "commits",
    "commit_count",
    "git_facts_hash",
)
_RAW_ITEM_FIELDS = frozenset({"content", "diff_text", "text", "table_rows"})


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT",
        "Context Manifest Core 所消费的正式上游身份发生不可解释的不一致。",
    )


def _candidate_drift() -> HTTPException:
    return _error(
        "CONTEXT_MANIFEST_CANDIDATE_DRIFT",
        "重新闭合的 Context Candidate Set 与 Model Call Ledger 冻结身份不一致。",
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_fields(value: object, fields: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(field not in value for field in fields):
        raise _upstream_inconsistent()
    return value


def _validate_ledger_shape(ledger: object) -> Mapping[str, object]:
    return _require_fields(ledger, _LEDGER_IDENTITY_FIELDS)


def _validate_budget_shape(budget: object, ledger: Mapping[str, object]) -> dict[str, object]:
    mapping = _require_fields(budget, _BUDGET_OUTPUT_FIELDS)
    if set(mapping.keys()) != set(_BUDGET_OUTPUT_FIELDS):
        raise _upstream_inconsistent()
    if mapping["schema_version"] != "model_budget_profile_v1":
        raise _upstream_inconsistent()
    if any(mapping[field] != ledger[field] for field in _BUDGET_LEDGER_FIELDS):
        raise _upstream_inconsistent()
    if (
        mapping["budget_authority_state"] != "assertion_only"
        or mapping["token_count_state"] != "not_counted"
        or mapping["model_send_state"] != "not_admitted"
    ):
        raise _upstream_inconsistent()
    return {field: mapping[field] for field in _BUDGET_OUTPUT_FIELDS}


def _validate_candidate_shape(candidate: object) -> Mapping[str, object]:
    mapping = _require_fields(candidate, _CANDIDATE_TOP_LEVEL_FIELDS)
    if mapping["schema_version"] != "context_candidate_set_v1":
        raise _upstream_inconsistent()
    if not isinstance(mapping["profile"], Mapping):
        raise _upstream_inconsistent()
    if not isinstance(mapping["range"], Mapping):
        raise _upstream_inconsistent()
    if not isinstance(mapping["items"], list):
        raise _upstream_inconsistent()
    unsupported = mapping["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()
    return mapping


def _project_profile(candidate: Mapping[str, object]) -> dict[str, object]:
    profile = _require_fields(candidate["profile"], _PROFILE_FIELDS)
    if (
        profile["resolved_content_redaction_state"] != "pending"
        or profile["model_send_state"] != "not_admitted"
    ):
        raise _upstream_inconsistent()
    result = {field: profile[field] for field in _PROFILE_FIELDS}
    if "progress_context_hash" in profile:
        result["progress_context_hash"] = profile["progress_context_hash"]
    result["selection_state"] = "preserved_pending_processing"
    return result


def _project_range(candidate: Mapping[str, object]) -> dict[str, object]:
    range_value = _require_fields(candidate["range"], _RANGE_FIELDS)
    commits = range_value["commits"]
    if not isinstance(commits, list):
        raise _upstream_inconsistent()
    result = {field: range_value[field] for field in _RANGE_FIELDS}
    result["commits"] = list(commits)
    result["selection_state"] = "preserved"
    return result


def _glob_pattern_error(pattern: str) -> bool:
    """Reject damaged segment-globs instead of silently treating them as literals.

    `**` is recursive only when it occupies a whole path segment. `*`, `?`, and bracket classes
    are single-segment wildcards. Backslash/path traversal safety remains owned by
    `_path_pattern_error`; this check closes grammar shapes that fnmatch would otherwise accept as
    harmless literals and thereby fail open for a hard-deny rule.
    """

    normalized = pattern.rstrip("/")
    if not normalized or "//" in normalized:
        return True

    for segment in normalized.split("/"):
        index = 0
        while index < len(segment):
            if segment[index] != "[":
                index += 1
                continue

            cursor = index + 1
            if cursor < len(segment) and segment[cursor] in {"!", "^"}:
                cursor += 1
            if cursor < len(segment) and segment[cursor] == "]":
                cursor += 1
            closing = segment.find("]", cursor)
            if closing < 0:
                return True
            index = closing + 1

    return False


def _validated_exclusion_pattern(value: object) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or _path_pattern_error(value) is not None
        or _glob_pattern_error(value)
    ):
        raise _upstream_inconsistent()
    return value


def _profile_global_exclusion_rules(candidate: Mapping[str, object]) -> list[dict[str, object]]:
    """Return only profile-wide hard deny rules.

    R2 defines top-level exclude_patterns as paths that must not be analysed or sent. Per-module
    exclusions are scoped to an individual confirmed function/module and must not be promoted to a
    project-wide deny until a module-aware context projection exists.
    """
    profile = candidate["profile"]
    if not isinstance(profile, Mapping):
        raise _upstream_inconsistent()
    content = profile.get("content")
    if not isinstance(content, Mapping):
        raise _upstream_inconsistent()
    top_level = content.get("exclude_patterns")
    modules = profile_planned_modules(content)
    if not isinstance(top_level, list) or not isinstance(modules, list):
        raise _upstream_inconsistent()

    for module in modules:
        if not isinstance(module, Mapping):
            raise _upstream_inconsistent()
        client_id = module.get("client_id")
        exclusions = module.get("exclusions")
        if type(client_id) is not str or not client_id or not isinstance(exclusions, list):
            raise _upstream_inconsistent()

    rules: list[dict[str, object]] = []
    for raw_pattern in top_level:
        pattern = _validated_exclusion_pattern(raw_pattern)
        rules.append({"scope": "profile", "pattern": pattern})
    return rules


def _glob_matches(path: str, pattern: str) -> bool:
    """Match one repository-relative path with explicit segment-aware recursive semantics."""

    path_parts = tuple(path.split("/"))
    pattern_parts = tuple(pattern.split("/"))
    memo: dict[tuple[int, int], bool] = {}

    def match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        cached = memo.get(key)
        if cached is not None:
            return cached

        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and match(path_index + 1, pattern_index + 1)
            )

        memo[key] = result
        return result

    return match(0, 0)


def _path_matches_exclusion(path: object, pattern: str) -> bool:
    if type(path) is not str or not path or "\\" in path or path.startswith("/"):
        raise _upstream_inconsistent()
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _upstream_inconsistent()

    normalized_pattern = pattern.rstrip("/")
    if not normalized_pattern or _glob_pattern_error(pattern):
        raise _upstream_inconsistent()

    has_glob = any(token in normalized_pattern for token in ("*", "?", "["))
    if not has_glob:
        return path == normalized_pattern or path.startswith(normalized_pattern + "/")

    return _glob_matches(path, normalized_pattern)


def _project_items(
    candidate: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    rules = _profile_global_exclusion_rules(candidate)
    projected: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for item in candidate["items"]:
        if not isinstance(item, Mapping):
            raise _upstream_inconsistent()
        if _RAW_ITEM_FIELDS.intersection(item.keys()) or "selection_state" in item:
            raise _upstream_inconsistent()
        if item.get("model_send_state") != "not_admitted":
            raise _upstream_inconsistent()

        matched_rules: list[dict[str, object]] = []
        if item.get("type") == "git_file_fact":
            path = item.get("path")
            for rule in rules:
                pattern = rule["pattern"]
                if type(pattern) is not str:
                    raise _upstream_inconsistent()
                if _path_matches_exclusion(path, pattern):
                    matched_rules.append(dict(rule))

        value = dict(item)
        if matched_rules:
            value["selection_state"] = "excluded_by_project_profile"
            value["matched_exclusion_rules"] = matched_rules
            excluded.append(value)
        else:
            value["selection_state"] = "preserved_pending_processing"
            projected.append(value)

    policy = (
        PROFILE_EXCLUSION_SELECTION_POLICY_VERSION if rules else SELECTION_POLICY_VERSION
    )
    return projected, excluded, policy


def _hash_payload(core: Mapping[str, object]) -> dict[str, object]:
    return dict(core)


def build_context_manifest_core(
    *,
    model_call_id: int,
    budget_record: Mapping[str, object],
) -> dict[str, object]:
    """Build a deterministic, non-final Context Manifest Core bound to formal upstream closures.

    This function intentionally does not redact content, count tokens, decide budget fit, persist a
    manifest, create a provider request, or grant model-send admission.
    """
    ledger = _validate_ledger_shape(get_model_call(model_call_id))
    budget = _validate_budget_shape(
        build_model_budget_profile(
            model_call_id=model_call_id,
            budget_record=budget_record,
        ),
        ledger,
    )
    candidate = _validate_candidate_shape(build_context_candidate_set(ledger["snapshot_id"]))
    if any(candidate[field] != ledger[field] for field in _CANDIDATE_LEDGER_FIELDS):
        raise _candidate_drift()

    items, excluded, selection_policy_version = _project_items(candidate)
    core: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_stage": MANIFEST_STAGE,
        "model_call_id": ledger["model_call_id"],
        "call_identity_hash": ledger["call_identity_hash"],
        "task_type": ledger["task_type"],
        "snapshot_id": ledger["snapshot_id"],
        "snapshot_hash": ledger["snapshot_hash"],
        "project_id": ledger["project_id"],
        "candidate_set_hash": ledger["candidate_set_hash"],
        "budget_profile": budget,
        "selection_policy_version": selection_policy_version,
        "profile": _project_profile(candidate),
        "range": _project_range(candidate),
        "items": items,
        "excluded": excluded,
        "compression": [],
        "unsupported_context_sources": list(candidate["unsupported_context_sources"]),
        "estimated_tokens": None,
        "exact_tokens": None,
        "redaction_state": REDACTION_STATE,
        "token_count_state": TOKEN_COUNT_STATE,
        "budget_fit_state": BUDGET_FIT_STATE,
        "framing_state": FRAMING_STATE,
        "model_send_state": MODEL_SEND_STATE,
        "final_manifest_state": FINAL_MANIFEST_STATE,
    }
    core["manifest_core_hash"] = _stable_hash(_hash_payload(core))
    return core
