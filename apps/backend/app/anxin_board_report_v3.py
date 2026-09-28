"""ApprovalSnapshot-bound formal Anxin Board Report V3.

V3 keeps the current V2 visual/report semantics but removes caller authority over
module identity and formal provenance. Module id/name/order come from the exact bound
ProjectProfile. A valid exact-name daily AI stage is authoritative; when today's AI
payload cannot safely resolve one module, a confirmed ProjectProfile V2 implementation
mapping may supply only a conservative lower-bound stage. Formal provenance comes only
from the immutable ApprovalSnapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from app.anxin_board_report import _stable_hash
from app.anxin_board_report_v2 import (
    SCHEMA_VERSION as V2_SCHEMA_VERSION,
    build_anxin_board_report_v2,
    validate_anxin_board_report_v2,
    _overall_message,
)
from app.client_stage_summary import build_client_stage_summary
from app.project_profile_v2 import profile_planned_modules


SCHEMA_VERSION = "anxin_board_report_v3"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# A repository reconciliation baseline proves code implementation evidence only.  It
# must never be promoted directly to the formal "已完成" stage, which also represents
# the agreed development/checking boundary in client-facing output.
_BASELINE_IMPLEMENTATION_STAGE = {
    "partial": "开发中",
    "implemented": "等待测试",
    "unknown": "暂时无法确认",
    "not_started": "暂时无法确认",
}

_BASE_KEYS = (
    "title",
    "motto",
    "project_name",
    "report_date",
    "profile_id",
    "profile_version_no",
    "profile_content_hash",
    "source_prd_id",
    "module_count",
    "completed_module_count",
    "active_module_count",
    "unknown_module_count",
    "overall_message",
    "modules",
    "daily_change",
    "manager_supplement",
)
_PROVENANCE_KEYS = (
    "approval_snapshot_id",
    "approval_snapshot_hash",
    "report_version_id",
    "report_version_no",
    "report_content_hash",
    "validation_result_id",
    "validation_result_hash",
    "evidence_snapshot_id",
    "evidence_snapshot_hash",
    "git_snapshot_id",
    "git_facts_hash",
    "git_branch",
    "git_from_commit",
    "git_to_commit",
    "prd_id",
    "prd_source_hash",
    "prd_parsed_hash",
    "prd_structured_hash",
    "prd_document_fingerprint",
    "model_execution_result_id",
    "execution_result_hash",
    "model_call_id",
    "call_identity_hash",
    "provider",
    "model_id",
    "model_version",
    "actual_model",
    "provider_runtime_fingerprint",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_hash",
    "authorization_hash",
    "supplement_version_id",
    "supplement_content_hash",
    "supplement_provided_by",
    "supplement_provided_at",
    "supplement_provided_timezone",
    "supplement_source_type",
    "confirmed_by",
    "confirmed_at",
    "confirmed_timezone",
    "confirmed_utc_offset_minutes",
    "human_acknowledged",
)
_RESULT_KEYS = (
    "schema_version",
    *_BASE_KEYS,
    *_PROVENANCE_KEYS,
    "anxin_board_report_hash",
)
_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "anxin_board_report_hash")


class AnxinBoardReportV3Error(ValueError):
    code = "ANXIN_BOARD_REPORT_V3_INVALID"


def _fail(message: str = AnxinBoardReportV3Error.code) -> AnxinBoardReportV3Error:
    return AnxinBoardReportV3Error(message)


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise _fail()
    return value


def _approval_report_date(approval: Mapping[str, object]) -> str:
    confirmed_at = approval.get("confirmed_at")
    offset = approval.get("confirmed_utc_offset_minutes")
    if type(confirmed_at) is not str or type(offset) is not int or offset < -840 or offset > 840:
        raise _fail()
    try:
        instant = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail() from exc
    if instant.tzinfo is None:
        raise _fail()
    return instant.astimezone(timezone(timedelta(minutes=offset))).date().isoformat()


def _approved_report_payload(ai_raw: Mapping[str, object]) -> Mapping[str, object]:
    content = ai_raw.get("content")
    if not isinstance(content, Mapping):
        raise _fail()
    if ai_raw.get("task_type") == "daily_report_regenerate":
        content = content.get("new_report")
    if not isinstance(content, Mapping):
        raise _fail()
    return content


def _baseline_stages(content: Mapping[str, object]) -> dict[str, str]:
    """Return safe per-module fallback stages from a V2 reconciliation baseline.

    The normal V2 authority validator later re-closes the whole Profile and its content
    hash.  This helper remains conservative on its own: malformed/duplicate mappings are
    ignored rather than guessed, and no baseline status can produce ``已完成``.
    """

    if content.get("schema_version") != "project_profile_v2":
        return {}
    raw = content.get("implementation_mappings")
    if type(raw) is not list:
        return {}
    result: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    for mapping in raw:
        if not isinstance(mapping, Mapping):
            continue
        module_id = mapping.get("planned_module_id")
        status = mapping.get("status")
        if type(module_id) is not str or not module_id or status not in _BASELINE_IMPLEMENTATION_STAGE:
            continue
        if module_id in result:
            duplicate_ids.add(module_id)
            continue
        result[module_id] = _BASELINE_IMPLEMENTATION_STAGE[str(status)]
    for module_id in duplicate_ids:
        result.pop(module_id, None)
    return result


def _module_summaries_from_approved_report(
    *, profile: Mapping[str, object], ai_raw: Mapping[str, object]
) -> list[dict[str, object]]:
    content = profile.get("content")
    modules = profile_planned_modules(content) if isinstance(content, Mapping) else None
    if type(modules) is not list or not modules:
        raise _fail("ProjectProfile modules 无法用于 V3 正式物化。")

    module_identity: list[tuple[str, str]] = []
    seen_profile_names: set[str] = set()
    for module in modules:
        if not isinstance(module, Mapping):
            raise _fail()
        client_id = module.get("client_id")
        name = module.get("name")
        if (
            type(client_id) is not str
            or not client_id
            or type(name) is not str
            or not name
            or name in seen_profile_names
        ):
            raise _fail("ProjectProfile module identity/name 不唯一。")
        seen_profile_names.add(name)
        module_identity.append((client_id, name))

    baseline_by_id = _baseline_stages(content) if isinstance(content, Mapping) else {}
    payload = _approved_report_payload(ai_raw)
    feature_progress = payload.get("feature_progress")
    items = feature_progress if type(feature_progress) is list else []
    by_name: dict[str, list[Mapping[str, object]]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = item.get("feature")
        if type(name) is str and name in seen_profile_names:
            by_name.setdefault(name, []).append(item)

    summaries: list[dict[str, object]] = []
    for client_id, name in module_identity:
        matches = by_name.get(name, [])
        if len(matches) == 1:
            item = matches[0]
            stage = item.get("stage")
            evidence_ok = True
            if stage == "已完成":
                evidence_ids = item.get("evidence_ids")
                evidence_ok = type(evidence_ids) is list and bool(evidence_ids)
            if type(stage) is str and evidence_ok:
                try:
                    summaries.append(build_client_stage_summary(module_name=name, stage=stage))
                    continue
                except (TypeError, ValueError):
                    pass

        # Missing, duplicate, renamed, invalid-stage, or unsupported-completed daily AI
        # output cannot become a formal assertion.  If Human-confirmed V2 reconciliation
        # already proves a lower-bound implementation state, carry only that conservative
        # state forward; otherwise stay explicitly unknown.
        fallback_stage = baseline_by_id.get(client_id, "暂时无法确认")
        summaries.append(build_client_stage_summary(module_name=name, stage=fallback_stage))
    return summaries


def _approval_provenance(approval: Mapping[str, object]) -> dict[str, object]:
    result = {key: approval.get(key) for key in _PROVENANCE_KEYS}
    for field in (
        "approval_snapshot_id",
        "report_version_id",
        "report_version_no",
        "validation_result_id",
        "evidence_snapshot_id",
        "git_snapshot_id",
        "prd_id",
        "model_execution_result_id",
        "model_call_id",
    ):
        _positive_int(result[field])
    for field in (
        "approval_snapshot_hash",
        "report_content_hash",
        "validation_result_hash",
        "evidence_snapshot_hash",
        "git_facts_hash",
        "prd_source_hash",
        "prd_parsed_hash",
        "prd_structured_hash",
        "prd_document_fingerprint",
        "execution_result_hash",
        "call_identity_hash",
        "qualification_hash",
        "authorization_hash",
    ):
        if not _is_hash(result[field]):
            raise _fail()
    for field in (
        "git_branch",
        "git_from_commit",
        "git_to_commit",
        "provider",
        "model_id",
        "model_version",
        "actual_model",
        "provider_runtime_fingerprint",
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
        "confirmed_by",
        "confirmed_at",
        "confirmed_timezone",
    ):
        if type(result[field]) is not str or not result[field]:
            raise _fail()
    if type(result["confirmed_utc_offset_minutes"]) is not int:
        raise _fail()
    if result["human_acknowledged"] is not True:
        raise _fail()
    if result["supplement_version_id"] is None:
        for field in (
            "supplement_content_hash",
            "supplement_provided_by",
            "supplement_provided_at",
            "supplement_provided_timezone",
            "supplement_source_type",
        ):
            if result[field] is not None:
                raise _fail()
    else:
        _positive_int(result["supplement_version_id"])
        if not _is_hash(result["supplement_content_hash"]):
            raise _fail()
        for field in (
            "supplement_provided_by",
            "supplement_provided_at",
            "supplement_provided_timezone",
            "supplement_source_type",
        ):
            if type(result[field]) is not str or not result[field]:
                raise _fail()
    return result


def build_anxin_board_report_v3(
    *,
    project_name: str,
    profile: Mapping[str, object],
    ai_raw: Mapping[str, object],
    daily_change: Mapping[str, object],
    manager_supplement: str,
    approval_snapshot: Mapping[str, object],
    module_summaries: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build V3 from one exact approved candidate; no caller-supplied stage authority."""
    provenance = _approval_provenance(approval_snapshot)
    if (
        profile.get("id") != approval_snapshot.get("profile_id")
        or profile.get("version_no") != approval_snapshot.get("profile_version_no")
        or profile.get("content_hash") != approval_snapshot.get("profile_content_hash")
        or profile.get("source_prd_id") != approval_snapshot.get("prd_id")
    ):
        raise _fail("ProjectProfile 与 ApprovalSnapshot 不一致。")

    cumulative = module_summaries is not None
    if module_summaries is None:
        module_summaries = _module_summaries_from_approved_report(profile=profile, ai_raw=ai_raw)
    v2 = build_anxin_board_report_v2(
        project_name=project_name,
        report_date=_approval_report_date(approval_snapshot),
        profile=profile,
        module_summaries=module_summaries,
        daily_change=daily_change,
        manager_supplement=manager_supplement,
    )
    if cumulative:
        v2["overall_message"] = _overall_message(
            total=v2["module_count"], completed=v2["completed_module_count"],
            active=v2["active_module_count"], unknown=v2["unknown_module_count"], development_only=True,
        )
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        **{key: deepcopy(v2[key]) for key in _BASE_KEYS},
        **provenance,
    }
    result["anxin_board_report_hash"] = _stable_hash(
        {key: deepcopy(result[key]) for key in _HASH_KEYS}
    )
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("anxin_board_report_v3 key construction drift")
    return result


