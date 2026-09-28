"""Git 连接 API 与数据库状态机测试；不访问互联网或真实凭据。"""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import git_connections, main, projects  # noqa: E402
from app.git_client import GitClientError  # noqa: E402


HEAD = "1" * 40


class SuccessfulGitClient:
    calls = []

    def __init__(self):
        type(self).calls.append("init")

    def get_version(self):
        self.calls.append("version")
        return "git version 2.49.0.windows.1"

    def validate_branch(self, branch):
        self.calls.append(("validate", branch))

    def clone_branch(self, url, branch, access):
        self.calls.append(("clone", branch))
        target = access.path
        assert target.is_dir()
        assert not list(target.iterdir())
        (target / ".git").mkdir()

    def inspect_workspace(self, access):
        self.calls.append("inspect")
        repo = access.path
        assert (repo / ".git").is_dir()

    def assert_clean(self, access):
        self.calls.append("clean")

    def fetch_branch(self, access, branch):
        self.calls.append(("fetch", branch))

    def checkout_remote_head(self, access, branch):
        self.calls.append(("checkout", branch))

    def assert_size_limit(self, access):
        self.calls.append("size")

    def get_local_head(self, access):
        self.calls.append("local_head")
        return HEAD

    def get_remote_head_ref(self, access, branch):
        self.calls.append(("remote_ref", branch))
        return HEAD

    def get_remote_head(self, url, branch):
        self.calls.append(("remote_network", branch))
        return HEAD


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(tmp_path / "projects"))
    SuccessfulGitClient.calls = []
    monkeypatch.setattr(git_connections, "_git_client_factory", SuccessfulGitClient)
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path


def _create(client):
    response = client.post("/api/projects", json={"name": "Git 项目"})
    assert response.status_code == 201
    return response.json()


def _configure(client, project, *, url="https://example.invalid/org/repo.git", branch="main", name=None):
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": name or project["name"], "git_url": url, "branch": branch,
            "version": project["version"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["project"]


def test_status_and_check_project_not_found(client):
    api, _ = client
    for method, path in (("get", "/api/projects/999/git/status"), ("post", "/api/projects/999/git/check")):
        response = getattr(api, method)(path)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_empty_git_configuration_returns_409(client):
    api, _ = client
    project = _create(api)
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_CONFIG_REQUIRED"
    assert SuccessfulGitClient.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.invalid/repo.git",
        "https://token@example.invalid/repo.git",
        "https://example.invalid/repo.git?token=secret",
        "https://example.invalid/repo.git#secret",
        "ssh://user:password@example.invalid/repo.git",
    ],
)
def test_secret_bearing_urls_are_rejected_on_save(client, url):
    api, _ = client
    project = _create(api)
    response = api.put(
        f"/api/projects/{project['id']}",
        json={"name": project["name"], "git_url": url, "branch": "main", "version": 1},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"
    assert url not in response.text
    assert "example.invalid" not in response.text


@pytest.mark.parametrize("url", ["ssh://git@example.invalid/org/repo.git", "git@example.invalid:org/repo.git"])
def test_ssh_and_scp_are_saved_but_never_execute_git(client, monkeypatch, url):
    api, _ = client
    project = _configure(api, _create(api), url=url)

    def forbidden_factory():
        raise AssertionError("Git must not start for SSH/SCP")

    monkeypatch.setattr(git_connections, "_git_client_factory", forbidden_factory)
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "GIT_PROTOCOL_NOT_ENABLED"
    assert "未启用 SSH" in body["error_summary"]


def test_testing_state_rejects_second_attempt_without_git(client):
    api, tmp_path = client
    project = _configure(api, _create(api))
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            """INSERT INTO project_git_connections
            (project_id, status, attempt_id, checked_url_hash, checked_branch, started_at)
            VALUES (?, 'testing', ?, ?, 'main', datetime('now'))""",
            (project["id"], "a" * 32, "b" * 64),
        )
    SuccessfulGitClient.calls = []
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "GIT_CHECK_IN_PROGRESS"
    assert SuccessfulGitClient.calls == []


