"""Context Git Diff Resolver V1：historical replay、deterministic diff、安全边界与零副作用。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_resolver, db  # noqa: E402
from app.evidence_snapshots import _git_facts_hash, _stable_hash  # noqa: E402
from app.git_analysis import RangeCandidate, _file_evidence_candidates  # noqa: E402
from app.git_client import GitClient, _parse_numstat_lines  # noqa: E402
from app.git_snapshots import _file_manifest_records  # noqa: E402

SNAPSHOT_SCHEMA = "evidence_snapshot_core_v2"
NOW = "2026-08-13T00:00:00+00:00"
DIFF_LIMIT = 5 * 1024 * 1024
VALID_ORIGIN = "https://historical.example.invalid/repo.git"


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
            "GIT_AUTHOR_NAME": "Resolver Test",
            "GIT_AUTHOR_EMAIL": "resolver@example.invalid",
            "GIT_COMMITTER_NAME": "Resolver Test",
            "GIT_COMMITTER_EMAIL": "resolver@example.invalid",
        }
    )
    return env


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> bytes:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repo), *args],
        env=_git_env(),
        input=input_bytes,
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


def _exact_diff(repo: Path, from_commit: str, to_commit: str, path: str) -> bytes:
    return subprocess.run(
        [
            shutil.which("git") or "git",
            "--no-replace-objects",
            "--no-lazy-fetch",
            "--literal-pathspecs",
            f"--attr-source={to_commit}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-C",
            str(repo),
            "diff",
            "--patch",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-color-moved",
            "--ws-error-highlight=none",
            "--text",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--unified=3",
            "--inter-hunk-context=0",
            "--full-index",
            "--default-prefix",
            "--no-relative",
            from_commit,
            to_commit,
            "--",
            path,
        ],
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=True,
    ).stdout


def _db_dump(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump())


def _index_hash(repo: Path) -> str | None:
    index = repo / ".git" / "index"
    if not index.exists():
        return None
    return hashlib.sha256(index.read_bytes()).hexdigest()


def _state_signature(state: dict) -> tuple[str, str, bytes, str | None, bytes]:
    repo = state["repo"]
    return (
        _db_dump(state["db_path"]),
        _git(repo, "rev-parse", "HEAD").decode().strip(),
        _git(repo, "show-ref", check=False),
        _index_hash(repo),
        _git(repo, "status", "--porcelain"),
    )


def _freeze_range(
    tmp_path: Path,
    monkeypatch,
    *,
    target_path: str = "src/example.txt",
    a_bytes: bytes = b"alpha\n",
    b_bytes: bytes = b"alpha\nbeta\n",
    decoy: tuple[str, bytes, bytes] | None = None,
) -> dict:
    if shutil.which("git") is None:
        pytest.fail("Git is required for Context Git Diff Resolver tests")

    db_path = tmp_path / "resolver.db"
    projects_root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(projects_root))
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name, status, created_at, git_url, branch) "
            "VALUES ('Git Resolver', 'active', ?, '', 'main')",
            (NOW,),
        ).lastrowid

    repo = projects_root / str(project_id) / "repo"
    repo.parent.mkdir(parents=True, exist_ok=True)
    repo.mkdir()
    subprocess.run(
        [shutil.which("git") or "git", "init", "-b", "main", str(repo)],
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    origin_url = VALID_ORIGIN
    _git(repo, "remote", "add", "origin", origin_url)

    _write(repo, target_path, a_bytes)
    if decoy is not None:
        _write(repo, decoy[0], decoy[1])
    commit_a = _commit(repo, "A")

    _write(repo, target_path, b_bytes)
    if decoy is not None:
        _write(repo, decoy[0], decoy[2])
    commit_b = _commit(repo, "B")

    raw_numstat = _git(repo, "diff", "--numstat", "--no-renames", commit_a, commit_b)
    numstat_lines = raw_numstat.decode("utf-8").splitlines()
    rows = _parse_numstat_lines(numstat_lines)
    candidates = _file_evidence_candidates(rows)
    replay_candidate = RangeCandidate(
        baseline_commit=commit_a,
        remote_head=commit_b,
        changed_file_count=len(candidates),
        files=candidates,
    )
    manifest_hash, file_records = _file_manifest_records(replay_candidate)
    target_record = next(record for record in file_records if record["path"] == target_path)
    commits = [
        line
        for line in _git(repo, "rev-list", "--reverse", f"{commit_a}..{commit_b}")
        .decode()
        .splitlines()
        if line
    ]
    diff_bytes = len(_git(repo, "diff", "--no-renames", commit_a, commit_b))
    git_snapshot_for_hash = {
        "branch": "main",
        "from_commit": commit_a,
        "to_commit": commit_b,
        "commits_json": json.dumps(commits, separators=(",", ":")),
        "commit_count": len(commits),
        "changed_file_count": len(file_records),
        "added_lines": sum(record["added_lines"] or 0 for record in file_records),
        "deleted_lines": sum(record["deleted_lines"] or 0 for record in file_records),
        "diff_bytes": diff_bytes,
    }
    git_facts_hash = _git_facts_hash(git_snapshot_for_hash)
    project_config_hash = _stable_hash(
        {"repository_url": origin_url, "branch": "main"}
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE projects SET git_url = ? WHERE id = ?", (origin_url, project_id)
        )
        git_snapshot_id = conn.execute(
            """
            INSERT INTO git_snapshots (
                project_id, analysis_lineage_id, branch, from_commit, to_commit,
                commits_json, commit_count, changed_file_count, added_lines,
                deleted_lines, diff_bytes, file_manifest_hash, frozen_at
            ) VALUES (?, 1, 'main', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                commit_a,
                commit_b,
                git_snapshot_for_hash["commits_json"],
                len(commits),
                len(file_records),
                git_snapshot_for_hash["added_lines"],
                git_snapshot_for_hash["deleted_lines"],
                diff_bytes,
                manifest_hash,
                NOW,
            ),
        ).lastrowid
        for record in file_records:
            evidence_id = f"git:file:{git_snapshot_id}:{record['ordinal']:03d}"
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
                    evidence_id,
                    record["path"],
                    record["added_lines"],
                    record["deleted_lines"],
                    1 if record["is_binary"] else 0,
                    record["file_facts_hash"],
                ),
            )
        target_evidence_id = (
            f"git:file:{git_snapshot_id}:{target_record['ordinal']:03d}"
        )
        snapshot_id = conn.execute(
            """
            INSERT INTO evidence_snapshots (
                schema_version, project_id, git_snapshot_id, analysis_lineage_id,
                branch, from_commit, to_commit, project_repository_url,
                project_config_hash, git_facts_hash, prd_id, prd_source_hash,
                prd_parsed_hash, prd_structured_hash, prd_document_fingerprint,
                profile_id, profile_content_hash, snapshot_hash, frozen_at
            ) VALUES (?, ?, ?, 1, 'main', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                SNAPSHOT_SCHEMA,
                project_id,
                git_snapshot_id,
                commit_a,
                commit_b,
                origin_url,
                project_config_hash,
                git_facts_hash,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                "6" * 64,
                NOW,
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO evidence_items (
                snapshot_id, evidence_id, type, source_ref, content_hash,
                selected, redaction_state
            ) VALUES (?, ?, 'git_file_fact', ?, ?, 1, 'not_applicable')
            """,
            (
                snapshot_id,
                target_evidence_id,
                f"git_file_evidence:{git_snapshot_id}:{target_record['ordinal']}",
                target_record["file_facts_hash"],
            ),
        )

    return {
        "db_path": db_path,
        "projects_root": projects_root,
        "project_id": project_id,
        "repo": repo,
        "origin_url": origin_url,
        "from_commit": commit_a,
        "to_commit": commit_b,
        "git_snapshot_id": git_snapshot_id,
        "snapshot_id": snapshot_id,
        "evidence_id": target_evidence_id,
        "target_path": target_path,
        "target_record": target_record,
        "manifest_hash": manifest_hash,
        "git_facts_hash": git_facts_hash,
        "project_config_hash": project_config_hash,
    }


