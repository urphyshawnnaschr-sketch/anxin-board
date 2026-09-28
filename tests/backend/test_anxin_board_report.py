from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from app.anxin_board_report import (
    AnxinBoardReportError,
    DEFAULT_MANAGER_SUPPLEMENT,
    MODULES,
    build_anxin_board_report,
)
from app.client_stage_summary import build_client_stage_summary
from app.plain_language_change_summary import build_plain_language_change_summary


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stage(name: str, stage: str = "开发中") -> dict[str, object]:
    return build_client_stage_summary(module_name=name, stage=stage)


def _modules(stages: dict[str, str] | None = None) -> list[dict[str, object]]:
    overrides = stages or {}
    return [_stage(name, overrides.get(module_id, "开发中")) for module_id, name in MODULES]


def _evidence(state: str = "ready") -> dict[str, object]:
    if state == "ready":
        evidence_state = "complete"
        has_change = True
        commit_count = 3
        changed_file_count = 6
        added_lines = 300
        deleted_lines = 120
        production = 3
        tests = 2
        ci = 1
        docs = operations = other = 0
        areas = [
            {"area": "backend", "file_count": 3},
            {"area": "backend_tests", "file_count": 2},
            {"area": "ci", "file_count": 1},
        ]
    elif state == "no_change":
        evidence_state = "complete_no_change"
        has_change = False
        commit_count = changed_file_count = added_lines = deleted_lines = 0
        production = tests = ci = docs = operations = other = 0
        areas = []
    elif state == "unavailable":
        evidence_state = "unavailable_checkpoint_unreachable"
        has_change = False
        commit_count = changed_file_count = added_lines = deleted_lines = 0
        production = tests = ci = docs = operations = other = 0
        areas = []
    elif state == "partial":
        evidence_state = "partial_capacity_exceeded"
        has_change = True
        commit_count = 4
        changed_file_count = 20
        added_lines = 1000
        deleted_lines = 200
        production = 10
        tests = 5
        ci = 2
        docs = operations = other = 1
        areas = [
            {"area": "backend", "file_count": 10},
            {"area": "backend_tests", "file_count": 5},
            {"area": "ci", "file_count": 2},
            {"area": "documentation", "file_count": 1},
            {"area": "installer", "file_count": 1},
            {"area": "other", "file_count": 1},
        ]
    else:
        raise AssertionError(f"unsupported test state: {state}")

    result: dict[str, object] = {
        "schema_version": "development_change_evidence_v1",
        "evidence_state": evidence_state,
        "has_change": has_change,
        "commit_count": commit_count,
        "changed_file_count": changed_file_count,
        "added_lines": added_lines,
        "deleted_lines": deleted_lines,
        "line_change_total": added_lines + deleted_lines,
        "line_change_metric_role": "supporting_evidence_only",
        "production_change_file_count": production,
        "test_change_file_count": tests,
        "ci_change_file_count": ci,
        "documentation_change_file_count": docs,
        "operations_change_file_count": operations,
        "other_change_file_count": other,
        "affected_areas": areas,
    }
    result["development_change_evidence_hash"] = _hash(result)
    return result


def _daily_change(state: str = "ready") -> dict[str, object]:
    return build_plain_language_change_summary(evidence=_evidence(state))


def _build(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "project_name": "研发进度 AI Agent",
        "report_date": "2026-08-21",
        "module_summaries": _modules(),
        "daily_change": _daily_change(),
    }
    args.update(overrides)
    return build_anxin_board_report(**args)


