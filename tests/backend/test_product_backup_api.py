"""HTTP contract tests for the guarded SQLite-only product backup export."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import local_session_api  # noqa: E402
from app import product_backup_api  # noqa: E402
from app.product_backup import (  # noqa: E402
    PRODUCT_DATABASE_ARCHIVE_PATH,
    PRODUCT_DATABASE_LOGICAL_TYPE,
    PRODUCT_DATA_ROOT_ID,
    validate_product_backup,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(product_backup_api.router)
    return app


def _headers(**extra: str) -> dict[str, str]:
    headers = {
        "host": "127.0.0.1:5173",
        "origin": "http://127.0.0.1:5173",
    }
    headers.update(extra)
    return headers


def _open_session(client: TestClient) -> str:
    local_session_api.invalidate_local_session_guard()
    assert local_session_api.configure_local_session_guard("backup-bootstrap-test") is True
    exchanged = client.post(
        "/api/local-session/exchange",
        json={"bootstrap_secret": "backup-bootstrap-test"},
        headers=_headers(),
    )
    assert exchanged.status_code == 200
    return str(exchanged.json()["session_token"])


def _create_database(path: Path, value: str = "backup-api") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker(value) VALUES (?)", (value,))
        conn.commit()


def test_backup_export_requires_live_same_origin_session_and_returns_valid_product_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "data" / "anxinboard.db"
    _create_database(db_path)
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))

    client = TestClient(_app())
    token = _open_session(client)
    try:
        missing_session = client.post(
            "/api/product-backup/export",
            headers=_headers(**{"x-request-id": "backup-req-missing-session"}),
        )
        assert missing_session.status_code == 403
        assert missing_session.json()["detail"]["code"] == "LOCAL_SESSION_SESSION_INVALID"

        response = client.post(
            "/api/product-backup/export",
            headers=_headers(
                **{
                    "x-anxin-session": token,
                    "x-request-id": "backup-req-1",
                }
            ),
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"].startswith("application/zip")
        disposition = response.headers["content-disposition"]
        assert "AnxinBoard-backup.zip" in disposition
        assert str(tmp_path) not in disposition
        assert b"backup-bootstrap-test" not in response.content
        assert token.encode("utf-8") not in response.content

        package_path = tmp_path / "downloaded-backup.zip"
        package_path.write_bytes(response.content)
        validated = validate_product_backup(package_path)
        assert len(validated.entries) == 1
        entry = validated.entries[0]
        assert entry.logical_type == PRODUCT_DATABASE_LOGICAL_TYPE
        assert entry.source_root_id == PRODUCT_DATA_ROOT_ID
        assert entry.archive_path == PRODUCT_DATABASE_ARCHIVE_PATH
    finally:
        local_session_api.invalidate_local_session_guard()


def test_backup_export_rejects_cross_origin_before_reading_product_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "data" / "anxinboard.db"
    _create_database(db_path)
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))

    client = TestClient(_app())
    token = _open_session(client)
    try:
        response = client.post(
            "/api/product-backup/export",
            headers={
                "host": "127.0.0.1:5173",
                "origin": "http://evil.example",
                "x-anxin-session": token,
                "x-request-id": "backup-req-cross-origin",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "LOCAL_SESSION_ORIGIN_INVALID"
    finally:
        local_session_api.invalidate_local_session_guard()


def test_backup_export_missing_database_is_bounded_conflict_without_path_or_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "missing" / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))

    client = TestClient(_app())
    token = _open_session(client)
    try:
        response = client.post(
            "/api/product-backup/export",
            headers=_headers(
                **{
                    "x-anxin-session": token,
                    "x-request-id": "backup-req-missing-db",
                }
            ),
        )
        assert response.status_code == 409
        payload = response.json()
        assert payload["detail"]["code"] == "PRODUCT_BACKUP_UNAVAILABLE"
        serialized = response.text
        assert str(tmp_path) not in serialized
        assert token not in serialized
        assert "backup-bootstrap-test" not in serialized
    finally:
        local_session_api.invalidate_local_session_guard()


def test_product_backup_router_exposes_export_only_and_no_restore_route() -> None:
    paths = {route.path for route in product_backup_api.router.routes}
    assert paths == {"/api/product-backup/export"}
    assert all("restore" not in path.casefold() for path in paths)
