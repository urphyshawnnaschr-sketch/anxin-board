from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.report_approval_recipient_binding as binding_module  # noqa: E402
from app.db import get_connection, init_db  # noqa: E402
from app.recipient_config import save_recipient_config  # noqa: E402
from app.report_approval_recipient_binding import (  # noqa: E402
    ApprovalRecipientBindingError,
    create_approval_recipient_binding,
    get_approval_recipient_binding,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def binding_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES (?, ?, ?)",
            ("mail-r3", "active", "2026-09-06T00:00:00+00:00"),
        )
        project_id = int(cursor.lastrowid)
        conn.commit()

    approval = {
        "approval_snapshot_id": 701,
        "project_id": project_id,
        "report_version_id": 801,
        "approval_snapshot_hash": _h("approval-701"),
        "report_content_hash": _h("report-801"),
    }
    monkeypatch.setattr(
        binding_module,
        "get_report_approval_snapshot",
        lambda *, project_id, report_version_id: (
            dict(approval)
            if project_id == approval["project_id"] and report_version_id == approval["report_version_id"]
            else None
        ),
    )
    return {"path": path, "project_id": project_id, "approval": approval}


def _save_config(project_id: int, recipients: tuple[str, ...], expected_version_no: int):
    return save_recipient_config(
        project_id=project_id,
        to_recipients=recipients,
        created_by="Mail R3 Test",
        expected_version_no=expected_version_no,
    )


def _bind(project_id: int, config: dict[str, object], *, idem: str = "bind-r3-1"):
    return create_approval_recipient_binding(
        project_id=project_id,
        report_version_id=801,
        expected_recipient_config_version_id=int(config["id"]),
        expected_recipient_config_version_no=int(config["version_no"]),
        confirmed_by="Project Manager",
        confirmed_timezone="Asia/Shanghai",
        confirmed_utc_offset_minutes=480,
        human_confirmed=True,
        idempotency_key=idem,
    )


def test_r3_01_explicit_human_binding_freezes_exact_current_r1_version(binding_db):
    project_id = int(binding_db["project_id"])
    config = _save_config(project_id, ("alpha@example.test", "beta@example.test"), 0)

    bound = _bind(project_id, config)
    reopened = get_approval_recipient_binding(project_id=project_id, report_version_id=801)

    assert reopened == bound
    assert bound["recipient_config_version_id"] == config["id"]
    assert bound["recipient_config_version_no"] == 1
    assert bound["recipients_hash"] == config["recipients_hash"]
    assert bound["to_recipients"] == ["alpha@example.test", "beta@example.test"]
    assert bound["human_acknowledged"] == 1
    assert len(str(bound["binding_hash"])) == 64

    with get_connection() as conn:
        triggers = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_report_approval_recipient_bindings_%'"
            ).fetchall()
        }
    assert {
        "trg_report_approval_recipient_bindings_no_update",
        "trg_report_approval_recipient_bindings_no_delete",
    } <= triggers


def test_r3_02_binding_does_not_drift_when_recipient_config_later_changes(binding_db):
    project_id = int(binding_db["project_id"])
    first = _save_config(project_id, ("first@example.test",), 0)
    bound = _bind(project_id, first)
    _save_config(project_id, ("second@example.test",), 1)

    reopened = get_approval_recipient_binding(project_id=project_id, report_version_id=801)
    assert reopened["binding_hash"] == bound["binding_hash"]
    assert reopened["recipient_config_version_id"] == first["id"]
    assert reopened["to_recipients"] == ["first@example.test"]

    replay = _bind(project_id, first)
    assert replay["binding_hash"] == bound["binding_hash"]


def test_r3_03_new_binding_rejects_stale_recipient_config_selection(binding_db):
    project_id = int(binding_db["project_id"])
    first = _save_config(project_id, ("first@example.test",), 0)
    _save_config(project_id, ("second@example.test",), 1)

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        _bind(project_id, first)
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_CONFIG_STALE"
    assert get_approval_recipient_binding(project_id=project_id, report_version_id=801) is None


def test_r3_04_report_must_already_have_immutable_approval(binding_db, monkeypatch):
    project_id = int(binding_db["project_id"])
    config = _save_config(project_id, ("one@example.test",), 0)
    monkeypatch.setattr(binding_module, "get_report_approval_snapshot", lambda **_: None)

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        _bind(project_id, config)
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_APPROVAL_REQUIRED"


