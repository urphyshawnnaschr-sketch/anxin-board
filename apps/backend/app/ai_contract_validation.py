"""Deterministic validation for AI responses and formal-report eligibility."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
import re
from typing import Any

from .ai_contracts import (
    AI_CONTRACT_SCHEMA_VERSION,
    AUTHORIZATION_FIELDS,
    BILLING_STATUSES,
    COST_FIELDS,
    ENVELOPE_FIELDS,
    IMPLEMENTATION_SCOPES,
    MAX_ARRAY_LENGTH,
    MAX_COST_AMOUNT,
    MAX_MAPPING_FIELDS,
    MAX_NESTING_DEPTH,
    MAX_SAFE_PATH_KEY_LENGTH,
    MAX_STRING_LENGTH,
    MAX_TRAVERSAL_NODES,
    MODEL_IDENTITY_FIELDS,
    NESTED_OBJECT_SCHEMAS,
    QUALIFICATION_FIELDS,
    RESULT_ARRAY_ITEM_SCHEMAS,
    RESULT_FIELD_RULES,
    RESULT_FIELDS,
    RISK_LEVELS,
    SOURCE_TYPES,
    STAGES,
    STATUSES,
    TASK_TYPES,
    UNKNOWN_VALUE,
    USAGE_FIELDS,
    WARNING_FIELDS,
    FieldRule,
    FormalEligibilityResult,
    ModelQualificationRecord,
    ObjectSchema,
    ValidationIssue,
    ValidationResult,
    stable_issues,
)


_SAFE_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROGRESS_VALUE_PATTERN = (
    r"(?:\d+(?:\.\d+)?\s*(?:[%％]|pct\b|percent\b)|"
    r"百分之[零〇一二两三四五六七八九十百\d]+|"
    r"[零〇一二两三四五六七八九十百\d]+成)"
)
_PROGRESS_PATTERN = re.compile(
    rf"(?:(?:项目|整体|当前(?:项目|开发|任务)?|开发|任务).{{0,8}}"
    rf"(?:进度|进展|完成(?:进度|率|度)?|推进(?:到)?|做到|到|达到)|完成度)"
    rf".{{0,8}}{_PROGRESS_VALUE_PATTERN}"
    r"|(?:project\s+)?progress.{0,10}\d+(?:\.\d+)?\s*(?:[%％]|pct\b|percent\b)",
    re.IGNORECASE,
)
_AUDIT_PATTERN = re.compile(
    r"独立审计(?:结论)?|第三方审计(?:结论)?|审计结论|官方认证|权威认证|"
    r"模型厂商.{0,8}背书|independent\s+audit|third[- ]party\s+audit|"
    r"official(?:ly)?\s+certif(?:ied|ication)",
    re.IGNORECASE,
)
_CANDIDATE_APPLY_PATTERN = re.compile(
    r"候选.{0,8}(?:已|已经)(?:被)?(?:正式|明确|直接|自动)?"
    r"(?:采纳|采用|确认|生效|应用|入库)|"
    r"(?:已|已经)(?:被)?(?:正式|明确|直接|自动)?"
    r"(?:写入|写进|纳入).{0,4}正式项目档案|"
    r"(?:已|已经)(?:被)?(?:正式|明确|直接)?自动(?:应用|采用|生效)|"
    r"(?:已|已经)确认(?:应用|采用)|"
    r"自动(?:正式|明确|直接)?(?:应用|采用|生效|写入正式项目档案)",
    re.IGNORECASE,
)
_AUTO_EDIT_PATTERN = re.compile(
    r"(?:AI原文|原文).{0,8}(?:已|已经)(?:被)?(?:正式|明确|直接|自动)?"
    r"(?:修改|改写|修正|更新|调整|改动|替换|覆盖)|"
    r"系统(?:已|已经)(?:正式|明确|直接|自动)?"
    r"(?:修改|改写|修正|更新|调整|改动|替换|覆盖).{0,8}"
    r"(?:AI原文|原文|项目经理补充)|"
    r"(?:已|已经)自动(?:修改|改写|修正|更新|调整|改动|替换|覆盖)"
    r".{0,12}(?:AI原文|原文|项目经理补充)|"
    r"自动(?:正式|明确|直接)?(?:修改|改写|修正|更新|调整|改动|替换|覆盖)"
    r"(?:AI原文|原文|项目经理补充)",
    re.IGNORECASE,
)
_NEGATION_SUFFIX = re.compile(
    r"(?:尚无|尚未(?:经过|获得|得到|被)?|未(?:经|经过|获|获得|得到|被)?|"
    r"没有(?:经过|获得|得到|被)?|不会(?:被)?|不得(?:声称)?|"
    r"不代表|不提供|并非|非|not(?:\s+an?)?|never|without)"
    r"(?:[\s的]|任何(?:形式的)?|正式|实际|真正|明确|直接|相关|有效|最终){0,4}$",
    re.IGNORECASE,
)

_SAFETY_CODES = frozenset(
    {
        "AI_RESOURCE_DEPTH_EXCEEDED",
        "AI_RESOURCE_NODE_LIMIT_EXCEEDED",
        "AI_RESOURCE_ARRAY_LIMIT_EXCEEDED",
        "AI_RESOURCE_MAPPING_LIMIT_EXCEEDED",
        "AI_RESOURCE_STRING_LIMIT_EXCEEDED",
        "AI_CYCLIC_REFERENCE",
        "AI_MAPPING_KEY_INVALID",
    }
)
_DAILY_CONCLUSION_FIELDS = (
    "feature_progress",
    "code_change_summary",
    "test_evidence",
    "risks",
    "unknown_items",
    "source_warnings",
)


@dataclass(frozen=True, slots=True)
class _TraversalNode:
    path: str
    value: Any
    key: str | None


def _issue(code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message, severity="error")


def _warning(code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message, severity="warning")


def _is_non_empty_string(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _safe_key(key: Any) -> bool:
    return (
        type(key) is str
        and len(key) <= MAX_SAFE_PATH_KEY_LENGTH
        and _SAFE_KEY_PATTERN.fullmatch(key) is not None
    )


def _child_path(path: str, key: Any) -> str:
    return f"{path}.{key}" if _safe_key(key) else f"{path}.<unsafe-key>"


def _ordered_items(value: Mapping[Any, Any]) -> list[tuple[Any, Any]]:
    indexed = list(enumerate(value.items()))

    def order(entry: tuple[int, tuple[Any, Any]]) -> tuple[object, ...]:
        index, (key, _) = entry
        if type(key) is str:
            return (0, key[:MAX_SAFE_PATH_KEY_LENGTH], len(key), index)
        return (1, type(key).__name__, index)

    return [item for _, item in sorted(indexed, key=order)]


def _bounded_inspect(root: Any, root_path: str = "$") -> tuple[list[ValidationIssue], tuple[_TraversalNode, ...]]:
    """Inspect without recursion and stop deterministically at registered limits."""

    issues: list[ValidationIssue] = []
    nodes: list[_TraversalNode] = []
    stack: list[tuple[Any, str, str | None, int, frozenset[int]]] = [
        (root, root_path, None, 0, frozenset())
    ]
    visited_nodes = 0

    while stack:
        value, path, key, depth, ancestors = stack.pop()
        if depth > MAX_NESTING_DEPTH:
            issues.append(
                _issue(
                    "AI_RESOURCE_DEPTH_EXCEEDED",
                    path,
                    "输入嵌套深度超过契约上限。",
                )
            )
            continue

        visited_nodes += 1
        if visited_nodes > MAX_TRAVERSAL_NODES:
            issues.append(
                _issue(
                    "AI_RESOURCE_NODE_LIMIT_EXCEEDED",
                    root_path,
                    "输入节点数量超过契约上限。",
                )
            )
            break

        nodes.append(_TraversalNode(path=path, value=value, key=key))
        if type(value) is str and len(value) > MAX_STRING_LENGTH:
            issues.append(
                _issue(
                    "AI_RESOURCE_STRING_LIMIT_EXCEEDED",
                    path,
                    "字符串长度超过契约上限。",
                )
            )
            continue

        if isinstance(value, Mapping):
            identity = id(value)
            if identity in ancestors:
                issues.append(_issue("AI_CYCLIC_REFERENCE", path, "输入包含循环引用。"))
                continue
            if len(value) > MAX_MAPPING_FIELDS:
                issues.append(
                    _issue(
                        "AI_RESOURCE_MAPPING_LIMIT_EXCEEDED",
                        path,
                        "对象字段数量超过契约上限。",
                    )
                )
                continue
            next_ancestors = ancestors | {identity}
            children = []
            for child_key, child_value in _ordered_items(value):
                child_path = _child_path(path, child_key)
                if not _safe_key(child_key):
                    issues.append(
                        _issue(
                            "AI_MAPPING_KEY_INVALID",
                            child_path,
                            "对象字段名不符合安全路径规则。",
                        )
                    )
                    continue
                children.append(
                    (child_value, child_path, child_key, depth + 1, next_ancestors)
                )
            stack.extend(reversed(children))
        elif type(value) is list:
            identity = id(value)
            if identity in ancestors:
                issues.append(_issue("AI_CYCLIC_REFERENCE", path, "输入包含循环引用。"))
                continue
            if len(value) > MAX_ARRAY_LENGTH:
                issues.append(
                    _issue(
                        "AI_RESOURCE_ARRAY_LIMIT_EXCEEDED",
                        path,
                        "数组长度超过契约上限。",
                    )
                )
                continue
            next_ancestors = ancestors | {identity}
            for index in range(len(value) - 1, -1, -1):
                stack.append(
                    (
                        value[index],
                        f"{path}[{index}]",
                        None,
                        depth + 1,
                        next_ancestors,
                    )
                )

    return issues, tuple(nodes)


def _required_and_extra_fields(
    value: Mapping[Any, Any], required: tuple[str, ...], path: str, *,
    missing_code: str = "AI_REQUIRED_FIELD_MISSING",
    extra_code: str = "AI_EXTRA_FIELD",
) -> list[ValidationIssue]:
    issues = []
    for field in required:
        if field not in value:
            issues.append(_issue(missing_code, f"{path}.{field}", "缺少必填字段。"))
    extras = [key for key in value if key not in required]
    extras.sort(key=lambda key: (0, key) if type(key) is str else (1, type(key).__name__))
    for field in extras:
        issues.append(_issue(extra_code, _child_path(path, field), "字段未在契约中登记。"))
    return issues


def _enum_error(field: str) -> tuple[str, str]:
    if field == "stage":
        return "AI_STAGE_INVALID", "阶段值无效。"
    if field == "source_type":
        return "AI_SOURCE_TYPE_INVALID", "来源类型无效。"
    if field == "implementation_scope":
        return "AI_SCOPE_INVALID", "实施范围无效。"
    return "AI_ENUM_INVALID", "枚举值无效。"


def _validate_field_rule(
    field: str, value: Any, path: str, rule: FieldRule
) -> list[ValidationIssue]:
    issues = []
    if rule.kind == "string":
        if type(value) is not str or (rule.non_empty and not value.strip()):
            issues.append(_issue("AI_SCHEMA_INVALID", path, "字段必须是非空字符串。"))
        elif rule.allowed_values and value not in rule.allowed_values:
            code, message = _enum_error(field)
            issues.append(_issue(code, path, message))
    elif rule.kind == "bool":
        if type(value) is not bool:
            issues.append(_issue("AI_SCHEMA_INVALID", path, "字段必须是bool。"))
        elif rule.constant is not None and value is not rule.constant:
            issues.append(_issue("AI_SCHEMA_INVALID", path, "字段值不符合契约。"))
    elif rule.kind == "array":
        if type(value) is not list:
            issues.append(_issue("AI_SCHEMA_INVALID", path, "字段必须是数组。"))
        elif len(value) < rule.min_items:
            issues.append(_issue("AI_REQUIRED_FIELD_MISSING", path, "数组不能为空。"))
    elif rule.kind == "object":
        if not isinstance(value, Mapping):
            issues.append(_issue("AI_SCHEMA_INVALID", path, "字段必须是对象。"))
    elif rule.kind == "string_list":
        if type(value) is not list:
            issues.append(_issue("AI_SCHEMA_INVALID", path, "字段必须是字符串数组。"))
        else:
            if len(value) < rule.min_items:
                code = "AI_EVIDENCE_REQUIRED" if "evidence" in field else "AI_REQUIRED_FIELD_MISSING"
                issues.append(_issue(code, path, "数组不能为空。"))
            for index, item in enumerate(value):
                if type(item) is not str or (rule.non_empty and not item.strip()):
                    issues.append(
                        _issue(
                            "AI_SCHEMA_INVALID",
                            f"{path}[{index}]",
                            "数组项必须是非空字符串。",
                        )
                    )
    return issues


def _validate_nested_object(
    value: Any, path: str, schema: ObjectSchema
) -> list[ValidationIssue]:
    if not isinstance(value, Mapping):
        return [_issue("AI_SCHEMA_INVALID", path, "数组项必须是对象。")]

    issues = _required_and_extra_fields(value, schema.required_fields, path)
    if schema.evidence_field and schema.evidence_field not in value:
        issues.append(
            _issue(
                "AI_EVIDENCE_REQUIRED",
                f"{path}.{schema.evidence_field}",
                "结论项必须引用证据。",
            )
        )
    for field, rule in schema.fields:
        if field in value:
            issues.extend(_validate_field_rule(field, value[field], f"{path}.{field}", rule))
    return issues


def _validate_result_contract(
    task_type: str, result: Mapping[Any, Any], path: str = "$.result"
) -> list[ValidationIssue]:
    rules = RESULT_FIELD_RULES[task_type]
    required = RESULT_FIELDS[task_type]
    issues = _required_and_extra_fields(result, required, path)

    matching_contracts = [
        candidate
        for candidate, fields in RESULT_FIELDS.items()
        if set(result) == set(fields)
    ]
    if matching_contracts and task_type not in matching_contracts:
        issues.append(
            _issue("AI_TASK_RESULT_MISMATCH", path, "result结构与task_type不匹配。")
        )

    for field, rule in rules:
        if field in result:
            issues.extend(_validate_field_rule(field, result[field], f"{path}.{field}", rule))

    if task_type == "project_profile_build" and "evidence_refs" not in result:
        issues.append(
            _issue(
                "AI_EVIDENCE_REQUIRED",
                f"{path}.evidence_refs",
                "项目档案摘要必须引用证据。",
            )
        )

    array_schemas = RESULT_ARRAY_ITEM_SCHEMAS[task_type]
    for field, schema_name in array_schemas.items():
        items = result.get(field)
        if type(items) is not list:
            continue
        schema = NESTED_OBJECT_SCHEMAS[schema_name]
        for index, item in enumerate(items):
            issues.extend(
                _validate_nested_object(item, f"{path}.{field}[{index}]", schema)
            )

    if task_type == "daily_report_regenerate":
        new_report = result.get("new_report")
        if isinstance(new_report, Mapping):
            issues.extend(
                _validate_result_contract(
                    "daily_report_generate", new_report, f"{path}.new_report"
                )
            )
    elif task_type == "report_contradiction_check":
        has_conflict = result.get("has_conflict")
        conflicts = result.get("conflicts")
        if type(has_conflict) is bool and type(conflicts) is list:
            if has_conflict and not conflicts:
                issues.append(
                    _issue(
                        "AI_SCHEMA_INVALID",
                        f"{path}.conflicts",
                        "has_conflict为True时conflicts不能为空。",
                    )
                )
            elif not has_conflict and conflicts:
                issues.append(
                    _issue(
                        "AI_SCHEMA_INVALID",
                        f"{path}.conflicts",
                        "has_conflict为False时conflicts必须为空。",
                    )
                )
    return issues


def _validate_evidence_closure(
    nodes: tuple[_TraversalNode, ...], allowed_evidence_ids: frozenset[str]
) -> list[ValidationIssue]:
    issues = []
    for node in nodes:
        if not node.path.startswith("$.result"):
            continue
        if node.key == "evidence_id":
            references = ((node.path, node.value),)
        elif node.key in ("evidence_ids", "evidence_refs"):
            if type(node.value) is not list:
                continue
            references = tuple(
                (f"{node.path}[{index}]", item)
                for index, item in enumerate(node.value)
            )
        else:
            continue
        for reference_path, reference in references:
            if not _is_non_empty_string(reference):
                continue
            if reference not in allowed_evidence_ids:
                issues.append(
                    _issue(
                        "AI_EVIDENCE_UNKNOWN",
                        reference_path,
                        "证据引用不在调用方允许集合中。",
                    )
                )
    return issues


def _is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 32) : start]
    return _NEGATION_SUFFIX.search(prefix) is not None


def _has_affirmative_claim(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _is_negated(text, match.start()) for match in pattern.finditer(text))


def _validate_text_redlines(
    task_type: str, nodes: tuple[_TraversalNode, ...]
) -> list[ValidationIssue]:
    issues = []
    for node in nodes:
        if not node.path.startswith("$.result") or type(node.value) is not str:
            continue
        text = node.value
        if _PROGRESS_PATTERN.search(text):
            issues.append(
                _warning(
                    "AI_PERCENTAGE_FORBIDDEN",
                    node.path,
                    "检测到可能的项目进度百分比表达，请项目经理人工确认。",
                )
            )
        if _has_affirmative_claim(_AUDIT_PATTERN, text):
            issues.append(
                _warning(
                    "AI_AUDIT_CLAIM_FORBIDDEN",
                    node.path,
                    "检测到可能的审计、认证或背书表述，请项目经理人工确认。",
                )
            )
        if task_type == "project_profile_build" and _has_affirmative_claim(
            _CANDIDATE_APPLY_PATTERN, text
        ):
            issues.append(
                _warning(
                    "AI_CANDIDATE_AUTO_APPLY_FORBIDDEN",
                    node.path,
                    "检测到候选可能被描述为已经确认或生效，请项目经理人工确认。",
                )
            )
        if task_type == "report_contradiction_check" and _has_affirmative_claim(
            _AUTO_EDIT_PATTERN, text
        ):
            issues.append(
                _warning(
                    "AI_CONTRADICTION_AUTO_EDIT_FORBIDDEN",
                    node.path,
                    "检测到可能声称已经修改原文，请项目经理人工确认。",
                )
            )
    return issues


def _validate_model_identity(value: Any) -> list[ValidationIssue]:
    if not isinstance(value, Mapping):
        code = "AI_MODEL_MISMATCH" if type(value) is list else "AI_MODEL_IDENTITY_MISSING"
        return [_issue(code, "$.actual_model", "实际模型身份结构无效。")]
    issues = _required_and_extra_fields(
        value,
        MODEL_IDENTITY_FIELDS,
        "$.actual_model",
        missing_code="AI_MODEL_IDENTITY_MISSING",
    )
    for field in MODEL_IDENTITY_FIELDS:
        if field in value and not _is_non_empty_string(value[field]):
            issues.append(
                _issue(
                    "AI_MODEL_IDENTITY_MISSING",
                    f"$.actual_model.{field}",
                    "实际模型身份字段缺失。",
                )
            )
    return issues


def _validate_usage(value: Any) -> list[ValidationIssue]:
    if value == UNKNOWN_VALUE:
        return []
    if not isinstance(value, Mapping):
        return [_issue("AI_SCHEMA_INVALID", "$.usage", "usage契约无效。")]
    issues = _required_and_extra_fields(value, USAGE_FIELDS, "$.usage")
    for field in USAGE_FIELDS:
        if field in value and (type(value[field]) is not int or value[field] < 0):
            issues.append(
                _issue(
                    "AI_SCHEMA_INVALID",
                    f"$.usage.{field}",
                    "Token数量必须是非负整数且不能是bool。",
                )
            )
    return issues


def _validate_cost_and_billing(cost: Any, billing_status: Any) -> list[ValidationIssue]:
    issues = []
    if billing_status not in BILLING_STATUSES:
        issues.append(
            _issue(
                "AI_ENUM_INVALID",
                "$.billing_status",
                "billing_status枚举无效。",
            )
        )

    if cost == UNKNOWN_VALUE:
        if billing_status == "known":
            issues.append(
                _issue(
                    "AI_BILLING_CONSISTENCY_INVALID",
                    "$.billing_status",
                    "未知费用必须使用unknown计费状态。",
                )
            )
        return issues
    if not isinstance(cost, Mapping):
        issues.append(_issue("AI_SCHEMA_INVALID", "$.cost", "cost契约无效。"))
        return issues

    issues.extend(_required_and_extra_fields(cost, COST_FIELDS, "$.cost"))
    amount = cost.get("amount")
    if type(amount) is int:
        amount_is_valid = 0 <= amount <= MAX_COST_AMOUNT
    elif type(amount) is float:
        amount_is_valid = math.isfinite(amount) and 0 <= amount <= MAX_COST_AMOUNT
    else:
        amount_is_valid = False
    if not amount_is_valid:
        issues.append(
            _issue(
                "AI_SCHEMA_INVALID",
                "$.cost.amount",
                "费用必须是契约上限内的有限非负数且不能是bool。",
            )
        )
    if not _is_non_empty_string(cost.get("currency")):
        issues.append(
            _issue("AI_SCHEMA_INVALID", "$.cost.currency", "币种必须是非空字符串。")
        )
    if billing_status == "unknown":
        issues.append(
            _issue(
                "AI_BILLING_CONSISTENCY_INVALID",
                "$.billing_status",
                "已知费用对象必须使用known计费状态。",
            )
        )
    return issues


def _validate_warnings(value: Any) -> list[ValidationIssue]:
    if type(value) is not list:
        return [_issue("AI_SCHEMA_INVALID", "$.warnings", "warnings必须是数组。")]
    issues = []
    for index, warning in enumerate(value):
        path = f"$.warnings[{index}]"
        if not isinstance(warning, Mapping):
            issues.append(_issue("AI_SCHEMA_INVALID", path, "warning项必须是对象。"))
            continue
        issues.extend(_required_and_extra_fields(warning, WARNING_FIELDS, path))
        for field in WARNING_FIELDS:
            if field in warning and not _is_non_empty_string(warning[field]):
                issues.append(
                    _issue(
                        "AI_SCHEMA_INVALID",
                        f"{path}.{field}",
                        "warning字段必须是非空字符串。",
                    )
                )
    return issues


def _validate_envelope(response: Mapping[Any, Any]) -> list[ValidationIssue]:
    issues = _required_and_extra_fields(response, ENVELOPE_FIELDS, "$")
    for field in ("local_task_id", "provider_request_id", "output_schema_version"):
        if field in response and not _is_non_empty_string(response[field]):
            issues.append(_issue("AI_SCHEMA_INVALID", f"$.{field}", "字段必须是非空字符串。"))
    if response.get("schema_version") != AI_CONTRACT_SCHEMA_VERSION:
        issues.append(_issue("AI_SCHEMA_INVALID", "$.schema_version", "契约版本无效。"))
    if response.get("task_type") not in TASK_TYPES:
        issues.append(_issue("AI_ENUM_INVALID", "$.task_type", "任务类型无效。"))
    if response.get("status") not in STATUSES:
        issues.append(_issue("AI_ENUM_INVALID", "$.status", "任务状态无效。"))
    if "actual_model" in response:
        issues.extend(_validate_model_identity(response["actual_model"]))
    if "usage" in response:
        issues.extend(_validate_usage(response["usage"]))
    if "cost" in response and "billing_status" in response:
        issues.extend(
            _validate_cost_and_billing(response["cost"], response["billing_status"])
        )
    if "warnings" in response:
        issues.extend(_validate_warnings(response["warnings"]))
    return issues


def _allowed_evidence_set(value: Any) -> tuple[list[ValidationIssue], frozenset[str]]:
    if type(value) not in (set, frozenset) or any(
        not _is_non_empty_string(item) for item in value
    ):
        return (
            [_issue("AI_SCHEMA_INVALID", "$.allowed_evidence_ids", "允许证据ID集合无效。")],
            frozenset(),
        )
    return [], frozenset(value)


def validate_ai_response(
    response: Mapping[str, Any] | Any, allowed_evidence_ids: set[str]
) -> ValidationResult:
    """Validate without mutation, recursion, external state, or unstable ordering."""

    safety_issues, nodes = _bounded_inspect(response)
    if safety_issues:
        return ValidationResult(stable_issues(safety_issues))
    if not isinstance(response, Mapping):
        return ValidationResult((_issue("AI_SCHEMA_INVALID", "$", "AI响应必须是对象。"),))

    issues = _validate_envelope(response)
    evidence_set_issues, allowed = _allowed_evidence_set(allowed_evidence_ids)
    issues.extend(evidence_set_issues)

    task_type = response.get("task_type")
    result = response.get("result")
    if task_type in TASK_TYPES:
        if not isinstance(result, Mapping):
            issues.append(_issue("AI_SCHEMA_INVALID", "$.result", "result必须是对象。"))
        else:
            issues.extend(_validate_result_contract(task_type, result))
            issues.extend(_validate_evidence_closure(nodes, allowed))
            issues.extend(_validate_text_redlines(task_type, nodes))
    return ValidationResult(stable_issues(issues))


def _qualification_mapping(
    record: Mapping[str, Any] | ModelQualificationRecord | None,
) -> Mapping[str, Any] | None:
    if isinstance(record, ModelQualificationRecord):
        return asdict(record)
    return record if isinstance(record, Mapping) else None


def _validate_qualification(
    record: Mapping[str, Any] | ModelQualificationRecord | None,
    expected: Mapping[str, Any],
) -> list[ValidationIssue]:
    qualification = _qualification_mapping(record)
    if qualification is None:
        return [
            _issue(
                "AI_MODEL_NOT_QUALIFIED",
                "$.qualification_record",
                "缺少有效的模型资格记录。",
            )
        ]
    issues = _required_and_extra_fields(
        qualification,
        QUALIFICATION_FIELDS,
        "$.qualification_record",
        missing_code="AI_MODEL_NOT_QUALIFIED",
        extra_code="AI_MODEL_NOT_QUALIFIED",
    )
    for field in QUALIFICATION_FIELDS:
        if field not in qualification:
            continue
        value = qualification[field]
        if not _is_non_empty_string(value) or value != expected[field]:
            issues.append(
                _issue(
                    "AI_MODEL_NOT_QUALIFIED",
                    f"$.qualification_record.{field}",
                    "模型资格记录与当前正式报告条件不一致。",
                )
            )
    return issues


def _validate_authorization(authorization: Any, provider: Any) -> list[ValidationIssue]:
    path = "$.data_sending_authorization"
    if not isinstance(authorization, Mapping):
        return [
            _issue(
                "AI_DATA_AUTHORIZATION_REQUIRED",
                path,
                "必须提供绑定实际provider的授权对象。",
            )
        ]
    issues = _required_and_extra_fields(
        authorization,
        AUTHORIZATION_FIELDS,
        path,
        missing_code="AI_DATA_AUTHORIZATION_REQUIRED",
        extra_code="AI_DATA_AUTHORIZATION_REQUIRED",
    )
    if "provider" in authorization and (
        not _is_non_empty_string(authorization["provider"])
        or authorization["provider"] != provider
    ):
        issues.append(
            _issue(
                "AI_DATA_AUTHORIZATION_REQUIRED",
                f"{path}.provider",
                "授权provider必须与实际provider一致。",
            )
        )
    for field in ("authorized", "valid"):
        if field in authorization and (
            type(authorization[field]) is not bool or authorization[field] is not True
        ):
            issues.append(
                _issue(
                    "AI_DATA_AUTHORIZATION_REQUIRED",
                    f"{path}.{field}",
                    "授权状态必须严格为True。",
                )
            )
    return issues


def _has_formal_conclusion(response: Mapping[str, Any]) -> bool:
    result = response.get("result")
    if not isinstance(result, Mapping):
        return False
    if response.get("task_type") == "daily_report_regenerate":
        result = result.get("new_report")
        if not isinstance(result, Mapping):
            return False
    return any(type(result.get(field)) is list and bool(result[field]) for field in _DAILY_CONCLUSION_FIELDS)


def assess_formal_report_eligibility(
    response: Mapping[str, Any] | Any,
    *,
    requested_model_id: str,
    required_provider: str,
    qualification_record: Mapping[str, Any] | ModelQualificationRecord | None,
    data_sending_authorization: Mapping[str, Any] | Any,
    fallback_occurred: bool,
    rule_version: str,
    output_schema_version: str,
    benchmark_sample_pack_version: str,
    allowed_evidence_ids: set[str],
) -> FormalEligibilityResult:
    """Fail closed unless every formal-report qualification condition passes."""

    validation = validate_ai_response(response, allowed_evidence_ids)
    issues = list(validation.issues)
    if not isinstance(response, Mapping):
        return FormalEligibilityResult(False, stable_issues(issues))

    if response.get("task_type") not in ("daily_report_generate", "daily_report_regenerate"):
        issues.append(
            _issue(
                "AI_TASK_RESULT_MISMATCH",
                "$.task_type",
                "只有日报生成或重分析任务可以申请正式报告资格。",
            )
        )
    if response.get("status") != "succeeded":
        issues.append(_issue("AI_SCHEMA_INVALID", "$.status", "正式报告要求任务成功。"))
    if not _has_formal_conclusion(response):
        issues.append(
            _issue(
                "AI_FORMAL_REPORT_CONTENT_REQUIRED",
                "$.result",
                "正式报告至少需要一个通过证据校验的结构化结论。",
            )
        )

    actual_model = response.get("actual_model")
    actual = actual_model if isinstance(actual_model, Mapping) else {}
    if _is_non_empty_string(actual.get("id")) and actual.get("id") != requested_model_id:
        issues.append(
            _issue("AI_MODEL_MISMATCH", "$.actual_model.id", "实际模型与请求模型不一致。")
        )
    if _is_non_empty_string(actual.get("provider")) and actual.get("provider") != required_provider:
        issues.append(
            _issue(
                "AI_MODEL_MISMATCH",
                "$.actual_model.provider",
                "实际provider与必需provider不一致。",
            )
        )

    if type(fallback_occurred) is not bool:
        issues.append(
            _issue(
                "AI_FALLBACK_TYPE_INVALID",
                "$.fallback_occurred",
                "fallback_occurred必须严格为bool。",
            )
        )
    elif fallback_occurred is True:
        issues.append(
            _issue("AI_FALLBACK_FORBIDDEN", "$.fallback_occurred", "正式报告禁止fallback。")
        )

    expected_qualification = {
        "provider": actual.get("provider"),
        "model_id": actual.get("id"),
        "model_version": actual.get("version"),
        "rule_version": rule_version,
        "output_schema_version": output_schema_version,
        "benchmark_sample_pack_version": benchmark_sample_pack_version,
        "qualification_status": "qualified",
    }
    issues.extend(_validate_qualification(qualification_record, expected_qualification))
    issues.extend(
        _validate_authorization(data_sending_authorization, actual.get("provider"))
    )
    if response.get("output_schema_version") != output_schema_version:
        issues.append(
            _issue(
                "AI_MODEL_NOT_QUALIFIED",
                "$.output_schema_version",
                "响应输出Schema版本与资格门版本不一致。",
            )
        )

    ordered = stable_issues(issues)
    return FormalEligibilityResult(
        eligible=not any(issue.severity == "error" for issue in ordered),
        issues=ordered,
    )
