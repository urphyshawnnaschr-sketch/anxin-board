"""GitSnapshot 确认的精确匹配、漂移拒绝、幂等与不可变事实测试。"""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import git_connections, git_snapshots, main  # noqa: E402


A = "a" * 40
B = "b" * 40
C = "c" * 40
COMMITS_AB = ["1" * 40, B]


class SnapshotGitClient:
    origin_head = A
    fetched_head = A
    continuity = "continuous"
    capacity_exceeded = False

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
        return [("5", "2", "one.py"), ("3", "1", "two.py")], type(self).capacity_exceeded

    def measure_unified_diff_bytes(self, access, base_commit, head_commit, byte_limit):
        return 321, False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(tmp_path / "projects"))
    SnapshotGitClient.origin_head = A
    SnapshotGitClient.fetched_head = A
    SnapshotGitClient.continuity = "continuous"
    SnapshotGitClient.capacity_exceeded = False
    monkeypatch.setattr(git_connections, "_git_client_factory", SnapshotGitClient)
    monkeypatch.setattr(git_snapshots, "_git_client_factory", SnapshotGitClient)
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path


def _prepare(client):
    project = client.post("/api/projects", json={"name": "GitSnapshot 项目"}).json()
    project = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "git_url": "https://example.invalid/org/repo.git",
            "branch": "main",
            "version": project["version"],
        },
    ).json()["project"]
    connected = client.post(f"/api/projects/{project['id']}/git/check")
    assert connected.status_code == 200, connected.text
    lineage_response = client.post(f"/api/projects/{project['id']}/analysis-lineages")
    assert lineage_response.status_code == 201, lineage_response.text
    return project, lineage_response.json()["lineage"]


def _confirm(client, project_id, lineage, *, expected_to=B):
    return client.post(
        f"/api/projects/{project_id}/git-snapshots",
        json={
            "expected_lineage_id": lineage["id"],
            "expected_from_commit": lineage["baseline_commit"],
            "expected_to_commit": expected_to,
        },
    )