def test_r3_05_human_confirmation_is_mandatory(binding_db):
    project_id = int(binding_db["project_id"])
    config = _save_config(project_id, ("one@example.test",), 0)

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        create_approval_recipient_binding(
            project_id=project_id,
            report_version_id=801,
            expected_recipient_config_version_id=int(config["id"]),
            expected_recipient_config_version_no=int(config["version_no"]),
            confirmed_by="Project Manager",
            confirmed_timezone="Asia/Shanghai",
            confirmed_utc_offset_minutes=480,
            human_confirmed=False,
            idempotency_key="bind-r3-human-gate",
        )
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_HUMAN_CONFIRMATION_REQUIRED"


def test_r3_06_one_approval_cannot_be_silently_rebound(binding_db):
    project_id = int(binding_db["project_id"])
    first = _save_config(project_id, ("first@example.test",), 0)
    _bind(project_id, first)
    second = _save_config(project_id, ("second@example.test",), 1)

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        _bind(project_id, second, idem="bind-r3-2")
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_ALREADY_BOUND"


def test_r3_07_binding_table_is_append_only_and_tamper_fails_closed(binding_db):
    project_id = int(binding_db["project_id"])
    config = _save_config(project_id, ("one@example.test",), 0)
    bound = _bind(project_id, config)

    with get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE report_approval_recipient_bindings SET confirmed_by='Other' WHERE id=?",
                (bound["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM report_approval_recipient_bindings WHERE id=?",
                (bound["id"],),
            )

        conn.execute("DROP TRIGGER trg_report_approval_recipient_bindings_no_update")
        conn.execute(
            "UPDATE report_approval_recipient_bindings SET recipients_hash=? WHERE id=?",
            (_h("tampered"), bound["id"]),
        )
        conn.commit()

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        get_approval_recipient_binding(project_id=project_id, report_version_id=801)
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_STORED_INVALID"


def test_r3_08_idempotency_key_cannot_cross_report_identity(binding_db, monkeypatch):
    project_id = int(binding_db["project_id"])
    original_approval = dict(binding_db["approval"])
    config = _save_config(project_id, ("one@example.test",), 0)
    _bind(project_id, config, idem="shared-idem")

    approval_two = {
        "approval_snapshot_id": 702,
        "project_id": project_id,
        "report_version_id": 802,
        "approval_snapshot_hash": _h("approval-702"),
        "report_content_hash": _h("report-802"),
    }

    def approval_reader(*, project_id: int, report_version_id: int):
        if project_id != original_approval["project_id"]:
            return None
        if report_version_id == original_approval["report_version_id"]:
            return dict(original_approval)
        if report_version_id == approval_two["report_version_id"]:
            return dict(approval_two)
        return None

    monkeypatch.setattr(binding_module, "get_report_approval_snapshot", approval_reader)

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        create_approval_recipient_binding(
            project_id=project_id,
            report_version_id=802,
            expected_recipient_config_version_id=int(config["id"]),
            expected_recipient_config_version_no=int(config["version_no"]),
            confirmed_by="Project Manager",
            confirmed_timezone="Asia/Shanghai",
            confirmed_utc_offset_minutes=480,
            human_confirmed=True,
            idempotency_key="shared-idem",
        )
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_IDEMPOTENCY_CONFLICT"


def test_r3_09_same_idempotency_key_with_changed_parameters_conflicts(binding_db):
    project_id = int(binding_db["project_id"])
    config = _save_config(project_id, ("one@example.test",), 0)
    _bind(project_id, config, idem="same-key")

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        create_approval_recipient_binding(
            project_id=project_id,
            report_version_id=801,
            expected_recipient_config_version_id=int(config["id"]),
            expected_recipient_config_version_no=int(config["version_no"]),
            confirmed_by="Different Human",
            confirmed_timezone="Asia/Shanghai",
            confirmed_utc_offset_minutes=480,
            human_confirmed=True,
            idempotency_key="same-key",
        )
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_IDEMPOTENCY_CONFLICT"


def test_r3_approval_owner_legacy_id_only_fails_closed(binding_db, monkeypatch):
    project_id = int(binding_db["project_id"])
    config = _save_config(project_id, ("legacy-id@example.test",), 0)
    approval = dict(binding_db["approval"])
    legacy_id = approval.pop("approval_snapshot_id")
    approval["id"] = legacy_id
    monkeypatch.setattr(binding_module, "get_report_approval_snapshot", lambda **_: dict(approval))

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        _bind(project_id, config, idem="legacy-id-only")
    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_APPROVAL_INVALID"
    assert get_approval_recipient_binding(project_id=project_id, report_version_id=801) is None
