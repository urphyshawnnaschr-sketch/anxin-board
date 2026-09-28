"""项目档案后端候选、版本更新与人工确认门测试。

覆盖任务书 K 区全部 26 项：幂等建表、契约、错误码、no-op、stale、
五组真实并发/受控竞争与故障注入。全部使用临时 SQLite 与测试内合成内容，
不接触正式数据库、客户数据、模型、SMTP、真实 Git 或网络。
"""

import hashlib
import importlib
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db, main, project_profiles  # noqa: E402
from prd_fixtures import make_md  # noqa: E402

SCHEMA_VERSION = "project_profile_manual_v1"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    prd_root = tmp_path / "prdroot"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(prd_root))
    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, tmp_path


# ---------- 辅助 ----------


def _create_project(client, name="档案项目"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def _confirm_prd(client, project_id, text="PRD 正文"):
    upload = client.post(
        f"/api/projects/{project_id}/prd-versions",
        files={"file": ("prd.md", make_md(text), "application/octet-stream")},
    )
    assert upload.status_code == 201, upload.text
    version = upload.json()
    confirm = client.post(
        f"/api/prd-versions/{version['id']}/confirm",
        json={"confirmed_by": "项目经理"},
    )
    assert confirm.status_code == 200, confirm.text
    return version


def _base_content(modules=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "project_summary": "档案摘要",
        "modules": modules if modules is not None else [],
        "domain_glossary": [],
        "exclude_patterns": [],
        "notes": "",
    }


def _complete_module(**overrides):
    module = {
        "client_id": "module-1",
        "name": "登录模块",
        "description": "登录功能",
        "prd_refs": ["登录需求"],
        "requirements": ["用户名密码登录"],
        "paths": [
            {
                "type": "backend",
                "pattern": "apps/backend/app/auth/**",
                "required": True,
                "note": "",
            }
        ],
        "exclusions": [],
    }
    module.update(overrides)
    return module


def _complete_content(**module_overrides):
    return _base_content([_complete_module(**module_overrides)])


def _canonical(content: dict) -> str:
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _create_candidate(client, project_id, content=None):
    content = content if content is not None else _base_content()
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 201, response.text
    return response.json()


def _confirm_profile(client, profile_id, edit_version=1, confirmed_by="项目经理"):
    response = client.post(
        f"/api/profile-candidates/{profile_id}/confirm",
        json={"edit_version": edit_version, "confirmed_by": confirmed_by},
    )
    return response


def _put_profile(client, profile_id, edit_version, content):
    return client.put(
        f"/api/profile-candidates/{profile_id}",
        json={"edit_version": edit_version, "content": content},
    )


def _parallel(barrier, results, kind, fn, timeout=30):
    """工作线程先等起跑屏障，再执行请求；异常进入 results，屏障超时不静默。"""
    try:
        barrier.wait(timeout=timeout)
        response = fn()
        results.append((kind, response.status_code, response.json()))
    except Exception as exc:  # noqa: BLE001
        results.append((kind, "error", str(exc)))


class _FailConnection:
    """包一层真实连接：满足谓词的 SQL 一律抛错，用于制造数据库写入失败。"""

    def __init__(self, real, predicate):
        self._real = real
        self._predicate = predicate

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args, **kwargs):
        if self._predicate(str(sql)):
            raise sqlite3.OperationalError("simulated failure")
        return self._real.execute(sql, *args, **kwargs)


def _install_fail(monkeypatch, predicate):
    real = project_profiles.get_connection

    def failing_get_connection():
        return _FailConnection(real(), predicate)

    monkeypatch.setattr(project_profiles, "get_connection", failing_get_connection)
    return real


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
    conn.commit()
    conn.close()


# ---------- K-1：init_db 幂等建立表和索引 ----------


def test_init_db_idempotent_on_old_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    _create_old_schema_db(db_path)
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    importlib.reload(main)
    with TestClient(main.app):
        pass
    with TestClient(main.app):
        pass

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "project_profiles" in tables
    assert "ux_project_profiles_confirmed" in indexes
    assert "ux_project_profiles_candidate" in indexes


def test_init_db_idempotent_on_fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    importlib.reload(main)
    with TestClient(main.app):
        pass
    with TestClient(main.app):
        pass

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "project_profiles" in tables
    assert "ux_project_profiles_confirmed" in indexes
    assert "ux_project_profiles_candidate" in indexes


# ---------- K-2 / K-3 / K-4：创建候选基础契约 ----------


