"""Adversarial black-box tests for deterministic AI contract validation."""

from __future__ import annotations

from copy import deepcopy
import math

import pytest

from app.ai_contract_validation import (
    assess_formal_report_eligibility,
    validate_ai_response,
)
from app.ai_contracts import (
    IMPLEMENTATION_SCOPES,
    MAX_ARRAY_LENGTH,
    MAX_COST_AMOUNT,
    MAX_MAPPING_FIELDS,
    MAX_NESTING_DEPTH,
    MAX_STRING_LENGTH,
    MAX_TRAVERSAL_NODES,
    RISK_LEVELS,
    SOURCE_TYPES,
    STAGES,
    ModelQualificationRecord,
)


ALLOWED_EVIDENCE = {"ev-1", "ev-2"}

FEATURE = {
    "name": "登录",
    "description": "登录能力候选",
    "source_type": "prd_fact",
    "evidence_ids": ["ev-1"],
    "candidate": True,
}
MODULE_PATH = {
    "path": "apps/backend/app/login.py",
    "description": "登录模块候选路径",
    "implementation_scope": "后端",
    "source_type": "git_fact",
    "evidence_ids": ["ev-1"],
    "candidate": True,
}
TERMINOLOGY = {
    "term": "会话",
    "definition": "登录后的访问上下文候选定义",
    "source_type": "prd_fact",
    "evidence_ids": ["ev-1"],
    "candidate": True,
}
RULE_CANDIDATE = {
    "content": "排除未绑定证据的断言",
    "reason": "避免无来源结论",
    "source_type": "ai_analysis",
    "evidence_ids": ["ev-1"],
    "candidate": True,
}
UNMAPPED_AREA = {
    "area": "邮件通知",
    "reason": "现有证据不足",
    "implementation_scope": "暂时无法确认",
    "source_type": "ai_analysis",
    "evidence_ids": ["ev-1"],
    "candidate": True,
}
FEATURE_PROGRESS = {
    "feature": "登录",
    "stage": "开发中",
    "source_type": "git_fact",
    "implementation_scope": "后端",
    "evidence_ids": ["ev-1"],
}
CODE_CHANGE = {
    "content": "增加登录校验",
    "source_type": "git_fact",
    "implementation_scope": "后端",
    "evidence_ids": ["ev-1"],
}
SOURCED_CONCLUSION = {
    "content": "登录测试通过",
    "source_type": "git_fact",
    "evidence_ids": ["ev-1"],
}
RISK = {
    "content": "外部身份源尚未联调",
    "risk_level": "suspected",
    "source_type": "ai_analysis",
    "evidence_ids": ["ev-1"],
}
CORRECTION = {
    "reason": "根据新增证据重新分析",
    "handled": True,
    "evidence_ids": ["ev-2"],
}
CONTRADICTION_ITEM = {
    "content": "登录状态描述一致",
    "source_type": "ai_analysis",
    "evidence_ids": ["ev-1"],
}

ARRAY_CASES = (
    ("project_profile_build", "features", FEATURE),
    ("project_profile_build", "module_path_candidates", MODULE_PATH),
    ("project_profile_build", "terminology", TERMINOLOGY),
    ("project_profile_build", "exclusion_rule_candidates", RULE_CANDIDATE),
    ("project_profile_build", "analysis_rule_candidates", RULE_CANDIDATE),
    ("project_profile_build", "unmapped_areas", UNMAPPED_AREA),
    ("daily_report_generate", "feature_progress", FEATURE_PROGRESS),
    ("daily_report_generate", "code_change_summary", CODE_CHANGE),
    ("daily_report_generate", "test_evidence", SOURCED_CONCLUSION),
    ("daily_report_generate", "risks", RISK),
    ("daily_report_generate", "unknown_items", SOURCED_CONCLUSION),
    ("daily_report_generate", "source_warnings", SOURCED_CONCLUSION),
    ("daily_report_regenerate", "correction_trace", CORRECTION),
    ("report_contradiction_check", "checked_items", CONTRADICTION_ITEM),
    ("report_contradiction_check", "conflicts", CONTRADICTION_ITEM),
    ("report_contradiction_check", "unresolved_items", CONTRADICTION_ITEM),
)


def _project_result():
    return {
        "project_summary": "项目档案候选摘要",
        "features": [deepcopy(FEATURE)],
        "module_path_candidates": [deepcopy(MODULE_PATH)],
        "terminology": [deepcopy(TERMINOLOGY)],
        "exclusion_rule_candidates": [deepcopy(RULE_CANDIDATE)],
        "analysis_rule_candidates": [deepcopy(RULE_CANDIDATE)],
        "unmapped_areas": [deepcopy(UNMAPPED_AREA)],
        "evidence_refs": ["ev-1"],
    }


def _daily_result():
    return {
        "plain_summary": "今日推进登录能力",
        "feature_progress": [deepcopy(FEATURE_PROGRESS)],
        "code_change_summary": [deepcopy(CODE_CHANGE)],
        "test_evidence": [deepcopy(SOURCED_CONCLUSION)],
        "risks": [deepcopy(RISK)],
        "unknown_items": [deepcopy(SOURCED_CONCLUSION)],
        "source_warnings": [deepcopy(SOURCED_CONCLUSION)],
    }


def _result(task_type):
    if task_type == "project_profile_build":
        return _project_result()
    if task_type == "daily_report_generate":
        return _daily_result()
    if task_type == "daily_report_regenerate":
        return {
            "new_report": _daily_result(),
            "correction_trace": [deepcopy(CORRECTION)],
        }
    return {
        "has_conflict": False,
        "checked_items": [deepcopy(CONTRADICTION_ITEM)],
        "conflicts": [],
        "unresolved_items": [deepcopy(CONTRADICTION_ITEM)],
        "recommended_action": "建议保留现有描述并继续核对证据",
    }


