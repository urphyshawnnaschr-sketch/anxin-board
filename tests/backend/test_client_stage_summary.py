"""Acceptance tests for client-facing stage summaries."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import client_stage_summary  # noqa: E402


FORMAL_STAGES = (
    "开发中",
    "等待联调",
    "等待测试",
    "测试中",
    "已完成",
    "暂时无法确认",
)


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


def _build(stage: str = "开发中"):
    return client_stage_summary.build_client_stage_summary(
        module_name="安心看板自动生成",
        stage=stage,
    )


def test_t01_entry_has_exact_two_keyword_only_parameters():
    sig = inspect.signature(client_stage_summary.build_client_stage_summary)
    assert list(sig.parameters) == ["module_name", "stage"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())


@pytest.mark.parametrize("stage", FORMAL_STAGES)
def test_t02_all_six_formal_stages_are_supported(stage):
    result = _build(stage)
    assert result["stage"] == stage
    assert result["display_stage"] == stage


def test_t03_developing_is_plain_language_and_actionable():
    result = _build("开发中")
    assert result["summary"] == "这项工作正在推进，已经进入实际开发。"
    assert "继续完成" in result["next_step"]


def test_t04_waiting_integration_explains_the_meaning_without_jargon():
    result = _build("等待联调")
    assert "相关功能连起来一起验证" in result["summary"]
    assert "联调" not in result["summary"]
    assert "联调" not in result["next_step"]


def test_t05_waiting_test_does_not_claim_testing_has_started():
    result = _build("等待测试")
    assert "等待进入完整检查" in result["summary"]
    assert result["tone"] == "waiting"


def test_t06_testing_explains_checking_in_client_language():
    result = _build("测试中")
    assert "检查阶段" in result["summary"]
    assert "是否稳定" in result["summary"]


def test_t07_done_is_explicitly_bounded_to_the_agreed_scope():
    result = _build("已完成")
    assert "当前约定范围内" in result["summary"]
    assert "已完成内容" in result["summary"]


def test_t08_unknown_never_turns_missing_evidence_into_no_change_or_done():
    result = _build("暂时无法确认")
    rendered = result["summary"] + result["next_step"]
    assert "信息还不足" in rendered
    assert "没有变化" not in rendered
    assert "已完成" not in rendered


def test_t09_unsupported_stage_fails_closed():
    with pytest.raises(client_stage_summary.ClientStageSummaryError):
        _build("返工中")


@pytest.mark.parametrize("module_name", ["", " 安心看板", "安心看板 ", "a" * 81, "安心\n看板"])
def test_t10_invalid_module_names_fail_closed(module_name):
    with pytest.raises(client_stage_summary.ClientStageSummaryError):
        client_stage_summary.build_client_stage_summary(
            module_name=module_name,
            stage="开发中",
        )


def test_t11_unicode_module_name_is_preserved_exactly():
    result = client_stage_summary.build_client_stage_summary(
        module_name="正式 AI 报告",
        stage="测试中",
    )
    assert result["module_name"] == "正式 AI 报告"


def test_t12_result_schema_is_exact_and_stable():
    result = _build()
    assert tuple(result) == (
        "schema_version",
        "module_name",
        "stage",
        "display_stage",
        "tone",
        "summary",
        "next_step",
        "client_stage_summary_hash",
    )
    assert result["schema_version"] == "client_stage_summary_v1"


def test_t13_hash_is_independently_recomputable():
    result = _build("等待测试")
    payload = {
        key: result[key]
        for key in result
        if key != "client_stage_summary_hash"
    }
    assert result["client_stage_summary_hash"] == _stable_hash(payload)


def test_t14_repeated_calls_are_deterministic():
    assert _build("测试中") == _build("测试中")


def test_t15_generated_client_copy_contains_no_percentage_or_score_language():
    for stage in FORMAL_STAGES:
        result = _build(stage)
        rendered = result["summary"] + result["next_step"]
        assert "%" not in rendered
        assert "百分比" not in rendered
        assert "完成度" not in rendered
        assert "评分" not in rendered


def test_t16_generated_client_copy_does_not_expose_engineering_workflow_terms():
    forbidden = (
        "PR",
        "CI",
        "SHA",
        "Token",
        "Framing",
        "commit",
        "diff",
        "runner",
        "provider",
    )
    for stage in FORMAL_STAGES:
        result = _build(stage)
        rendered = result["summary"] + result["next_step"]
        assert all(term not in rendered for term in forbidden)


def test_t17_tones_are_small_presentation_only_vocabulary():
    tones = {_build(stage)["tone"] for stage in FORMAL_STAGES}
    assert tones == {"active", "waiting", "checking", "done", "unknown"}


def test_t18_module_has_no_io_db_network_provider_or_ai_dependency():
    source = Path(client_stage_summary.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(
        {
            "sqlite3",
            "subprocess",
            "pathlib",
            "httpx",
            "requests",
            "socket",
            "smtplib",
            "openai",
            "anthropic",
        }
    )