def _db_state(db_path, project_id):
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM git_snapshots WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        lineage = conn.execute(
            "SELECT id, baseline_commit, status FROM analysis_lineages "
            "WHERE project_id = ? ORDER BY sequence_no",
            (project_id,),
        ).fetchall()
        connection = conn.execute(
            "SELECT status, remote_head, local_head FROM project_git_connections WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return count, lineage, connection


def test_t01_exact_match_freezes_git_facts(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["snapshot"] == {
        "id": body["snapshot"]["id"],
        "project_id": project["id"],
        "analysis_lineage_id": lineage["id"],
        "branch": "main",
        "from_commit": A,
        "to_commit": B,
        "commits": COMMITS_AB,
        "commit_count": 2,
        "changed_file_count": 2,
        "added_lines": 8,
        "deleted_lines": 3,
        "diff_bytes": 321,
        "frozen_at": body["snapshot"]["frozen_at"],
    }
    count, lineages, connection = _db_state(tmp_path / "test.db", project["id"])
    assert count == 1
    assert lineages == [(lineage["id"], A, "active")]
    assert connection == ("connected", A, A)


def test_t02_push_before_confirmation_is_stale_and_zero_write(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    SnapshotGitClient().fetch_branch(None, "main")

    # 项目经理看到 A→B 后，远端新增 C；workspace 尚未重新 fetch，仍只看到 B。
    SnapshotGitClient.origin_head = C
    assert SnapshotGitClient.origin_head == C
    assert SnapshotGitClient.fetched_head == B

    response = _confirm(api, project["id"], lineage, expected_to=B)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_TO_COMMIT_STALE"
    assert SnapshotGitClient.fetched_head == C
    count, lineages, connection = _db_state(tmp_path / "test.db", project["id"])
    assert count == 0
    assert lineages == [(lineage["id"], A, "active")]
    assert connection == ("connected", A, A)


def test_t03_lineage_change_is_rejected_without_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B

    response = api.post(
        f"/api/projects/{project['id']}/git-snapshots",
        json={
            "expected_lineage_id": lineage["id"] + 1,
            "expected_from_commit": A,
            "expected_to_commit": B,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANALYSIS_LINEAGE_MISMATCH"
    assert _db_state(tmp_path / "test.db", project["id"])[0] == 0


def test_t04_from_commit_change_is_rejected_without_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    changed_from = "d" * 40
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE analysis_lineages SET baseline_commit = ? WHERE id = ?",
            (changed_from, lineage["id"]),
        )
    SnapshotGitClient.origin_head = B

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_FROM_COMMIT_STALE"
    count, lineages, _connection = _db_state(tmp_path / "test.db", project["id"])
    assert count == 0
    assert lineages == [(lineage["id"], changed_from, "active")]


def test_t05_duplicate_confirmation_after_remote_push_returns_same_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B

    first = _confirm(api, project["id"], lineage)
    first_snapshot = first.json()["snapshot"]
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.row_factory = sqlite3.Row
        frozen_row = dict(
            conn.execute(
                "SELECT * FROM git_snapshots WHERE id = ?", (first_snapshot["id"],)
            ).fetchone()
        )

    SnapshotGitClient.origin_head = C
    assert SnapshotGitClient.fetched_head == B

    second = _confirm(api, project["id"], lineage)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["snapshot"] == first_snapshot
    assert SnapshotGitClient.fetched_head == C
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM git_snapshots")]
    assert rows == [frozen_row]


def test_t06_push_after_freeze_does_not_change_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    frozen = _confirm(api, project["id"], lineage).json()["snapshot"]

    SnapshotGitClient.origin_head = C

    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM git_snapshots WHERE id = ?", (frozen["id"],)).fetchone()
    assert row["from_commit"] == A
    assert row["to_commit"] == B
    assert row["commits_json"] == '["' + COMMITS_AB[0] + '","' + B + '"]'
    assert row["commit_count"] == 2
    assert row["changed_file_count"] == 2
    assert row["added_lines"] == 8
    assert row["deleted_lines"] == 3
    assert row["diff_bytes"] == 321


def test_t07_capacity_exceeded_is_blocked_without_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    SnapshotGitClient.capacity_exceeded = True
    db_path = tmp_path / "test.db"
    before_state = _db_state(db_path, project["id"])
    assert before_state == (
        0,
        [(lineage["id"], A, "active")],
        ("connected", A, A),
    )

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_CAPACITY_EXCEEDED"
    assert _db_state(db_path, project["id"]) == before_state


def test_t08_checkpoint_unreachable_is_blocked_without_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    SnapshotGitClient.continuity = "checkpoint_unreachable"
    db_path = tmp_path / "test.db"
    before_state = _db_state(db_path, project["id"])
    assert before_state == (
        0,
        [(lineage["id"], A, "active")],
        ("connected", A, A),
    )

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_CHECKPOINT_UNREACHABLE"
    assert _db_state(db_path, project["id"]) == before_state


def test_no_new_commit_is_blocked_without_empty_snapshot(client):
    api, tmp_path = client
    project, lineage = _prepare(api)

    response = _confirm(api, project["id"], lineage, expected_to=A)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_EMPTY_RANGE"
    assert _db_state(tmp_path / "test.db", project["id"])[0] == 0


def test_git_config_change_after_git_read_is_rejected_by_transaction_cas(client, monkeypatch):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    real_read = git_snapshots._read_current_candidate

    def read_then_change_config(project_id, current_project, current_lineage):
        candidate = real_read(project_id, current_project, current_lineage)
        with sqlite3.connect(tmp_path / "test.db") as conn:
            conn.execute(
                "UPDATE projects SET git_url = ? WHERE id = ?",
                ("https://example.invalid/other/repo.git", project_id),
            )
        return candidate

    monkeypatch.setattr(git_snapshots, "_read_current_candidate", read_then_change_config)

    response = _confirm(api, project["id"], lineage)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_CONFIG_STALE"
    assert _db_state(tmp_path / "test.db", project["id"])[0] == 0


def test_active_lineage_change_after_git_read_is_rejected_by_transaction_cas(
    client, monkeypatch
):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    real_read = git_snapshots._read_current_candidate
    observed = {}

    def read_then_switch_active_lineage(project_id, current_project, current_lineage):
        candidate = real_read(project_id, current_project, current_lineage)
        observed["candidate"] = candidate
        with sqlite3.connect(tmp_path / "test.db") as conn:
            next_sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM analysis_lineages "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE analysis_lineages SET status = 'closed', closed_at = ? WHERE id = ?",
                ("2026-08-07T00:00:00+00:00", current_lineage["id"]),
            )
            cursor = conn.execute(
                """
                INSERT INTO analysis_lineages (
                    project_id, sequence_no, branch, baseline_commit, status, created_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (
                    project_id,
                    next_sequence,
                    current_project["branch"],
                    A,
                    "2026-08-07T00:00:01+00:00",
                ),
            )
            observed["new_lineage_id"] = cursor.lastrowid
        return candidate

    monkeypatch.setattr(
        git_snapshots, "_read_current_candidate", read_then_switch_active_lineage
    )

    response = _confirm(api, project["id"], lineage)

    assert observed["candidate"].baseline_commit == A
    assert observed["candidate"].remote_head == B
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANALYSIS_LINEAGE_MISMATCH"
    count, lineages, connection = _db_state(tmp_path / "test.db", project["id"])
    assert count == 0
    assert lineages == [
        (lineage["id"], A, "closed"),
        (observed["new_lineage_id"], A, "active"),
    ]
    assert connection == ("connected", A, A)


def test_baseline_change_after_git_read_is_rejected_by_transaction_cas(
    client, monkeypatch
):
    api, tmp_path = client
    project, lineage = _prepare(api)
    SnapshotGitClient.origin_head = B
    changed_from = "d" * 40
    real_read = git_snapshots._read_current_candidate
    observed = {}

    def read_then_change_baseline(project_id, current_project, current_lineage):
        candidate = real_read(project_id, current_project, current_lineage)
        observed["candidate"] = candidate
        with sqlite3.connect(tmp_path / "test.db") as conn:
            conn.execute(
                "UPDATE analysis_lineages SET baseline_commit = ? WHERE id = ?",
                (changed_from, current_lineage["id"]),
            )
        return candidate

    monkeypatch.setattr(
        git_snapshots, "_read_current_candidate", read_then_change_baseline
    )

    response = _confirm(api, project["id"], lineage)

    assert observed["candidate"].baseline_commit == A
    assert observed["candidate"].remote_head == B
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_SNAPSHOT_FROM_COMMIT_STALE"
    count, lineages, connection = _db_state(tmp_path / "test.db", project["id"])
    assert count == 0
    assert lineages == [(lineage["id"], changed_from, "active")]
    assert connection == ("connected", A, A)
