"""Black-box tests for the centralized AI contract schema registry."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.ai_contract_validation import validate_ai_response
from app.ai_contracts import (
    ENVELOPE_FIELDS,
    MAX_ARRAY_LENGTH,
    MAX_COST_AMOUNT,
    MAX_MAPPING_FIELDS,
    MAX_NESTING_DEPTH,
    MAX_STRING_LENGTH,
    MAX_TRAVERSAL_NODES,
    NESTED_OBJECT_SCHEMAS,
    RESULT_ARRAY_ITEM_SCHEMAS,
    RESULT_FIELDS,
    ValidationIssue,
    ValidationResult,
)


ALLOWED_EVIDENCE = {"ev-1"}


def _project_profile_response():
    return {
        "local_task_id": "local-1",
        "provider_request_id": "unknown",
        "task_type": "project_profile_build",
        "status": "succeeded",
        "schema_version": "ai-agent-contract/1.0",
        "output_schema_version": "project-profile/1.0",
        "actual_model": {
            "provider": "provider-a",
            "id": "model-a",
            "version": "2026-08",
        },
        "usage": "unknown",
        "cost": "unknown",
        "billing_status": "unknown",
        "warnings": [],
        "result": {
            "project_summary": "候选项目档案摘要",
            "features": [
                {
                    "name": "登录",
                    "description": "登录能力候选",
                    "source_type": "prd_fact",
                    "evidence_ids": ["ev-1"],
                    "candidate": True,
                }
            ],
            "module_path_candidates": [],
            "terminology": [],
            "exclusion_rule_candidates": [],
            "analysis_rule_candidates": [],
            "unmapped_areas": [],
            "evidence_refs": ["ev-1"],
        },
    }


def _pairs(validation):
    return [(issue.code, issue.path) for issue in validation.issues]


def test_validation_result_is_stable_and_machine_readable():
    issue = ValidationIssue("AI_SCHEMA_INVALID", "$.result", "结构无效。")
    result = ValidationResult((issue,))
    assert result.is_valid is False
    assert result.issues[0].code == "AI_SCHEMA_INVALID"
    assert result.issues[0].path == "$.result"


def test_minimal_project_profile_contract_is_valid_and_input_is_unchanged():
    response = _project_profile_response()
    before = deepcopy(response)
    first = validate_ai_response(response, ALLOWED_EVIDENCE)
    second = validate_ai_response(response, ALLOWED_EVIDENCE)
    assert first.is_valid is True
    assert first.issues == ()
    assert second == first
    assert response == before


def test_result_top_level_fields_remain_locked():
    assert dict(RESULT_FIELDS) == {
        "project_profile_build": (
            "project_summary",
            "features",
            "module_path_candidates",
            "terminology",
            "exclusion_rule_candidates",
            "analysis_rule_candidates",
            "unmapped_areas",
            "evidence_refs",
        ),
        "daily_report_generate": (
            "plain_summary",
            "feature_progress",
            "code_change_summary",
            "test_evidence",
            "risks",
            "unknown_items",
            "source_warnings",
        ),
        "daily_report_regenerate": ("new_report", "correction_trace"),
        "report_contradiction_check": (
            "has_conflict",
            "checked_items",
            "conflicts",
            "unresolved_items",
            "recommended_action",
        ),
    }


def test_every_result_array_uses_a_registered_exact_item_schema():
    registered = set(NESTED_OBJECT_SCHEMAS)
    referenced = {
        schema_name
        for task_schemas in RESULT_ARRAY_ITEM_SCHEMAS.values()
        for schema_name in task_schemas.values()
    }
    assert referenced == registered
    for schema in NESTED_OBJECT_SCHEMAS.values():
        assert schema.required_fields == tuple(name for name, _ in schema.fields)
        assert schema.evidence_field in schema.required_fields


def test_resource_limits_are_centralized_and_usable_for_normal_reports():
    assert 8 <= MAX_NESTING_DEPTH <= 64
    assert MAX_TRAVERSAL_NODES >= 1_000
    assert MAX_ARRAY_LENGTH >= 50
    assert MAX_STRING_LENGTH >= 1_000
    assert MAX_MAPPING_FIELDS >= len(ENVELOPE_FIELDS)
    assert MAX_COST_AMOUNT == 1_000_000


@pytest.mark.parametrize("missing", ENVELOPE_FIELDS)
def test_each_envelope_field_is_required(missing):
    response = _project_profile_response()
    del response[missing]
    assert ("AI_REQUIRED_FIELD_MISSING", f"$.{missing}") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_extra_envelope_and_result_fields_have_exact_paths():
    response = _project_profile_response()
    response["extra"] = True
    response["result"]["extra"] = True
    assert _pairs(validate_ai_response(response, ALLOWED_EVIDENCE)) == [
        ("AI_EXTRA_FIELD", "$.extra"),
        ("AI_EXTRA_FIELD", "$.result.extra"),
    ]


@pytest.mark.parametrize("candidate", (False, 0, 1, "true", None))
def test_project_candidates_are_strictly_marked_candidate(candidate):
    response = _project_profile_response()
    response["result"]["features"][0]["candidate"] = candidate
    assert (
        "AI_SCHEMA_INVALID",
        "$.result.features[0].candidate",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize("state_field", ("confirmed", "applied", "effective"))
def test_project_candidates_cannot_add_confirmed_or_applied_state(state_field):
    response = _project_profile_response()
    response["result"]["features"][0][state_field] = False
    assert (
        "AI_EXTRA_FIELD",
        f"$.result.features[0].{state_field}",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