def _response(task_type="daily_report_generate"):
    output_versions = {
        "project_profile_build": "project-profile/1.0",
        "daily_report_generate": "daily-report/1.0",
        "daily_report_regenerate": "daily-report/1.0",
        "report_contradiction_check": "contradiction-check/1.0",
    }
    return {
        "local_task_id": "local-1",
        "provider_request_id": "unknown",
        "task_type": task_type,
        "status": "succeeded",
        "schema_version": "ai-agent-contract/1.0",
        "output_schema_version": output_versions[task_type],
        "actual_model": {
            "provider": "provider-a",
            "id": "model-a",
            "version": "2026-08",
        },
        "usage": "unknown",
        "cost": "unknown",
        "billing_status": "unknown",
        "warnings": [],
        "result": _result(task_type),
    }


def _pairs(validation):
    return [(issue.code, issue.path) for issue in validation.issues]


def _qualification(**overrides):
    record = {
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "samples/1.0",
        "qualification_status": "qualified",
    }
    record.update(overrides)
    return record


def _authorization(**overrides):
    authorization = {
        "provider": "provider-a",
        "authorized": True,
        "valid": True,
    }
    authorization.update(overrides)
    return authorization


def _assess(response=None, **overrides):
    arguments = {
        "requested_model_id": "model-a",
        "required_provider": "provider-a",
        "qualification_record": _qualification(),
        "data_sending_authorization": _authorization(),
        "fallback_occurred": False,
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "samples/1.0",
        "allowed_evidence_ids": ALLOWED_EVIDENCE,
    }
    arguments.update(overrides)
    actual_response = response if response is not None else _response()
    return assess_formal_report_eligibility(actual_response, **arguments)


def _array(response, task_type, field):
    return response["result"][field]


@pytest.mark.parametrize(
    "task_type",
    (
        "project_profile_build",
        "daily_report_generate",
        "daily_report_regenerate",
        "report_contradiction_check",
    ),
)
def test_four_complete_minimal_contracts_are_valid(task_type):
    validation = validate_ai_response(_response(task_type), ALLOWED_EVIDENCE)
    assert validation.is_valid is True
    assert validation.issues == ()


@pytest.mark.parametrize(
    ("result_task", "declared_task"),
    (
        ("project_profile_build", "daily_report_generate"),
        ("daily_report_generate", "daily_report_regenerate"),
        ("daily_report_regenerate", "report_contradiction_check"),
        ("report_contradiction_check", "project_profile_build"),
    ),
)
def test_each_result_contract_rejects_task_type_mismatch(result_task, declared_task):
    response = _response(declared_task)
    response["result"] = _result(result_task)
    assert ("AI_TASK_RESULT_MISMATCH", "$.result") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(("task_type", "field", "valid_item"), ARRAY_CASES)
@pytest.mark.parametrize("bad_item", (42, None, "not-an-object", {}))
def test_every_nested_array_rejects_non_object_or_empty_item(
    task_type, field, valid_item, bad_item
):
    response = _response(task_type)
    _array(response, task_type, field)[:] = [bad_item]
    path = f"$.result.{field}[0]"
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    if bad_item == {}:
        assert any(issue_path.startswith(f"{path}.") for _, issue_path in pairs)
        assert ("AI_EVIDENCE_REQUIRED", f"{path}.evidence_ids") in pairs
    else:
        assert ("AI_SCHEMA_INVALID", path) in pairs


@pytest.mark.parametrize(("task_type", "field", "valid_item"), ARRAY_CASES)
def test_each_required_field_of_every_nested_schema_is_enforced(
    task_type, field, valid_item
):
    for missing in valid_item:
        response = _response(task_type)
        item = deepcopy(valid_item)
        del item[missing]
        _array(response, task_type, field)[:] = [item]
        path = f"$.result.{field}[0].{missing}"
        pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
        expected = "AI_EVIDENCE_REQUIRED" if missing == "evidence_ids" else "AI_REQUIRED_FIELD_MISSING"
        assert (expected, path) in pairs


