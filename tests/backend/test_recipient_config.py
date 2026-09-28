from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from app import db
from app import recipient_config as recipient_config_module
from app.recipient_config import (
    RecipientConfigError,
    canonicalize_to_recipients,
    get_current_recipient_config,
    get_recipient_config_history,
    save_recipient_config,
)


@pytest.fixture()
def recipient_db(tmp_path, monkeypatch):
    db_path = tmp_path / "recipient-config.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, status, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?)",
            ("Recipient Test", "active", "2026-09-04T00:00:00+00:00", "2026-09-04T00:00:00+00:00", 1),
        )
        project_id = int(conn.execute("SELECT id FROM projects").fetchone()["id"])
    return project_id, db_path


def _expect_code(code: str, function, *args, **kwargs):
    with pytest.raises(RecipientConfigError) as raised:
        function(*args, **kwargs)
    assert raised.value.code == code


def test_canonicalization_preserves_human_order_and_local_case():
    assert canonicalize_to_recipients(
        ["Alice@EXAMPLE.COM", "bob@sub.Example.com", "Alice@example.com", "alice@example.com"]
    ) == (
        "Alice@example.com",
        "bob@sub.example.com",
        "alice@example.com",
    )


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["hello"],
        ["a@b"],
        ["a@@example.com"],
        [".a@example.com"],
        ["a..b@example.com"],
        ["a@-example.com"],
        ["a@example-.com"],
        ["a@exa_mple.com"],
        ['"a b"@example.com'],
        ["Alice <alice@example.com>"],
        ["group:alice@example.com;"],
        ["*@example.com"],
        [" alice@example.com"],
        ["alice@example.com\r\nBcc:x@example.com"],
        ["用户@example.com"],
    ],
)
def test_invalid_or_out_of_scope_mailboxes_are_rejected(value):
    _expect_code("RECIPIENT_CONFIG_INPUT_INVALID", canonicalize_to_recipients, value)


def test_recipient_count_is_bounded():
    values = [f"user{index}@example.com" for index in range(101)]
    _expect_code("RECIPIENT_CONFIG_INPUT_INVALID", canonicalize_to_recipients, values)


def test_save_rejects_unordered_iterable_input(recipient_db):
    project_id, _ = recipient_db
    _expect_code(
        "RECIPIENT_CONFIG_INPUT_INVALID",
        save_recipient_config,
        project_id=project_id,
        to_recipients={"a@example.com", "b@example.com"},
        created_by="PM",
        expected_version_no=0,
    )


@pytest.mark.parametrize(
    "created_by",
    [
        "PM\u0080X",
        "PM\u0085X",
        "PM\u009fX",
        "PM\u2028X",
        "PM\u2029X",
    ],
)
def test_created_by_rejects_c1_and_unicode_line_separators(recipient_db, created_by):
    project_id, _ = recipient_db
    _expect_code(
        "RECIPIENT_CONFIG_INPUT_INVALID",
        save_recipient_config,
        project_id=project_id,
        to_recipients=["a@example.com"],
        created_by=created_by,
        expected_version_no=0,
    )


def test_save_versions_noop_and_history_are_immutable(recipient_db):
    project_id, _ = recipient_db
    first = save_recipient_config(
        project_id=project_id,
        to_recipients=["Alice@EXAMPLE.COM", "bob@example.com"],
        created_by="PM-A",
        expected_version_no=0,
    )
    assert first["version_no"] == 1
    assert first["to_recipients"] == ["Alice@example.com", "bob@example.com"]
    assert first["predecessor_version_id"] is None

    replay = save_recipient_config(
        project_id=project_id,
        to_recipients=["Alice@example.com", "bob@EXAMPLE.COM"],
        created_by="PM-B",
        expected_version_no=1,
    )
    assert replay["id"] == first["id"]
    assert replay["version_no"] == 1
    assert replay["created_by"] == "PM-A"

    second = save_recipient_config(
        project_id=project_id,
        to_recipients=["carol@example.com"],
        created_by="PM-B",
        expected_version_no=1,
    )
    third = save_recipient_config(
        project_id=project_id,
        to_recipients=["Alice@example.com", "bob@example.com"],
        created_by="PM-C",
        expected_version_no=2,
    )
    assert [first["version_no"], second["version_no"], third["version_no"]] == [1, 2, 3]
    assert second["predecessor_version_id"] == first["id"]
    assert third["predecessor_version_id"] == second["id"]

    history = get_recipient_config_history(project_id)
    assert [item["id"] for item in history] == [first["id"], second["id"], third["id"]]
    assert history[0]["to_recipients"] == ["Alice@example.com", "bob@example.com"]
    assert get_current_recipient_config(project_id)["id"] == third["id"]