def test_create_candidate_missing_project_404(env):
    client, _ = env
    response = client.post("/api/projects/99999/profile-candidates", json=_base_content())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_create_candidate_without_active_prd_409(env):
    client, _ = env
    project_id = _create_project(client)
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=_base_content())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_PARSE_NOT_CONFIRMED"


def test_create_candidate_source_and_hash_exact(env):
    client, _ = env
    project_id = _create_project(client)
    prd_version = _confirm_prd(client, project_id)
    content = _complete_content()
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 201
    body = response.json()
    assert body["project_id"] == project_id
    assert body["source_prd_id"] == prd_version["id"]
    assert body["status"] == "candidate"
    assert body["version_no"] == 1
    assert body["edit_version"] == 1
    assert body["content"] == content
    assert body["content_hash"] == _sha256(_canonical(body["content"]))


# ---------- K-5：V2 候选使旧候选 superseded，confirmed 不变 ----------


def test_create_v2_supersedes_old_candidate_keeps_confirmed(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)

    c0 = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, c0["id"]).status_code == 200

    c1 = _create_candidate(client, project_id, _base_content())
    assert c1["status"] == "candidate"
    c2 = _create_candidate(client, project_id, _base_content([_complete_module(client_id="c2")]))
    assert c2["status"] == "candidate"
    assert c2["version_no"] == c1["version_no"] + 1

    history = client.get(f"/api/projects/{project_id}/profiles").json()
    by_no = {v["version_no"]: v for v in history}
    assert by_no[c0["version_no"]]["status"] == "confirmed"
    assert by_no[c1["version_no"]]["status"] == "superseded"
    assert by_no[c2["version_no"]]["status"] == "candidate"
    candidates = [v for v in history if v["status"] == "candidate"]
    confirmed = [v for v in history if v["status"] == "confirmed"]
    assert len(candidates) == 1 and candidates[0]["id"] == c2["id"]
    assert len(confirmed) == 1 and confirmed[0]["id"] == c0["id"]


# ---------- K-6：不完整候选允许保存 ----------


def test_incomplete_candidate_saved_without_loss(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _base_content(
        [
            {
                "client_id": "empty-module",
                "name": "",
                "description": "",
                "prd_refs": [],
                "requirements": ["   "],
                "paths": [],
                "exclusions": [],
            }
        ]
    )
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 201
    body = response.json()
    assert body["content"]["modules"][0]["requirements"] == [""]
    detail = client.get(f"/api/profile-candidates/{body['id']}").json()
    assert detail["content"]["modules"][0]["name"] == ""
    assert detail["content"]["modules"][0]["paths"] == []


# ---------- K-7：非法路径矩阵 400 且无半更新 ----------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/abs",
        "//x",
        "~/x",
        "C:/evil",
        "C:\\evil",
        "a\\b",
        "a:b",
        "a:/b",
        "./x",
        "../x",
        "a/../b",
        "a/./b",
        "a\x00b",
        "a\x1fb",
        "a\u007fb",
        "a\u0085b",
        "https://example.com/x",
        "file:///etc/passwd",
        "//server/share",
        "x" * 501,
    ],
)
def test_invalid_path_patterns_rejected(env, bad):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _base_content([_complete_module(paths=[{"type": "other", "pattern": bad, "required": True, "note": ""}])])
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 400, repr(bad)
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_PROFILE_INPUT"

    history = client.get(f"/api/projects/{project_id}/profiles").json()
    assert history == []


@pytest.mark.parametrize(
    "good",
    [
        "apps/frontend/**",
        "apps/backend/app/*.py",
        "tests/backend/test_*.py",
        "docs",
        "a[b].py",
        "a?c",
    ],
)
def test_valid_path_patterns_accepted(env, good):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _base_content([_complete_module(paths=[{"type": "other", "pattern": good, "required": True, "note": ""}])])
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 201, repr(good)


# ---------- K-8：重复 client_id、额外字段、超限输入 ----------


def test_duplicate_client_id_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _base_content([_complete_module(client_id="dup"), _complete_module(client_id="dup")])
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_PROFILE_INPUT"
    assert client.get(f"/api/projects/{project_id}/profiles").json() == []


def test_extra_fields_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)

    content_with_secret = dict(_complete_content())
    content_with_secret["api_key"] = "sk-secret"
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content_with_secret)
    assert response.status_code == 422

    module_with_extra = _complete_module()
    module_with_extra["token"] = "secret"
    response = client.post(
        f"/api/projects/{project_id}/profile-candidates",
        json=_base_content([module_with_extra]),
    )
    assert response.status_code == 422

    path_with_extra = _complete_module()["paths"][0]
    path_with_extra["password"] = "secret"
    response = client.post(
        f"/api/projects/{project_id}/profile-candidates",
        json=_base_content([_complete_module(paths=[path_with_extra])]),
    )
    assert response.status_code == 422

    assert client.get(f"/api/projects/{project_id}/profiles").json() == []