@pytest.mark.parametrize(("task_type", "field", "valid_item"), ARRAY_CASES)
def test_every_nested_schema_forbids_extra_fields(task_type, field, valid_item):
    response = _response(task_type)
    item = deepcopy(valid_item)
    item["unexpected"] = "value"
    _array(response, task_type, field)[:] = [item]
    assert (
        "AI_EXTRA_FIELD",
        f"$.result.{field}[0].unexpected",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(("task_type", "field", "valid_item"), ARRAY_CASES)
def test_every_conclusion_category_requires_nonempty_known_string_evidence(
    task_type, field, valid_item
):
    for evidence, code, suffix in (
        ([], "AI_EVIDENCE_REQUIRED", ""),
        (["ev-missing"], "AI_EVIDENCE_UNKNOWN", "[0]"),
        ([7], "AI_SCHEMA_INVALID", "[0]"),
        (["   "], "AI_SCHEMA_INVALID", "[0]"),
    ):
        response = _response(task_type)
        item = deepcopy(valid_item)
        item["evidence_ids"] = evidence
        _array(response, task_type, field)[:] = [item]
        path = f"$.result.{field}[0].evidence_ids{suffix}"
        assert (code, path) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    ("task_type", "field", "item_field", "bad_value"),
    (
        ("project_profile_build", "features", "name", 7),
        ("project_profile_build", "features", "candidate", 1),
        ("project_profile_build", "module_path_candidates", "implementation_scope", []),
        ("project_profile_build", "terminology", "definition", None),
        ("project_profile_build", "exclusion_rule_candidates", "reason", {}),
        ("project_profile_build", "unmapped_areas", "area", True),
        ("daily_report_generate", "feature_progress", "stage", False),
        ("daily_report_generate", "code_change_summary", "implementation_scope", 1),
        ("daily_report_generate", "test_evidence", "source_type", []),
        ("daily_report_generate", "risks", "risk_level", 1),
        ("daily_report_generate", "unknown_items", "content", None),
        ("daily_report_generate", "source_warnings", "content", {}),
        ("daily_report_regenerate", "correction_trace", "handled", 1),
        ("report_contradiction_check", "checked_items", "source_type", True),
    ),
)
def test_nested_field_types_are_exact(task_type, field, item_field, bad_value):
    response = _response(task_type)
    _array(response, task_type, field)[0][item_field] = bad_value
    assert (
        "AI_SCHEMA_INVALID",
        f"$.result.{field}[0].{item_field}",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    ("task_type", "field", "item_field"),
    (
        ("project_profile_build", "features", "name"),
        ("project_profile_build", "module_path_candidates", "description"),
        ("project_profile_build", "terminology", "term"),
        ("project_profile_build", "exclusion_rule_candidates", "content"),
        ("project_profile_build", "unmapped_areas", "reason"),
        ("daily_report_generate", "feature_progress", "feature"),
        ("daily_report_generate", "code_change_summary", "content"),
        ("daily_report_generate", "test_evidence", "content"),
        ("daily_report_generate", "risks", "content"),
        ("daily_report_regenerate", "correction_trace", "reason"),
        ("report_contradiction_check", "checked_items", "content"),
    ),
)
def test_readable_nested_text_cannot_be_blank(task_type, field, item_field):
    response = _response(task_type)
    _array(response, task_type, field)[0][item_field] = " \t "
    assert (
        "AI_SCHEMA_INVALID",
        f"$.result.{field}[0].{item_field}",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


def test_project_summary_and_evidence_refs_are_strict():
    response = _response("project_profile_build")
    response["result"]["project_summary"] = "   "
    response["result"]["evidence_refs"] = []
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    assert ("AI_SCHEMA_INVALID", "$.result.project_summary") in pairs
    assert ("AI_EVIDENCE_REQUIRED", "$.result.evidence_refs") in pairs


@pytest.mark.parametrize("reference", ("ev-unknown", 7, None, "   "))
def test_project_evidence_refs_close_over_allowed_ids(reference):
    response = _response("project_profile_build")
    response["result"]["evidence_refs"] = [reference]
    expected = "AI_EVIDENCE_UNKNOWN" if reference == "ev-unknown" else "AI_SCHEMA_INVALID"
    assert (expected, "$.result.evidence_refs[0]") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("value", STAGES)
def test_all_stage_values_are_accepted(value):
    response = _response()
    response["result"]["feature_progress"][0]["stage"] = value
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


@pytest.mark.parametrize("value", SOURCE_TYPES)
def test_all_source_type_values_are_accepted(value):
    response = _response()
    response["result"]["feature_progress"][0]["source_type"] = value
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


@pytest.mark.parametrize("value", IMPLEMENTATION_SCOPES)
def test_all_implementation_scope_values_are_accepted(value):
    response = _response()
    response["result"]["feature_progress"][0]["implementation_scope"] = value
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


@pytest.mark.parametrize("value", RISK_LEVELS)
def test_all_risk_level_values_are_accepted(value):
    response = _response()
    response["result"]["risks"][0]["risk_level"] = value
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


@pytest.mark.parametrize(
    ("field", "value", "code", "path"),
    (
        ("stage", "即将完成", "AI_STAGE_INVALID", "$.result.feature_progress[0].stage"),
        ("source_type", "git", "AI_SOURCE_TYPE_INVALID", "$.result.feature_progress[0].source_type"),
        ("implementation_scope", "运维", "AI_SCOPE_INVALID", "$.result.feature_progress[0].implementation_scope"),
    ),
)
def test_invalid_business_enums_have_specific_code_and_path(field, value, code, path):
    response = _response()
    response["result"]["feature_progress"][0][field] = value
    assert (code, path) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


def test_invalid_risk_level_uses_stable_enum_code():
    response = _response()
    response["result"]["risks"][0]["risk_level"] = "official_audit"
    assert ("AI_ENUM_INVALID", "$.result.risks[0].risk_level") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_deep_unknown_evidence_id_has_exact_path():
    response = _response("daily_report_regenerate")
    response["result"]["new_report"]["risks"][0]["evidence_ids"] = ["ev-unknown"]
    assert (
        "AI_EVIDENCE_UNKNOWN",
        "$.result.new_report.risks[0].evidence_ids[0]",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


def test_regeneration_reuses_the_full_daily_schema():
    response = _response("daily_report_regenerate")
    response["result"]["new_report"]["test_evidence"] = [42]
    assert (
        "AI_SCHEMA_INVALID",
        "$.result.new_report.test_evidence[0]",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize("new_report", (None, [], "report", 42, {}))
def test_regeneration_rejects_malformed_new_report(new_report):
    response = _response("daily_report_regenerate")
    response["result"]["new_report"] = new_report
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    if new_report == {}:
        assert ("AI_REQUIRED_FIELD_MISSING", "$.result.new_report.plain_summary") in pairs
    else:
        assert ("AI_SCHEMA_INVALID", "$.result.new_report") in pairs


@pytest.mark.parametrize("trace", ([], [{}], ["fixed"], [None]))
def test_regeneration_correction_trace_cannot_be_empty_or_malformed(trace):
    response = _response("daily_report_regenerate")
    response["result"]["correction_trace"] = trace
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    if trace == []:
        assert ("AI_REQUIRED_FIELD_MISSING", "$.result.correction_trace") in pairs
    elif trace == [{}]:
        assert ("AI_REQUIRED_FIELD_MISSING", "$.result.correction_trace[0].reason") in pairs
    else:
        assert ("AI_SCHEMA_INVALID", "$.result.correction_trace[0]") in pairs


@pytest.mark.parametrize("has_conflict", (None, 0, 1, "false", []))
def test_has_conflict_is_strict_bool(has_conflict):
    response = _response("report_contradiction_check")
    response["result"]["has_conflict"] = has_conflict
    assert ("AI_SCHEMA_INVALID", "$.result.has_conflict") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_conflict_true_requires_nonempty_conflicts():
    response = _response("report_contradiction_check")
    response["result"]["has_conflict"] = True
    response["result"]["conflicts"] = []
    assert ("AI_SCHEMA_INVALID", "$.result.conflicts") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_conflict_false_forbids_nonempty_conflicts():
    response = _response("report_contradiction_check")
    response["result"]["has_conflict"] = False
    response["result"]["conflicts"] = [deepcopy(CONTRADICTION_ITEM)]
    assert ("AI_SCHEMA_INVALID", "$.result.conflicts") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("action", ("", "   ", None, 7))
def test_recommended_action_must_be_readable_text(action):
    response = _response("report_contradiction_check")
    response["result"]["recommended_action"] = action
    assert ("AI_SCHEMA_INVALID", "$.result.recommended_action") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_plain_summary_cannot_be_blank():
    response = _response()
    response["result"]["plain_summary"] = " \t "
    assert ("AI_SCHEMA_INVALID", "$.result.plain_summary") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_summary_without_any_structured_conclusion_is_not_formally_eligible():
    response = _response()
    for field in (
        "feature_progress",
        "code_change_summary",
        "test_evidence",
        "risks",
        "unknown_items",
        "source_warnings",
    ):
        response["result"][field] = []
    result = _assess(response)
    assert result.eligible is False
    assert ("AI_FORMAL_REPORT_CONTENT_REQUIRED", "$.result") in _pairs(result)


def test_empty_dictionary_is_not_silently_replaced_by_test_helper():
    result = _assess({})
    assert result.eligible is False
    assert ("AI_REQUIRED_FIELD_MISSING", "$.local_task_id") in _pairs(result)


@pytest.mark.parametrize(
    "text",
    (
        "项目进度80%",
        "整体完成率80％",
        "当前完成八成",
        "项目完成80 pct",
        "完成度百分之八十",
        "project progress 80 percent",
    ),
)
def test_project_progress_percentage_forms_are_forbidden(text):
    response = _response()
    response["result"]["plain_summary"] = text
    assert ("AI_PERCENTAGE_FORBIDDEN", "$.result.plain_summary") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "text",
    (
        "测试覆盖率80%",
        "错误率5%",
        "接口通过率100%",
        "版本号为1.2.0",
        "日期为2026-08-05",
        "金额为80.5元",
        "Token数量为1000",
        "CPU utilization is 80 percent",
    ),
)
def test_technical_metrics_are_not_misclassified_as_project_progress(text):
    response = _response()
    response["result"]["plain_summary"] = text
    assert ("AI_PERCENTAGE_FORBIDDEN", "$.result.plain_summary") not in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "claim",
    (
        "这是独立审计结论",
        "已获官方认证",
        "这是第三方审计结论",
        "已获权威认证",
        "模型厂商为结论背书",
        "This is an independent audit.",
    ),
)
def test_audit_certification_and_endorsement_claims_are_forbidden(claim):
    response = _response()
    response["result"]["plain_summary"] = claim
    assert ("AI_AUDIT_CLAIM_FORBIDDEN", "$.result.plain_summary") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "disclaimer",
    (
        "本结果未经过独立审计",
        "不代表官方认证",
        "不得声称官方认证",
        "本工具不提供审计结论",
        "This is not an independent audit.",
        "Provided without official certification.",
    ),
)
def test_audit_disclaimers_are_allowed(disclaimer):
    response = _response()
    response["result"]["plain_summary"] = disclaimer
    assert ("AI_AUDIT_CLAIM_FORBIDDEN", "$.result.plain_summary") not in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "claim",
    ("候选已采纳", "已写入正式项目档案", "已自动生效", "已确认应用"),
)
def test_project_profile_cannot_claim_candidate_was_applied(claim):
    response = _response("project_profile_build")
    response["result"]["project_summary"] = claim
    assert (
        "AI_CANDIDATE_AUTO_APPLY_FORBIDDEN",
        "$.result.project_summary",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "disclaimer",
    ("候选尚未确认", "不会自动生效", "必须人工确认后才生效", "candidate not applied"),
)
def test_project_candidate_disclaimers_are_allowed(disclaimer):
    response = _response("project_profile_build")
    response["result"]["project_summary"] = disclaimer
    assert (
        "AI_CANDIDATE_AUTO_APPLY_FORBIDDEN",
        "$.result.project_summary",
    ) not in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "claim",
    ("AI原文已经修改", "系统已更新原文", "已自动修正项目经理补充"),
)
def test_contradiction_check_cannot_claim_automatic_edit(claim):
    response = _response("report_contradiction_check")
    response["result"]["recommended_action"] = claim
    assert (
        "AI_CONTRADICTION_AUTO_EDIT_FORBIDDEN",
        "$.result.recommended_action",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "disclaimer",
    ("不会自动修改AI原文", "不得修改AI原文", "仅提出修改建议", "will not edit the original"),
)
def test_automatic_edit_disclaimers_are_allowed(disclaimer):
    response = _response("report_contradiction_check")
    response["result"]["recommended_action"] = disclaimer
    assert (
        "AI_CONTRADICTION_AUTO_EDIT_FORBIDDEN",
        "$.result.recommended_action",
    ) not in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "text",
    (
        "整体已做到八成",
        "开发已到八成",
        "任务进展八成",
        "项目已经做到八成",
        "当前开发完成了八成",
        "整体推进到80 pct",
        "项目完成度达到80 percent",
        "整体目前已推进到80 pct",
    ),
)
def test_second_review_progress_synonyms_are_forbidden(text):
    response = _response()
    response["result"]["plain_summary"] = text
    assert ("AI_PERCENTAGE_FORBIDDEN", "$.result.plain_summary") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "claim",
    (
        "这是第三方审计报告",
        "已经通过独立审计",
        "获得官方认证",
        "已经取得权威认证",
        "结论得到模型厂商背书",
        "本结论已经顺利通过独立审计",
    ),
)
def test_second_review_audit_synonyms_are_forbidden(claim):
    response = _response()
    response["result"]["plain_summary"] = claim
    assert ("AI_AUDIT_CLAIM_FORBIDDEN", "$.result.plain_summary") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "disclaimer",
    (
        "本结果没有经过独立审计",
        "本结果尚无第三方审计结论",
        "本结果未获官方认证",
        "没有官方认证",
        "尚未获得权威认证",
        "本结果未得到模型厂商背书",
        "本结果没有经过任何独立审计",
    ),
)
def test_second_review_audit_negations_are_allowed(disclaimer):
    response = _response()
    response["result"]["plain_summary"] = disclaimer
    assert ("AI_AUDIT_CLAIM_FORBIDDEN", "$.result.plain_summary") not in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize(
    "claim",
    (
        "候选已被采纳",
        "候选已经入库",
        "已纳入正式项目档案",
        "候选已采用",
        "候选已经写进正式项目档案",
        "候选已被正式采纳",
    ),
)
def test_second_review_candidate_apply_synonyms_are_forbidden(claim):
    response = _response("project_profile_build")
    response["result"]["project_summary"] = claim
    assert (
        "AI_CANDIDATE_AUTO_APPLY_FORBIDDEN",
        "$.result.project_summary",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "disclaimer",
    (
        "候选尚未被采纳",
        "候选没有入库",
        "尚未纳入正式项目档案",
        "候选不会被自动采用",
        "候选尚未被正式采纳",
    ),
)
def test_second_review_candidate_apply_negations_are_allowed(disclaimer):
    response = _response("project_profile_build")
    response["result"]["project_summary"] = disclaimer
    assert (
        "AI_CANDIDATE_AUTO_APPLY_FORBIDDEN",
        "$.result.project_summary",
    ) not in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "claim",
    (
        "AI原文已被调整",
        "AI原文已经改动",
        "系统已替换原文",
        "原文已被覆盖",
        "系统已经调整AI原文",
        "系统已经正式替换原文",
    ),
)
def test_second_review_original_edit_synonyms_are_forbidden(claim):
    response = _response("report_contradiction_check")
    response["result"]["recommended_action"] = claim
    assert (
        "AI_CONTRADICTION_AUTO_EDIT_FORBIDDEN",
        "$.result.recommended_action",
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    "disclaimer",
    (
        "AI原文没有被调整",
        "AI原文尚未改动",
        "系统不会替换原文",
        "原文尚未被覆盖",
        "系统不会直接替换原文",
    ),
)
def test_second_review_original_edit_negations_are_allowed(disclaimer):
    response = _response("report_contradiction_check")
    response["result"]["recommended_action"] = disclaimer
    assert (
        "AI_CONTRADICTION_AUTO_EDIT_FORBIDDEN",
        "$.result.recommended_action",
    ) not in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize(
    ("task_type", "field", "text", "code"),
    (
        (
            "daily_report_generate",
            "plain_summary",
            "项目进度80%",
            "AI_PERCENTAGE_FORBIDDEN",
        ),
        (
            "daily_report_generate",
            "plain_summary",
            "这是独立审计结论",
            "AI_AUDIT_CLAIM_FORBIDDEN",
        ),
        (
            "project_profile_build",
            "project_summary",
            "候选已采纳",
            "AI_CANDIDATE_AUTO_APPLY_FORBIDDEN",
        ),
        (
            "report_contradiction_check",
            "recommended_action",
            "AI原文已经修改",
            "AI_CONTRADICTION_AUTO_EDIT_FORBIDDEN",
        ),
    ),
)
def test_language_redline_issues_are_advisory_warnings(
    task_type, field, text, code
):
    response = _response(task_type)
    response["result"][field] = text

    validation = validate_ai_response(response, ALLOWED_EVIDENCE)
    issue = next(issue for issue in validation.issues if issue.code == code)

    assert issue.path == f"$.result.{field}"
    assert issue.severity == "warning"
    assert "可能" in issue.message
    assert "人工确认" in issue.message


