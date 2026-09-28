"""后端 API 测试：使用临时 SQLite 数据库，不触碰正式数据库。"""

import importlib
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db, main, projects  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def _create(client, name="测试项目"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def _update_payload(**overrides):
    payload = {"name": "新名称", "git_url": "https://github.com/example/example.git", "branch": "main", "version": 1}
    payload.update(overrides)
    return payload


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_project(client):
    response = client.post("/api/projects", json={"name": "测试项目"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "测试项目"
    assert body["status"] == "draft"
    assert body["id"] >= 1
    assert body["created_at"]


def test_create_uses_new_field_defaults(client):
    created = _create(client)
    assert created["git_url"] is None
    assert created["branch"] == "main"
    assert created["version"] == 1
    assert created["updated_at"] == created["created_at"]


def test_empty_name_rejected(client):
    response = client.post("/api/projects", json={"name": ""})
    assert response.status_code == 400
    response = client.post("/api/projects", json={"name": "   "})
    assert response.status_code == 400


def test_list_returns_created_project(client):
    client.post("/api/projects", json={"name": "列表项目"})
    response = client.get("/api/projects")
    assert response.status_code == 200
    projects = response.json()
    assert len(projects) == 1
    assert projects[0]["name"] == "列表项目"
    assert projects[0]["status"] == "draft"
    assert "git_url" in projects[0]
    assert "branch" in projects[0]
    assert "version" in projects[0]
    assert "updated_at" in projects[0]


def test_get_project_detail_success(client):
    created = _create(client)
    response = client.get(f"/api/projects/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["name"] == created["name"]
    assert body["status"] == "draft"
    assert body["git_url"] is None
    assert body["branch"] == "main"
    assert body["created_at"]
    assert body["updated_at"]
    assert body["version"] == 1


def test_get_project_not_found(client):
    response = client.get("/api/projects/99999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_update_project_success(client):
    created = _create(client, name="原名称")
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(
            name="新名称",
            git_url="git@github.com:example/example.git",
            branch="develop",
            version=1,
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    project = body["project"]
    assert project["name"] == "新名称"
    assert project["git_url"] == "git@github.com:example/example.git"
    assert project["branch"] == "develop"
    assert project["status"] == "draft"
    assert project["created_at"] == created["created_at"]
    assert project["version"] == 2
    assert project["updated_at"] >= created["updated_at"]


def test_update_list_returns_new_values(client):
    created = _create(client, name="原名称")
    client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="改后名称", git_url=None, branch="main", version=1),
    )
    response = client.get("/api/projects")
    assert response.status_code == 200
    projects = response.json()
    assert projects[0]["name"] == "改后名称"
    assert projects[0]["version"] == 2
    assert projects[0]["updated_at"] >= created["updated_at"]


def test_update_detail_returns_new_values(client):
    created = _create(client, name="原名称")
    client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="改后名称", git_url="ssh://git@example.com/r.git", branch="feature/x", version=1),
    )
    response = client.get(f"/api/projects/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "改后名称"
    assert body["git_url"] == "ssh://git@example.com/r.git"
    assert body["branch"] == "feature/x"
    assert body["version"] == 2


def test_update_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "restart.db"))
    importlib.reload(main)
    with TestClient(main.app) as first_client:
        created = _create(first_client, name="重启验证项目")
        response = first_client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="重启后名称", git_url="https://github.com/a/b.git", branch="dev", version=1),
        )
        assert response.status_code == 200

    importlib.reload(main)
    with TestClient(main.app) as second_client:
        projects = second_client.get("/api/projects").json()
        detail = second_client.get(f"/api/projects/{created['id']}").json()
    assert len(projects) == 1
    assert projects[0]["name"] == "重启后名称"
    assert detail["name"] == "重启后名称"
    assert detail["git_url"] == "https://github.com/a/b.git"
    assert detail["branch"] == "dev"
    assert detail["version"] == 2


def test_update_rejects_empty_name(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="   ", git_url=None, branch="main", version=1),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_rejects_too_long_name(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="x" * 101, git_url=None, branch="main", version=1),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_rejects_control_char_in_name(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="ab\x00cd", git_url=None, branch="main", version=1),
    )
    assert response.status_code == 400


