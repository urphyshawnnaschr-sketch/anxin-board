"""Daily Change Evidence Core V1 acceptance tests."""

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

from app import development_change_evidence as evidence  # noqa: E402
from app.git_analysis import FileEvidenceCandidate, RangeCandidate  # noqa: E402


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


def _text(path: str, added: int, deleted: int = 0) -> FileEvidenceCandidate:
    return FileEvidenceCandidate(
        path=path,
        added_lines=added,
        deleted_lines=deleted,
        is_binary=False,
    )


def _binary(path: str) -> FileEvidenceCandidate:
    return FileEvidenceCandidate(
        path=path,
        added_lines=None,
        deleted_lines=None,
        is_binary=True,
    )


def _candidate(**overrides) -> RangeCandidate:
    files = (
        _text("apps/backend/app/example.py", 10, 2),
        _text("tests/backend/test_example.py", 8, 1),
    )
    values = {
        "baseline_commit": BASE,
        "remote_head": HEAD,
        "commits": [C1, C2],
        "commit_count": 2,
        "changed_file_count": 2,
        "added_lines": 18,
        "deleted_lines": 3,
        "diff_bytes": 999,
        "files": files,
        "continuity": "continuous",
        "capacity": "within",
    }
    values.update(overrides)
    return RangeCandidate(**values)


def _build(candidate: RangeCandidate | None = None):
    return evidence.build_development_change_evidence(candidate=candidate or _candidate())


def test_t01_entry_is_one_keyword_only_candidate_parameter():
    signature = inspect.signature(evidence.build_development_change_evidence)
    assert list(signature.parameters) == ["candidate"]
    assert signature.parameters["candidate"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        evidence.build_development_change_evidence(_candidate())  # type: ignore[misc]


def test_t02_happy_path_projects_objective_change_metrics():
    result = _build()
    assert result["schema_version"] == "development_change_evidence_v1"
    assert result["evidence_scope"] == "git_range_objective_facts"
    assert result["evidence_state"] == "complete"
    assert result["has_change"] is True
    assert result["commit_count"] == 2
    assert result["changed_file_count"] == 2
    assert result["added_lines"] == 18
    assert result["deleted_lines"] == 3
    assert result["line_change_total"] == 21
    assert result["production_change_file_count"] == 1
    assert result["test_change_file_count"] == 1


def test_t03_affected_areas_are_fixed_order_and_have_independent_counts():
    frontend_e2e_path = "apps/frontend/tests/e2e/anxin-board-preview.spec.js"
    files = (
        _text("apps/frontend/src/App.vue", 4),
        _text(frontend_e2e_path, 6, 1),
        _text(".github/workflows/ci.yml", 2, 1),
        _text("docs/guide.md", 3),
        _text("scripts/run.ps1", 5, 2),
        _text("misc.txt", 1),
    )
    candidate = _candidate(
        files=files,
        changed_file_count=6,
        added_lines=21,
        deleted_lines=4,
    )
    result = _build(candidate)
    areas = result["affected_areas"]
    assert [item["area"] for item in areas] == [
        "frontend",
        "frontend_tests",
        "ci",
        "documentation",
        "tooling",
        "other",
    ]
    assert result["production_change_file_count"] == 1
    assert result["test_change_file_count"] == 1
    assert result["ci_change_file_count"] == 1
    assert result["documentation_change_file_count"] == 1
    assert result["operations_change_file_count"] == 1
    assert result["other_change_file_count"] == 1
    e2e_projection = next(item for item in result["files"] if item["path"] == frontend_e2e_path)
    assert e2e_projection["area"] == "frontend_tests"


def test_t04_binary_files_are_visible_but_do_not_fake_line_counts():
    files = (_text("apps/backend/app/a.py", 2, 1), _binary("apps/frontend/public/logo.png"))
    result = _build(
        _candidate(files=files, changed_file_count=2, added_lines=2, deleted_lines=1)
    )
    assert result["text_file_count"] == 1
    assert result["binary_file_count"] == 1
    binary = result["files"][1]
    assert binary["is_binary"] is True
    assert binary["added_lines"] is None
    assert binary["deleted_lines"] is None


def test_t05_no_new_commit_is_complete_no_change():
    candidate = RangeCandidate(
        baseline_commit=BASE,
        remote_head=BASE,
        continuity="no_new_commit",
    )
    result = _build(candidate)
    assert result["evidence_state"] == "complete_no_change"
    assert result["has_change"] is False
    assert result["commit_count"] == 0
    assert result["files"] == []
    assert result["affected_areas"] == []


def test_t06_checkpoint_unreachable_is_fail_visible_not_fake_zero_progress():
    candidate = RangeCandidate(
        baseline_commit=BASE,
        remote_head=HEAD,
        continuity="checkpoint_unreachable",
    )
    result = _build(candidate)
    assert result["evidence_state"] == "unavailable_checkpoint_unreachable"
    assert result["has_change"] is False
    assert result["changed_file_count"] == 0


def test_t07_capacity_exceeded_is_partial_and_allows_bounded_lists():
    files = (_text("apps/backend/app/a.py", 7),)
    candidate = _candidate(
        commits=[C1],
        commit_count=501,
        files=files,
        changed_file_count=150,
        added_lines=10001,
        deleted_lines=0,
        capacity="capacity_exceeded",
    )
    result = _build(candidate)
    assert result["evidence_state"] == "partial_capacity_exceeded"
    assert result["commit_count"] == 501
    assert result["commit_ids"] == [C1]
    assert len(result["files"]) == 1


def test_t08_line_counts_are_explicitly_supporting_evidence_only():
    result = _build()
    assert result["line_change_metric_role"] == "supporting_evidence_only"
    assert "progress" not in result
    assert "completion" not in result
    assert "quality_score" not in result
    assert "percentage" not in result


def test_t09_result_hash_is_independently_recomputed_from_all_visible_fields():
    result = _build()
    payload = {
        key: deepcopy(value)
        for key, value in result.items()
        if key != "development_change_evidence_hash"
    }
    assert result["development_change_evidence_hash"] == _canonical_hash(payload)
    assert len(result["development_change_evidence_hash"]) == 64


def test_t10_result_copies_mutable_commit_list_and_does_not_mutate_candidate():
    candidate = _candidate()
    original = deepcopy(candidate.commits)
    result = _build(candidate)
    assert candidate.commits == original
    candidate.commits.append("5" * 40)
    assert result["commit_ids"] == original


def test_t11_invalid_commit_identity_fails_closed():
    with pytest.raises(evidence.DevelopmentChangeEvidenceError) as caught:
        _build(_candidate(remote_head="NOT-A-SHA"))
    assert caught.value.code == "DEVELOPMENT_CHANGE_EVIDENCE_INVALID"


def test_t12_duplicate_commit_ids_fail_closed():
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(commits=[C1, C1]))