def test_language_warning_alone_keeps_validation_valid_and_visible():
    response = _response()
    response["result"]["plain_summary"] = "这是独立审计结论"

    validation = validate_ai_response(response, ALLOWED_EVIDENCE)

    assert validation.is_valid is True
    warning = next(
        issue for issue in validation.issues if issue.code == "AI_AUDIT_CLAIM_FORBIDDEN"
    )
    assert warning.severity == "warning"
    assert not any(issue.severity == "error" for issue in validation.issues)


def test_language_warning_alone_does_not_block_formal_eligibility():
    response = _response()
    response["result"]["plain_summary"] = "这是独立审计结论"

    result = _assess(response)

    assert result.eligible is True
    warning = next(
        issue for issue in result.issues if issue.code == "AI_AUDIT_CLAIM_FORBIDDEN"
    )
    assert warning.severity == "warning"
    assert not any(issue.severity == "error" for issue in result.issues)


def test_language_warning_does_not_hide_schema_error():
    response = _response()
    response["result"]["plain_summary"] = "这是独立审计结论"
    response["result"]["feature_progress"][0]["stage"] = "invalid-stage"

    validation = validate_ai_response(response, ALLOWED_EVIDENCE)

    assert validation.is_valid is False
    issues = {issue.code: issue for issue in validation.issues}
    assert issues["AI_AUDIT_CLAIM_FORBIDDEN"].severity == "warning"
    assert issues["AI_STAGE_INVALID"].severity == "error"