def validate_anxin_board_report_v3(
    value: object,
    *,
    profile: Mapping[str, object],
    approval_snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Validate V3 against the exact bound Profile and ApprovalSnapshot."""
    if not isinstance(value, Mapping) or tuple(value) != _RESULT_KEYS:
        raise _fail()
    if value.get("schema_version") != SCHEMA_VERSION or not _is_hash(value.get("anxin_board_report_hash")):
        raise _fail()

    provenance = _approval_provenance(approval_snapshot)
    if any(value.get(key) != expected for key, expected in provenance.items()):
        raise _fail("V3 provenance 与 ApprovalSnapshot 不一致。")
    if (
        value.get("profile_id") != approval_snapshot.get("profile_id")
        or value.get("profile_version_no") != approval_snapshot.get("profile_version_no")
        or value.get("profile_content_hash") != approval_snapshot.get("profile_content_hash")
        or value.get("source_prd_id") != approval_snapshot.get("prd_id")
        or value.get("report_date") != _approval_report_date(approval_snapshot)
    ):
        raise _fail()

    v2_candidate: dict[str, object] = {
        "schema_version": V2_SCHEMA_VERSION,
        **{key: deepcopy(value[key]) for key in _BASE_KEYS},
    }
    v2_hash_payload = {key: deepcopy(v2_candidate[key]) for key in v2_candidate}
    v2_candidate["anxin_board_report_hash"] = _stable_hash(v2_hash_payload)
    try:
        validated_v2 = validate_anxin_board_report_v2(v2_candidate, profile=profile)
    except (TypeError, ValueError) as exc:
        raise _fail() from exc

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        **{key: deepcopy(validated_v2[key]) for key in _BASE_KEYS},
        **provenance,
        "anxin_board_report_hash": value["anxin_board_report_hash"],
    }
    if _stable_hash({key: deepcopy(result[key]) for key in _HASH_KEYS}) != result["anxin_board_report_hash"]:
        raise _fail()
    return result
