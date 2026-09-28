"""Frozen GitSnapshot -> plain-language change summary read-seam acceptance tests."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import db  # noqa: E402
from app import development_change_evidence_source as evidence_source  # noqa: E402
from app import git_snapshots  # noqa: E402
from app import plain_language_change_summary as summary_core  # noqa: E402
from app import plain_language_change_summary_source as source  # noqa: E402
from app.git_analysis import FileEvidenceCandidate, RangeCandidate  # noqa: E402


BASE = "a" * 40
HEAD = "b" * 40
C1 = "1" * 40
C2 = "2" * 40
NOW = "2026-08-22T00:00:00+00:00"


def _candidate() -> RangeCandidate:
    return RangeCandidate(
        baseline_commit=BASE,
        remote_head=HEAD,
        commits=[C1, C2],
        commit_count=2,
        changed_file_count=3,
        added_lines=8,
        deleted_lines=3,
        diff_bytes=321,
        files=(
            FileEvidenceCandidate(
                path="apps/backend/app/example.py",
                added_lines=5,
                deleted_lines=2,
                is_binary=False,
            ),
            FileEvidenceCandidate(
                path="apps/frontend/public/logo.png",
                added_lines=None,
                deleted_lines=None,
                is_binary=True,
            ),
            FileEvidenceCandidate(
                path="tests/backend/test_example.py",
                added_lines=3,
                deleted_lines=1,
                is_binary=False,
            ),
        ),
        continuity="continuous",
        capacity="within",
    )


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    db.init_db()
    return path


def _insert_project(db_path: Path, *, name: str = "Plain Language Source 项目") -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO projects (name, status, created_at, updated_at)
            VALUES (?, 'active', ?, ?)
            """,
            (name, NOW, NOW),
        )
        return int(cursor.lastrowid)