def test_language_warning_does_not_hide_evidence_error_from_eligibility():
    response = _response()
    response["result"]["plain_summary"] = "这是独立审计结论"
    response["result"]["feature_progress"][0]["evidence_ids"] = ["unknown"]

    result = _assess(response)

    assert result.eligible is False
    issues = {issue.code: issue for issue in result.issues}
    assert issues["AI_AUDIT_CLAIM_FORBIDDEN"].severity == "warning"
    assert issues["AI_EVIDENCE_UNKNOWN"].severity == "error"


def test_language_warning_does_not_hide_authorization_error_from_eligibility():
    response = _response()
    response["result"]["plain_summary"] = "这是独立审计结论"

    result = _assess(
        response,
        data_sending_authorization=_authorization(provider="provider-b"),
    )

    assert result.eligible is False
    issues = {issue.code: issue for issue in result.issues}
    assert issues["AI_AUDIT_CLAIM_FORBIDDEN"].severity == "warning"
    assert issues["AI_DATA_AUTHORIZATION_REQUIRED"].severity == "error"


def test_language_warning_validation_is_stable_and_does_not_mutate_input():
    response = _response()
    response["result"]["plain_summary"] = "这是独立审计结论"
    before = deepcopy(response)

    first = validate_ai_response(response, ALLOWED_EVIDENCE)
    second = validate_ai_response(response, ALLOWED_EVIDENCE)

    assert first == second
    assert list(first.issues) == sorted(
        first.issues,
        key=lambda issue: (issue.path, issue.code, issue.message, issue.severity),
    )
    assert all(issue.severity == "warning" for issue in first.issues)
    assert response == before