def test_success_clones_managed_workspace_and_persists_heads(client):
    api, tmp_path = client
    project = _configure(api, _create(api))
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "connected", "checked_branch": "main",
        "git_version": "git version 2.49.0.windows.1", "remote_head": HEAD,
        "local_head": HEAD, "last_checked_at": body["last_checked_at"],
        "error_code": None, "error_summary": None,
    }
    assert (tmp_path / "projects" / str(project["id"]) / "repo" / ".git").is_dir()
    assert not list((tmp_path / "projects").rglob(".repo-clone-*"))
    assert SuccessfulGitClient.calls.count(("clone", "main")) == 1
    assert ("fetch", "main") not in SuccessfulGitClient.calls
    assert SuccessfulGitClient.calls.count(("remote_ref", "main")) == 1
    assert not any(
        isinstance(call, tuple) and call[0] == "remote_network"
        for call in SuccessfulGitClient.calls
    )

    restored = api.get(f"/api/projects/{project['id']}/git/status")
    assert restored.status_code == 200
    assert restored.json() == body


def test_second_success_fetches_same_workspace(client):
    api, _ = client
    project = _configure(api, _create(api))
    assert api.post(f"/api/projects/{project['id']}/git/check").status_code == 200
    SuccessfulGitClient.calls = []
    assert api.post(f"/api/projects/{project['id']}/git/check").status_code == 200
    assert "inspect" in SuccessfulGitClient.calls
    assert SuccessfulGitClient.calls.count(("fetch", "main")) == 1
    assert SuccessfulGitClient.calls.count(("remote_ref", "main")) == 1
    assert not any(isinstance(call, tuple) and call[0] == "clone" for call in SuccessfulGitClient.calls)
    assert not any(
        isinstance(call, tuple) and call[0] == "remote_network"
        for call in SuccessfulGitClient.calls
    )


@pytest.mark.parametrize(
    "code",
    ["GIT_AUTH_FAILED", "GIT_REPOSITORY_NOT_FOUND", "GIT_NETWORK_UNAVAILABLE", "GIT_BRANCH_NOT_FOUND"],
)
def test_stable_git_failures_never_mark_connected(client, monkeypatch, code):
    api, tmp_path = client
    project = _configure(api, _create(api))

    class FailedGitClient(SuccessfulGitClient):
        def clone_branch(self, url, branch, access):
            target = access.path
            (target / "partial").write_text("attempt-owned", encoding="utf-8")
            raise GitClientError(code)

    monkeypatch.setattr(git_connections, "_git_client_factory", FailedGitClient)
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == code
    assert body["remote_head"] is None and body["local_head"] is None
    assert "example.invalid" not in response.text
    assert not list((tmp_path / "projects").rglob(".repo-clone-*"))


def test_competing_attempt_directory_is_never_deleted(client, monkeypatch):
    api, _ = client
    project = _configure(api, _create(api))
    real_reserve = git_connections.reserve_attempt_workspace
    observed = {}

    def competing_reserve(paths, attempt_id):
        paths.temporary.mkdir()
        sentinel = paths.temporary / "competitor.txt"
        sentinel.write_text("competitor-owned", encoding="utf-8")
        observed["temporary"] = paths.temporary
        observed["sentinel"] = sentinel
        return real_reserve(paths, attempt_id)

    monkeypatch.setattr(
        git_connections,
        "reserve_attempt_workspace",
        competing_reserve,
    )
    response = api.post(f"/api/projects/{project['id']}/git/check")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "GIT_WORKSPACE_CONFLICT"
    assert observed["temporary"].is_dir()
    assert observed["sentinel"].read_text(encoding="utf-8") == "competitor-owned"


class _FinalSaveFailConnection:
    def __init__(self, real):
        self.real = real

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *args):
        return self.real.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args, **kwargs):
        statement = " ".join(str(sql).split()).upper()
        if statement.startswith("UPDATE PROJECT_GIT_CONNECTIONS") and "ATTEMPT_ID" in statement:
            raise sqlite3.OperationalError("simulated final save failure")
        return self.real.execute(sql, *args, **kwargs)


def test_final_state_save_failure_never_returns_connected(client, monkeypatch):
    api, tmp_path = client
    project = _configure(api, _create(api))
    real_get_connection = git_connections.get_connection
    monkeypatch.setattr(
        git_connections,
        "get_connection",
        lambda: _FinalSaveFailConnection(real_get_connection()),
    )
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "GIT_CHECK_SAVE_FAILED"
    assert "connected" not in response.text
    with sqlite3.connect(tmp_path / "test.db") as conn:
        status = conn.execute(
            "SELECT status FROM project_git_connections WHERE project_id = ?", (project["id"],)
        ).fetchone()[0]
    assert status == "testing"


