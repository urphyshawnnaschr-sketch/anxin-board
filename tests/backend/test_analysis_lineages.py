"""分析链路 API 与数据库状态测试；GitClient 用受控桩代替，不访问互联网。"""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import analysis_lineages, git_connections, main, projects  # noqa: E402
from app.git_workspace_locks import GitOperationInProgress  # noqa: E402


HEAD = "5" * 40


class SuccessfulGitClient:
    calls = []

    def __init__(self):
        type(self).calls.append("init")

    def get_remote_head(self, url, branch):
        return HEAD

    def get_version(self):
        return "git version 2.49.0.windows.1"

    def validate_branch(self, branch):
        pass

    def clone_branch(self, url, branch, access):
        target = access.path
        (target / ".git").mkdir(parents=True, exist_ok=True)

    def inspect_workspace(self, access):
        pass

    def assert_clean(self, access):
        pass

    def fetch_branch(self, access, branch):
        type(self).calls.append(("fetch", branch))

    def checkout_remote_head(self, access, branch):
        pass

    def assert_size_limit(self, access):
        pass

    def get_local_head(self, access):
        return HEAD

    def get_remote_head_ref(self, access, branch):
        return HEAD

    def is_ancestor(self, access, ancestor_commit, descendant_commit):
        return True

    def list_commits(self, access, base_commit, head_commit):
        return []

    def get_numstat(self, access, base_commit, head_commit):
        return []

    def list_commits_bounded(self, access, base_commit, head_commit, commit_limit):
        return [], False

    def get_numstat_bounded(self, access, base_commit, head_commit, line_limit):
        return [], False

    def measure_unified_diff_bytes(self, access, base_commit, head_commit, byte_limit):
        return 0, False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(tmp_path / "projects"))
    SuccessfulGitClient.calls = []
    monkeypatch.setattr(git_connections, "_git_client_factory", SuccessfulGitClient)
    monkeypatch.setattr(analysis_lineages, "_git_client_factory", SuccessfulGitClient)
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path


def _create(client):
    response = client.post("/api/projects", json={"name": "Git 基线项目"})
    assert response.status_code == 201
    return response.json()