@pytest.mark.parametrize("field", ("provider", "id", "version"))
def test_actual_model_identity_fields_are_required(field):
    response = _response()
    del response["actual_model"][field]
    assert ("AI_MODEL_IDENTITY_MISSING", f"$.actual_model.{field}") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_actual_model_identity_forbids_extra_fields():
    response = _response()
    response["actual_model"]["alias"] = "model"
    assert ("AI_EXTRA_FIELD", "$.actual_model.alias") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_multiple_actual_models_are_rejected():
    response = _response()
    response["actual_model"] = [response["actual_model"], deepcopy(response["actual_model"])]
    assert ("AI_MODEL_MISMATCH", "$.actual_model") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_known_usage_cost_and_warning_contract_is_valid():
    response = _response()
    response["usage"] = {"input_tokens": 0, "output_tokens": 19}
    response["cost"] = {"amount": 0.0, "currency": "CNY"}
    response["billing_status"] = "known"
    response["warnings"] = [{"code": "SOURCE_DELAY", "message": "来源可能延迟"}]
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


def test_explicit_unknown_usage_and_cost_contract_is_valid():
    response = _response()
    assert response["usage"] == "unknown"
    assert response["cost"] == "unknown"
    assert response["billing_status"] == "unknown"
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


@pytest.mark.parametrize("value", (None, 0, 1, 1.5, True, "0", []))
def test_usage_only_accepts_unknown_or_exact_object(value):
    response = _response()
    response["usage"] = value
    assert ("AI_SCHEMA_INVALID", "$.usage") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_empty_usage_object_reports_its_missing_exact_fields():
    response = _response()
    response["usage"] = {}
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    assert ("AI_REQUIRED_FIELD_MISSING", "$.usage.input_tokens") in pairs
    assert ("AI_REQUIRED_FIELD_MISSING", "$.usage.output_tokens") in pairs


@pytest.mark.parametrize("field", ("input_tokens", "output_tokens"))
@pytest.mark.parametrize("value", (-1, True, False, 1.5, math.nan, math.inf))
def test_token_counts_are_nonnegative_integers_excluding_bool(field, value):
    response = _response()
    response["usage"] = {"input_tokens": 1, "output_tokens": 2}
    response["usage"][field] = value
    assert ("AI_SCHEMA_INVALID", f"$.usage.{field}") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("mutation_path", ("missing", "extra"))
def test_usage_object_has_exact_fields(mutation_path):
    response = _response()
    response["usage"] = {"input_tokens": 1, "output_tokens": 2}
    if mutation_path == "missing":
        del response["usage"]["output_tokens"]
        expected = ("AI_REQUIRED_FIELD_MISSING", "$.usage.output_tokens")
    else:
        response["usage"]["cached_tokens"] = 1
        expected = ("AI_EXTRA_FIELD", "$.usage.cached_tokens")
    assert expected in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


@pytest.mark.parametrize("amount", (0, 0.0, 1, 1.25, MAX_COST_AMOUNT))
def test_finite_nonnegative_known_costs_are_valid(amount):
    response = _response()
    response["cost"] = {"amount": amount, "currency": "USD"}
    response["billing_status"] = "known"
    assert validate_ai_response(response, ALLOWED_EVIDENCE).is_valid is True


@pytest.mark.parametrize(
    "amount",
    (
        -1,
        MAX_COST_AMOUNT + 1,
        True,
        False,
        math.nan,
        math.inf,
        -math.inf,
        "0",
        None,
    ),
)
def test_cost_amount_rejects_negative_nonfinite_bool_and_nonnumeric_values(amount):
    response = _response()
    response["cost"] = {"amount": amount, "currency": "USD"}
    response["billing_status"] = "known"
    assert ("AI_SCHEMA_INVALID", "$.cost.amount") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("amount", (10**309, 10**1000))
def test_second_review_huge_integer_cost_fails_closed_without_mutation(amount):
    response = _response()
    response["cost"] = {"amount": amount, "currency": "USD"}
    response["billing_status"] = "known"
    before = deepcopy(response)

    first = validate_ai_response(response, ALLOWED_EVIDENCE)
    second = validate_ai_response(response, ALLOWED_EVIDENCE)

    assert first == second
    assert ("AI_SCHEMA_INVALID", "$.cost.amount") in _pairs(first)
    amount_issue = next(issue for issue in first.issues if issue.path == "$.cost.amount")
    assert str(amount) not in amount_issue.message
    assert response == before


@pytest.mark.parametrize("amount", (10**309, 10**1000))
def test_second_review_huge_integer_cost_blocks_formal_eligibility(amount):
    response = _response()
    response["cost"] = {"amount": amount, "currency": "USD"}
    response["billing_status"] = "known"
    before = deepcopy(response)

    result = _assess(response)

    assert result.eligible is False
    assert ("AI_SCHEMA_INVALID", "$.cost.amount") in _pairs(result)
    assert response == before


@pytest.mark.parametrize("currency", ("", "   ", None, 7, True))
def test_cost_currency_is_nonempty_text(currency):
    response = _response()
    response["cost"] = {"amount": 1, "currency": currency}
    response["billing_status"] = "known"
    assert ("AI_SCHEMA_INVALID", "$.cost.currency") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("cost", (None, 0, 1.5, True, "free", [], {}))
def test_cost_only_accepts_unknown_or_exact_object(cost):
    response = _response()
    response["cost"] = cost
    response["billing_status"] = "known"
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    if cost == {}:
        assert ("AI_REQUIRED_FIELD_MISSING", "$.cost.amount") in pairs
    else:
        assert ("AI_SCHEMA_INVALID", "$.cost") in pairs