def test_configuration_change_invalidates_connected_state_but_name_change_preserves_it(client):
    api, _ = client
    project = _configure(api, _create(api))
    assert api.post(f"/api/projects/{project['id']}/git/check").json()["status"] == "connected"

    renamed = _configure(api, project, name="只改名称")
    assert api.get(f"/api/projects/{project['id']}/git/status").json()["status"] == "connected"

    changed = _configure(api, renamed, branch="develop")
    state = api.get(f"/api/projects/{changed['id']}/git/status").json()
    assert state["status"] == "not_tested"
    assert state["remote_head"] is None and state["local_head"] is None


class _DeleteFailConnection:
    def __init__(self, real):
        self.real = real

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *args):
        return self.real.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args, **kwargs):
        if str(sql).lstrip().upper().startswith("DELETE FROM PROJECT_GIT_CONNECTIONS"):
            raise sqlite3.OperationalError("simulated invalidation failure")
        return self.real.execute(sql, *args, **kwargs)


def test_connection_invalidation_failure_rolls_back_project_save(client, monkeypatch):
    api, _ = client
    project = _configure(api, _create(api))
    assert api.post(f"/api/projects/{project['id']}/git/check").json()["status"] == "connected"
    real_get_connection = projects.get_connection
    monkeypatch.setattr(projects, "get_connection", lambda: _DeleteFailConnection(real_get_connection()))
    response = api.put(
        f"/api/projects/{project['id']}",
        json={"name": project["name"], "git_url": project["git_url"], "branch": "develop", "version": project["version"]},
    )
    assert response.status_code == 500
    monkeypatch.setattr(projects, "get_connection", real_get_connection)
    current = api.get(f"/api/projects/{project['id']}").json()
    assert current["branch"] == "main" and current["version"] == project["version"]
    assert api.get(f"/api/projects/{project['id']}/git/status").json()["status"] == "connected"


def test_startup_converges_interrupted_attempt(client):
    api, tmp_path = client
    project = _configure(api, _create(api))
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            """INSERT INTO project_git_connections
            (project_id, status, attempt_id, checked_url_hash, checked_branch, started_at)
            VALUES (?, 'testing', ?, ?, 'main', datetime('now'))""",
            (project["id"], "c" * 32, "d" * 64),
        )
    importlib.reload(main)
    with TestClient(main.app) as restarted:
        body = restarted.get(f"/api/projects/{project['id']}/git/status").json()
    assert body["status"] == "failed"
    assert body["error_code"] == "GIT_CHECK_INTERRUPTED"


def test_old_attempt_cannot_overwrite_new_attempt(client):
    api, tmp_path = client
    project = _configure(api, _create(api))
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            """INSERT INTO project_git_connections
            (project_id, status, attempt_id, checked_url_hash, checked_branch, started_at)
            VALUES (?, 'testing', ?, ?, 'main', datetime('now'))""",
            (project["id"], "e" * 32, "f" * 64),
        )
    with pytest.raises(Exception) as raised:
        git_connections._finish_attempt(
            project["id"], "0" * 32, status="connected", branch="main",
            git_version="git version test", remote_head=HEAD, local_head=HEAD,
        )
    assert getattr(raised.value, "status_code", None) == 500
    with sqlite3.connect(tmp_path / "test.db") as conn:
        row = conn.execute(
            "SELECT status, attempt_id FROM project_git_connections WHERE project_id = ?", (project["id"],)
        ).fetchone()
    assert row == ("testing", "e" * 32)


def test_database_and_response_do_not_store_url_command_or_absolute_workspace(client):
    api, tmp_path = client
    project = _configure(api, _create(api), url="https://private-host.invalid/org/repo.git")
    response = api.post(f"/api/projects/{project['id']}/git/check")
    assert response.status_code == 200
    with sqlite3.connect(tmp_path / "test.db") as conn:
        row = conn.execute(
            "SELECT * FROM project_git_connections WHERE project_id = ?", (project["id"],)
        ).fetchone()
    persisted = "|".join("" if value is None else str(value) for value in row)
    public = response.text
    for forbidden in ("private-host.invalid", str(tmp_path), "ls-remote", "clone", "password", "token"):
        assert forbidden.lower() not in persisted.lower()
        assert forbidden.lower() not in public.lower()
    assert row[8] == "repo"


def test_all_workspace_writes_stay_under_test_root(client):
    api, tmp_path = client
    project = _configure(api, _create(api))
    assert api.post(f"/api/projects/{project['id']}/git/check").status_code == 200
    entries = list((tmp_path / "projects").rglob("*"))
    assert entries
    assert all(str(entry.resolve()).startswith(str((tmp_path / "projects").resolve())) for entry in entries)
