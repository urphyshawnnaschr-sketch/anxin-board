"""Focused regression for fail-closed EvidenceSnapshot exact replay integrity."""

import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

import test_evidence_snapshots as base


client = base.client


def test_tampered_snapshot_hash_fails_closed_on_exact_replay(client):
    api, db_path = client
    facts = base._prepare(api, db_path)
    first = base._confirm(api, facts)
    assert first.status_code == 201, first.text
    snapshot_id = first.json()["snapshot"]["id"]

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE evidence_snapshots SET snapshot_hash = ? WHERE id = ?",
            ("f" * 64, snapshot_id),
        )

    replay = base._confirm(api, facts)
    assert replay.status_code == 500, replay.text
    assert replay.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID"
    assert len(base._evidence_rows(db_path)) == 1


def test_tampered_evidence_item_hash_fails_closed_on_exact_replay(client):
    api, db_path = client
    facts = base._prepare(api, db_path)
    first = base._confirm(api, facts)
    assert first.status_code == 201, first.text
    snapshot_id = first.json()["snapshot"]["id"]

    with sqlite3.connect(db_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM evidence_items WHERE snapshot_id = ? ORDER BY id LIMIT 1",
            (snapshot_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE evidence_items SET content_hash = ? WHERE id = ?",
            ("f" * 64, item_id),
        )

    replay = base._confirm(api, facts)
    assert replay.status_code == 500, replay.text
    assert replay.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID"
    assert len(base._evidence_rows(db_path)) == 1
    assert len(base._evidence_item_rows(db_path)) > 0


@pytest.mark.parametrize("tamper_target", ["snapshot", "item"])
def test_tampered_authority_still_fails_closed_after_app_restart(client, tamper_target):
    api, db_path = client
    facts = base._prepare(api, db_path, name=f"Restart replay {tamper_target}")
    first = base._confirm(api, facts)
    assert first.status_code == 201, first.text
    snapshot_id = first.json()["snapshot"]["id"]

    with sqlite3.connect(db_path) as conn:
        if tamper_target == "snapshot":
            conn.execute(
                "UPDATE evidence_snapshots SET snapshot_hash = ? WHERE id = ?",
                ("e" * 64, snapshot_id),
            )
        else:
            item_id = conn.execute(
                "SELECT id FROM evidence_items WHERE snapshot_id = ? ORDER BY id LIMIT 1",
                (snapshot_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE evidence_items SET content_hash = ? WHERE id = ?",
                ("e" * 64, item_id),
            )

    importlib.reload(base.main)
    with TestClient(base.main.app) as restarted:
        replay = base._confirm(restarted, facts)

    assert replay.status_code == 500, replay.text
    assert replay.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID"
    assert len(base._evidence_rows(db_path)) == 1
    assert len(base._evidence_item_rows(db_path)) > 0


@pytest.mark.parametrize(
    ("column", "value"),
    [("selected", 0), ("redaction_state", "tampered")],
)
def test_tampered_evidence_item_state_fails_closed_on_exact_replay(client, column, value):
    api, db_path = client
    facts = base._prepare(api, db_path, name=f"Child state tamper {column}")
    first = base._confirm(api, facts)
    assert first.status_code == 201, first.text
    snapshot_id = first.json()["snapshot"]["id"]

    with sqlite3.connect(db_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM evidence_items WHERE snapshot_id = ? ORDER BY id LIMIT 1",
            (snapshot_id,),
        ).fetchone()[0]
        conn.execute(
            f"UPDATE evidence_items SET {column} = ? WHERE id = ?",
            (value, item_id),
        )

    replay = base._confirm(api, facts)
    assert replay.status_code == 500, replay.text
    assert replay.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID"
    assert len(base._evidence_rows(db_path)) == 1
