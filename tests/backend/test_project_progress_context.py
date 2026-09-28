"""Frozen cumulative context must never follow mutable latest progress."""
import json
import sqlite3

import pytest

from app import project_progress_context as progress_context


def snapshot():
    return {"id": 4, "project_id": 1, "snapshot_hash": "1" * 64,
            "project_repository_url": "https://github.com/example/project.git",
            "branch": "main", "analysis_lineage_id": 2, "profile_id": 3,
            "profile_content_hash": "2" * 64,
            "from_commit": "a" * 40, "to_commit": "c" * 40}


@pytest.fixture()
def conn():
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("BEGIN")
    yield value
    value.close()


def test_freeze_captures_previous_state_and_does_not_reread_latest(conn, monkeypatch):
    previous = {"snapshot_hash": "3" * 64, "report": {"head_sha": "b" * 40},
                "modules": [{"module_id": "login", "name": "用户登录", "stage": "已完成"}]}
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: previous)
    frozen = progress_context.freeze_progress_context(conn, snapshot=snapshot(), commits=["b" * 40, "c" * 40])
    previous["modules"][0]["stage"] = "开发中"
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: pytest.fail("live state read"))
    reread = progress_context.read_progress_context(conn, snapshot=snapshot())
    assert reread == frozen
    assert reread["previous"]["modules"][0]["stage"] == "已完成"


def test_explicit_no_previous_is_immutable_even_after_future_reports(conn, monkeypatch):
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: None)
    frozen = progress_context.freeze_progress_context(conn, snapshot=snapshot(), commits=["c" * 40])
    assert frozen["previous"] is None
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: pytest.fail("must stay frozen"))
    assert progress_context.read_progress_context(conn, snapshot=snapshot()) == frozen
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM evidence_progress_contexts")


def test_foreign_or_future_head_is_rejected_before_send(conn, monkeypatch):
    previous = {"snapshot_hash": "3" * 64, "report": {"head_sha": "d" * 40}, "modules": []}
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: previous)
    with pytest.raises(progress_context.ProgressContextError):
        progress_context.freeze_progress_context(conn, snapshot=snapshot(), commits=["c" * 40])


def test_historical_unbound_context_remains_absent(conn):
    assert progress_context.read_progress_context(conn, snapshot=snapshot()) is None
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='evidence_progress_contexts'").fetchone() is None


def test_binding_identity_and_content_tampering_are_rejected(conn, monkeypatch):
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: None)
    progress_context.freeze_progress_context(conn, snapshot=snapshot(), commits=["c" * 40])
    for key, value in (("snapshot_hash", "4" * 64), ("profile_id", 8), ("branch", "other")):
        with pytest.raises(progress_context.ProgressContextError):
            progress_context.read_progress_context(conn, snapshot={**snapshot(), key: value})


def test_context_requires_existing_transaction(conn, monkeypatch):
    conn.rollback()
    monkeypatch.setattr(progress_context, "read_latest", lambda *_a, **_k: None)
    with pytest.raises(progress_context.ProgressContextError):
        progress_context.freeze_progress_context(conn, snapshot=snapshot(), commits=[])
