"""GitSnapshot 同源 FileEvidence Manifest 的原子冻结与幂等测试。"""

import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db, git_connections, git_snapshots, main  # noqa: E402


A = "a" * 40
B = "b" * 40
C = "c" * 40
COMMITS_AB = ["1" * 40, B]
DEFAULT_ROWS = [("5", "2", "one.py"), ("3", "1", "two.py")]


def _stable_hash(value):
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FileEvidenceGitClient:
    origin_head = A
    fetched_head = A
    continuity = "continuous"
    capacity_exceeded = False
    numstat_rows = DEFAULT_ROWS.copy()
    numstat_calls = 0

    def get_remote_head(self, url, branch):
        return type(self).origin_head

    def get_version(self):
        return "git version 2.49.0.windows.1"

    def validate_branch(self, branch):
        pass

    def clone_branch(self, url, branch, access):
        (access.path / ".git").mkdir(parents=True, exist_ok=True)

    def inspect_workspace(self, access):
        pass

    def assert_clean(self, access):
        pass

    def fetch_branch(self, access, branch):
        type(self).fetched_head = type(self).origin_head

    def checkout_remote_head(self, access, branch):
        pass

    def assert_size_limit(self, access):
        pass

    def get_local_head(self, access):
        return type(self).fetched_head

    def get_remote_head_ref(self, access, branch):
        return type(self).fetched_head

    def is_ancestor(self, access, ancestor_commit, descendant_commit):
        return type(self).continuity == "continuous"

    def list_commits_bounded(self, access, base_commit, head_commit, commit_limit):
        return COMMITS_AB.copy(), False

    def count_commits(self, access, base_commit, head_commit):
        return len(COMMITS_AB)

    def get_numstat_bounded(self, access, base_commit, head_commit, line_limit):
        type(self).numstat_calls += 1
        return type(self).numstat_rows.copy(), type(self).capacity_exceeded

    def measure_unified_diff_bytes(self, access, base_commit, head_commit, byte_limit):
        return 321, False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(tmp_path / "projects"))
    FileEvidenceGitClient.origin_head = A
    FileEvidenceGitClient.fetched_head = A
    FileEvidenceGitClient.continuity = "continuous"
    FileEvidenceGitClient.capacity_exceeded = False
    FileEvidenceGitClient.numstat_rows = DEFAULT_ROWS.copy()
    FileEvidenceGitClient.numstat_calls = 0
    monkeypatch.setattr(git_connections, "_git_client_factory", FileEvidenceGitClient)
    monkeypatch.setattr(git_snapshots, "_git_client_factory", FileEvidenceGitClient)
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path / "test.db"


def _prepare(api):
    project = api.post("/api/projects", json={"name": "FileEvidence 项目"}).json()
    project = api.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "git_url": "https://example.invalid/org/repo.git",
            "branch": "main",
            "version": project["version"],
        },
    ).json()["project"]
    connected = api.post(f"/api/projects/{project['id']}/git/check")
    assert connected.status_code == 200, connected.text
    lineage = api.post(f"/api/projects/{project['id']}/analysis-lineages")
    assert lineage.status_code == 201, lineage.text
    return project, lineage.json()["lineage"]


def _confirm(api, project_id, lineage, *, expected_to=B):
    return api.post(
        f"/api/projects/{project_id}/git-snapshots",
        json={
            "expected_lineage_id": lineage["id"],
            "expected_from_commit": lineage["baseline_commit"],
            "expected_to_commit": expected_to,
        },
    )


def _manifest_state(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        snapshots = [
            dict(row)
            for row in conn.execute("SELECT * FROM git_snapshots ORDER BY id")
        ]
        files = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM git_file_evidence ORDER BY git_snapshot_id, ordinal"
            )
        ]
    return snapshots, files


def _expected_manifest(rows):
    records = []
    for ordinal, (added_raw, deleted_raw, path) in enumerate(rows, start=1):
        is_binary = added_raw == "-"
        facts = {
            "path": path,
            "added_lines": None if is_binary else int(added_raw),
            "deleted_lines": None if is_binary else int(deleted_raw),
            "is_binary": is_binary,
        }
        records.append(
            {
                "ordinal": ordinal,
                **facts,
                "file_facts_hash": _stable_hash(facts),
            }
        )
    manifest_hash = _stable_hash(
        {
            "schema_version": "git_file_manifest_v1",
            "files": records,
        }
    )
    return manifest_hash, records


