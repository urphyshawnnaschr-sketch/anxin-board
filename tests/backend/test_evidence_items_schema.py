"""EvidenceItem Storage Contract V1 的幂等建表与快照内唯一性测试。"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db  # noqa: E402


EXPECTED_COLUMNS = [
    "id",
    "snapshot_id",
    "evidence_id",
    "type",
    "source_ref",
    "content_hash",
    "selected",
    "redaction_state",
]


def _insert_item(conn, *, snapshot_id, evidence_id, selected=1):
    conn.execute(
        """
        INSERT INTO evidence_items (
            snapshot_id, evidence_id, type, source_ref, content_hash,
            selected, redaction_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            evidence_id,
            "opaque-test-type",
            "opaque-test-source",
            "a" * 64,
            selected,
            "opaque-test-state",
        ),
    )


def test_init_db_creates_evidence_item_storage_contract_idempotently(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))

    db.init_db()
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = conn.execute("PRAGMA table_info(evidence_items)").fetchall()
        indexes = conn.execute("PRAGMA index_list(evidence_items)").fetchall()

    assert [column[1] for column in columns] == EXPECTED_COLUMNS
    assert any(index[2] == 1 for index in indexes)


def test_evidence_id_is_unique_within_snapshot_but_reusable_across_snapshots(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        _insert_item(conn, snapshot_id=1, evidence_id="shared-evidence")
        _insert_item(conn, snapshot_id=2, evidence_id="shared-evidence")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(conn, snapshot_id=1, evidence_id="shared-evidence")

        rows = conn.execute(
            "SELECT snapshot_id, evidence_id FROM evidence_items ORDER BY snapshot_id"
        ).fetchall()

    assert rows == [(1, "shared-evidence"), (2, "shared-evidence")]


def test_selected_accepts_only_boolean_storage_values(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        _insert_item(conn, snapshot_id=1, evidence_id="selected", selected=1)
        _insert_item(conn, snapshot_id=1, evidence_id="not-selected", selected=0)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(conn, snapshot_id=1, evidence_id="invalid-selected", selected=2)