def _configure(client, project, *, url="https://example.invalid/org/repo.git", branch="main"):
    response = client.put(
        f"/api/projects/{project['id']}",
        json={"name": project["name"], "git_url": url, "branch": branch, "version": project["version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["project"]


def _connect(client, project):
    response = client.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"
    return project


def _establish(client, project):
    response = client.post(f"/api/projects/{project['id']}/analysis-lineages")
    assert response.status_code == 201, response.text
    return response.json()


def test_create_rejects_not_connected(client):
    api, _ = client
    project = _configure(api, _create(api))
    response = api.post(f"/api/projects/{project['id']}/analysis-lineages")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_NOT_CONNECTED"


def test_create_rejects_config_changed_after_connect(client):
    api, tmp_path = client
    project = _connect(api, _configure(api, _create(api)))
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute("UPDATE projects SET branch = 'develop' WHERE id = ?", (project["id"],))
    response = api.post(f"/api/projects/{project['id']}/analysis-lineages")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_CONFIG_CHANGED"


def test_duplicate_create_returns_existing_record(client, tmp_path):
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    first = _establish(api, project)
    with sqlite3.connect(tmp_path / "test.db") as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM analysis_lineages WHERE project_id = ? AND status = 'active'",
            (project["id"],),
        ).fetchone()[0]
    assert rows == 1
    second = _establish(api, project)
    assert first["lineage"]["id"] == second["lineage"]["id"]
    assert second["created"] is False
    assert second["lineage"]["status"] == "active"


def test_active_unique_index_is_enforced_at_db_level(client, tmp_path):
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    _establish(api, project)
    with sqlite3.connect(tmp_path / "test.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO analysis_lineages
                (project_id, sequence_no, branch, baseline_commit, status, created_at)
                VALUES (?, 99, 'main', ?, 'active', '2026-01-01T00:00:00+00:00')""",
                (project["id"], "a" * 40),
            )


def test_lineage_reads_after_restart(client, tmp_path):
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    established = _establish(api, project)
    importlib.reload(main)
    with TestClient(main.app) as restarted:
        body = restarted.get(f"/api/projects/{project['id']}/analysis-lineage").json()
    assert body["status"] == "active"
    assert body["lineage"]["id"] == established["lineage"]["id"]


def test_different_projects_each_get_own_lineage(client):
    api, _ = client
    p1 = _connect(api, _configure(api, _create(api)))
    p2 = _connect(api, _configure(api, _create(api)))
    _establish(api, p1)
    _establish(api, p2)
    l1 = api.get(f"/api/projects/{p1['id']}/analysis-lineage").json()
    l2 = api.get(f"/api/projects/{p2['id']}/analysis-lineage").json()
    assert l1["lineage"]["id"] != l2["lineage"]["id"]
    assert l1["lineage"]["baseline_commit"] == HEAD


def test_no_lineage_reads_without_500(client):
    api, _ = client
    project = _configure(api, _create(api))
    response = api.get(f"/api/projects/{project['id']}/analysis-lineage")
    assert response.status_code == 200
    assert response.json()["status"] == "no_lineage"
    assert response.json()["lineage"] is None


def test_range_refresh_does_not_change_baseline(client, tmp_path):
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    established = _establish(api, project)
    response = api.post(f"/api/projects/{project['id']}/git/range-candidate")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_new_commit"
    assert body["candidate"]["baseline_commit"] == established["lineage"]["baseline_commit"]
    with sqlite3.connect(tmp_path / "test.db") as conn:
        baseline = conn.execute(
            "SELECT baseline_commit FROM analysis_lineages WHERE project_id = ?",
            (project["id"],),
        ).fetchone()[0]
    assert baseline == established["lineage"]["baseline_commit"]


def test_range_refresh_without_workspace_is_git_failed(client, tmp_path):
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    _establish(api, project)
    with sqlite3.connect(tmp_path / "test.db") as conn:
        pass
    root = tmp_path / "projects" / str(project["id"]) / "repo"
    if root.exists():
        import shutil

        shutil.rmtree(str(root))
    response = api.post(f"/api/projects/{project['id']}/git/range-candidate")
    assert response.status_code == 200
    assert response.json()["status"] == "git_failed"


def test_git_connection_and_range_refresh_share_mutex(client):
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    lock = analysis_lineages.project_workspace_lock(project["id"])
    lock.__enter__()
    try:
        response = api.post(f"/api/projects/{project['id']}/git/range-candidate")
        assert response.status_code == 200
        assert response.json()["status"] == "git_operation_in_progress"

        check = api.post(f"/api/projects/{project['id']}/git/check")
        assert check.status_code == 409
        assert check.json()["detail"]["code"] == "GIT_OPERATION_IN_PROGRESS"
        # Fix #4: must preserve current status in 409 response
        assert check.json()["detail"]["current"] is not None
        assert check.json()["detail"]["current"]["status"] == "connected"
    finally:
        lock.__exit__(None, None, None)


def test_git_connection_lock_contention_preserves_current(client, tmp_path):
    """Fix #4: lock contention on check_git_connection returns 409 with current status."""
    api, _ = client
    project = _connect(api, _configure(api, _create(api)))
    lock = analysis_lineages.project_workspace_lock(project["id"])
    lock.__enter__()
    try:
        check = api.post(f"/api/projects/{project['id']}/git/check")
        assert check.status_code == 409
        assert check.json()["detail"]["code"] == "GIT_OPERATION_IN_PROGRESS"
        current = check.json()["detail"]["current"]
        assert current is not None
        assert current["status"] == "connected"
        assert current["checked_branch"] == "main"
        assert current["remote_head"] == HEAD
        assert current["local_head"] == HEAD
    finally:
        lock.__exit__(None, None, None)


def test_project_not_found_all_routes(client):
    api, _ = client
    assert api.get("/api/projects/999/analysis-lineage").status_code == 404
    assert api.post("/api/projects/999/analysis-lineages").status_code == 404
    assert api.post("/api/projects/999/git/range-candidate").status_code == 404