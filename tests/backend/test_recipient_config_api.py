from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.recipient_config_api as recipient_api  # noqa: E402
from app.db import get_connection, init_db  # noqa: E402
from app.recipient_config_api import (  # noqa: E402
    SaveRecipientConfigRequest,
    get_recipient_config_http,
    save_recipient_config_http,
)


@pytest.fixture()
def recipient_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES ('Project A', 'active', '2026-09-06T00:00:00+00:00')"
        )
        project_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    monkeypatch.setattr(recipient_api, "require_local_write_request", lambda *_args, **_kwargs: None)
    return project_id


def test_api01_empty_project_config_is_explicitly_unconfigured(recipient_db: int):
    result = get_recipient_config_http(recipient_db)
    assert result == {
        "schema_version": "recipient_config_settings_v1",
        "configured": False,
        "version_no": 0,
        "to_recipients": [],
    }


def test_api02_save_reuses_r1_canonical_order_and_domain_case(recipient_db: int):
    result = save_recipient_config_http(
        recipient_db,
        SaveRecipientConfigRequest(
            to_recipients=[
                "Alice@Example.TEST",
                "bob@example.test",
                "Alice@example.test",
            ],
            expected_version_no=0,
        ),
        object(),
    )

    # R1 preserves local-part case; only exact canonical duplicates collapse.
    assert result["configured"] is True
    assert result["version_no"] == 1
    assert result["to_recipients"] == [
        "Alice@example.test",
        "bob@example.test",
    ]
    assert get_recipient_config_http(recipient_db) == result


def test_api03_stale_version_conflict_is_exposed_without_overwrite(recipient_db: int):
    save_recipient_config_http(
        recipient_db,
        SaveRecipientConfigRequest(
            to_recipients=["a@example.test"],
            expected_version_no=0,
        ),
        object(),
    )

    with pytest.raises(HTTPException) as caught:
        save_recipient_config_http(
            recipient_db,
            SaveRecipientConfigRequest(
                to_recipients=["b@example.test"],
                expected_version_no=0,
            ),
            object(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "RECIPIENT_CONFIG_VERSION_CONFLICT"
    assert get_recipient_config_http(recipient_db)["to_recipients"] == ["a@example.test"]


def test_api04_invalid_recipient_uses_r1_error_contract(recipient_db: int):
    with pytest.raises(HTTPException) as caught:
        save_recipient_config_http(
            recipient_db,
            SaveRecipientConfigRequest(
                to_recipients=["Display Name <a@example.test>"],
                expected_version_no=0,
            ),
            object(),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "RECIPIENT_CONFIG_INPUT_INVALID"


def test_api05_write_guard_runs_before_recipient_mutation(
    recipient_db: int, monkeypatch: pytest.MonkeyPatch
):
    def reject(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_SESSION_TEST_REJECTED"})

    monkeypatch.setattr(recipient_api, "require_local_write_request", reject)
    with pytest.raises(HTTPException) as caught:
        save_recipient_config_http(
            recipient_db,
            SaveRecipientConfigRequest(
                to_recipients=["a@example.test"],
                expected_version_no=0,
            ),
            object(),
        )

    assert caught.value.status_code == 403
    assert get_recipient_config_http(recipient_db)["configured"] is False


def test_api06_unknown_project_is_404_for_read_and_write(recipient_db: int):
    missing = recipient_db + 999
    with pytest.raises(HTTPException) as caught:
        get_recipient_config_http(missing)
    assert caught.value.status_code == 404

    with pytest.raises(HTTPException) as caught:
        save_recipient_config_http(
            missing,
            SaveRecipientConfigRequest(
                to_recipients=["a@example.test"],
                expected_version_no=0,
            ),
            object(),
        )
    assert caught.value.status_code == 404