def test_cost_object_forbids_extra_fields():
    response = _response()
    response["cost"] = {"amount": 1, "currency": "USD", "estimated": True}
    response["billing_status"] = "known"
    assert ("AI_EXTRA_FIELD", "$.cost.estimated") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("status", (None, "free", "known-free", 0, True, {}))
def test_billing_status_is_a_fixed_enum(status):
    response = _response()
    response["billing_status"] = status
    assert ("AI_ENUM_INVALID", "$.billing_status") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_unknown_cost_cannot_claim_known_billing():
    response = _response()
    response["billing_status"] = "known"
    assert ("AI_BILLING_CONSISTENCY_INVALID", "$.billing_status") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_known_cost_cannot_claim_unknown_billing():
    response = _response()
    response["cost"] = {"amount": 0, "currency": "USD"}
    response["billing_status"] = "unknown"
    assert ("AI_BILLING_CONSISTENCY_INVALID", "$.billing_status") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


@pytest.mark.parametrize("warning", (42, None, "warning", [], {}))
def test_warning_items_are_strict_objects(warning):
    response = _response()
    response["warnings"] = [warning]
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    if warning == {}:
        assert ("AI_REQUIRED_FIELD_MISSING", "$.warnings[0].code") in pairs
    else:
        assert ("AI_SCHEMA_INVALID", "$.warnings[0]") in pairs


@pytest.mark.parametrize("field", ("code", "message"))
@pytest.mark.parametrize("value", ("", "   ", None, 7, True))
def test_warning_fields_are_nonempty_strings(field, value):
    response = _response()
    response["warnings"] = [{"code": "W", "message": "warning"}]
    response["warnings"][0][field] = value
    assert ("AI_SCHEMA_INVALID", f"$.warnings[0].{field}") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_warning_items_forbid_extra_fields():
    response = _response()
    response["warnings"] = [{"code": "W", "message": "warning", "raw": "secret"}]
    assert ("AI_EXTRA_FIELD", "$.warnings[0].raw") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_error_order_and_repeated_results_are_stable():
    response = _response()
    response["z_extra"] = True
    response["a_extra"] = True
    response["result"]["feature_progress"][0]["stage"] = "invalid"
    response["result"]["feature_progress"][0]["evidence_ids"] = ["unknown"]
    first = validate_ai_response(response, ALLOWED_EVIDENCE)
    second = validate_ai_response(response, ALLOWED_EVIDENCE)
    assert first == second
    assert list(first.issues) == sorted(
        first.issues,
        key=lambda issue: (issue.path, issue.code, issue.message, issue.severity),
    )


def test_validation_never_mutates_nested_input():
    response = _response("daily_report_regenerate")
    before = deepcopy(response)
    validate_ai_response(response, ALLOWED_EVIDENCE)
    assert response == before


def _wrapped(value, count):
    for _ in range(count):
        value = [value]
    return value


def test_depth_exactly_at_limit_does_not_raise_resource_error():
    response = _response()
    response["warnings"] = _wrapped("leaf", MAX_NESTING_DEPTH - 1)
    pairs = _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))
    assert all(code != "AI_RESOURCE_DEPTH_EXCEEDED" for code, _ in pairs)


def test_depth_over_limit_fails_without_recursion_error():
    response = _response()
    response["warnings"] = _wrapped("leaf", MAX_NESTING_DEPTH)
    assert (
        "AI_RESOURCE_DEPTH_EXCEEDED",
        "$.warnings" + "[0]" * MAX_NESTING_DEPTH,
    ) in _pairs(validate_ai_response(response, ALLOWED_EVIDENCE))


def test_node_count_over_limit_is_bounded_and_deterministic():
    response = _response()
    list_count = MAX_TRAVERSAL_NODES // MAX_ARRAY_LENGTH + 1
    response["node_probe"] = {
        f"k{index}": [0] * MAX_ARRAY_LENGTH for index in range(list_count)
    }
    first = validate_ai_response(response, ALLOWED_EVIDENCE)
    second = validate_ai_response(response, ALLOWED_EVIDENCE)
    assert first == second
    assert ("AI_RESOURCE_NODE_LIMIT_EXCEEDED", "$") in _pairs(first)


def test_array_length_over_limit_has_exact_path():
    response = _response()
    response["warnings"] = [0] * (MAX_ARRAY_LENGTH + 1)
    assert ("AI_RESOURCE_ARRAY_LIMIT_EXCEEDED", "$.warnings") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_mapping_field_count_over_limit_has_exact_path():
    response = _response()
    response["result"] = {f"k{index}": 0 for index in range(MAX_MAPPING_FIELDS + 1)}
    assert ("AI_RESOURCE_MAPPING_LIMIT_EXCEEDED", "$.result") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_string_length_over_limit_has_exact_path():
    response = _response()
    response["result"]["plain_summary"] = "x" * (MAX_STRING_LENGTH + 1)
    assert ("AI_RESOURCE_STRING_LIMIT_EXCEEDED", "$.result.plain_summary") in _pairs(
        validate_ai_response(response, ALLOWED_EVIDENCE)
    )


def test_self_referencing_dict_is_rejected_without_mutation():
    response = _response()
    result = response["result"]
    result["loop"] = result
    before_keys = tuple(result)
    validation = validate_ai_response(response, ALLOWED_EVIDENCE)
    assert ("AI_CYCLIC_REFERENCE", "$.result.loop") in _pairs(validation)
    assert tuple(result) == before_keys
    assert result["loop"] is result


def test_self_referencing_list_is_rejected_without_mutation():
    response = _response()
    warnings = []
    warnings.append(warnings)
    response["warnings"] = warnings
    validation = validate_ai_response(response, ALLOWED_EVIDENCE)
    assert ("AI_CYCLIC_REFERENCE", "$.warnings[0]") in _pairs(validation)
    assert len(warnings) == 1
    assert warnings[0] is warnings


