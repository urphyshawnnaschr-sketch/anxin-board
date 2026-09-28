from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.report_approval_recipient_binding as binding_module  # noqa: E402
from app.report_approval_recipient_binding import (  # noqa: E402
    ApprovalRecipientBindingError,
    create_approval_recipient_binding,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _UnexpectedIntegrityConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=()):
        normalized = " ".join(sql.split()).casefold()
        if normalized.startswith("begin immediate"):
            return None
        if "where idempotency_key = ?" in normalized:
            return _RowResult(None)
        if "where project_id = ? and report_version_id = ?" in normalized:
            return _RowResult(None)
        if normalized.startswith("insert into report_approval_recipient_bindings"):
            raise sqlite3.IntegrityError("unexpected residual constraint failure")
        raise AssertionError(f"unexpected SQL in classification harness: {sql}")


class _RowResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def test_r3_residual_sqlite_integrity_failure_is_not_misreported_as_user_conflict(monkeypatch):
    approval = {
        "approval_snapshot_id": 41,
        "approval_snapshot_hash": _h("approval-41"),
        "report_content_hash": _h("report-51"),
    }
    recipients_hash = _h('["one@example.test"]')
    current_config = {
        "id": 61,
        "version_no": 1,
        "recipients_hash": recipients_hash,
        "to_recipients": ["one@example.test"],
    }

    monkeypatch.setattr(binding_module, "_ensure_schema", lambda: None)
    monkeypatch.setattr(binding_module, "_approval_fact", lambda **_: dict(approval))
    monkeypatch.setattr(binding_module, "_recipient_history", lambda _: [dict(current_config)])
    monkeypatch.setattr(
        binding_module,
        "get_connection",
        lambda: _UnexpectedIntegrityConnection(),
    )

    with pytest.raises(ApprovalRecipientBindingError) as caught:
        create_approval_recipient_binding(
            project_id=7,
            report_version_id=51,
            expected_recipient_config_version_id=61,
            expected_recipient_config_version_no=1,
            confirmed_by="Project Manager",
            confirmed_timezone="Asia/Shanghai",
            confirmed_utc_offset_minutes=480,
            human_confirmed=True,
            idempotency_key="r3-residual-storage",
        )

    assert caught.value.code == "REPORT_APPROVAL_RECIPIENT_STORED_INVALID"
    assert caught.value.code != "REPORT_APPROVAL_RECIPIENT_CONFLICT"