@pytest.fixture()
def frozen_git(tmp_path, monkeypatch):
    return _freeze_range(tmp_path, monkeypatch)


def _resolve(state: dict) -> dict:
    return context_resolver.resolve_git_file_fact(
        state["snapshot_id"], state["evidence_id"]
    )


def _assert_code(state: dict, code: str) -> None:
    with pytest.raises(HTTPException) as caught:
        _resolve(state)
    assert caught.value.detail["code"] == code


def test_t01_exact_text_diff_fixed_invocation_and_idempotency(
    frozen_git, monkeypatch
):
    seen: list[list[str]] = []

    class RecordingGitClient(GitClient):
        def _exec(self, args, *, cwd):
            seen.append(list(args))
            return super()._exec(args, cwd=cwd)

        def _run_bounded_process(self, args, **kwargs):
            seen.append(list(args))
            return super()._run_bounded_process(args, **kwargs)

    monkeypatch.setattr(context_resolver, "_git_client_factory", RecordingGitClient)
    first = _resolve(frozen_git)
    second = _resolve(frozen_git)
    expected = _exact_diff(
        frozen_git["repo"],
        frozen_git["from_commit"],
        frozen_git["to_commit"],
        frozen_git["target_path"],
    )
    assert first == second
    assert first["diff_text"].encode("utf-8") == expected
    assert first["resolved_content_hash"] == hashlib.sha256(expected).hexdigest()
    assert first["resolved_diff_bytes"] == len(expected)
    assert first["resolved_content_redaction_state"] == "pending"
    assert first["content_hash"] == frozen_git["target_record"]["file_facts_hash"]
    patch_calls = [args for args in seen if "diff" in args and "--patch" in args]
    assert patch_calls
    for args in patch_calls:
        for required in (
            "--no-replace-objects",
            "--no-lazy-fetch",
            "--literal-pathspecs",
            f"--attr-source={frozen_git['to_commit']}",
            f"core.attributesFile={os.devnull}",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--unified=3",
            "--inter-hunk-context=0",
            "--full-index",
            "--default-prefix",
            "--no-relative",
        ):
            assert required in args