def test_update_rejects_invalid_git_url(client):
    created = _create(client)
    for bad in ["ftp://example.com/r.git", "http://example.com/r.git", "example", "git@github.com"]:
        response = client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="新名称", git_url=bad, branch="main", version=1),
        )
        assert response.status_code == 400, bad
        assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_rejects_git_url_with_empty_host_or_path(client):
    created = _create(client)
    for bad in ["https://", "https://host", "https://host/", "ssh://", "ssh://host", "ssh://host/", "git@host:", "git@:", "@host:x"]:
        response = client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="新名称", git_url=bad, branch="main", version=1),
        )
        assert response.status_code == 400, bad
        assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_rejects_git_url_with_internal_whitespace_or_control(client):
    created = _create(client)
    for bad in [
        "https://exa mple.com/r.git",
        "https://example.com/r gi t",
        "https://example.com/r\x00git",
        "ssh://git@example.com/r gi t",
        "ssh://git@example.com/r\x01git",
        "git@example.com:r gi t",
        "git@example.com:r\x02git",
        "https://example.com/r\ttab.git",
    ]:
        response = client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="新名称", git_url=bad, branch="main", version=1),
        )
        assert response.status_code == 400, bad
        assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_rejects_too_long_git_url(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="新名称", git_url="https://x/" + "a" * 500, branch="main", version=1),
    )
    assert response.status_code == 400


def test_update_normalizes_empty_git_url_to_null(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="新名称", git_url="   ", branch="main", version=1),
    )
    assert response.status_code == 200
    assert response.json()["project"]["git_url"] is None


def test_update_rejects_invalid_branch(client):
    created = _create(client)
    bad_branches = [
        "",
        "   ",
        "main dev",
        "..",
        "a..b",
        "//",
        "a//b",
        "/main",
        "main/",
        "main.lock",
        "a@{b",
        "a\\b",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "x" * 256,
    ]
    for bad in bad_branches:
        response = client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="新名称", git_url=None, branch=bad, version=1),
        )
        assert response.status_code == 400, repr(bad)
        assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_accepts_valid_branches(client):
    created = _create(client)
    version = created["version"]
    for good in ["main", "develop", "feature/x", "release/1.0", "a.b-c_1"]:
        response = client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="新名称", git_url=None, branch=good, version=version),
        )
        assert response.status_code == 200, repr(good)
        version = response.json()["project"]["version"]


def test_update_rejects_version_below_one(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="新名称", git_url=None, branch="main", version=0),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_INPUT"


def test_update_rejects_boolean_version(client):
    """任务书 6.2 要求 version 为 integer：布尔值不是 integer，必须 422。"""
    created = _create(client)
    for bad in [True, False]:
        response = client.put(
            f"/api/projects/{created['id']}",
            json=_update_payload(name="新名称", git_url=None, branch="main", version=bad),
        )
        assert response.status_code == 422, bad


def test_update_accepts_integer_version(client):
    """version=1 为合法 integer，应正常接受。"""
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="新名称", git_url=None, branch="main", version=1),
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True


def test_update_rejects_float_version(client):
    """任务书 6.2 要求 version 为 integer：1.0 是 float 而非 integer，必须 422。"""
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="新名称", git_url=None, branch="main", version=1.0),
    )
    assert response.status_code == 422


def test_update_rejects_string_version(client):
    """任务书 6.2 要求 version 为 integer：字符串不是 integer，必须 422。"""
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="新名称", git_url=None, branch="main", version="1"),
    )
    assert response.status_code == 422


def test_update_missing_required_field_is_422(client):
    created = _create(client)
    response = client.put(
        f"/api/projects/{created['id']}",
        json={"name": "只有名称"},
    )
    assert response.status_code == 422


def test_update_not_found_returns_404(client):
    response = client.put(
        "/api/projects/99999",
        json=_update_payload(version=1),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_update_ignores_status_and_time_fields(client):
    created = _create(client, name="原名称")
    response = client.put(
        f"/api/projects/{created['id']}",
        json={
            "name": "改后名称",
            "git_url": None,
            "branch": "main",
            "version": 1,
            "status": "active",
            "created_at": "2000-01-01T00:00:00+00:00",
            "updated_at": "2000-01-01T00:00:00+00:00",
        },
    )
    assert response.status_code == 200
    project = response.json()["project"]
    assert project["status"] == "draft"
    assert project["created_at"] == created["created_at"]
    assert project["updated_at"] >= created["updated_at"]


def test_repeat_same_content_no_version_bump(client):
    created = _create(client, name="原名称")
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="原名称", git_url=None, branch="main", version=1),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is False
    assert body["project"]["version"] == 1
    assert body["project"]["updated_at"] == created["updated_at"]

    detail = client.get(f"/api/projects/{created['id']}").json()
    assert detail["version"] == 1
    assert detail["updated_at"] == created["updated_at"]


def test_repeat_same_content_old_version_returns_409(client):
    created = _create(client, name="原名称")
    first = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="改后名称", git_url=None, branch="main", version=1),
    )
    assert first.status_code == 200
    assert first.json()["changed"] is True

    stale = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="改后名称", git_url=None, branch="main", version=1),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["detail"]["current"]["version"] == 2


def test_stale_version_returns_409(client):
    created = _create(client, name="原名称")
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="别人改的名称", git_url=None, branch="main", version=5),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_stale_version_does_not_overwrite(client):
    created = _create(client, name="原名称")
    first = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="先保存的名称", git_url=None, branch="main", version=1),
    )
    assert first.status_code == 200

    stale = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="旧版本试图覆盖", git_url=None, branch="main", version=1),
    )
    assert stale.status_code == 409

    detail = client.get(f"/api/projects/{created['id']}").json()
    assert detail["name"] == "先保存的名称"
    assert detail["version"] == 2


