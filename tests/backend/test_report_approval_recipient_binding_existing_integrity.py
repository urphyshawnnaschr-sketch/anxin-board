from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import app.report_approval_recipient_binding as binding_module
from app.db import get_connection, init_db
from app.recipient_config import save_recipient_config
from app.report_approval_recipient_binding import (
    ApprovalRecipientBindingError,
    create_approval_recipient_binding,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def existing_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES (?, ?, ?)",
            ("mail-r3-existing-integrity", "active", "2026-09-06T00:00:00+00:00"),
        )
        project_id = int(cursor.lastrowid)
        conn.commit()

    approval = {
        "approval_snapshot_id": 1701,
        "project_id": project_id,
        "report_version_id": 1801,
        "approval_snapshot_hash": _h("approval-1701"),
        "report_content_hash": _h("report-1801"),
    }
    monkeypatch.setattr(
        binding_module,
        "get_report_approval_snapshot",
        lambda *, project_id, report_version_id: (
            dict(approval)
            if project_id == approval["project_id"]
            and report_version_id == approval["report_version_id"]
            else None
        ),
    )

    config = save_recipient_config(
        project_id=project_id,
        to_recipients=("owner@example.test",),
        created_by="Mail R3 Integrity Test",
        expected_version_no=0,
    )
    bound = create_approval_recipient_binding(
        project_id=project_id,
        report_version_id=1801,
        expected_recipient_config_version_id=int(config["id"]),
        expected_recipient_config_version_no=int(config["version_no"]),
        confirmed_by="Project Manager",
        confirmed_timezone="Asia/Shanghai",
        confirmed_utc_offset_minutes=480,
        human_confirmed=True,
        idempotency_key="existing-integrity-original",
    )
    return {
        "project_id": project_id,
        "config": config,
        "bound": bound,
    }


def _create_again(*, project_id: int, config: dict[str, object], idempotency_key: str):
    return create_approval_recipient_binding(
        project_id=project_id,
        report_version_id=1801,
        expected_recipient_config_version_id=int(config["id"]),
        expected_recipient_config_version_no=int(config["version_no"]),
        confirmed_by="Project Manager",
        confirmed_timezone="Asia/Shanghai",
        confirmed_utc_offset_minutes=480,
        human_confirmed=True,
        idempotency_key=idempotency_key,
    )


def test_valid_existing_binding_with_new_idempotency_key_remains_already_bound(existing_binding):
    with pytest.raises(ApprovalRecipientBindingError) as caught:
        _create_again(
            project_id=int(existing_binding["project_id"]),
            config=existing_binding["config"],
            idempotency_key="existing-integrity-new-key-valid",
        )

    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_ALREADY_BOUND"


def test_corrupt_existing_binding_with_new_idempotency_key_is_stored_invalid(existing_binding):
    bound = existing_binding["bound"]
    with get_connection() as conn:
        conn.execute("DROP TRIGGER trg_report_approval_recipient_bindings_no_update")
        conn.execute(
            "UPDATE report_approval_recipient_bindings SET binding_hash = ? WHERE id = ?",
            (_h("tampered-existing-binding"), bound["id"]),
        )
        conn.commit()

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        _create_again(
            project_id=int(existing_binding["project_id"]),
            config=existing_binding["config"],
            idempotency_key="existing-integrity-new-key-corrupt",
        )

    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_STORED_INVALID"