def test_wrong_schema_version_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _complete_content()
    content["schema_version"] = "project_profile_manual_v2"
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 422


def test_over_limit_inputs_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)

    cases = [
        {"project_summary": "x" * 2001},
        {"notes": "x" * 2001},
        {"modules": [_complete_module(name="x" * 101)]},
        {"modules": [_complete_module(description="x" * 2001)]},
        {"modules": [_complete_module(prd_refs=["x" * 201])]},
        {"modules": [_complete_module(requirements=["x" * 501])]},
        {"modules": [_complete_module(paths=[{"type": "other", "pattern": "ok/**", "required": True, "note": "x" * 501}])]},
        {"modules": [_complete_module(client_id="x" * 65)]},
        {"modules": [_complete_module(client_id="bad id")]},
        {"domain_glossary": [{"term": "x" * 101, "definition": "d", "aliases": []}]},
        {"domain_glossary": [{"term": "t", "definition": "x" * 501, "aliases": []}]},
        {"domain_glossary": [{"term": "t", "definition": "d", "aliases": ["x" * 101]}]},
        {"exclude_patterns": ["x" * 501]},
    ]
    for override in cases:
        content = _base_content([_complete_module()])
        content.update(override)
        response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
        assert response.status_code == 422, override

    assert client.get(f"/api/projects/{project_id}/profiles").json() == []


def test_invalid_path_type_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _base_content([_complete_module(paths=[{"type": "mobile", "pattern": "x", "required": True, "note": ""}])])
    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert response.status_code == 422


# ---------- K-9：单版读取与历史契约 ----------


def test_get_single_and_history_contract(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    c1 = _create_candidate(client, project_id, _complete_content())
    c2 = _create_candidate(client, project_id, _base_content())

    single = client.get(f"/api/profile-candidates/{c1['id']}")
    assert single.status_code == 200
    body = single.json()
    assert body["content"] == c1["content"]
    assert body["content_hash"] == c1["content_hash"]
    assert body["edit_version"] == 1
    assert body["status"] == "superseded"

    history = client.get(f"/api/projects/{project_id}/profiles")
    assert history.status_code == 200
    rows = history.json()
    assert [v["version_no"] for v in rows] == [c2["version_no"], c1["version_no"]]
    for item in rows:
        for field in (
            "id", "project_id", "version_no", "source_prd_id", "status", "content_hash",
            "edit_version", "created_at", "updated_at", "confirmed_by", "confirmed_at", "active",
        ):
            assert field in item, field
        assert "content" not in item
        assert "content_json" not in item
    assert rows[0]["active"] is False


def test_get_profile_not_found(env):
    client, _ = env
    response = client.get("/api/profile-candidates/99999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_PROFILE_NOT_FOUND"


def test_history_missing_project_404(env):
    client, _ = env
    response = client.get("/api/projects/99999/profiles")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


# ---------- K-10：PUT changed=true 原子递增；no-op 不变 ----------


def test_put_change_and_noop(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content_a = _base_content([_complete_module(requirements=["登录"])])
    profile = _create_candidate(client, project_id, content_a)

    content_b = _base_content([_complete_module(requirements=["登录", "登出"])])
    changed = _put_profile(client, profile["id"], 1, content_b)
    assert changed.status_code == 200
    assert changed.json()["changed"] is True
    assert changed.json()["profile"]["edit_version"] == 2
    assert changed.json()["profile"]["updated_at"] >= profile["updated_at"]

    noop = _put_profile(client, profile["id"], 2, content_b)
    assert noop.status_code == 200
    assert noop.json()["changed"] is False
    assert noop.json()["profile"]["edit_version"] == 2
    assert noop.json()["profile"]["updated_at"] == changed.json()["profile"]["updated_at"]

    detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert detail["edit_version"] == 2
    assert detail["content"] == content_b


# ---------- K-11：StrictInt 422 与 0 业务 400 ----------


@pytest.mark.parametrize("bad_version", [True, False, 1.0, "1"])
def test_put_strict_int_version_422(env, bad_version):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id)
    response = _put_profile(client, profile["id"], bad_version, _complete_content())
    assert response.status_code == 422, repr(bad_version)


@pytest.mark.parametrize("bad_version", [True, False, 1.0, "1"])
def test_confirm_strict_int_version_422(env, bad_version):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id)
    response = _confirm_profile(client, profile["id"], bad_version)
    assert response.status_code == 422, repr(bad_version)


def test_put_zero_version_400(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id)
    response = _put_profile(client, profile["id"], 0, _complete_content())
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_PROFILE_INPUT"


def test_confirm_zero_version_400(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id)
    response = _confirm_profile(client, profile["id"], 0)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_PROFILE_INPUT"


@pytest.mark.parametrize("bad_confirmed_by", ["\u007f", "a\u007fb", "\u0085", "a\u0085b", "a\u009fb"])
def test_confirm_rejects_unicode_control_confirmed_by(env, bad_confirmed_by):
    """confirmed_by 中的 DEL U+007F 与 C1 控制字符 U+0080-009F 必须 400 且不改变状态。"""
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    c1 = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, c1["id"]).status_code == 200

    c2 = _create_candidate(client, project_id, _complete_content(client_id="module-2"))
    response = client.post(
        f"/api/profile-candidates/{c2['id']}/confirm",
        json={"edit_version": 1, "confirmed_by": bad_confirmed_by},
    )
    assert response.status_code == 400, repr(bad_confirmed_by)
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_PROFILE_INPUT"

    detail = client.get(f"/api/profile-candidates/{c2['id']}").json()
    assert detail["status"] == "candidate"
    assert detail["edit_version"] == 1
    assert detail["confirmed_by"] is None
    assert detail["confirmed_at"] is None
    active = [v for v in client.get(f"/api/projects/{project_id}/profiles").json() if v["active"]]
    assert len(active) == 1 and active[0]["id"] == c1["id"]