def test_expected_version_conflict_cannot_overwrite(recipient_db):
    project_id, _ = recipient_db
    save_recipient_config(
        project_id=project_id,
        to_recipients=["a@example.com"],
        created_by="PM",
        expected_version_no=0,
    )
    _expect_code(
        "RECIPIENT_CONFIG_VERSION_CONFLICT",
        save_recipient_config,
        project_id=project_id,
        to_recipients=["b@example.com"],
        created_by="PM",
        expected_version_no=0,
    )
    assert len(get_recipient_config_history(project_id)) == 1


def test_unexpected_sqlite_integrity_error_is_stored_invalid(recipient_db, monkeypatch):
    project_id, _ = recipient_db
    real_get_connection = recipient_config_module.get_connection

    class IntegrityFaultConnection:
        def __init__(self):
            self._inner = real_get_connection()

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def execute(self, sql, parameters=()):
            if "INSERT INTO recipient_config_versions" in sql:
                raise sqlite3.IntegrityError("injected unexpected schema integrity failure")
            return self._inner.execute(sql, parameters)

        def commit(self):
            return self._inner.commit()

        def rollback(self):
            return self._inner.rollback()

    monkeypatch.setattr(
        recipient_config_module,
        "get_connection",
        lambda: IntegrityFaultConnection(),
    )

    _expect_code(
        "RECIPIENT_CONFIG_STORED_INVALID",
        save_recipient_config,
        project_id=project_id,
        to_recipients=["a@example.com"],
        created_by="PM",
        expected_version_no=0,
    )
    assert get_recipient_config_history(project_id) == []


def test_missing_project_fails_closed(recipient_db):
    _project_id, _ = recipient_db
    _expect_code(
        "RECIPIENT_CONFIG_PROJECT_NOT_FOUND",
        save_recipient_config,
        project_id=9999,
        to_recipients=["a@example.com"],
        created_by="PM",
        expected_version_no=0,
    )


def test_append_only_triggers_reject_update_and_delete(recipient_db):
    project_id, _ = recipient_db
    item = save_recipient_config(
        project_id=project_id,
        to_recipients=["a@example.com"],
        created_by="PM",
        expected_version_no=0,
    )
    with db.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE recipient_config_versions SET created_by = 'x' WHERE id = ?", (item["id"],))
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM recipient_config_versions WHERE id = ?", (item["id"],))


def test_tampered_row_hash_fails_closed_even_if_trigger_is_bypassed(recipient_db):
    project_id, _ = recipient_db
    item = save_recipient_config(
        project_id=project_id,
        to_recipients=["a@example.com"],
        created_by="PM",
        expected_version_no=0,
    )
    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER trg_recipient_config_no_update")
        conn.execute("UPDATE recipient_config_versions SET created_by = 'other' WHERE id = ?", (item["id"],))
    _expect_code("RECIPIENT_CONFIG_STORED_INVALID", get_current_recipient_config, project_id)


def test_tampered_predecessor_chain_fails_closed(recipient_db):
    project_id, _ = recipient_db
    first = save_recipient_config(
        project_id=project_id,
        to_recipients=["a@example.com"],
        created_by="PM",
        expected_version_no=0,
    )
    second = save_recipient_config(
        project_id=project_id,
        to_recipients=["b@example.com"],
        created_by="PM",
        expected_version_no=1,
    )
    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER trg_recipient_config_no_update")
        conn.execute(
            "UPDATE recipient_config_versions SET predecessor_version_id = NULL WHERE id = ?",
            (second["id"],),
        )
    _expect_code("RECIPIENT_CONFIG_STORED_INVALID", get_recipient_config_history, project_id)
    assert first["id"] != second["id"]


def test_module_has_no_network_credential_or_transport_authority():
    source = Path(importlib.import_module("app.recipient_config").__file__).read_text(encoding="utf-8")
    forbidden_imports = (
        r"\bimport\s+(?:smtplib|socket|requests|httpx)\b",
        r"\bfrom\s+(?:smtplib|socket|requests|httpx)\b",
        r"\bfrom\s+app\.(?:mail_gateway|secret_store|windows_credential_store)\b",
    )
    for pattern in forbidden_imports:
        assert re.search(pattern, source) is None
