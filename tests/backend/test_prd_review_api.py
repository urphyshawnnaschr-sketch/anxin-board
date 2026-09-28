"""Protected full PRD review endpoint tests."""
from __future__ import annotations

import importlib
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import local_session_api, main  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(tmp_path / "prdroot"))
    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, tmp_path
    local_session_api.invalidate_local_session_guard()


def _create_project(client: TestClient) -> int:
    response = client.post("/api/projects", json={"name": "完整审阅项目"})
    assert response.status_code == 201
    return response.json()["id"]


def _upload_text(client: TestClient, project_id: int, text: str) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/prd-versions",
        files={"file": ("需求.txt", text.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _session_headers(client: TestClient) -> dict[str, str]:
    local_session_api.invalidate_local_session_guard()
    assert local_session_api.configure_local_session_guard("bootstrap-test") is True
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "bootstrap-test"},
        headers={"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"},
    )
    assert exchanged.status_code == 200
    return {
        "host": "127.0.0.1:5173",
        "x-anxin-session": exchanged.json()["session_token"],
        "x-request-id": "read-prd-1",
    }


def test_full_review_requires_live_launcher_session(env):
    client, _ = env
    project_id = _create_project(client)
    version = _upload_text(client, project_id, "正文" * 1200)
    assert version["preview_truncated"] is True

    denied = client.get(f"/api/prd-versions/{version['id']}/review")
    assert denied.status_code == 503
    assert denied.json()["detail"]["code"] == "LOCAL_SESSION_UNAVAILABLE"

    headers = _session_headers(client)
    allowed = client.get(
        f"/api/prd-versions/{version['id']}/review?offset=0&limit=1000",
        headers=headers,
    )
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    body = allowed.json()
    assert body["schema_version"] == "prd_parsed_review_v1"
    assert body["version_id"] == version["id"]
    assert body["parsed_hash"] == version["parsed_hash"]
    assert body["offset"] == 0
    assert body["next_offset"] == 1000
    assert body["complete"] is False
    assert len(body["content"]) == 1000


def test_full_review_can_page_to_exact_complete_text(env):
    client, _ = env
    project_id = _create_project(client)
    text = "第一段\n" + ("完整解析正文-" * 900) + "\n最后一段"
    version = _upload_text(client, project_id, text)
    headers = _session_headers(client)

    offset = 0
    parts: list[str] = []
    parsed_hash = None
    while True:
        headers["x-request-id"] = f"read-prd-{offset}"
        response = client.get(
            f"/api/prd-versions/{version['id']}/review?offset={offset}&limit=777",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        if parsed_hash is None:
            parsed_hash = body["parsed_hash"]
        assert body["parsed_hash"] == parsed_hash == version["parsed_hash"]
        assert body["offset"] == offset
        parts.append(body["content"])
        offset = body["next_offset"]
        if body["complete"]:
            assert offset == body["total_chars"]
            break

    assert "".join(parts) == text


def test_full_review_fails_closed_if_parsed_artifact_changes(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload_text(client, project_id, "可信正文" * 600)
    headers = _session_headers(client)

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT parsed_path FROM prd_versions WHERE id = ?", (version["id"],)).fetchone()
    conn.close()
    assert row is not None
    (tmp_path / "prdroot" / row["parsed_path"]).write_text("已被修改", encoding="utf-8")

    response = client.get(f"/api/prd-versions/{version['id']}/review", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_REVIEW_INTEGRITY_MISMATCH"