def test_normal_freeze_persists_complete_file_manifest_from_same_numstat_read(client):
    api, db_path = client
    project, lineage = _prepare(api)
    FileEvidenceGitClient.origin_head = B

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    snapshot = response.json()["snapshot"]
    assert "file_manifest_hash" not in snapshot
    assert FileEvidenceGitClient.numstat_calls == 1

    snapshots, files = _manifest_state(db_path)
    assert len(snapshots) == 1
    assert len(files) == 2
    manifest_hash, expected_records = _expected_manifest(DEFAULT_ROWS)
    assert snapshots[0]["file_manifest_hash"] == manifest_hash
    assert snapshots[0]["changed_file_count"] == len(files) == 2
    assert snapshots[0]["added_lines"] == 8
    assert snapshots[0]["deleted_lines"] == 3

    for actual, expected in zip(files, expected_records, strict=True):
        assert actual["git_snapshot_id"] == snapshot["id"]
        assert actual["ordinal"] == expected["ordinal"]
        assert actual["evidence_id"] == (
            f"git:file:{snapshot['id']}:{expected['ordinal']:03d}"
        )
        assert actual["path"] == expected["path"]
        assert actual["added_lines"] == expected["added_lines"]
        assert actual["deleted_lines"] == expected["deleted_lines"]
        assert actual["is_binary"] == int(expected["is_binary"])
        assert actual["file_facts_hash"] == expected["file_facts_hash"]


def test_binary_file_is_frozen_without_inventing_line_counts(client):
    api, db_path = client
    project, lineage = _prepare(api)
    rows = [("-", "-", "blob.bin"), ("2", "0", "text.txt")]
    FileEvidenceGitClient.numstat_rows = rows
    FileEvidenceGitClient.origin_head = B

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 201, response.text
    snapshots, files = _manifest_state(db_path)
    assert snapshots[0]["changed_file_count"] == 2
    assert snapshots[0]["added_lines"] == 2
    assert snapshots[0]["deleted_lines"] == 0
    assert files[0]["path"] == "blob.bin"
    assert files[0]["is_binary"] == 1
    assert files[0]["added_lines"] is None
    assert files[0]["deleted_lines"] is None
    assert files[1]["is_binary"] == 0

    manifest_hash, _records = _expected_manifest(rows)
    assert snapshots[0]["file_manifest_hash"] == manifest_hash


def test_exact_retry_after_remote_push_does_not_mutate_frozen_manifest(client):
    api, db_path = client
    project, lineage = _prepare(api)
    FileEvidenceGitClient.origin_head = B
    first = _confirm(api, project["id"], lineage)
    assert first.status_code == 201, first.text
    before = _manifest_state(db_path)

    FileEvidenceGitClient.origin_head = C
    FileEvidenceGitClient.numstat_rows = [("99", "0", "new.py")]
    second = _confirm(api, project["id"], lineage, expected_to=B)

    assert second.status_code == 201, second.text
    assert second.json()["created"] is False
    assert second.json()["snapshot"] == first.json()["snapshot"]
    assert _manifest_state(db_path) == before
    assert FileEvidenceGitClient.numstat_calls == 2


def test_capacity_exceeded_creates_neither_snapshot_nor_file_manifest(client):
    api, db_path = client
    project, lineage = _prepare(api)
    FileEvidenceGitClient.origin_head = B
    FileEvidenceGitClient.capacity_exceeded = True

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_CAPACITY_EXCEEDED"
    assert _manifest_state(db_path) == ([], [])


def test_file_manifest_insert_failure_rolls_back_parent_snapshot(client):
    api, db_path = client
    project, lineage = _prepare(api)
    FileEvidenceGitClient.origin_head = B
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_git_file_evidence
            BEFORE INSERT ON git_file_evidence
            BEGIN
                SELECT RAISE(ABORT, 'forced file evidence failure');
            END
            """
        )

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_SAVE_FAILED"
    assert "forced file evidence failure" not in response.text
    assert _manifest_state(db_path) == ([], [])


def test_legacy_snapshot_without_manifest_remains_replayable_and_is_not_backfilled(client):
    api, db_path = client
    project, lineage = _prepare(api)
    now = "2026-08-08T00:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        legacy_id = conn.execute(
            """
            INSERT INTO git_snapshots (
                project_id, analysis_lineage_id, branch, from_commit, to_commit,
                commits_json, commit_count, changed_file_count, added_lines,
                deleted_lines, diff_bytes, file_manifest_hash, frozen_at
            ) VALUES (?, ?, 'main', ?, ?, ?, 2, 2, 8, 3, 321, NULL, ?)
            """,
            (
                project["id"],
                lineage["id"],
                A,
                B,
                json.dumps(COMMITS_AB, separators=(",", ":")),
                now,
            ),
        ).lastrowid

    FileEvidenceGitClient.origin_head = C
    response = _confirm(api, project["id"], lineage, expected_to=B)

    assert response.status_code == 201, response.text
    assert response.json()["created"] is False
    assert response.json()["snapshot"]["id"] == legacy_id
    snapshots, files = _manifest_state(db_path)
    assert snapshots[0]["file_manifest_hash"] is None
    assert files == []