def test_t01_signature_is_keyword_only_and_keeps_manager_default() -> None:
    signature = inspect.signature(build_anxin_board_report)
    assert list(signature.parameters) == [
        "project_name",
        "report_date",
        "module_summaries",
        "daily_change",
        "manager_supplement",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    assert signature.parameters["manager_supplement"].default == DEFAULT_MANAGER_SUPPLEMENT


def test_t02_assembles_fixed_11_modules_in_fixed_order() -> None:
    result = _build()
    assert result["module_count"] == 11
    assert [(item["module_id"], item["name"]) for item in result["modules"]] == list(MODULES)


def test_t03_title_motto_project_and_date_are_fixed_or_preserved() -> None:
    result = _build(project_name="甲方 A 项目")
    assert result["title"] == "安心看板"
    assert result["motto"] == "非己所安，不加于物"
    assert result["project_name"] == "甲方 A 项目"
    assert result["report_date"] == "2026-08-21"


def test_t04_counts_completed_active_and_unknown_without_percentage() -> None:
    result = _build(
        module_summaries=_modules(
            {"git": "已完成", "prd": "已完成", "email": "暂时无法确认"}
        )
    )
    assert result["completed_module_count"] == 2
    assert result["unknown_module_count"] == 1
    assert result["active_module_count"] == 8
    assert "%" not in result["overall_message"]
    assert "百分比" not in result["overall_message"]


def test_t05_overall_message_uses_current_scope_and_exact_all_done_copy() -> None:
    partly_done = _build(module_summaries=_modules({"git": "已完成"}))
    assert "完成当前约定范围内" in partly_done["overall_message"]

    all_done = _build(
        module_summaries=_modules({module_id: "已完成" for module_id, _ in MODULES})
    )
    assert all_done["completed_module_count"] == 11
    assert all_done["active_module_count"] == 0
    assert all_done["unknown_module_count"] == 0
    assert all_done["overall_message"] == "当前约定范围已完成。"


def test_t06_unknown_stage_is_fail_visible_in_overall_message() -> None:
    result = _build(module_summaries=_modules({"email": "暂时无法确认"}))
    assert "暂时缺少足够依据" in result["overall_message"]
    assert "暂不下结论" in result["overall_message"]


def test_t07_default_manager_supplement_is_preserved() -> None:
    result = _build()
    assert result["manager_supplement"] == "同意大模型的日报。"


def test_t08_custom_plain_language_manager_supplement_is_allowed() -> None:
    result = _build(manager_supplement="今天的变化符合预期，继续按当前计划推进。")
    assert result["manager_supplement"] == "今天的变化符合预期，继续按当前计划推进。"


def test_t09_internal_engineering_terms_are_rejected_from_client_copy() -> None:
    modules = _modules()
    modules[0] = deepcopy(modules[0])
    modules[0]["summary"] = "今天 CI 已经通过。"
    modules[0]["client_stage_summary_hash"] = _hash(
        {key: value for key, value in modules[0].items() if key != "client_stage_summary_hash"}
    )
    with pytest.raises(AnxinBoardReportError):
        _build(module_summaries=modules)


@pytest.mark.parametrize(
    "supplement",
    [
        "今天大约完成 80%。",
        "今天完成率 80 分。",
        "今天质量评分 95 分。",
        "今天个人绩效评分 95 分。",
    ],
)
def test_t10_percentage_or_score_language_is_rejected_from_manager_supplement(
    supplement: str,
) -> None:
    with pytest.raises(AnxinBoardReportError):
        _build(manager_supplement=supplement)


def test_t11_stage_summary_hash_tamper_fails_closed() -> None:
    modules = _modules()
    modules[3]["summary"] = "被篡改的说明"
    with pytest.raises(AnxinBoardReportError):
        _build(module_summaries=modules)


def test_t12_stage_summary_name_or_order_drift_fails_closed() -> None:
    modules = _modules()
    modules[0], modules[1] = modules[1], modules[0]
    with pytest.raises(AnxinBoardReportError):
        _build(module_summaries=modules)


def test_t13_requires_exactly_11_modules() -> None:
    with pytest.raises(AnxinBoardReportError):
        _build(module_summaries=_modules()[:-1])


def test_t14_daily_change_states_come_from_formal_producer_and_preserve_state() -> None:
    for state in ("ready", "no_change", "unavailable", "partial"):
        produced = _daily_change(state)
        result = _build(daily_change=produced)
        assert result["daily_change"]["display_state"] == state
        assert result["daily_change"]["headline"] == produced["headline"]
        assert result["daily_change"]["summary"] == produced["summary"]
        assert "source_evidence_hash" not in result["daily_change"]


def test_t15_daily_change_hash_tamper_fails_closed() -> None:
    change = _daily_change()
    change["headline"] = "被篡改"
    with pytest.raises(AnxinBoardReportError):
        _build(daily_change=change)


def test_t16_invalid_date_blank_or_forbidden_project_name_fails_closed() -> None:
    with pytest.raises(AnxinBoardReportError):
        _build(report_date="2026-02-30")
    with pytest.raises(AnxinBoardReportError):
        _build(project_name="")
    with pytest.raises(AnxinBoardReportError):
        _build(project_name="CI 内部项目")
    with pytest.raises(AnxinBoardReportError):
        _build(project_name="质量评分项目")


def test_t17_result_schema_is_exact() -> None:
    result = _build()
    assert list(result) == [
        "schema_version",
        "title",
        "motto",
        "project_name",
        "report_date",
        "module_count",
        "completed_module_count",
        "active_module_count",
        "unknown_module_count",
        "overall_message",
        "modules",
        "daily_change",
        "manager_supplement",
        "anxin_board_report_hash",
    ]
    assert result["schema_version"] == "anxin_board_report_v1"


def test_t18_hash_is_independently_recomputable_and_deterministic() -> None:
    first = _build()
    second = _build()
    payload = {key: deepcopy(value) for key, value in first.items() if key != "anxin_board_report_hash"}
    assert first["anxin_board_report_hash"] == _hash(payload)
    assert first == second


def test_t19_inputs_are_not_mutated() -> None:
    modules = _modules()
    change = _daily_change()
    modules_before = deepcopy(modules)
    change_before = deepcopy(change)
    _build(module_summaries=modules, daily_change=change)
    assert modules == modules_before
    assert change == change_before


def test_t20_module_has_no_io_db_git_network_provider_or_ai_dependency() -> None:
    source = Path("apps/backend/app/anxin_board_report.py").read_text(encoding="utf-8")
    forbidden = (
        "requests",
        "httpx",
        "socket",
        "subprocess",
        "sqlite3",
        "sqlalchemy",
        "openai",
        "anthropic",
        "gitpython",
        "pathlib",
        "open(",
    )
    lowered = source.lower()
    assert all(term not in lowered for term in forbidden)
