"""Context Candidate Set V1：真实 producer/resolver 链、共享 closure 与安全边界测试。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_resolver, db, evidence_snapshots, prd, project_profiles  # noqa: E402
from app.evidence_snapshots import EvidenceSnapshotConfirm, _git_facts_hash  # noqa: E402
from app.git_analysis import RangeCandidate, _file_evidence_candidates  # noqa: E402
from app.git_client import GitClient, _parse_numstat_lines  # noqa: E402
from app.git_snapshots import _file_manifest_records  # noqa: E402

NOW = "2026-08-13T00:00:00+00:00"
VALID_ORIGIN = "https://candidate.example.invalid/repo.git"
PRD_SCHEMA = "prd_structured_evidence_v1"
PRD_PARSER = "prd-structured-parser-1.0"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith(("git_", "gcm_", "ssh_"))
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Candidate Test",
            "GIT_AUTHOR_EMAIL": "candidate@example.invalid",
            "GIT_COMMITTER_NAME": "Candidate Test",
            "GIT_COMMITTER_EMAIL": "candidate@example.invalid",
        }
    )
    return env


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repo), *args],
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").decode().strip().lower()


def _write(repo: Path, path: str, data: bytes) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _db_dump(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump())


def _index_hash(repo: Path) -> str | None:
    index = repo / ".git" / "index"
    return hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None


def _git_state(repo: Path) -> tuple[bytes, str | None, bytes, bytes]:
    return (
        _git(repo, "show-ref", check=False),
        _index_hash(repo),
        _git(repo, "status", "--porcelain"),
        _git(repo, "config", "--local", "--list", "--show-origin", check=False),
    )


def _state_signature(state: dict) -> tuple[str, bytes, bytes, tuple[bytes, str | None, bytes, bytes]]:
    return (
        _db_dump(state["db_path"]),
        state["prd_target"].read_bytes(),
        _git(state["repo"], "rev-parse", "HEAD"),
        _git_state(state["repo"]),
    )


def _candidate_code(state: dict) -> str:
    with pytest.raises(HTTPException) as caught:
        context_resolver.build_context_candidate_set(state["snapshot_id"])
    return caught.value.detail["code"]


def _make_state(tmp_path: Path, monkeypatch) -> dict:
    if shutil.which("git") is None:
        pytest.fail("Git is required for Context Candidate Set tests")

    db_path = tmp_path / "candidate.db"
    projects_root = tmp_path / "projects"
    prd_root = tmp_path / "prd-root"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(prd_root))
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name, status, created_at, git_url, branch) "
            "VALUES ('Candidate', 'active', ?, ?, 'main')",
            (NOW, VALID_ORIGIN),
        ).lastrowid

    repo = projects_root / str(project_id) / "repo"
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [shutil.which("git") or "git", "init", "-b", "main", str(repo)],
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    _git(repo, "remote", "add", "origin", VALID_ORIGIN)
    _write(repo, "src/a.txt", b"alpha\n")
    _write(repo, "src/b.txt", b"bravo\n")
    _write(repo, "assets/data.bin", b"\x00A\x01")
    from_commit = _commit(repo, "A")
    _write(repo, "src/a.txt", b"alpha\na2\n")
    _write(repo, "src/b.txt", b"bravo\nb2\n")
    _write(repo, "assets/data.bin", b"\x00B\x02")
    to_commit = _commit(repo, "B")

    rows = _parse_numstat_lines(
        _git(repo, "diff", "--numstat", "--no-renames", from_commit, to_commit)
        .decode("utf-8")
        .splitlines()
    )
    candidates = _file_evidence_candidates(rows)
    range_candidate = RangeCandidate(
        baseline_commit=from_commit,
        remote_head=to_commit,
        changed_file_count=len(candidates),
        files=candidates,
    )
    manifest_hash, file_records = _file_manifest_records(range_candidate)
    commits = [
        item
        for item in _git(repo, "rev-list", "--reverse", f"{from_commit}..{to_commit}")
        .decode()
        .splitlines()
        if item
    ]
    git_snapshot_for_hash = {
        "branch": "main",
        "from_commit": from_commit,
        "to_commit": to_commit,
        "commits_json": json.dumps(commits, separators=(",", ":")),
        "commit_count": len(commits),
        "changed_file_count": len(file_records),
        "added_lines": sum(record["added_lines"] or 0 for record in file_records),
        "deleted_lines": sum(record["deleted_lines"] or 0 for record in file_records),
        "diff_bytes": len(_git(repo, "diff", "--no-renames", from_commit, to_commit)),
    }

    source_hash = _sha(b"candidate-prd-source")
    parsed_hash = _sha(b"candidate-prd-parsed")
    fingerprint = prd._document_fingerprint(PRD_PARSER, PRD_SCHEMA, "md", source_hash)
    blocks = [
        {
            "ordinal": 1,
            "kind": "paragraph",
            "evidence_id": f"prd:block:{fingerprint}:1",
            "content_hash": _sha(b"prd-block-one"),
            "text": "冻结正文一",
            "heading_level": None,
            "table_rows": None,
            "page_no": 1,
        },
        {
            "ordinal": 2,
            "kind": "table",
            "evidence_id": f"prd:block:{fingerprint}:2",
            "content_hash": _sha(b"prd-block-two"),
            "text": None,
            "heading_level": None,
            "table_rows": [["A", "B"], ["1", "2"]],
            "page_no": 2,
        },
    ]
    structured = {
        "schema_version": PRD_SCHEMA,
        "parser_version": PRD_PARSER,
        "source_format": "md",
        "source_hash": source_hash,
        "document_fingerprint": fingerprint,
        "blocks": blocks,
    }
    structured_raw = prd._canonical_json_bytes(structured)
    structured_path = f"{project_id}/prd/structured.json"
    prd_target = prd_root / structured_path
    prd_target.parent.mkdir(parents=True, exist_ok=True)
    prd_target.write_bytes(structured_raw)

    profile_model = project_profiles.ProjectProfileContent(
        schema_version=project_profiles.SCHEMA_VERSION,
        project_summary="冻结项目档案",
        modules=[
            {
                "client_id": "core",
                "name": "核心模块",
                "requirements": ["生成候选上下文"],
                "paths": [{"type": "backend", "pattern": "apps/backend/**"}],
            }
        ],
        domain_glossary=[],
        exclude_patterns=[],
        notes="historical",
    )
    profile_json, profile_hash = project_profiles._canonicalize(profile_model)

    with sqlite3.connect(db_path) as conn:
        lineage_id = conn.execute(
            """
            INSERT INTO analysis_lineages (
                project_id, sequence_no, branch, baseline_commit, status, created_at
            ) VALUES (?, 1, 'main', ?, 'active', ?)
            """,
            (project_id, from_commit, NOW),
        ).lastrowid
        git_snapshot_id = conn.execute(
            """
            INSERT INTO git_snapshots (
                project_id, analysis_lineage_id, branch, from_commit, to_commit,
                commits_json, commit_count, changed_file_count, added_lines,
                deleted_lines, diff_bytes, file_manifest_hash, frozen_at
            ) VALUES (?, ?, 'main', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                lineage_id,
                from_commit,
                to_commit,
                git_snapshot_for_hash["commits_json"],
                git_snapshot_for_hash["commit_count"],
                git_snapshot_for_hash["changed_file_count"],
                git_snapshot_for_hash["added_lines"],
                git_snapshot_for_hash["deleted_lines"],
                git_snapshot_for_hash["diff_bytes"],
                manifest_hash,
                NOW,
            ),
        ).lastrowid
        for record in file_records:
            conn.execute(
                """
                INSERT INTO git_file_evidence (
                    git_snapshot_id, ordinal, evidence_id, path, added_lines,
                    deleted_lines, is_binary, file_facts_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    git_snapshot_id,
                    record["ordinal"],
                    f"git:file:{git_snapshot_id}:{record['ordinal']:03d}",
                    record["path"],
                    record["added_lines"],
                    record["deleted_lines"],
                    1 if record["is_binary"] else 0,
                    record["file_facts_hash"],
                ),
            )
        prd_id = conn.execute(
            """
            INSERT INTO prd_versions (
                project_id, version_no, original_filename, source_path, source_hash,
                size_bytes, parsed_path, parsed_hash, parser_version,
                structured_path, structured_hash, structured_schema_version,
                structured_parser_version, document_fingerprint, status,
                warnings_json, created_at, confirmed_by, confirmed_at
            ) VALUES (?, 1, 'prd.md', 'source.md', ?, 10, 'parsed.txt', ?,
                      'legacy-parser', ?, ?, ?, ?, ?, 'parse_confirmed', '[]', ?, 'pm', ?)
            """,
            (
                project_id,
                source_hash,
                parsed_hash,
                structured_path,
                _sha(structured_raw),
                PRD_SCHEMA,
                PRD_PARSER,
                fingerprint,
                NOW,
                NOW,
            ),
        ).lastrowid
        profile_id = conn.execute(
            """
            INSERT INTO project_profiles (
                project_id, version_no, source_prd_id, status, content_json,
                content_hash, edit_version, created_at, updated_at, confirmed_by, confirmed_at
            ) VALUES (?, 1, ?, 'confirmed', ?, ?, 2, ?, ?, 'pm', ?)
            """,
            (project_id, prd_id, profile_json, profile_hash, NOW, NOW, NOW),
        ).lastrowid

    produced = evidence_snapshots.confirm_evidence_snapshot(
        project_id,
        EvidenceSnapshotConfirm(
            expected_git_snapshot_id=git_snapshot_id,
            expected_lineage_id=lineage_id,
            expected_prd_id=prd_id,
            expected_profile_id=profile_id,
        ),
    )
    snapshot_id = produced["snapshot"]["id"]
    return {
        "db_path": db_path,
        "projects_root": projects_root,
        "prd_root": prd_root,
        "prd_target": prd_target,
        "prd_raw": structured_raw,
        "repo": repo,
        "project_id": project_id,
        "lineage_id": lineage_id,
        "git_snapshot_id": git_snapshot_id,
        "prd_id": prd_id,
        "profile_id": profile_id,
        "snapshot_id": snapshot_id,
        "from_commit": from_commit,
        "to_commit": to_commit,
        "commits": commits,
        "file_records": file_records,
        "blocks": blocks,
        "profile_content": profile_model.model_dump(),
        "profile_hash": profile_hash,
    }


@pytest.fixture()
def candidate_state(tmp_path, monkeypatch):
    return _make_state(tmp_path, monkeypatch)


def test_t01_real_happy_path_and_deterministic_hash(candidate_state):
    first = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    second = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    assert first == second
    assert first["schema_version"] == "context_candidate_set_v1"
    assert first["range"]["commits"] == candidate_state["commits"]
    assert first["profile"]["profile_id"] == candidate_state["profile_id"]
    assert first["profile"]["profile_content_hash"] == candidate_state["profile_hash"]
    assert first["profile"]["content"] == candidate_state["profile_content"]
    assert first["profile"]["model_send_state"] == "not_admitted"
    git_count = len(candidate_state["file_records"])
    assert [item["type"] for item in first["items"][:git_count]] == [
        "git_file_fact"
    ] * git_count
    assert [item["type"] for item in first["items"][git_count:]] == [
        "prd_block"
    ] * len(candidate_state["blocks"])
    assert all("diff_text" not in item for item in first["items"])
    assert all(
        "text" not in item and "table_rows" not in item
        for item in first["items"]
        if item["type"] == "prd_block"
    )
    assert len(first["candidate_set_hash"]) == 64


def test_t02_current_mutable_drift_does_not_rewrite_history(candidate_state):
    baseline = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    new_profile = project_profiles.ProjectProfileContent(
        schema_version=project_profiles.SCHEMA_VERSION,
        project_summary="current profile drift",
    )
    new_profile_json, new_profile_hash = project_profiles._canonicalize(new_profile)
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        conn.execute(
            "UPDATE projects SET git_url = 'https://current.example.invalid/new.git', "
            "branch = 'develop' WHERE id = ?",
            (candidate_state["project_id"],),
        )
        conn.execute(
            "UPDATE prd_versions SET status = 'superseded' WHERE id = ?",
            (candidate_state["prd_id"],),
        )
        new_prd_id = conn.execute(
            """
            INSERT INTO prd_versions (
                project_id, version_no, original_filename, source_path, source_hash,
                size_bytes, status, warnings_json, created_at
            ) VALUES (?, 2, 'new.md', 'new.md', ?, 1, 'parse_confirmed', '[]', ?)
            """,
            (candidate_state["project_id"], _sha(b"new-prd"), NOW),
        ).lastrowid
        conn.execute(
            "UPDATE project_profiles SET status = 'superseded' WHERE id = ?",
            (candidate_state["profile_id"],),
        )
        conn.execute(
            """
            INSERT INTO project_profiles (
                project_id, version_no, source_prd_id, status, content_json,
                content_hash, edit_version, created_at, updated_at
            ) VALUES (?, 2, ?, 'confirmed', ?, ?, 1, ?, ?)
            """,
            (
                candidate_state["project_id"],
                new_prd_id,
                new_profile_json,
                new_profile_hash,
                NOW,
                NOW,
            ),
        )
    assert context_resolver.build_context_candidate_set(candidate_state["snapshot_id"]) == baseline


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "project",
        "source_prd",
        "hash",
        "bad_json",
        "noncanonical",
        "schema",
        "extra",
    ],
)
def test_t03_historical_profile_closure_fails_closed(candidate_state, case):
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        if case == "missing":
            conn.execute(
                "DELETE FROM project_profiles WHERE id = ?",
                (candidate_state["profile_id"],),
            )
        elif case == "project":
            conn.execute(
                "UPDATE project_profiles SET project_id = project_id + 100 WHERE id = ?",
                (candidate_state["profile_id"],),
            )
        elif case == "source_prd":
            conn.execute(
                "UPDATE project_profiles SET source_prd_id = source_prd_id + 100 WHERE id = ?",
                (candidate_state["profile_id"],),
            )
        elif case == "hash":
            conn.execute(
                "UPDATE project_profiles SET content_hash = ? WHERE id = ?",
                ("f" * 64, candidate_state["profile_id"]),
            )
        elif case == "bad_json":
            conn.execute(
                "UPDATE project_profiles SET content_json = '{' WHERE id = ?",
                (candidate_state["profile_id"],),
            )
        else:
            row = conn.execute(
                "SELECT content_json FROM project_profiles WHERE id = ?",
                (candidate_state["profile_id"],),
            ).fetchone()
            content = json.loads(row[0])
            if case == "noncanonical":
                raw = json.dumps(content, ensure_ascii=False, indent=2)
            elif case == "schema":
                content["schema_version"] = "wrong"
                raw = json.dumps(
                    content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            else:
                content["extra"] = True
                raw = json.dumps(
                    content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            conn.execute(
                "UPDATE project_profiles SET content_json = ? WHERE id = ?",
                (raw, candidate_state["profile_id"]),
            )
    assert _candidate_code(candidate_state) == "CONTEXT_CANDIDATE_PROFILE_INVALID"


def test_t04_snapshot_admission_fails_closed(candidate_state):
    with pytest.raises(HTTPException) as caught:
        context_resolver.build_context_candidate_set(0)
    assert caught.value.detail["code"] == "CONTEXT_CANDIDATE_SNAPSHOT_UNSUPPORTED"
    with pytest.raises(HTTPException) as caught:
        context_resolver.build_context_candidate_set(999999)
    assert caught.value.detail["code"] == "CONTEXT_CANDIDATE_SNAPSHOT_UNSUPPORTED"
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_snapshots SET schema_version = 'evidence_snapshot_core_v1' "
            "WHERE id = ?",
            (candidate_state["snapshot_id"],),
        )
    assert _candidate_code(candidate_state) == "CONTEXT_CANDIDATE_SNAPSHOT_UNSUPPORTED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("from_commit", "bad"),
        ("profile_id", 0),
        ("profile_content_hash", "bad"),
    ],
)
def test_t04_required_snapshot_identity_fails_closed(candidate_state, field, value):
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        conn.execute(
            f"UPDATE evidence_snapshots SET {field} = ? WHERE id = ?",
            (value, candidate_state["snapshot_id"]),
        )
    assert _candidate_code(candidate_state) == "CONTEXT_CANDIDATE_SNAPSHOT_UNSUPPORTED"


@pytest.mark.parametrize("case", ["type", "selected", "source_ref", "content_hash"])
def test_t05_evidence_item_full_set_is_strict(candidate_state, case):
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        row = conn.execute(
            "SELECT evidence_id FROM evidence_items WHERE snapshot_id = ? "
            "AND type = 'git_file_fact' ORDER BY id LIMIT 1",
            (candidate_state["snapshot_id"],),
        ).fetchone()
        evidence_id = row[0]
        if case == "type":
            conn.execute(
                "UPDATE evidence_items SET type = 'unknown' WHERE snapshot_id = ? "
                "AND evidence_id = ?",
                (candidate_state["snapshot_id"], evidence_id),
            )
        elif case == "selected":
            conn.execute(
                "UPDATE evidence_items SET selected = 0 WHERE snapshot_id = ? "
                "AND evidence_id = ?",
                (candidate_state["snapshot_id"], evidence_id),
            )
        elif case == "source_ref":
            conn.execute(
                "UPDATE evidence_items SET source_ref = 'wrong:1:1' WHERE snapshot_id = ? "
                "AND evidence_id = ?",
                (candidate_state["snapshot_id"], evidence_id),
            )
        else:
            conn.execute(
                "UPDATE evidence_items SET content_hash = ? WHERE snapshot_id = ? "
                "AND evidence_id = ?",
                ("f" * 64, candidate_state["snapshot_id"], evidence_id),
            )
    assert _candidate_code(candidate_state) == "CONTEXT_CANDIDATE_ITEM_SET_INVALID"


def test_t05_db_insertion_order_does_not_change_fixed_output(candidate_state):
    baseline = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        rows = conn.execute(
            """
            SELECT evidence_id, type, source_ref, content_hash, selected, redaction_state
            FROM evidence_items
            WHERE snapshot_id = ?
            ORDER BY id
            """,
            (candidate_state["snapshot_id"],),
        ).fetchall()
        conn.execute(
            "DELETE FROM evidence_items WHERE snapshot_id = ?",
            (candidate_state["snapshot_id"],),
        )
        for row in reversed(rows):
            conn.execute(
                """
                INSERT INTO evidence_items (
                    snapshot_id, evidence_id, type, source_ref, content_hash,
                    selected, redaction_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (candidate_state["snapshot_id"], *tuple(row)),
            )
    rebuilt = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    assert rebuilt == baseline


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_t05_missing_or_extra_item_fails_closed(candidate_state, case):
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        if case == "missing":
            row = conn.execute(
                "SELECT evidence_id FROM evidence_items WHERE snapshot_id = ? "
                "AND type = 'prd_block' ORDER BY id LIMIT 1",
                (candidate_state["snapshot_id"],),
            ).fetchone()
            conn.execute(
                "DELETE FROM evidence_items WHERE snapshot_id = ? AND evidence_id = ?",
                (candidate_state["snapshot_id"], row[0]),
            )
        else:
            conn.execute(
                """
                INSERT INTO evidence_items (
                    snapshot_id, evidence_id, type, source_ref, content_hash,
                    selected, redaction_state
                ) VALUES (?, 'git:file:extra:999', 'git_file_fact', ?, ?, 1, 'not_applicable')
                """,
                (
                    candidate_state["snapshot_id"],
                    f"git_file_evidence:{candidate_state['git_snapshot_id']}:999",
                    "f" * 64,
                ),
            )
    assert _candidate_code(candidate_state) == "CONTEXT_CANDIDATE_ITEM_SET_INVALID"


def test_t05_zero_file_manifest_rejects_stray_file_evidence(candidate_state):
    empty_manifest_hash = context_resolver._git_snapshot_stable_hash(
        {"schema_version": context_resolver._FILE_MANIFEST_SCHEMA_VERSION, "files": []}
    )
    with sqlite3.connect(candidate_state["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM git_snapshots WHERE id = ?",
            (candidate_state["git_snapshot_id"],),
        ).fetchone()
        git_snapshot = dict(row)
        git_snapshot["changed_file_count"] = 0
        git_snapshot["file_manifest_hash"] = empty_manifest_hash
        with pytest.raises(HTTPException) as caught:
            context_resolver._read_candidate_git_manifest(
                conn,
                git_snapshot=git_snapshot,
                git_items={},
            )
    assert caught.value.detail["code"] == "CONTEXT_GIT_FROZEN_FACTS_INVALID"


def test_t06_git_full_range_closure_runs_once_per_build(candidate_state, monkeypatch):
    calls = {
        "snapshot": 0,
        "git_snapshot": 0,
        "manifest": 0,
        "workspace": 0,
        "rev_list": 0,
        "numstat": 0,
        "exact": 0,
    }

    def wrap(name):
        original = getattr(context_resolver, name)

        def counted(*args, **kwargs):
            key = {
                "_read_candidate_snapshot": "snapshot",
                "_read_historical_git_snapshot": "git_snapshot",
                "_read_candidate_git_manifest": "manifest",
                "_open_candidate_workspace": "workspace",
                "_replay_commit_list": "rev_list",
                "_replay_file_evidence": "numstat",
                "_read_exact_diff": "exact",
            }[name]
            calls[key] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(context_resolver, name, counted)

    for helper in (
        "_read_candidate_snapshot",
        "_read_historical_git_snapshot",
        "_read_candidate_git_manifest",
        "_open_candidate_workspace",
        "_replay_commit_list",
        "_replay_file_evidence",
        "_read_exact_diff",
    ):
        wrap(helper)
    result = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    text_count = sum(
        not record["is_binary"] for record in candidate_state["file_records"]
    )
    assert calls == {
        "snapshot": 1,
        "git_snapshot": 1,
        "manifest": 1,
        "workspace": 1,
        "rev_list": 1,
        "numstat": 1,
        "exact": text_count,
    }
    git_items = [item for item in result["items"] if item["type"] == "git_file_fact"]
    text_items = [item for item in git_items if not item["is_binary"]]
    assert len(text_items) == text_count
    for item in text_items:
        assert len(item["resolved_content_hash"]) == 64
        int(item["resolved_content_hash"], 16)
        assert item["resolved_diff_bytes"] > 0
        assert item["resolved_content_redaction_state"] == "pending"
        assert item["model_send_state"] == "not_admitted"
        assert "diff_text" not in item
    assert sum(item["content_kind"] == "binary_metadata_only" for item in git_items) == 1
    assert all("diff_text" not in item for item in result["items"])


def test_t07_prd_artifact_closure_runs_once_for_multiple_blocks(
    candidate_state, monkeypatch
):
    original = context_resolver._read_exact_artifact
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(context_resolver, "_read_exact_artifact", counted)
    result = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    prd_items = [item for item in result["items"] if item["type"] == "prd_block"]
    assert calls == 1
    assert len(prd_items) == 2
    assert all("text" not in item and "table_rows" not in item for item in prd_items)


def test_t08_binary_is_metadata_only(candidate_state):
    result = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    binary = next(
        item
        for item in result["items"]
        if item["type"] == "git_file_fact" and item["is_binary"]
    )
    assert binary["resolved_content_hash"] is None
    assert binary["resolved_diff_bytes"] == 0
    assert binary["resolved_content_redaction_state"] == "not_applicable"
    assert binary["model_send_state"] == "not_admitted"
    assert "diff_text" not in binary


@pytest.mark.parametrize(
    "case,expected",
    [
        ("prd_escape", "PRD_STORAGE_ESCAPE"),
        ("git_dirty", "GIT_WORKSPACE_DIRTY"),
        ("attributes", "CONTEXT_GIT_ATTRIBUTES_UNSAFE"),
    ],
)
def test_t09_existing_safety_errors_propagate(candidate_state, case, expected):
    if case == "prd_escape":
        with sqlite3.connect(candidate_state["db_path"]) as conn:
            conn.execute(
                "UPDATE prd_versions SET structured_path = '../escape.json' WHERE id = ?",
                (candidate_state["prd_id"],),
            )
    elif case == "git_dirty":
        _write(candidate_state["repo"], "dirty.txt", b"dirty\n")
    else:
        attributes = candidate_state["repo"] / ".git" / "info" / "attributes"
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text("*.txt binary\n", encoding="utf-8")
    assert _candidate_code(candidate_state) == expected


def test_t10_analysis_rules_gap_is_explicit(candidate_state):
    result = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    assert result["unsupported_context_sources"] == [
        "analysis_rules:not_frozen_in_snapshot_v2"
    ]


def test_t11_success_is_zero_side_effect(candidate_state):
    before = _state_signature(candidate_state)
    context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    assert _state_signature(candidate_state) == before


def test_t11_failure_is_zero_side_effect(candidate_state):
    _write(candidate_state["repo"], "dirty.txt", b"dirty\n")
    before = _state_signature(candidate_state)
    assert _candidate_code(candidate_state) == "GIT_WORKSPACE_DIRTY"
    assert _state_signature(candidate_state) == before


def test_t12_no_network_db_write_or_git_mutation_path(candidate_state, monkeypatch):
    original_factory = db.get_connection

    def read_only_connection():
        conn = original_factory()
        denied = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE,
        }

        def authorize(action, _arg1, _arg2, _db_name, _trigger):
            return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(context_resolver, "get_connection", read_only_connection)

    def no_network(*_args, **_kwargs):
        raise AssertionError("provider/network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", no_network)
    seen: list[list[str]] = []

    class GuardedGitClient(GitClient):
        @staticmethod
        def _assert_no_mutation(args):
            forbidden = {
                "fetch",
                "clone",
                "pull",
                "checkout",
                "switch",
                "reset",
                "merge",
            }
            if any(arg in forbidden for arg in args):
                raise AssertionError(f"forbidden Git mutation/network command: {args}")

        def _exec(self, args, *, cwd):
            self._assert_no_mutation(args)
            seen.append(list(args))
            return super()._exec(args, cwd=cwd)

        def _run_bounded_process(self, args, **kwargs):
            self._assert_no_mutation(args)
            seen.append(list(args))
            return super()._run_bounded_process(args, **kwargs)

    monkeypatch.setattr(context_resolver, "_git_client_factory", GuardedGitClient)

    import builtins

    original_import = builtins.__import__
    forbidden_runtime_modules = ("redaction", "report", "smtp", "provider", "model_call")

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        lowered = name.casefold()
        if any(fragment in lowered for fragment in forbidden_runtime_modules):
            raise AssertionError(f"forbidden downstream capability import: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = context_resolver.build_context_candidate_set(candidate_state["snapshot_id"])
    assert result["schema_version"] == "context_candidate_set_v1"
    assert seen
