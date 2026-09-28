"""Acceptance tests for client-facing plain-language change summaries."""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import plain_language_change_summary as summary  # noqa: E402


BASE = "1" * 40
HEAD = "2" * 40
C1 = "3" * 40
C2 = "4" * 40


def _canonical_hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _area(area: str, file_count: int, added: int = 0, deleted: int = 0):
    return {
        "area": area,
        "file_count": file_count,
        "text_file_count": file_count,
        "binary_file_count": 0,
        "added_lines": added,
        "deleted_lines": deleted,
    }


def _evidence(**overrides):
    value = {
        "schema_version": "development_change_evidence_v1",
        "evidence_scope": "git_range_objective_facts",
        "baseline_commit": BASE,
        "remote_head": HEAD,
        "continuity": "continuous",
        "capacity": "within",
        "evidence_state": "complete",
        "has_change": True,
        "commit_count": 2,
        "commit_ids": [C1, C2],
        "changed_file_count": 4,
        "added_lines": 120,
        "deleted_lines": 30,
        "line_change_total": 150,
        "line_change_metric_role": "supporting_evidence_only",
        "text_file_count": 4,
        "binary_file_count": 0,
        "production_change_file_count": 2,
        "test_change_file_count": 1,
        "ci_change_file_count": 1,
        "documentation_change_file_count": 0,
        "operations_change_file_count": 0,
        "other_change_file_count": 0,
        "affected_areas": [
            _area("backend", 1, 40, 10),
            _area("frontend", 1, 30, 5),
            _area("frontend_tests", 1, 30, 10),
            _area("ci", 1, 20, 5),
        ],
        "files": [
            {"path": "apps/backend/app/example.py", "area": "backend", "added_lines": 40, "deleted_lines": 10, "is_binary": False},
            {"path": "apps/frontend/src/App.vue", "area": "frontend", "added_lines": 30, "deleted_lines": 5, "is_binary": False},
            {"path": "apps/frontend/tests/e2e/example.spec.js", "area": "frontend_tests", "added_lines": 30, "deleted_lines": 10, "is_binary": False},
            {"path": ".github/workflows/ci.yml", "area": "ci", "added_lines": 20, "deleted_lines": 5, "is_binary": False},
        ],
    }
    value.update(overrides)
    value.pop("development_change_evidence_hash", None)
    value["development_change_evidence_hash"] = _canonical_hash(value)
    return value


def _build(evidence=None):
    return summary.build_plain_language_change_summary(evidence=evidence or _evidence())


def _visible_text(result: dict[str, object]) -> str:
    pieces = [result["headline"], result["summary"], result["scope_note"]]
    for item in result["highlights"]:
        pieces.extend([item["label"], item["unit"]])
    return " ".join(str(piece) for piece in pieces)


