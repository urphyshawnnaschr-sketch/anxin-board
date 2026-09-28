"""Frozen GitSnapshot -> Daily Change Evidence read-seam acceptance tests."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import socket
import sqlite3
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import db  # noqa: E402
from app import development_change_evidence as core  # noqa: E402
from app import development_change_evidence_source as source  # noqa: E402
from app import git_snapshots  # noqa: E402
from app.git_analysis import FileEvidenceCandidate, RangeCandidate  # noqa: E402


BASE = "a" * 40
HEAD = "b" * 40
C1 = "1" * 40
C2 = "2" * 40
NOW = "2026-08-22T00:00:00+00:00"


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


def _candidate() -> RangeCandidate:
    files = (
        _text("apps/backend/app/example.py", 5, 2),
        _binary("apps/frontend/public/logo.png"),
        _text("tests/backend/test_example.py", 3, 1),
    )
    return RangeCandidate(
        baseline_commit=BASE,
        remote_head=HEAD,
        commits=[C1, C2],
        commit_count=2,
        changed_file_count=3,
        added_lines=8,
        deleted_lines=3,
        diff_bytes=321,
        files=files,
        continuity="continuous",
        capacity="within",
    )


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    db.init_db()
    return path


def _insert_project(db_path: Path, *, name: str = "Evidence Source 项目") -> int:
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
    return project_id, snapshot_id, candidate


def _update(db_path: Path, sql: str, params=(), *, ignore_checks: bool = False) -> None:
    with sqlite3.connect(db_path) as conn:
        if ignore_checks:
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(sql, params)


def _assert_error(code: str, callback) -> None:
    with pytest.raises(source.DevelopmentChangeEvidenceSourceError) as caught:
        callback()
    assert caught.value.code == code
    assert str(caught.value) == code


def _build(project_id: int, snapshot_id: int):
    return source.build_development_change_evidence_from_snapshot(
        project_id=project_id,
        git_snapshot_id=snapshot_id,
    )


def test_t01_entry_is_keyword_only_and_exact():
    signature = inspect.signature(source.build_development_change_evidence_from_snapshot)
    assert list(signature.parameters) == ["project_id", "git_snapshot_id"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_t02_valid_frozen_snapshot_returns_exact_formal_evidence_and_is_repeatable(db_path):
    project_id, snapshot_id, candidate = _seed_snapshot(db_path)

    first = _build(project_id, snapshot_id)
    second = _build(project_id, snapshot_id)
    expected = core.build_development_change_evidence(candidate=candidate)

    assert first == expected
    assert second == expected
    assert first["schema_version"] == "development_change_evidence_v1"
    assert first["baseline_commit"] == BASE
    assert first["remote_head"] == HEAD
    assert first["commit_ids"] == [C1, C2]
    assert first["changed_file_count"] == 3
    assert first["added_lines"] == 8
    assert first["deleted_lines"] == 3
    assert first["binary_file_count"] == 1
    assert first["files"][1]["is_binary"] is True


def test_t03_missing_or_invalid_project_uses_stable_project_not_found(db_path):
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_PROJECT_NOT_FOUND",
        lambda: _build(999, 1),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_PROJECT_NOT_FOUND",
        lambda: source.build_development_change_evidence_from_snapshot(
            project_id=0,
            git_snapshot_id=1,
        ),
    )


def test_t03a_oversized_positive_project_id_uses_stable_project_not_found(db_path):
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_PROJECT_NOT_FOUND",
        lambda: source.build_development_change_evidence_from_snapshot(
            project_id=2**63,
            git_snapshot_id=1,
        ),
    )


def test_t04_missing_or_invalid_snapshot_uses_stable_snapshot_not_found(db_path):
    project_id = _insert_project(db_path)
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND",
        lambda: _build(project_id, 999),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND",
        lambda: source.build_development_change_evidence_from_snapshot(
            project_id=project_id,
            git_snapshot_id=0,
        ),
    )


def test_t04a_oversized_positive_snapshot_id_uses_stable_snapshot_not_found(db_path):
    project_id = _insert_project(db_path)
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND",
        lambda: source.build_development_change_evidence_from_snapshot(
            project_id=project_id,
            git_snapshot_id=2**63,
        ),
    )


def test_t05_cross_project_snapshot_is_not_disclosed(db_path):
    owner_project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    other_project_id = _insert_project(db_path, name="Other")
    assert owner_project_id != other_project_id

    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND",
        lambda: _build(other_project_id, snapshot_id),
    )


def test_t06_malformed_commits_json_fails_closed(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_snapshots SET commits_json = ? WHERE id = ?",
        ("{bad-json", snapshot_id),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


@pytest.mark.parametrize(
    "commits_json,commit_count",
    [
        (json.dumps([C1, C2]), 3),
        (json.dumps(["not-a-sha", C2]), 2),
        (json.dumps([C1, C1]), 2),
    ],
)
def test_t07_commit_count_sha_and_uniqueness_corruption_fail_closed(
    db_path, commits_json, commit_count
):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_snapshots SET commits_json = ?, commit_count = ? WHERE id = ?",
        (commits_json, commit_count, snapshot_id),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t08_from_to_identity_corruption_fails_closed(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_snapshots SET to_commit = from_commit WHERE id = ?",
        (snapshot_id,),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t09_file_ordinal_gap_fails_closed(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_file_evidence SET ordinal = 4 WHERE git_snapshot_id = ? AND ordinal = 2",
        (snapshot_id,),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t10_file_count_mismatch_fails_closed(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_snapshots SET changed_file_count = 4 WHERE id = ?",
        (snapshot_id,),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t11_text_line_totals_mismatch_fails_closed(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_snapshots SET added_lines = added_lines + 1 WHERE id = ?",
        (snapshot_id,),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t12_binary_text_semantics_and_binary_flag_fail_closed(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_file_evidence SET added_lines = 1 WHERE git_snapshot_id = ? AND ordinal = 2",
        (snapshot_id,),
        ignore_checks=True,
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )

    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_file_evidence SET is_binary = 2 WHERE git_snapshot_id = ? AND ordinal = 1",
        (snapshot_id,),
        ignore_checks=True,
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


@pytest.mark.parametrize("bad_path", ["", "bad\x00path.py"])
def test_t13_empty_or_nul_path_fails_closed(db_path, bad_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_file_evidence SET path = ? WHERE git_snapshot_id = ? AND ordinal = 1",
        (bad_path, snapshot_id),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


@pytest.mark.parametrize("bad_manifest", [None, "bad", "0" * 64])
def test_t14_manifest_missing_format_or_content_mismatch_fails_closed(db_path, bad_manifest):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_snapshots SET file_manifest_hash = ? WHERE id = ?",
        (bad_manifest, snapshot_id),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t15_per_file_fact_hash_tamper_fails_manifest_closure(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    _update(
        db_path,
        "UPDATE git_file_evidence SET file_facts_hash = ? WHERE git_snapshot_id = ? AND ordinal = 1",
        ("0" * 64, snapshot_id),
    )
    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t16_sqlite_corruption_is_hidden_behind_stable_source_invalid(db_path):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE git_file_evidence")

    _assert_error(
        "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID",
        lambda: _build(project_id, snapshot_id),
    )


def test_t17_seam_never_invokes_git_workspace_or_network(db_path, monkeypatch):
    project_id, snapshot_id, candidate = _seed_snapshot(db_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Git/workspace/network access is forbidden in this seam")

    monkeypatch.setattr(git_snapshots, "_git_client_factory", forbidden)
    monkeypatch.setattr(git_snapshots, "open_workspace_access", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    assert _build(project_id, snapshot_id) == core.build_development_change_evidence(
        candidate=candidate
    )


def test_t18_manifest_and_daily_change_core_are_each_called_exactly_once(db_path, monkeypatch):
    project_id, snapshot_id, _candidate_value = _seed_snapshot(db_path)
    real_manifest = source._file_manifest_records
    real_build = source.build_development_change_evidence
    calls = {"manifest": 0, "build": 0}

    def manifest_wrapper(candidate):
        calls["manifest"] += 1
        return real_manifest(candidate)

    def build_wrapper(*, candidate):
        calls["build"] += 1
        return real_build(candidate=candidate)

    monkeypatch.setattr(source, "_file_manifest_records", manifest_wrapper)
    monkeypatch.setattr(source, "build_development_change_evidence", build_wrapper)

    result = _build(project_id, snapshot_id)
    assert result["schema_version"] == "development_change_evidence_v1"
    assert calls == {"manifest": 1, "build": 1}


def test_t19_source_is_read_only_and_has_no_direct_git_network_or_router_surface():
    text = Path(source.__file__).read_text(encoding="utf-8")
    upper = text.upper()
    assert "INSERT INTO" not in upper
    assert "UPDATE " not in upper
    assert "DELETE FROM" not in upper
    assert "BEGIN IMMEDIATE" not in upper
    assert "APIRouter" not in text
    assert "@router" not in text

    tree = ast.parse(text)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert not ({"socket", "subprocess", "httpx", "requests", "urllib"} & imports)