@pytest.mark.parametrize(
    "unsafe_key",
    (
        "sensitive-absolute-path-C:/customers/private/" + "x" * 100,
        "secret\nvalue",
        42,
    ),
)
def test_unsafe_mapping_keys_use_fixed_nonleaking_path(unsafe_key):
    response = _response()
    response[unsafe_key] = "never echo this secret value"
    validation = validate_ai_response(response, ALLOWED_EVIDENCE)
    assert ("AI_MAPPING_KEY_INVALID", "$.<unsafe-key>") in _pairs(validation)
    rendered = repr(validation.issues)
    assert "customers/private" not in rendered
    assert "never echo this secret value" not in rendered
    assert "secret\\nvalue" not in rendered


def test_formal_report_eligibility_accepts_one_qualified_model_and_bound_authorization():
    result = _assess()
    assert result.eligible is True
    assert result.issues == ()


def test_dataclass_qualification_record_is_accepted():
    record = ModelQualificationRecord(**_qualification())
    assert _assess(qualification_record=record).eligible is True


def test_requested_model_mismatch_is_rejected():
    result = _assess(requested_model_id="model-b")
    assert result.eligible is False
    assert ("AI_MODEL_MISMATCH", "$.actual_model.id") in _pairs(result)


def test_required_provider_mismatch_is_rejected():
    result = _assess(required_provider="provider-b")
    assert result.eligible is False
    assert ("AI_MODEL_MISMATCH", "$.actual_model.provider") in _pairs(result)


@pytest.mark.parametrize(
    ("fallback", "code"),
    (
        (True, "AI_FALLBACK_FORBIDDEN"),
        (None, "AI_FALLBACK_TYPE_INVALID"),
        (0, "AI_FALLBACK_TYPE_INVALID"),
        (1, "AI_FALLBACK_TYPE_INVALID"),
        ("", "AI_FALLBACK_TYPE_INVALID"),
        ("false", "AI_FALLBACK_TYPE_INVALID"),
        ({}, "AI_FALLBACK_TYPE_INVALID"),
        ([], "AI_FALLBACK_TYPE_INVALID"),
    ),
)
def test_fallback_true_and_all_non_bool_types_fail_closed(fallback, code):
    result = _assess(fallback_occurred=fallback)
    assert result.eligible is False
    assert (code, "$.fallback_occurred") in _pairs(result)


def test_explicit_false_fallback_is_the_only_passing_value():
    assert _assess(fallback_occurred=False).eligible is True


@pytest.mark.parametrize(
    "field",
    (
        "provider",
        "model_id",
        "model_version",
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
        "qualification_status",
    ),
)
def test_each_qualification_binding_must_match(field):
    result = _assess(qualification_record=_qualification(**{field: "mismatch"}))
    assert result.eligible is False
    assert ("AI_MODEL_NOT_QUALIFIED", f"$.qualification_record.{field}") in _pairs(result)


def test_qualification_record_forbids_extra_fields():
    record = _qualification()
    record["extra"] = True
    result = _assess(qualification_record=record)
    assert result.eligible is False
    assert ("AI_MODEL_NOT_QUALIFIED", "$.qualification_record.extra") in _pairs(result)


def test_boolean_authorization_shortcut_is_rejected():
    result = _assess(data_sending_authorization=True)
    assert result.eligible is False
    assert ("AI_DATA_AUTHORIZATION_REQUIRED", "$.data_sending_authorization") in _pairs(result)


def test_authorization_provider_must_match_actual_provider():
    result = _assess(data_sending_authorization=_authorization(provider="provider-b"))
    assert result.eligible is False
    assert (
        "AI_DATA_AUTHORIZATION_REQUIRED",
        "$.data_sending_authorization.provider",
    ) in _pairs(result)


@pytest.mark.parametrize("field", ("provider", "authorized", "valid"))
def test_each_authorization_field_is_required(field):
    authorization = _authorization()
    del authorization[field]
    result = _assess(data_sending_authorization=authorization)
    assert result.eligible is False
    assert (
        "AI_DATA_AUTHORIZATION_REQUIRED",
        f"$.data_sending_authorization.{field}",
    ) in _pairs(result)


def test_authorization_forbids_extra_fields():
    authorization = _authorization()
    authorization["expires"] = "later"
    result = _assess(data_sending_authorization=authorization)
    assert result.eligible is False
    assert (
        "AI_DATA_AUTHORIZATION_REQUIRED",
        "$.data_sending_authorization.expires",
    ) in _pairs(result)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", ""),
        ("provider", "   "),
        ("authorized", 1),
        ("authorized", "true"),
        ("authorized", False),
        ("valid", 1),
        ("valid", "true"),
        ("valid", False),
    ),
)
def test_authorization_values_are_strict(field, value):
    result = _assess(data_sending_authorization=_authorization(**{field: value}))
    assert result.eligible is False
    assert (
        "AI_DATA_AUTHORIZATION_REQUIRED",
        f"$.data_sending_authorization.{field}",
    ) in _pairs(result)


def test_non_daily_task_cannot_obtain_formal_report_eligibility():
    response = _response("project_profile_build")
    result = _assess(response, output_schema_version="project-profile/1.0")
    assert result.eligible is False
    assert ("AI_TASK_RESULT_MISMATCH", "$.task_type") in _pairs(result)


def test_unsuccessful_task_cannot_obtain_formal_report_eligibility():
    response = _response()
    response["status"] = "unknown"
    result = _assess(response)
    assert result.eligible is False
    assert ("AI_SCHEMA_INVALID", "$.status") in _pairs(result)


def test_response_output_schema_must_match_eligibility_input():
    result = _assess(output_schema_version="daily-report/2.0")
    assert result.eligible is False
    assert ("AI_MODEL_NOT_QUALIFIED", "$.output_schema_version") in _pairs(result)


def test_eligibility_includes_contract_and_evidence_failures_and_preserves_input():
    response = _response()
    response["result"]["feature_progress"][0]["evidence_ids"] = ["unknown"]
    before = deepcopy(response)
    first = _assess(response)
    second = _assess(response)
    assert first == second
    assert first.eligible is False
    assert (
        "AI_EVIDENCE_UNKNOWN",
        "$.result.feature_progress[0].evidence_ids[0]",
    ) in _pairs(first)
    assert response == before
