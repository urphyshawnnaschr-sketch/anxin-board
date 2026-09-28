"""Regression: report task/status reads must not contend for the project Git workspace lock."""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db, report_generation_tasks as tasks  # noqa: E402
from app.git_workspace_locks import project_workspace_lock  # noqa: E402


SNAPSHOT_HASH = "a" * 64


def test_task_refresh_and_recovery_stay_available_while_git_workspace_is_busy(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "task-lock-decoupling.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    db.init_db()
    with sqlite3.connect(db_path) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES ('Lock fixture', 'active', 'now')"
        ).lastrowid

    monkeypatch.setattr(
        tasks,
        "validate_context_snapshot_identity",
        lambda snapshot_id: {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "snapshot_hash": SNAPSHOT_HASH,
        },
    )
    created = tasks.create_report_generation_task(
        project_id=project_id,
        local_task_id="durable-task",
        evidence_snapshot_id=11,
        create_key="durable-create",
    )

    entered = threading.Event()
    release = threading.Event()

    def hold_workspace_lock() -> None:
        with project_workspace_lock(project_id):
            entered.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_workspace_lock, daemon=True)
    holder.start()
    assert entered.wait(timeout=5), "fixture failed to acquire project Git workspace lock"

    try:
        refreshed = tasks.get_report_generation_task(
            project_id=project_id,
            local_task_id="durable-task",
        )
        recovered = tasks.find_active_report_generation_task(
            project_id=project_id,
            evidence_snapshot_id=11,
        )

        assert refreshed["id"] == created["id"]
        assert recovered is not None
        assert recovered["id"] == created["id"]
        assert recovered["state"] == "queued"
        assert holder.is_alive(), "task/status read unexpectedly waited for Git lock release"
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder.is_alive()