def _seed_snapshot(db_path: Path, *, project_id: int | None = None):
    if project_id is None:
        project_id = _insert_project(db_path)
    candidate = _candidate()
    manifest_hash, records = git_snapshots._file_manifest_records(candidate)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO git_snapshots (
                project_id, analysis_lineage_id, branch, from_commit, to_commit,
                commits_json, commit_count, changed_file_count, added_lines,
                deleted_lines, diff_bytes, file_manifest_hash, frozen_at
            ) VALUES (?, 1, 'main', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                candidate.baseline_commit,
                candidate.remote_head,
                json.dumps(candidate.commits, separators=(",", ":")),
                candidate.commit_count,
                candidate.changed_file_count,
                candidate.added_lines,
                candidate.deleted_lines,
                candidate.diff_bytes,
                manifest_hash,
                NOW,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        for record in records:
            conn.execute(
                """
                INSERT INTO git_file_evidence (
                    git_snapshot_id, ordinal, evidence_id, path, added_lines,
                    deleted_lines, is_binary, file_facts_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    record["ordinal"],
                    f"git:file:{snapshot_id}:{record['ordinal']:03d}",
                    record["path"],
                    record["added_lines"],
                    record["deleted_lines"],
                    1 if record["is_binary"] else 0,
                    record["file_facts_hash"],
                ),
            )
    return project_id, snapshot_id


def _build(project_id: int, snapshot_id: int):
    return source.build_plain_language_change_summary_from_snapshot(
        project_id=project_id,
        git_snapshot_id=snapshot_id,
    )


def _assert_source_error(code: str, callback) -> None:
    with pytest.raises(evidence_source.DevelopmentChangeEvidenceSourceError) as caught:
        callback()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_t01_entry_is_keyword_only_and_exact():
    signature = inspect.signature(source.build_plain_language_change_summary_from_snapshot)
    assert list(signature.parameters) == ["project_id", "git_snapshot_id"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_t02_valid_frozen_snapshot_matches_formal_sequential_composition(db_path):
    project_id, snapshot_id = _seed_snapshot(db_path)

    evidence = evidence_source.build_development_change_evidence_from_snapshot(
        project_id=project_id,
        git_snapshot_id=snapshot_id,
    )
    expected = summary_core.build_plain_language_change_summary(evidence=evidence)
    actual = _build(project_id, snapshot_id)

    assert actual == expected
    assert actual["schema_version"] == "plain_language_change_summary_v1"
    assert actual["display_state"] == "ready"


def test_t03_same_snapshot_is_deterministic(db_path):
    project_id, snapshot_id = _seed_snapshot(db_path)
    assert _build(project_id, snapshot_id) == _build(project_id, snapshot_id)


def test_t04_project_missing_error_propagates_unchanged(db_path):
    _assert_source_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_PROJECT_NOT_FOUND",
        lambda: _build(999, 1),
    )


def test_t05_snapshot_missing_and_cross_project_errors_propagate_unchanged(db_path):
    owner_project_id, snapshot_id = _seed_snapshot(db_path)
    other_project_id = _insert_project(db_path, name="Other")

    _assert_source_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND",
        lambda: _build(owner_project_id, 999),
    )
    _assert_source_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND",
        lambda: _build(other_project_id, snapshot_id),
    )


def test_t06_frozen_source_corruption_error_propagates_unchanged(db_path):
    project_id, snapshot_id = _seed_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE git_snapshots SET commits_json = ? WHERE id = ?",
            ("{bad-json", snapshot_id),
        )

    _assert_source_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t07_invalid_evidence_returned_by_source_fails_in_formal_summary_core(monkeypatch):
    monkeypatch.setattr(
        source,
        "build_development_change_evidence_from_snapshot",
        lambda **_kwargs: {"schema_version": "development_change_evidence_v1"},
    )

    with pytest.raises(summary_core.PlainLanguageChangeSummaryError) as caught:
        _build(1, 1)
    assert caught.value.code == "PLAIN_LANGUAGE_CHANGE_SUMMARY_INVALID"


def test_t08_source_and_summary_core_are_each_called_exactly_once_with_same_evidence(monkeypatch):
    evidence = {"formal": "evidence"}
    summary = {"formal": "summary"}
    calls: list[tuple[str, object]] = []

    def fake_source(*, project_id, git_snapshot_id):
        calls.append(("source", (project_id, git_snapshot_id)))
        return evidence

    def fake_summary(*, evidence: object):
        calls.append(("summary", evidence))
        assert evidence is globals_evidence
        return summary

    globals_evidence = evidence
    monkeypatch.setattr(source, "build_development_change_evidence_from_snapshot", fake_source)
    monkeypatch.setattr(source, "build_plain_language_change_summary", fake_summary)

    result = _build(7, 9)

    assert result is summary
    assert calls == [("source", (7, 9)), ("summary", evidence)]


def test_t09_real_seam_does_not_invoke_git_workspace_or_network(db_path, monkeypatch):
    project_id, snapshot_id = _seed_snapshot(db_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Git/workspace/network access is forbidden in this seam")

    monkeypatch.setattr(git_snapshots, "_git_client_factory", forbidden)
    monkeypatch.setattr(git_snapshots, "open_workspace_access", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    result = _build(project_id, snapshot_id)
    assert result["schema_version"] == "plain_language_change_summary_v1"


def test_t10_source_has_no_db_sql_router_network_git_or_duplicate_algorithms():
    module_source = Path(source.__file__).read_text(encoding="utf-8")
    upper = module_source.upper()

    assert "get_connection" not in module_source
    assert "APIRouter" not in module_source
    assert "subprocess" not in module_source
    assert "socket" not in module_source
    assert "git_client" not in module_source
    assert "git_snapshots" not in module_source
    assert "hashlib" not in module_source
    assert "json" not in module_source
    assert "plain_language_change_summary_v1" not in module_source
    assert "development_change_evidence_v1" not in module_source
    for statement in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert statement not in upper


def test_t11_output_does_not_reexpose_internal_git_details(db_path):
    project_id, snapshot_id = _seed_snapshot(db_path)
    result = _build(project_id, snapshot_id)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)

    for forbidden in (
        C1,
        C2,
        "apps/backend/app/example.py",
        "apps/frontend/public/logo.png",
        "tests/backend/test_example.py",
        "raw diff",
        "workspace",
    ):
        assert forbidden not in encoded

    assert set(result) == {
        "schema_version",
        "source_evidence_hash",
        "display_state",
        "headline",
        "summary",
        "highlights",
        "scope_note",
        "plain_language_change_summary_hash",
    }