def test_put_missing_edit_version_or_content_422(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id)
    response = client.put(f"/api/profile-candidates/{profile['id']}", json={"content": _complete_content()})
    assert response.status_code == 422
    response = client.put(f"/api/profile-candidates/{profile['id']}", json={"edit_version": 1})
    assert response.status_code == 422


# ---------- K-12：旧版本更新 409 且保留成功内容 ----------


def test_stale_update_keeps_successful_content(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content_a = _base_content([_complete_module(requirements=["甲"])])
    content_b = _base_content([_complete_module(requirements=["乙"])])
    profile = _create_candidate(client, project_id, content_a)

    first = _put_profile(client, profile["id"], 1, content_b)
    assert first.status_code == 200
    assert first.json()["changed"] is True

    stale = _put_profile(client, profile["id"], 1, _base_content([_complete_module(requirements=["丙"])]))
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "PROJECT_PROFILE_VERSION_CONFLICT"
    assert stale.json()["detail"]["current"]["edit_version"] == 2

    detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert detail["content"] == content_b
    assert detail["edit_version"] == 2


# ---------- K-13：confirmed/superseded 不可更新 ----------


def test_confirmed_and_superseded_not_updatable(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    c1 = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, c1["id"]).status_code == 200
    c2 = _create_candidate(client, project_id, _complete_content(client_id="module-2"))
    assert _confirm_profile(client, c2["id"]).status_code == 200

    for profile_id in (c1["id"], c2["id"]):
        response = _put_profile(client, profile_id, 2, _complete_content())
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "PROJECT_PROFILE_INVALID_STATE"

    response = _confirm_profile(client, c2["id"], 2)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROJECT_PROFILE_INVALID_STATE"

    detail = client.get(f"/api/profile-candidates/{c1['id']}").json()
    assert detail["status"] == "superseded"
    assert detail["content"] == c1["content"]


# ---------- K-14：零模块确认返回完整 missing，状态不变 ----------


def test_confirm_zero_module_returns_missing(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id, _base_content())

    response = _confirm_profile(client, profile["id"], 1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "PROJECT_PROFILE_INCOMPLETE"
    assert len(detail["missing"]) == 1
    assert detail["missing"][0]["scope"] == "profile"
    assert detail["missing"][0]["field"] == "modules"

    current = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert current["status"] == "candidate"
    assert current["edit_version"] == 1
    assert current["confirmed_by"] is None
    assert current["confirmed_at"] is None
    assert current["updated_at"] == profile["updated_at"]
    assert not any(v["active"] for v in client.get(f"/api/projects/{project_id}/profiles").json())


# ---------- K-15：多模块缺失一次全部返回 ----------


def test_confirm_multimodule_missing_all_at_once(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    modules = [
        _complete_module(client_id="m1", name="模块A", requirements=[], paths=[]),
        _complete_module(
            client_id="m2",
            name="",
            requirements=["有需求"],
            paths=[{"type": "other", "pattern": "docs/**", "required": True, "note": ""}],
        ),
    ]
    profile = _create_candidate(client, project_id, _base_content(modules))

    response = _confirm_profile(client, profile["id"], 1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "PROJECT_PROFILE_INCOMPLETE"
    missing_fields = {(m["module_client_id"], m["field"]) for m in detail["missing"]}
    assert missing_fields == {("m1", "requirements"), ("m1", "paths"), ("m2", "name")}

    current = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert current["status"] == "candidate"
    assert current["edit_version"] == 1
    assert current["content"]["modules"] == modules


# ---------- K-16：完整候选确认成功 ----------


def test_confirm_complete_candidate_success(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id, _complete_content())

    response = _confirm_profile(client, profile["id"], 1)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_by"] == "项目经理"
    assert body["confirmed_at"]
    assert body["edit_version"] == 2
    assert body["content"] == _complete_content()

    active = [v for v in client.get(f"/api/projects/{project_id}/profiles").json() if v["active"]]
    assert len(active) == 1 and active[0]["id"] == profile["id"]


# ---------- K-17：V2 确认后 V1 confirmed → superseded，只留一个 confirmed ----------


def test_confirm_v2_supersedes_v1(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    c1 = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, c1["id"]).status_code == 200
    c2 = _create_candidate(client, project_id, _complete_content(client_id="module-2"))
    assert _confirm_profile(client, c2["id"]).status_code == 200

    history = client.get(f"/api/projects/{project_id}/profiles").json()
    by_id = {v["id"]: v for v in history}
    assert by_id[c1["id"]]["status"] == "superseded"
    assert by_id[c2["id"]]["status"] == "confirmed"
    confirmed = [v for v in history if v["status"] == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0]["id"] == c2["id"]


# ---------- K-18：PRD 被替换后旧候选确认 stale ----------


def test_confirm_stale_source_prd_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    c1 = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, c1["id"]).status_code == 200
    c2 = _create_candidate(client, project_id, _base_content())

    _confirm_prd(client, project_id, text="替换后的 PRD 正文")

    response = _confirm_profile(client, c2["id"], 1)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROJECT_PROFILE_SOURCE_PRD_STALE"

    detail = client.get(f"/api/profile-candidates/{c2['id']}").json()
    assert detail["status"] == "candidate"
    assert detail["edit_version"] == 1
    assert detail["confirmed_by"] is None
    active = [v for v in client.get(f"/api/projects/{project_id}/profiles").json() if v["active"]]
    assert len(active) == 1 and active[0]["id"] == c1["id"]


# ---------- K-19：故障注入无半状态 ----------


def test_insert_failure_no_half_state(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    real = _install_fail(monkeypatch, lambda sql: sql.lstrip().upper().startswith("INSERT INTO PROJECT_PROFILES"))

    response = client.post(f"/api/projects/{project_id}/profile-candidates", json=_complete_content())
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PROJECT_PROFILE_SAVE_FAILED"

    monkeypatch.setattr(project_profiles, "get_connection", real)
    assert client.get(f"/api/projects/{project_id}/profiles").json() == []

    ok = client.post(f"/api/projects/{project_id}/profile-candidates", json=_complete_content())
    assert ok.status_code == 201


def test_update_failure_keeps_candidate(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id, _base_content([_complete_module(requirements=["原"])]))

    real = _install_fail(monkeypatch, lambda sql: sql.lstrip().upper().startswith("UPDATE PROJECT_PROFILES"))
    response = _put_profile(client, profile["id"], 1, _base_content([_complete_module(requirements=["改"])]))
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PROJECT_PROFILE_SAVE_FAILED"

    monkeypatch.setattr(project_profiles, "get_connection", real)
    detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert detail["content"]["modules"][0]["requirements"] == ["原"]
    assert detail["edit_version"] == 1


def test_confirm_failure_rolls_back_supersede(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    c1 = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, c1["id"]).status_code == 200
    c2 = _create_candidate(client, project_id, _complete_content(client_id="module-2"))

    real = _install_fail(monkeypatch, lambda sql: "confirmed_by" in sql)
    response = _confirm_profile(client, c2["id"], 1)
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PROJECT_PROFILE_SAVE_FAILED"

    monkeypatch.setattr(project_profiles, "get_connection", real)
    history = client.get(f"/api/projects/{project_id}/profiles").json()
    active = [v for v in history if v["active"]]
    assert len(active) == 1 and active[0]["id"] == c1["id"]
    detail = client.get(f"/api/profile-candidates/{c2['id']}").json()
    assert detail["status"] == "candidate"
    assert detail["edit_version"] == 1


# ---------- K-20：重启后数据保持 ----------


def test_restart_preserves_profiles(tmp_path, monkeypatch):
    db_path = tmp_path / "restart.db"
    prd_root = tmp_path / "prdroot"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(prd_root))
    importlib.reload(main)
    with TestClient(main.app) as first_client:
        project_id = _create_project(first_client)
        _confirm_prd(first_client, project_id)
        c1 = _create_candidate(first_client, project_id, _complete_content())
        _confirm_profile(first_client, c1["id"])
        c2 = _create_candidate(first_client, project_id, _base_content())
        _put_profile(first_client, c2["id"], 1, _base_content([_complete_module(client_id="module-2", name="改名")]))

    importlib.reload(main)
    with TestClient(main.app) as second_client:
        detail = second_client.get(f"/api/profile-candidates/{c2['id']}")
        history = second_client.get(f"/api/projects/{project_id}/profiles")

    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "candidate"
    assert body["edit_version"] == 2
    assert body["content"]["modules"][0]["name"] == "改名"
    rows = history.json()
    by_id = {v["id"]: v for v in rows}
    assert by_id[c1["id"]]["status"] == "confirmed"
    assert by_id[c2["id"]]["status"] == "candidate"
    assert len(rows) == 2


# ---------- K-21：隔离与无越界集成 ----------


def test_operations_only_inside_test_root(env):
    client, tmp_path = env
    formal_dir = tmp_path / "formal"
    formal_dir.mkdir()
    formal_db = formal_dir / "anxinboard.db"
    formal_db.write_text("FORMAL DB CONTENT", encoding="utf-8")
    formal_root = formal_dir / "projects"
    formal_root.mkdir()
    snapshot = {
        str(formal_db): (formal_db.stat().st_size, formal_db.stat().st_mtime_ns),
        str(formal_root): (formal_root.stat().st_size, formal_root.stat().st_mtime_ns),
    }

    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    profile = _create_candidate(client, project_id, _complete_content())
    _put_profile(client, profile["id"], 1, _complete_content())
    _confirm_profile(client, profile["id"], 2)

    assert str(db.get_db_path()).startswith(str(tmp_path))
    for path, before in snapshot.items():
        after = (Path(path).stat().st_size, Path(path).stat().st_mtime_ns)
        assert after == before, f"模拟正式路径被改动：{path}"
    assert sorted(p.name for p in formal_dir.iterdir()) == ["anxinboard.db", "projects"]


def test_module_has_no_forbidden_integration():
    source = Path(project_profiles.__file__).read_text(encoding="utf-8")
    for token in ("git_client", "smtplib", "subprocess", "requests", "urllib", "socket", "import app.prd", "app.model"):
        assert token not in source, token


# ---------- K-22：旧版本 + 相同内容仍 409，不得 no-op ----------


def test_stale_same_content_returns_conflict_not_noop(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content = _base_content([_complete_module(requirements=["登录"])])
    profile = _create_candidate(client, project_id, content)

    changed_content = _base_content([_complete_module(requirements=["登录", "登出"])])
    first = _put_profile(client, profile["id"], 1, changed_content)
    assert first.status_code == 200
    assert first.json()["changed"] is True

    stale = _put_profile(client, profile["id"], 1, changed_content)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "PROJECT_PROFILE_VERSION_CONFLICT"
    assert stale.json()["detail"]["current"]["edit_version"] == 2

    detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert detail["edit_version"] == 2
    assert detail["content"] == changed_content


# ---------- K-23：stale edit_version 确认 409 且都不改变 ----------


def test_stale_confirm_returns_conflict(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)

    # V1：完整候选，确认后成为唯一 active confirmed
    v1_content = _complete_content(requirements=["V1 已审阅需求"])
    v1 = _create_candidate(client, project_id, v1_content)
    v1_confirm = _confirm_profile(client, v1["id"])
    assert v1_confirm.status_code == 200, v1_confirm.text
    v1_state = client.get(f"/api/profile-candidates/{v1['id']}").json()
    assert v1_state["status"] == "confirmed"

    # V2 candidate，内容完整
    v2_content = _base_content([_complete_module(requirements=["V2 初始内容"])])
    v2 = _create_candidate(client, project_id, v2_content)
    before = client.get(f"/api/profile-candidates/{v2['id']}").json()
    assert before["status"] == "candidate"
    assert before["confirmed_by"] is None
    assert before["confirmed_at"] is None

    # PUT 更新 V2：edit_version 1 -> 2，内容真实变化
    put_content = _complete_content(requirements=["V2 更新后内容"])
    put = _put_profile(client, v2["id"], 1, put_content)
    assert put.status_code == 200, put.text
    assert put.json()["changed"] is True
    after_put = put.json()["profile"]
    assert after_put["edit_version"] == 2
    assert after_put["content"] == put_content

    # 使用旧 edit_version=1 确认 V2 -> 409
    response = _confirm_profile(client, v2["id"], 1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "PROJECT_PROFILE_VERSION_CONFLICT"
    assert "current" in detail
    assert detail["current"]["edit_version"] == 2

    # V2 candidate 不变（保持 PUT 成功后的状态）
    current = client.get(f"/api/profile-candidates/{v2['id']}").json()
    assert current["status"] == "candidate"
    assert current["edit_version"] == 2
    assert current["content"] == put_content
    assert current["content_hash"] == after_put["content_hash"]
    assert current["content_hash"] != before["content_hash"]
    assert current["confirmed_by"] is None
    assert current["confirmed_at"] is None

    # V1 confirmed 不变，仍为唯一 active profile，未被错误改成 superseded
    v1_now = client.get(f"/api/profile-candidates/{v1['id']}").json()
    assert v1_now["status"] == "confirmed"
    assert v1_now["content"] == v1_content
    assert v1_now["edit_version"] == v1_state["edit_version"]
    assert v1_now["confirmed_by"] == v1_state["confirmed_by"]
    assert v1_now["confirmed_at"] == v1_state["confirmed_at"]

    history = client.get(f"/api/projects/{project_id}/profiles").json()
    confirmed = [v for v in history if v["status"] == "confirmed"]
    candidates = [v for v in history if v["status"] == "candidate"]
    active = [v for v in history if v["active"]]
    assert len(confirmed) == 1 and confirmed[0]["id"] == v1["id"]
    assert len(candidates) == 1 and candidates[0]["id"] == v2["id"]
    assert len(active) == 1 and active[0]["id"] == v1["id"]


# ---------- K-24：双更新并发只允许一个成功 ----------


def test_concurrent_dual_update_only_one_wins(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    content_a = _base_content([_complete_module(requirements=["甲"])])
    content_b = _base_content([_complete_module(requirements=["乙"])])
    content_c = _base_content([_complete_module(requirements=["丙"])])
    profile = _create_candidate(client, project_id, content_a)

    barrier = threading.Barrier(3, timeout=30)
    results = []
    t1 = threading.Thread(
        target=_parallel,
        args=(barrier, results, "b", lambda: _put_profile(client, profile["id"], 1, content_b)),
    )
    t2 = threading.Thread(
        target=_parallel,
        args=(barrier, results, "c", lambda: _put_profile(client, profile["id"], 1, content_c)),
    )
    t1.start()
    t2.start()
    barrier.wait()
    t1.join()
    t2.join()

    assert len(results) == 2
    statuses = sorted(r[1] for r in results)
    assert statuses == [200, 409], results
    winner = next(r for r in results if r[1] == 200)
    loser = next(r for r in results if r[1] == 409)
    assert winner[2]["changed"] is True
    assert loser[2]["detail"]["code"] == "PROJECT_PROFILE_VERSION_CONFLICT"

    detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert detail["edit_version"] == 2
    assert detail["content"] == (content_b if winner[0] == "b" else content_c)
    assert detail["status"] == "candidate"


# ---------- K-25：PUT 与 confirm 受控竞争 ----------


def test_concurrent_put_vs_confirm_race(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)

    # V1：完整候选，确认后成为唯一 active confirmed
    v1_content = _complete_content(requirements=["V1 已审阅需求"])
    v1 = _create_candidate(client, project_id, v1_content)
    v1_confirm = _confirm_profile(client, v1["id"])
    assert v1_confirm.status_code == 200, v1_confirm.text
    v1_state = client.get(f"/api/profile-candidates/{v1['id']}").json()
    assert v1_state["status"] == "confirmed"

    # V2 candidate，使用完整、可确认的原始内容
    original = _base_content([_complete_module(requirements=["V2 已审阅的原始内容"])])
    profile = _create_candidate(client, project_id, original)
    base = client.get(f"/api/profile-candidates/{profile['id']}").json()
    assert base["status"] == "candidate"

    # PUT 新内容，与原始内容明显不同
    put_content = _base_content([_complete_module(requirements=["PUT 新写入的未审阅内容"])])

    barrier = threading.Barrier(3, timeout=30)
    results = []
    t1 = threading.Thread(
        target=_parallel,
        args=(barrier, results, "put", lambda: _put_profile(client, profile["id"], 1, put_content)),
    )
    t2 = threading.Thread(
        target=_parallel,
        args=(barrier, results, "confirm", lambda: _confirm_profile(client, profile["id"], 1)),
    )
    t1.start()
    t2.start()
    barrier.wait()
    t1.join()
    t2.join()

    assert len(results) == 2, results
    statuses = sorted(r[1] for r in results)
    assert statuses == [200, 409], results
    winner = next(r for r in results if r[1] == 200)
    loser = next(r for r in results if r[1] == 409)

    if winner[0] == "put":
        # PUT 获胜：confirm 因 stale edit_version 返回 409
        assert loser[0] == "confirm"
        assert loser[2]["detail"]["code"] == "PROJECT_PROFILE_VERSION_CONFLICT"

        detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
        assert detail["status"] == "candidate"
        assert detail["edit_version"] == 2
        assert detail["content"] == put_content
        assert detail["content"] != original
        assert detail["confirmed_by"] is None
        assert detail["confirmed_at"] is None

        # V1 保持唯一 confirmed 与唯一 active
        v1_now = client.get(f"/api/profile-candidates/{v1['id']}").json()
        assert v1_now["status"] == "confirmed"
        assert v1_now["content"] == v1_content
        assert v1_now["confirmed_by"] == v1_state["confirmed_by"]
        assert v1_now["confirmed_at"] == v1_state["confirmed_at"]

        history = client.get(f"/api/projects/{project_id}/profiles").json()
        confirmed = [v for v in history if v["status"] == "confirmed"]
        candidates = [v for v in history if v["status"] == "candidate"]
        active = [v for v in history if v["active"]]
        assert len(confirmed) == 1 and confirmed[0]["id"] == v1["id"]
        assert len(candidates) == 1 and candidates[0]["id"] == profile["id"]
        assert len(active) == 1 and active[0]["id"] == v1["id"]
    else:
        # confirm 获胜：确认原始已审阅内容，PUT 返回 INVALID_STATE
        assert winner[0] == "confirm"
        assert loser[0] == "put"
        assert loser[2]["detail"]["code"] == "PROJECT_PROFILE_INVALID_STATE"

        detail = client.get(f"/api/profile-candidates/{profile['id']}").json()
        assert detail["status"] == "confirmed"
        assert detail["content"] == original
        assert detail["content"] != put_content

        # V1 被 supersede，不再 active
        v1_now = client.get(f"/api/profile-candidates/{v1['id']}").json()
        assert v1_now["status"] == "superseded"

        history = client.get(f"/api/projects/{project_id}/profiles").json()
        confirmed = [v for v in history if v["status"] == "confirmed"]
        candidates = [v for v in history if v["status"] == "candidate"]
        active = [v for v in history if v["active"]]
        assert len(confirmed) == 1 and confirmed[0]["id"] == profile["id"]
        assert len(candidates) == 0
        assert len(active) == 1 and active[0]["id"] == profile["id"]


# ---------- K-26：双创建并发串行成功，版本连续 ----------


def test_concurrent_dual_create_serializes(env):
    client, _ = env
    project_id = _create_project(client)
    _confirm_prd(client, project_id)
    confirmed = _create_candidate(client, project_id, _complete_content())
    assert _confirm_profile(client, confirmed["id"]).status_code == 200
    content_a = _base_content([_complete_module(client_id="ra", requirements=["甲"])])
    content_b = _base_content([_complete_module(client_id="rb", requirements=["乙"])])

    barrier = threading.Barrier(3, timeout=30)
    results = []
    t1 = threading.Thread(
        target=_parallel,
        args=(
            barrier,
            results,
            "a",
            lambda: client.post(f"/api/projects/{project_id}/profile-candidates", json=content_a),
        ),
    )
    t2 = threading.Thread(
        target=_parallel,
        args=(
            barrier,
            results,
            "b",
            lambda: client.post(f"/api/projects/{project_id}/profile-candidates", json=content_b),
        ),
    )
    t1.start()
    t2.start()
    barrier.wait()
    t1.join()
    t2.join()

    assert len(results) == 2
    assert [r[1] for r in results] == [201, 201], results
    versions = sorted(r[2]["version_no"] for r in results)
    assert versions == [confirmed["version_no"] + 1, confirmed["version_no"] + 2]

    history = client.get(f"/api/projects/{project_id}/profiles").json()
    candidates = [v for v in history if v["status"] == "candidate"]
    superseded = [v for v in history if v["status"] == "superseded"]
    confirmed_rows = [v for v in history if v["status"] == "confirmed"]
    assert len(candidates) == 1 and candidates[0]["version_no"] == versions[1]
    assert len(superseded) == 1 and superseded[0]["version_no"] == versions[0]
    assert len(confirmed_rows) == 1 and confirmed_rows[0]["id"] == confirmed["id"]