def test_t13_duplicate_file_paths_fail_closed():
    files = (_text("apps/backend/app/a.py", 1), _text("apps/backend/app/a.py", 2))
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(files=files, changed_file_count=2, added_lines=3, deleted_lines=0))


def test_t14_binary_file_may_not_carry_line_counts():
    invalid = FileEvidenceCandidate(
        path="apps/frontend/public/logo.png",
        added_lines=1,
        deleted_lines=None,
        is_binary=True,
    )
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(files=(invalid,), changed_file_count=1, added_lines=0, deleted_lines=0))


def test_t15_within_capacity_commit_count_must_match_commit_list():
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(commit_count=3))


def test_t16_within_capacity_changed_file_count_must_match_file_list():
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(changed_file_count=3))


def test_t17_within_capacity_line_totals_must_match_file_facts():
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(added_lines=999))


def test_t18_result_schema_is_exact_and_contains_no_raw_diff_or_body():
    result = _build()
    assert tuple(result) == evidence._RESULT_KEYS
    forbidden = {"diff", "diff_text", "raw_body", "body", "content", "provider_request"}
    assert forbidden.isdisjoint(result)
    for item in result["files"]:
        assert set(item) == {"path", "area", "added_lines", "deleted_lines", "is_binary"}


def test_t19_utf8_path_is_preserved_and_nul_path_is_rejected():
    files = (_text("apps/backend/app/研发进度.py", 2),)
    result = _build(_candidate(files=files, changed_file_count=1, added_lines=2, deleted_lines=0))
    assert result["files"][0]["path"] == "apps/backend/app/研发进度.py"

    bad = (_text("apps/backend/app/bad\x00.py", 1),)
    with pytest.raises(evidence.DevelopmentChangeEvidenceError):
        _build(_candidate(files=bad, changed_file_count=1, added_lines=1, deleted_lines=0))


def test_t20_module_has_no_new_io_network_db_or_provider_dependency():
    source = Path(evidence.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert imports <= {"__future__", "copy", "hashlib", "json", "re", "app"}
    assert not ({"socket", "subprocess", "sqlite3", "httpx", "requests", "pathlib"} & imports)
