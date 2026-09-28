"""Stable types and centralized schemas for deterministic AI output contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Iterable


AI_CONTRACT_SCHEMA_VERSION: Final = "ai-agent-contract/1.0"
UNKNOWN_VALUE: Final = "unknown"

MAX_NESTING_DEPTH: Final = 24
MAX_TRAVERSAL_NODES: Final = 10_000
MAX_ARRAY_LENGTH: Final = 200
MAX_STRING_LENGTH: Final = 12_000
MAX_MAPPING_FIELDS: Final = 64
MAX_SAFE_PATH_KEY_LENGTH: Final = 64
MAX_COST_AMOUNT: Final = 1_000_000

TASK_TYPES: Final = (
    "project_profile_build",
    "daily_report_generate",
    "daily_report_regenerate",
    "report_contradiction_check",
)
STATUSES: Final = ("succeeded", "failed", "unknown")
BILLING_STATUSES: Final = ("known", "unknown")

STAGES: Final = (
    "开发中",
    "等待联调",
    "等待测试",
    "测试中",
    "已完成",
    "暂时无法确认",
)
SOURCE_TYPES: Final = (
    "git_fact",
    "prd_fact",
    "ai_analysis",
    "pm_external_fact",
    "fixed_disclaimer",
)
IMPLEMENTATION_SCOPES: Final = (
    "前端",
    "后端",
    "数据库",
    "接口",
    "测试",
    "配置",
    "跨模块",
    "暂时无法确认",
)
RISK_LEVELS: Final = ("suspected", "needs_client_action")

ENVELOPE_FIELDS: Final = (
    "local_task_id",
    "provider_request_id",
    "task_type",
    "status",
    "schema_version",
    "output_schema_version",
    "actual_model",
    "usage",
    "cost",
    "billing_status",
    "warnings",
    "result",
)


@dataclass(frozen=True, slots=True)
class FieldRule:
    """One required field in an exact object schema."""

    kind: str
    non_empty: bool = False
    min_items: int = 0
    allowed_values: tuple[object, ...] = ()
    constant: object | None = None


@dataclass(frozen=True, slots=True)
class ObjectSchema:
    """Exact immutable schema for one nested result-array item."""

    fields: tuple[tuple[str, FieldRule], ...]
    required_fields: tuple[str, ...]
    evidence_field: str | None = None


def _object_schema(
    fields: tuple[tuple[str, FieldRule], ...], *, evidence_field: str | None = None
) -> ObjectSchema:
    return ObjectSchema(
        fields=fields,
        required_fields=tuple(name for name, _ in fields),
        evidence_field=evidence_field,
    )


_TEXT = FieldRule("string", non_empty=True)
_BOOL = FieldRule("bool")
_CANDIDATE = FieldRule("bool", constant=True)
_EVIDENCE_IDS = FieldRule("string_list", non_empty=True, min_items=1)
_SOURCE_TYPE = FieldRule("string", non_empty=True, allowed_values=SOURCE_TYPES)
_SCOPE = FieldRule("string", non_empty=True, allowed_values=IMPLEMENTATION_SCOPES)
_STAGE = FieldRule("string", non_empty=True, allowed_values=STAGES)
_RISK = FieldRule("string", non_empty=True, allowed_values=RISK_LEVELS)


NESTED_OBJECT_SCHEMAS: Final = MappingProxyType(
    {
        "project_feature": _object_schema(
            (
                ("name", _TEXT),
                ("description", _TEXT),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
                ("candidate", _CANDIDATE),
            ),
            evidence_field="evidence_ids",
        ),
        "module_path_candidate": _object_schema(
            (
                ("path", _TEXT),
                ("description", _TEXT),
                ("implementation_scope", _SCOPE),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
                ("candidate", _CANDIDATE),
            ),
            evidence_field="evidence_ids",
        ),
        "terminology_candidate": _object_schema(
            (
                ("term", _TEXT),
                ("definition", _TEXT),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
                ("candidate", _CANDIDATE),
            ),
            evidence_field="evidence_ids",
        ),
        "rule_candidate": _object_schema(
            (
                ("content", _TEXT),
                ("reason", _TEXT),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
                ("candidate", _CANDIDATE),
            ),
            evidence_field="evidence_ids",
        ),
        "unmapped_area_candidate": _object_schema(
            (
                ("area", _TEXT),
                ("reason", _TEXT),
                ("implementation_scope", _SCOPE),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
                ("candidate", _CANDIDATE),
            ),
            evidence_field="evidence_ids",
        ),
        "feature_progress": _object_schema(
            (
                ("feature", _TEXT),
                ("stage", _STAGE),
                ("source_type", _SOURCE_TYPE),
                ("implementation_scope", _SCOPE),
                ("evidence_ids", _EVIDENCE_IDS),
            ),
            evidence_field="evidence_ids",
        ),
        "code_change": _object_schema(
            (
                ("content", _TEXT),
                ("source_type", _SOURCE_TYPE),
                ("implementation_scope", _SCOPE),
                ("evidence_ids", _EVIDENCE_IDS),
            ),
            evidence_field="evidence_ids",
        ),
        "sourced_conclusion": _object_schema(
            (
                ("content", _TEXT),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
            ),
            evidence_field="evidence_ids",
        ),
        "risk": _object_schema(
            (
                ("content", _TEXT),
                ("risk_level", _RISK),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
            ),
            evidence_field="evidence_ids",
        ),
        "correction_trace": _object_schema(
            (
                ("reason", _TEXT),
                ("handled", _BOOL),
                ("evidence_ids", _EVIDENCE_IDS),
            ),
            evidence_field="evidence_ids",
        ),
        "contradiction_item": _object_schema(
            (
                ("content", _TEXT),
                ("source_type", _SOURCE_TYPE),
                ("evidence_ids", _EVIDENCE_IDS),
            ),
            evidence_field="evidence_ids",
        ),
    }
)


RESULT_FIELD_RULES: Final = MappingProxyType(
    {
        "project_profile_build": (
            ("project_summary", _TEXT),
            ("features", FieldRule("array")),
            ("module_path_candidates", FieldRule("array")),
            ("terminology", FieldRule("array")),
            ("exclusion_rule_candidates", FieldRule("array")),
            ("analysis_rule_candidates", FieldRule("array")),
            ("unmapped_areas", FieldRule("array")),
            ("evidence_refs", FieldRule("string_list", non_empty=True, min_items=1)),
        ),
        "daily_report_generate": (
            ("plain_summary", _TEXT),
            ("feature_progress", FieldRule("array")),
            ("code_change_summary", FieldRule("array")),
            ("test_evidence", FieldRule("array")),
            ("risks", FieldRule("array")),
            ("unknown_items", FieldRule("array")),
            ("source_warnings", FieldRule("array")),
        ),
        "daily_report_regenerate": (
            ("new_report", FieldRule("object")),
            ("correction_trace", FieldRule("array", min_items=1)),
        ),
        "report_contradiction_check": (
            ("has_conflict", _BOOL),
            ("checked_items", FieldRule("array", min_items=1)),
            ("conflicts", FieldRule("array")),
            ("unresolved_items", FieldRule("array")),
            ("recommended_action", _TEXT),
        ),
    }
)

RESULT_FIELDS: Final = MappingProxyType(
    {
        task_type: tuple(name for name, _ in rules)
        for task_type, rules in RESULT_FIELD_RULES.items()
    }
)

RESULT_ARRAY_ITEM_SCHEMAS: Final = MappingProxyType(
    {
        "project_profile_build": MappingProxyType(
            {
                "features": "project_feature",
                "module_path_candidates": "module_path_candidate",
                "terminology": "terminology_candidate",
                "exclusion_rule_candidates": "rule_candidate",
                "analysis_rule_candidates": "rule_candidate",
                "unmapped_areas": "unmapped_area_candidate",
            }
        ),
        "daily_report_generate": MappingProxyType(
            {
                "feature_progress": "feature_progress",
                "code_change_summary": "code_change",
                "test_evidence": "sourced_conclusion",
                "risks": "risk",
                "unknown_items": "sourced_conclusion",
                "source_warnings": "sourced_conclusion",
            }
        ),
        "daily_report_regenerate": MappingProxyType(
            {"correction_trace": "correction_trace"}
        ),
        "report_contradiction_check": MappingProxyType(
            {
                "checked_items": "contradiction_item",
                "conflicts": "contradiction_item",
                "unresolved_items": "contradiction_item",
            }
        ),
    }
)

USAGE_FIELDS: Final = ("input_tokens", "output_tokens")
COST_FIELDS: Final = ("amount", "currency")
WARNING_FIELDS: Final = ("code", "message")
MODEL_IDENTITY_FIELDS: Final = ("provider", "id", "version")
AUTHORIZATION_FIELDS: Final = ("provider", "authorized", "valid")
QUALIFICATION_FIELDS: Final = (
    "provider",
    "model_id",
    "model_version",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_status",
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A machine-stable validation problem safe for API and UI consumers."""

    code: str
    path: str
    message: str
    severity: str = "error"


def stable_issues(issues: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    """Deduplicate and sort issues without depending on caller dictionary order."""

    unique = {
        (issue.path, issue.code, issue.message, issue.severity): issue for issue in issues
    }
    return tuple(unique[key] for key in sorted(unique))


@dataclass(frozen=True, slots=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True, slots=True)
class FormalEligibilityResult:
    eligible: bool
    issues: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelQualificationRecord:
    """Version-bound record proving one concrete model passed qualification."""

    provider: str
    model_id: str
    model_version: str
    rule_version: str
    output_schema_version: str
    benchmark_sample_pack_version: str
    qualification_status: str