def test_t01_entry_has_one_keyword_only_parameter():
    signature = inspect.signature(summary.build_plain_language_change_summary)
    assert list(signature.parameters) == ["evidence"]
    assert signature.parameters["evidence"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        summary.build_plain_language_change_summary(_evidence())  # type: ignore[misc]


def test_t02_complete_change_becomes_short_client_readable_copy():
    result = _build()
    assert result["display_state"] == "ready"
    assert result["headline"] == "本次有明确研发变化"
    assert "2 次代码更新记录" in result["summary"]
    assert "4 个文件" in result["summary"]
    assert "后台功能" in result["summary"]
    assert "页面体验" in result["summary"]
    assert "自动检查" in result["summary"]


def test_t03_test_and_ci_areas_are_deduplicated_to_one_plain_label():
    result = _build()
    assert result["summary"].count("自动检查") == 1
    assert "frontend_tests" not in _visible_text(result)
    assert "ci" not in _visible_text(result).lower()


def test_t04_frontend_e2e_only_is_described_as_automatic_checks_not_production_ui():
    evidence = _evidence(
        commit_count=1,
        commit_ids=[C1],
        changed_file_count=1,
        added_lines=18,
        deleted_lines=2,
        line_change_total=20,
        production_change_file_count=0,
        test_change_file_count=1,
        ci_change_file_count=0,
        affected_areas=[_area("frontend_tests", 1, 18, 2)],
        files=[
            {
                "path": "apps/frontend/tests/e2e/anxin-board-preview.spec.js",
                "area": "frontend_tests",
                "added_lines": 18,
                "deleted_lines": 2,
                "is_binary": False,
            }
        ],
    )
    result = _build(evidence)
    assert "自动检查" in result["summary"]
    assert "页面体验" not in result["summary"]
    assert "后台功能" not in result["summary"]
    assert result["highlights"][-1] == {"label": "自动检查相关文件", "value": 1, "unit": "个"}


def test_t05_line_count_is_explicitly_workload_evidence_not_progress_or_quality():
    result = _build()
    assert {"label": "新增或调整的内容", "value": 150, "unit": "行"} in result["highlights"]
    assert result["scope_note"] == "行数只用来说明本次的变化量，不代表项目完成度或质量高低。"


def test_t06_complete_no_change_does_not_claim_project_stopped():
    evidence = _evidence(
        remote_head=BASE,
        continuity="no_new_commit",
        evidence_state="complete_no_change",
        has_change=False,
        commit_count=0,
        commit_ids=[],
        changed_file_count=0,
        added_lines=0,
        deleted_lines=0,
        line_change_total=0,
        production_change_file_count=0,
        test_change_file_count=0,
        ci_change_file_count=0,
        affected_areas=[],
        files=[],
    )
    result = _build(evidence)
    assert result["display_state"] == "no_change"
    assert result["headline"] == "本次暂未发现新的代码变化"
    assert "不代表项目停工" in result["scope_note"]


def test_t07_unreachable_checkpoint_is_fail_visible_not_fake_no_change():
    evidence = _evidence(
        continuity="checkpoint_unreachable",
        evidence_state="unavailable_checkpoint_unreachable",
        has_change=False,
        commit_count=0,
        commit_ids=[],
        changed_file_count=0,
        added_lines=0,
        deleted_lines=0,
        line_change_total=0,
        production_change_file_count=0,
        test_change_file_count=0,
        ci_change_file_count=0,
        affected_areas=[],
        files=[],
    )
    result = _build(evidence)
    assert result["display_state"] == "unavailable"
    assert result["headline"] == "本次的研发变化暂时无法确认"
    assert "不能把未知情况写成“本次没有变化”" in result["summary"]
    assert result["highlights"] == []


def test_t08_capacity_exceeded_is_clearly_partial():
    evidence = _evidence(
        capacity="capacity_exceeded",
        evidence_state="partial_capacity_exceeded",
        commit_count=501,
        commit_ids=[C1],
        changed_file_count=150,
        added_lines=10_001,
        deleted_lines=500,
        line_change_total=10_501,
        production_change_file_count=1,
        test_change_file_count=0,
        ci_change_file_count=0,
        affected_areas=[_area("backend", 1, 7, 0)],
        files=[{"path": "apps/backend/app/a.py", "area": "backend", "added_lines": 7, "deleted_lines": 0, "is_binary": False}],
    )
    result = _build(evidence)
    assert result["display_state"] == "partial"
    assert result["headline"] == "本次有研发变化，但当前只能确认一部分"
    assert "不能当作完整结果" in result["summary"]
    assert result["highlights"][0]["label"] == "当前已确认的代码更新记录"


def test_t09_source_hash_binding_rejects_tampered_evidence():
    evidence = _evidence()
    evidence["changed_file_count"] = 99
    with pytest.raises(summary.PlainLanguageChangeSummaryError) as caught:
        _build(evidence)
    assert caught.value.code == "PLAIN_LANGUAGE_CHANGE_SUMMARY_INVALID"


def test_t10_line_total_mismatch_fails_closed_even_with_rehashed_input():
    evidence = _evidence(line_change_total=999)
    with pytest.raises(summary.PlainLanguageChangeSummaryError):
        _build(evidence)


def test_t11_complete_category_partition_must_equal_changed_file_count():
    evidence = _evidence(production_change_file_count=1)
    with pytest.raises(summary.PlainLanguageChangeSummaryError):
        _build(evidence)


def test_t12_unknown_area_fails_closed():
    evidence = _evidence(affected_areas=[_area("mystery", 4, 120, 30)])
    with pytest.raises(summary.PlainLanguageChangeSummaryError):
        _build(evidence)


def test_t13_duplicate_area_fails_closed():
    evidence = _evidence(
        affected_areas=[_area("backend", 1), _area("backend", 1), _area("frontend", 2)]
    )
    with pytest.raises(summary.PlainLanguageChangeSummaryError):
        _build(evidence)


def test_t14_source_schema_and_loc_role_are_hard_boundaries():
    with pytest.raises(summary.PlainLanguageChangeSummaryError):
        _build(_evidence(schema_version="development_change_evidence_v2"))
    with pytest.raises(summary.PlainLanguageChangeSummaryError):
        _build(_evidence(line_change_metric_role="progress_score"))


def test_t15_output_does_not_expose_commit_ids_file_paths_or_raw_file_list():
    result = _build()
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert C1 not in rendered
    assert C2 not in rendered
    assert "apps/backend/app/example.py" not in rendered
    assert "apps/frontend/tests/e2e" not in rendered
    assert "commit_ids" not in rendered
    assert '"files"' not in rendered


def test_t16_result_hash_is_independently_recomputable_and_repeat_is_deterministic():
    first = _build()
    second = _build()
    assert second == first
    payload = {
        key: deepcopy(first[key])
        for key in summary._HASH_KEYS
    }
    assert first["plain_language_change_summary_hash"] == _canonical_hash(payload)
    assert len(first["plain_language_change_summary_hash"]) == 64


def test_t17_source_mapping_is_not_mutated():
    evidence = _evidence()
    before = deepcopy(evidence)
    _build(evidence)
    assert evidence == before


def test_comparison_range_without_dates_is_not_claimed_as_today():
    # The input is a commit range with no proof that it falls within one day.
    visible = _visible_text(_build())
    assert "今天" not in visible and "当天" not in visible
    assert "本次" in visible


def test_t18_client_visible_copy_contains_no_internal_jargon_or_percentage():
    visible = _visible_text(_build()).lower()
    forbidden = ("pull request", " pr ", "ci", "sha", "token", "framing", "commit", "git", "%", "百分比")
    assert all(term not in f" {visible} " for term in forbidden)


def test_t19_result_schema_is_exact_and_separates_machine_identity_from_visible_copy():
    result = _build()
    assert tuple(result) == summary._RESULT_KEYS
    assert result["schema_version"] == "plain_language_change_summary_v1"
    assert len(result["source_evidence_hash"]) == 64
    assert set(result["highlights"][0]) == {"label", "value", "unit"}


def test_t20_module_has_no_io_db_network_provider_or_ai_dependency():
    source = Path(summary.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert imports <= {"__future__", "collections", "copy", "hashlib", "json", "re"}
    assert not ({"pathlib", "sqlite3", "socket", "subprocess", "httpx", "requests", "openai", "anthropic"} & imports)