def test_t02_historical_range_and_attributes_ignore_current_c(frozen_git):
    first = _resolve(frozen_git)
    _write(
        frozen_git["repo"],
        ".gitattributes",
        b"src/example.txt -diff binary\n",
    )
    _write(frozen_git["repo"], "later.txt", b"C\n")
    _commit(frozen_git["repo"], "C changes attributes")
    second = _resolve(frozen_git)
    assert second["diff_text"] == first["diff_text"]
    assert second["resolved_content_hash"] == first["resolved_content_hash"]
    assert frozen_git["to_commit"] not in {
        _git(frozen_git["repo"], "rev-parse", "HEAD").decode().strip()
    }


def test_t03_current_project_config_drift_is_ignored(frozen_git):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE projects SET git_url = 'https://new.example.invalid/repo.git', "
            "branch = 'other' WHERE id = ?",
            (frozen_git["project_id"],),
        )
    assert _resolve(frozen_git)["path"] == frozen_git["target_path"]


@pytest.mark.parametrize("field", ["project_repository_url", "project_config_hash"])
def test_t03_frozen_repository_identity_tamper_fails_closed(frozen_git, field):
    value = (
        "https://tampered.example.invalid/repo.git"
        if field == "project_repository_url"
        else "f" * 64
    )
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            f"UPDATE evidence_snapshots SET {field} = ? WHERE id = ?",
            (value, frozen_git["snapshot_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t03_workspace_origin_replacement_fails_closed(frozen_git):
    _git(
        frozen_git["repo"],
        "remote",
        "set-url",
        "origin",
        "https://other.example.invalid/repo.git",
    )
    _assert_code(frozen_git, "GIT_WORKSPACE_CONFLICT")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "evidence_snapshot_core_v1"),
        ("git_snapshot_id", "bad"),
        ("from_commit", "BAD"),
        ("to_commit", "A" * 40),
        ("project_config_hash", "bad"),
        ("git_facts_hash", "bad"),
    ],
)
def test_t04_snapshot_admission_is_strict(frozen_git, field, value):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            f"UPDATE evidence_snapshots SET {field} = ? WHERE id = ?",
            (value, frozen_git["snapshot_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_SNAPSHOT_UNSUPPORTED")


def test_t04_malformed_repository_url_with_self_consistent_hash_is_unsupported(
    frozen_git,
):
    invalid_url = "not-a-valid-git-url"
    invalid_hash = _stable_hash({"repository_url": invalid_url, "branch": "main"})
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_snapshots SET project_repository_url = ?, "
            "project_config_hash = ? WHERE id = ?",
            (invalid_url, invalid_hash, frozen_git["snapshot_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_SNAPSHOT_UNSUPPORTED")


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "prd_block"),
        ("selected", 0),
        ("redaction_state", "pending"),
        ("content_hash", "bad"),
    ],
)
def test_t05_item_admission_is_strict(frozen_git, field, value):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            f"UPDATE evidence_items SET {field} = ? "
            "WHERE snapshot_id = ? AND evidence_id = ?",
            (value, frozen_git["snapshot_id"], frozen_git["evidence_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_ITEM_INVALID")


@pytest.mark.parametrize(
    "source_ref",
    ["wrong:1:1", "git_file_evidence:1:0", "git_file_evidence:1:01"],
)
def test_t05_source_ref_is_canonical(frozen_git, source_ref):
    if source_ref.startswith("git_file_evidence:1:"):
        source_ref = source_ref.replace(
            "git_file_evidence:1:",
            f"git_file_evidence:{frozen_git['git_snapshot_id']}:",
        )
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_items SET source_ref = ? "
            "WHERE snapshot_id = ? AND evidence_id = ?",
            (source_ref, frozen_git["snapshot_id"], frozen_git["evidence_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_ITEM_INVALID")


def test_t05_source_ref_wrong_git_snapshot_id_is_invalid(frozen_git):
    wrong_source_ref = (
        f"git_file_evidence:{frozen_git['git_snapshot_id'] + 1}:"
        f"{frozen_git['target_record']['ordinal']}"
    )
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_items SET source_ref = ? "
            "WHERE snapshot_id = ? AND evidence_id = ?",
            (wrong_source_ref, frozen_git["snapshot_id"], frozen_git["evidence_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_ITEM_INVALID")


@pytest.mark.parametrize(
    "field,value",
    [
        ("branch", "other"),
        ("from_commit", "e" * 40),
        ("to_commit", "f" * 40),
        ("commit_count", 2),
        ("commits_json", json.dumps(["f" * 40], separators=(",", ":"))),
        ("added_lines", 999),
    ],
)
def test_t06_git_snapshot_identity_and_git_facts_close(frozen_git, field, value):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            f"UPDATE git_snapshots SET {field} = ? WHERE id = ?",
            (value, frozen_git["git_snapshot_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t06_snapshot_git_facts_hash_drift_fails_closed(frozen_git):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_snapshots SET git_facts_hash = ? WHERE id = ?",
            ("f" * 64, frozen_git["snapshot_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "changed.txt"),
        ("file_facts_hash", "f" * 64),
        ("evidence_id", "git:file:999:001"),
    ],
)
def test_t07_file_evidence_and_manifest_close(frozen_git, field, value):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            f"UPDATE git_file_evidence SET {field} = ? WHERE evidence_id = ?",
            (value, frozen_git["evidence_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t07_ordinal_gap_fails_closed(frozen_git):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE git_file_evidence SET ordinal = ordinal + 1 WHERE evidence_id = ?",
            (frozen_git["evidence_id"],),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t07_line_fact_drift_fails_closed(frozen_git):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE git_file_evidence SET added_lines = added_lines + 1 "
            "WHERE evidence_id = ?",
            (frozen_git["evidence_id"],),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t07_binary_fact_drift_fails_closed(frozen_git):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE git_file_evidence SET added_lines = NULL, deleted_lines = NULL, "
            "is_binary = 1 WHERE evidence_id = ?",
            (frozen_git["evidence_id"],),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t07_manifest_hash_drift_fails_closed(frozen_git):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE git_snapshots SET file_manifest_hash = ? WHERE id = ?",
            ("f" * 64, frozen_git["git_snapshot_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t07_item_content_hash_valid_but_mismatched_is_frozen_facts_invalid(
    frozen_git,
):
    with sqlite3.connect(frozen_git["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_items SET content_hash = ? "
            "WHERE snapshot_id = ? AND evidence_id = ?",
            ("e" * 64, frozen_git["snapshot_id"], frozen_git["evidence_id"]),
        )
    _assert_code(frozen_git, "CONTEXT_GIT_FROZEN_FACTS_INVALID")


def test_t08_missing_local_object_is_range_unavailable(frozen_git):
    _write(frozen_git["repo"], "later.txt", b"C\n")
    _commit(frozen_git["repo"], "C keeps current tree readable")
    object_path = (
        frozen_git["repo"]
        / ".git"
        / "objects"
        / frozen_git["to_commit"][:2]
        / frozen_git["to_commit"][2:]
    )
    if not object_path.exists():
        pytest.skip("fixture commit object is not loose")
    os.chmod(object_path, stat.S_IREAD | stat.S_IWRITE)
    object_path.unlink()
    _assert_code(frozen_git, "CONTEXT_GIT_RANGE_UNAVAILABLE")


def test_t08_promisor_config_preserves_workspace_conflict_before_replay(
    frozen_git, monkeypatch
):
    config = frozen_git["repo"] / ".git" / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write("\n[extensions]\n\tpartialClone = origin\n")

    def replay_must_not_run(*args, **kwargs):
        raise AssertionError(
            "object replay must not run after safe-local-config rejects promisor config"
        )

    monkeypatch.setattr(GitClient, "_run_bounded_process", replay_must_not_run)
    _assert_code(frozen_git, "GIT_WORKSPACE_CONFLICT")


def test_t08_replace_ref_does_not_change_replay(frozen_git):
    baseline = _resolve(frozen_git)
    _git(
        frozen_git["repo"],
        "replace",
        frozen_git["to_commit"],
        frozen_git["from_commit"],
    )
    after = _resolve(frozen_git)
    assert after["diff_text"] == baseline["diff_text"]
    assert after["resolved_content_hash"] == baseline["resolved_content_hash"]


def test_t09_literal_leading_dash_path_does_not_expand(
    frozen_git, tmp_path, monkeypatch
):
    state = _freeze_range(
        tmp_path / "literal",
        monkeypatch,
        target_path="-target.txt",
        a_bytes=b"one\n",
        b_bytes=b"one\ntwo\n",
        decoy=("decoy.txt", b"old\n", b"new\n"),
    )
    result = _resolve(state)
    expected = _exact_diff(
        state["repo"], state["from_commit"], state["to_commit"], "-target.txt"
    )
    assert result["diff_text"].encode() == expected
    assert "a/decoy.txt" not in result["diff_text"]


def test_t10_binary_is_metadata_only(tmp_path, monkeypatch):
    state = _freeze_range(
        tmp_path,
        monkeypatch,
        target_path="asset.bin",
        a_bytes=b"\x00old",
        b_bytes=b"\x00new",
    )
    result = _resolve(state)
    assert result["is_binary"] is True
    assert result["diff_text"] is None
    assert result["resolved_content_hash"] is None
    assert result["resolved_diff_bytes"] == 0
    assert result["resolved_content_redaction_state"] == "not_applicable"
    assert result["content_kind"] == "binary_metadata_only"


def test_t10_invalid_utf8_diff_fails_closed(tmp_path, monkeypatch):
    state = _freeze_range(
        tmp_path,
        monkeypatch,
        target_path="invalid.txt",
        a_bytes=b"\xff-old\n",
        b_bytes=b"\xfe-new\n",
    )
    if state["target_record"]["is_binary"]:
        pytest.skip("Git classified invalid UTF-8 fixture as binary")
    _assert_code(state, "CONTEXT_GIT_DIFF_INVALID")


@pytest.mark.parametrize("over", [False, True])
def test_t10_exact_size_boundary(frozen_git, monkeypatch, over):
    class BoundaryGitClient(GitClient):
        def _run_bounded_process(self, args, **kwargs):
            if "--patch" in args:
                return (DIFF_LIMIT + 1, True) if over else (DIFF_LIMIT, False)
            return super()._run_bounded_process(args, **kwargs)

        def _exec(self, args, *, cwd):
            if "--patch" in args:
                if over:
                    raise AssertionError(
                        "capture must not run after bounded over-limit result"
                    )
                return 0, b"x" * DIFF_LIMIT, b""
            return super()._exec(args, cwd=cwd)

    monkeypatch.setattr(context_resolver, "_git_client_factory", BoundaryGitClient)
    if over:
        _assert_code(frozen_git, "CONTEXT_GIT_DIFF_TOO_LARGE")
    else:
        result = _resolve(frozen_git)
        assert result["resolved_diff_bytes"] == DIFF_LIMIT
        assert len(result["diff_text"].encode()) == DIFF_LIMIT


def test_t11_no_fetch_network_or_checkout_entrypoint_is_used(frozen_git, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network/update entrypoint must not run")

    for name in (
        "fetch_branch",
        "get_remote_head",
        "clone_branch",
        "checkout_remote_head",
    ):
        monkeypatch.setattr(GitClient, name, boom)
    assert _resolve(frozen_git)["path"] == frozen_git["target_path"]


def test_t11_user_attributes_are_neutralized(frozen_git, monkeypatch, tmp_path):
    baseline = _resolve(frozen_git)
    xdg = tmp_path / "xdg"
    attributes = xdg / "git" / "attributes"
    attributes.parent.mkdir(parents=True)
    attributes.write_text("src/example.txt -diff binary\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    after = _resolve(frozen_git)
    assert after["diff_text"] == baseline["diff_text"]
    assert after["resolved_content_hash"] == baseline["resolved_content_hash"]


def test_t11_nonempty_info_attributes_fails_closed(frozen_git):
    attributes = frozen_git["repo"] / ".git" / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("src/example.txt binary\n", encoding="utf-8")
    _assert_code(frozen_git, "CONTEXT_GIT_ATTRIBUTES_UNSAFE")


@pytest.mark.skipif(
    os.name == "nt", reason="Windows symlink creation requires runner privileges"
)
def test_t11_info_attributes_symlink_preserves_workspace_escape(frozen_git, tmp_path):
    outside = tmp_path / "outside-attributes"
    outside.write_text("src/example.txt binary\n", encoding="utf-8")
    attributes = frozen_git["repo"] / ".git" / "info" / "attributes"
    attributes.symlink_to(outside)
    _assert_code(frozen_git, "GIT_WORKSPACE_ESCAPE")


def test_t11_dirty_workspace_preserves_git_workspace_dirty(frozen_git):
    _write(
        frozen_git["repo"], frozen_git["target_path"], b"uncommitted\n"
    )
    _assert_code(frozen_git, "GIT_WORKSPACE_DIRTY")


def test_t12_failure_path_is_zero_side_effect(frozen_git):
    _write(
        frozen_git["repo"], frozen_git["target_path"], b"uncommitted failure\n"
    )
    before = _state_signature(frozen_git)
    _assert_code(frozen_git, "GIT_WORKSPACE_DIRTY")
    after = _state_signature(frozen_git)
    assert after == before


def test_t12_deterministic_bytes_and_zero_side_effects(frozen_git):
    before = _state_signature(frozen_git)
    first = _resolve(frozen_git)
    after = _state_signature(frozen_git)
    assert after == before

    _git(
        frozen_git["repo"],
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=b"unrelated-object",
    )
    second_before = _state_signature(frozen_git)
    second = _resolve(frozen_git)
    second_after = _state_signature(frozen_git)
    assert second_after == second_before
    assert second["diff_text"] == first["diff_text"]
    assert second["resolved_content_hash"] == first["resolved_content_hash"]

    _write(frozen_git["repo"], "current-only.txt", b"C\n")
    _commit(frozen_git["repo"], "C after frozen B")
    third_before = _state_signature(frozen_git)
    third = _resolve(frozen_git)
    third_after = _state_signature(frozen_git)
    assert third_after == third_before
    assert third["diff_text"] == first["diff_text"]
    assert third["resolved_content_hash"] == first["resolved_content_hash"]