def _parallel_put(client, project_id, payload, barrier, results):
    try:
        response = client.put(f"/api/projects/{project_id}", json=payload)
        results.append((response.status_code, response.json()))
    except Exception as exc:  # noqa: BLE001
        results.append(("error", str(exc)))
    finally:
        barrier.wait()


def test_concurrent_same_version_only_one_succeeds(client):
    """R-05：两个相同 version、不同内容的请求最多一个成功，另一个必须 409。"""
    created = _create(client, name="并发项目")
    project_id = created["id"]

    barrier = threading.Barrier(3)
    results = []
    t1 = threading.Thread(
        target=_parallel_put,
        args=(client, project_id, _update_payload(name="并发甲", git_url=None, branch="main", version=1), barrier, results),
    )
    t2 = threading.Thread(
        target=_parallel_put,
        args=(client, project_id, _update_payload(name="并发乙", git_url=None, branch="main", version=1), barrier, results),
    )
    t1.start()
    t2.start()
    barrier.wait()
    t1.join()
    t2.join()

    assert len(results) == 2
    statuses = sorted(r[0] for r in results)
    assert statuses == [200, 409], results

    success = next(r for r in results if r[0] == 200)
    conflict = next(r for r in results if r[0] == 409)
    assert success[1]["changed"] is True
    assert conflict[1]["detail"]["code"] == "VERSION_CONFLICT"

    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["name"] == success[1]["project"]["name"]
    assert detail["version"] == 2


class _WriteFailConnection:
    """包一层真实连接：UPDATE 一律抛错，用于制造数据库写入失败。"""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args, **kwargs):
        if str(sql).lstrip().upper().startswith("UPDATE"):
            raise sqlite3.OperationalError("simulated write failure")
        return self._real.execute(sql, *args, **kwargs)


def test_db_write_failure_returns_500(client, monkeypatch):
    created = _create(client, name="原名称")
    real_get_connection = projects.get_connection

    def failing_get_connection():
        return _WriteFailConnection(real_get_connection())

    monkeypatch.setattr(projects, "get_connection", failing_get_connection)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="保存失败", git_url=None, branch="main", version=1),
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PROJECT_SAVE_FAILED"


def test_db_write_failure_preserves_original_data(client, monkeypatch):
    created = _create(client, name="原名称")
    real_get_connection = projects.get_connection

    def failing_get_connection():
        return _WriteFailConnection(real_get_connection())

    monkeypatch.setattr(projects, "get_connection", failing_get_connection)
    response = client.put(
        f"/api/projects/{created['id']}",
        json=_update_payload(name="保存失败", git_url=None, branch="main", version=1),
    )
    assert response.status_code == 500

    monkeypatch.setattr(projects, "get_connection", real_get_connection)
    detail = client.get(f"/api/projects/{created['id']}").json()
    assert detail["name"] == "原名称"
    assert detail["version"] == 1
    assert detail["updated_at"] == created["updated_at"]


def _create_old_schema_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO projects (name, status, created_at) VALUES (?, ?, ?)",
        ("老项目", "draft", "2026-07-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def test_old_four_column_db_upgrades_without_data_loss(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    _create_old_schema_db(db_path)

    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        projects = test_client.get("/api/projects").json()

    assert len(projects) == 1
    project = projects[0]
    assert project["name"] == "老项目"
    assert project["status"] == "draft"
    assert project["created_at"] == "2026-07-01T00:00:00+00:00"
    assert project["git_url"] is None
    assert project["branch"] == "main"
    assert project["updated_at"] == "2026-07-01T00:00:00+00:00"
    assert project["version"] == 1


def test_upgrade_runs_twice_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    _create_old_schema_db(db_path)

    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    importlib.reload(main)
    with TestClient(main.app):
        pass
    with TestClient(main.app):
        pass

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    rows = conn.execute("SELECT name, status, created_at FROM projects").fetchall()
    conn.close()

    assert {"git_url", "branch", "updated_at", "version"} <= columns
    assert len(rows) == 1
    assert rows[0][0] == "老项目"


def test_data_survives_restart(tmp_path, monkeypatch):
    """模拟完全重启：第一个应用实例写入后关闭，新实例仍能读到数据。"""
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "restart.db"))
    importlib.reload(main)
    with TestClient(main.app) as first_client:
        first_client.post("/api/projects", json={"name": "重启验证项目"})

    importlib.reload(main)
    with TestClient(main.app) as second_client:
        projects = second_client.get("/api/projects").json()
    assert len(projects) == 1
    assert projects[0]["name"] == "重启验证项目"
    assert projects[0]["status"] == "draft"
