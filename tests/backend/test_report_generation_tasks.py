"""Report Generation Task Core V1 acceptance tests."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db, report_generation_tasks as tasks  # noqa: E402


HASH_A = "a" * 64
HASH_B = "b" * 64


def _code(exc: HTTPException) -> str:
    return exc.detail["code"]


def _candidate(project_id: int, snapshot_id: int, snapshot_hash: str = HASH_A) -> dict:
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
    }


@pytest.fixture()
def task_state(tmp_path, monkeypatch):
    db_path = tmp_path / "task-core.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    db.init_db()
    with sqlite3.connect(db_path) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES ('Task Core', 'active', 'now')"
        ).lastrowid
    monkeypatch.setattr(
        tasks,
        "validate_context_snapshot_identity",
        lambda snapshot_id: _candidate(project_id, snapshot_id),
    )
    return {"db_path": db_path, "project_id": project_id}


def _create(task_state, *, local_task_id="task-1", snapshot_id=1, create_key="create-1"):
    return tasks.create_report_generation_task(
        project_id=task_state["project_id"],
        local_task_id=local_task_id,
        evidence_snapshot_id=snapshot_id,
        create_key=create_key,
    )


def _rows(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        tasks_count = conn.execute("SELECT COUNT(*) FROM report_generation_tasks").fetchone()[0]
        attempts_count = conn.execute(
            "SELECT COUNT(*) FROM report_generation_task_attempts"
        ).fetchone()[0]
    return tasks_count, attempts_count


def _force_state(db_path: Path, task_id: int, attempt_id: str, state: str):
    started = None if state == "queued" else "2026-08-25T00:00:00+00:00"
    finished = (
        "2026-08-25T00:01:00+00:00"
        if state in {"succeeded", "failed", "voided"}
        else None
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE report_generation_tasks SET state = ?, updated_at = 'force' WHERE id = ?",
            (state, task_id),
        )
        conn.execute(
            """
            UPDATE report_generation_task_attempts
            SET state = ?, started_at = ?, finished_at = ?, updated_at = 'force'
            WHERE attempt_id = ?
            """,
            (state, started, finished, attempt_id),
        )


def _transition_path(task_state, path):
    current = None
    for old, new in path:
        current = tasks.transition_report_generation_task(
            project_id=task_state["project_id"],
            local_task_id="task-1",
            expected_state=old,
            new_state=new,
        )
    return current


def test_schema_migration_is_idempotent_and_preserves_existing_data(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO projects (name, status, created_at) VALUES ('old', 'active', 'old')")
    db.init_db()
    db.init_db()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "report_generation_tasks" in tables
        assert "report_generation_task_attempts" in tables
        assert conn.execute("SELECT name FROM projects WHERE id = 1").fetchone()[0] == "old"


def test_active_checkpoint_index_is_exact_and_idempotent(task_state):
    db.init_db()
    db.init_db()

    with sqlite3.connect(task_state["db_path"]) as conn:
        index_rows = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA index_list(report_generation_tasks)"
            ).fetchall()
        }
        index = index_rows["ux_report_generation_tasks_active_checkpoint"]
        columns = [
            row[2]
            for row in conn.execute(
                "PRAGMA index_info(ux_report_generation_tasks_active_checkpoint)"
            ).fetchall()
        ]

    assert index[2] == 1
    assert index[4] == 1
    assert columns == ["project_id", "evidence_snapshot_id"]


def test_active_checkpoint_index_rejects_future_task_type_bypass(task_state):
    _create(task_state)
    with sqlite3.connect(task_state["db_path"]) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO report_generation_tasks (
                    schema_version, project_id, local_task_id, evidence_snapshot_id,
                    task_type, create_key, current_attempt_id, state, identity_hash,
                    created_at, updated_at
                ) VALUES (?, ?, 'future-task', 1, 'future_report_generate',
                          'future-create', 'future-attempt', 'queued', ?, 'now', 'now')
                """,
                (tasks.SCHEMA_VERSION, task_state["project_id"], HASH_B),
            )


def test_wrong_same_named_checkpoint_index_fails_closed(task_state):
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute(
            "DROP INDEX IF EXISTS ux_report_generation_tasks_active_checkpoint"
        )
        conn.execute(
            """
            CREATE INDEX ux_report_generation_tasks_active_checkpoint
            ON report_generation_tasks(project_id)
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.init_db()

    with sqlite3.connect(task_state["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = 'ux_report_generation_tasks_active_checkpoint'
            """,
        ).fetchone()
    assert row is not None
    assert "ON report_generation_tasks(project_id)" in row[0]


def test_legacy_duplicate_active_checkpoint_migration_fails_without_rewriting_history(
    task_state,
):
    first = _create(task_state)
    now = tasks._now()
    second_row = {
        "schema_version": tasks.SCHEMA_VERSION,
        "project_id": task_state["project_id"],
        "local_task_id": "legacy-task-2",
        "evidence_snapshot_id": 1,
        "task_type": tasks.TASK_TYPE,
        "create_key": "legacy-create-2",
        "created_at": now,
        "updated_at": now,
    }
    second_row["identity_hash"] = tasks._stable_hash(
        tasks._identity_payload(second_row)
    )

    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "DROP INDEX IF EXISTS ux_report_generation_tasks_active_checkpoint"
        )
        tasks._insert_new_task(
            conn,
            row=second_row,
            attempt_id="legacy-attempt-2",
            now=now,
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.init_db()

    with sqlite3.connect(task_state["db_path"]) as conn:
        rows = conn.execute(
            """
            SELECT id, local_task_id, state FROM report_generation_tasks
            WHERE project_id = ? AND evidence_snapshot_id = 1
            ORDER BY id
            """,
            (task_state["project_id"],),
        ).fetchall()
    assert rows == [
        (first["id"], "task-1", "queued"),
        (first["id"] + 1, "legacy-task-2", "queued"),
    ]


def test_valid_create_atomically_binds_first_attempt_and_exact_identity(task_state):
    created = _create(task_state)
    assert created["schema_version"] == tasks.SCHEMA_VERSION
    assert created["task_type"] == tasks.TASK_TYPE
    assert created["state"] == "queued"
    assert created["current_attempt_id"] == created["current_attempt"]["attempt_id"]
    assert created["current_attempt"]["task_id"] == created["id"]
    assert created["current_attempt"]["sequence_no"] == 1
    assert created["current_attempt"]["state"] == "queued"
    payload = {
        "schema_version": tasks.SCHEMA_VERSION,
        "local_task_id": "task-1",
        "project_id": task_state["project_id"],
        "evidence_snapshot_id": 1,
        "task_type": tasks.TASK_TYPE,
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert created["identity_hash"] == hashlib.sha256(raw).hexdigest()
    assert _rows(task_state["db_path"]) == (1, 1)


def test_exact_create_replay_returns_same_logical_task(task_state):
    first = _create(task_state)
    second = _create(task_state)
    assert second["id"] == first["id"]
    assert second["current_attempt_id"] == first["current_attempt_id"]
    assert _rows(task_state["db_path"]) == (1, 1)


def test_same_create_key_identity_drift_is_stable_conflict(task_state):
    _create(task_state)
    with pytest.raises(HTTPException) as caught:
        _create(task_state, snapshot_id=2)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_CREATE_CONFLICT"
    assert _rows(task_state["db_path"]) == (1, 1)


def test_different_create_key_same_logical_identity_is_conflict(task_state):
    _create(task_state)
    with pytest.raises(HTTPException) as caught:
        _create(task_state, create_key="create-2")
    assert _code(caught.value) == "REPORT_GENERATION_TASK_LOGICAL_TASK_CONFLICT"
    assert _rows(task_state["db_path"]) == (1, 1)


def test_same_logical_id_snapshot_drift_is_identity_conflict(task_state):
    _create(task_state)
    with pytest.raises(HTTPException) as caught:
        _create(task_state, create_key="create-2", snapshot_id=2)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_LOGICAL_IDENTITY_CONFLICT"
    assert _rows(task_state["db_path"]) == (1, 1)


@pytest.mark.parametrize(
    "path",
    [
        [],
        [("queued", "running")],
        [("queued", "running"), ("running", "unknown")],
    ],
    ids=["queued", "running", "unknown"],
)
def test_every_active_state_holds_checkpoint(task_state, path):
    _create(task_state)
    _transition_path(task_state, path)

    with pytest.raises(HTTPException) as caught:
        _create(task_state, local_task_id="task-2", create_key="create-2")

    assert _code(caught.value) == "REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT"
    assert caught.value.status_code == 409
    assert _rows(task_state["db_path"]) == (1, 1)


def test_different_checkpoint_in_same_project_is_allowed(task_state):
    first = _create(task_state)
    second = _create(
        task_state,
        local_task_id="task-2",
        snapshot_id=2,
        create_key="create-2",
    )

    assert second["id"] != first["id"]
    assert _rows(task_state["db_path"]) == (2, 2)


def test_same_numeric_checkpoint_in_different_projects_does_not_conflict(
    task_state, monkeypatch
):
    with sqlite3.connect(task_state["db_path"]) as conn:
        second_project_id = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES ('Other', 'active', 'now')"
        ).lastrowid

    bound_project = {"id": task_state["project_id"]}
    monkeypatch.setattr(
        tasks,
        "validate_context_snapshot_identity",
        lambda snapshot_id: _candidate(bound_project["id"], snapshot_id),
    )
    first = _create(task_state)
    bound_project["id"] = second_project_id
    second = tasks.create_report_generation_task(
        project_id=second_project_id,
        local_task_id="task-2",
        evidence_snapshot_id=1,
        create_key="create-2",
    )

    assert second["project_id"] != first["project_id"]
    assert _rows(task_state["db_path"]) == (2, 2)


@pytest.mark.parametrize(
    "path",
    [
        [("queued", "running"), ("running", "succeeded")],
        [("queued", "running"), ("running", "failed")],
        [
            ("queued", "running"),
            ("running", "unknown"),
            ("unknown", "voided"),
        ],
    ],
    ids=["succeeded", "failed", "voided"],
)
def test_every_terminal_state_releases_checkpoint_without_deleting_history(
    task_state, path
):
    first = _create(task_state)
    terminal = _transition_path(task_state, path)
    second = _create(task_state, local_task_id="task-2", create_key="create-2")

    assert terminal["state"] in {"succeeded", "failed", "voided"}
    assert second["id"] != first["id"]
    assert second["state"] == "queued"
    assert _rows(task_state["db_path"]) == (2, 2)


def test_exact_terminal_replay_precedes_new_active_checkpoint_owner(task_state):
    first = _create(task_state)
    _transition_path(
        task_state,
        [("queued", "running"), ("running", "succeeded")],
    )
    second = _create(task_state, local_task_id="task-2", create_key="create-2")

    replay = _create(task_state)

    assert replay["id"] == first["id"]
    assert replay["current_attempt_id"] == first["current_attempt_id"]
    assert replay["state"] == "succeeded"
    assert second["state"] == "queued"
    assert _rows(task_state["db_path"]) == (2, 2)


def test_corrupt_same_checkpoint_state_blocks_new_admission(task_state):
    created = _create(task_state)
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE report_generation_tasks SET state = 'corrupt' WHERE id = ?",
            (created["id"],),
        )
        conn.execute(
            """
            UPDATE report_generation_task_attempts
            SET state = 'corrupt'
            WHERE attempt_id = ?
            """,
            (created["current_attempt_id"],),
        )

    with pytest.raises(HTTPException) as caught:
        _create(task_state, local_task_id="task-2", create_key="create-2")

    assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"
    assert _rows(task_state["db_path"]) == (1, 1)


def test_two_independent_connections_race_for_same_checkpoint_only_one_wins(
    task_state, monkeypatch
):
    original = tasks._insert_new_task
    barrier = threading.Barrier(2, timeout=30)
    outcome_lock = threading.Lock()
    connection_ids: list[int] = []
    outcomes: dict[str, tuple[str, object]] = {}

    def synchronized_insert(conn, *, row, attempt_id, now):
        with outcome_lock:
            connection_ids.append(id(conn))
        barrier.wait()
        return original(conn, row=row, attempt_id=attempt_id, now=now)

    monkeypatch.setattr(tasks, "_insert_new_task", synchronized_insert)

    def create(name: str):
        try:
            result = _create(
                task_state,
                local_task_id=f"task-{name}",
                create_key=f"create-{name}",
            )
            outcome = ("created", result["id"])
        except HTTPException as exc:
            outcome = ("error", _code(exc))
        with outcome_lock:
            outcomes[name] = outcome

    threads = [
        threading.Thread(target=create, args=("a",)),
        threading.Thread(target=create, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert len(set(connection_ids)) == 2
    assert sorted(outcome[0] for outcome in outcomes.values()) == [
        "created",
        "error",
    ]
    assert {
        outcome[1] for outcome in outcomes.values() if outcome[0] == "error"
    } == {"REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT"}
    assert _rows(task_state["db_path"]) == (1, 1)


@pytest.mark.parametrize("bad", [True, False, "1", 1.0, 0, -1, 2**63])
def test_signed_sqlite_guard_rejects_project_id_before_seam_or_binding(task_state, monkeypatch, bad):
    called = False

    def forbidden(_snapshot_id):
        nonlocal called
        called = True
        raise AssertionError("formal seam must not be reached")

    monkeypatch.setattr(tasks, "validate_context_snapshot_identity", forbidden)
    with pytest.raises(HTTPException) as caught:
        tasks.create_report_generation_task(
            project_id=bad,
            local_task_id="task",
            evidence_snapshot_id=1,
            create_key="key",
        )
    assert _code(caught.value) == "REPORT_GENERATION_TASK_INPUT_INVALID"
    assert called is False


@pytest.mark.parametrize("bad", [True, False, "1", 1.0, 0, -1, 2**63])
def test_signed_sqlite_guard_rejects_snapshot_id_before_seam_or_binding(task_state, monkeypatch, bad):
    called = False

    def forbidden(_snapshot_id):
        nonlocal called
        called = True
        raise AssertionError("formal seam must not be reached")

    monkeypatch.setattr(tasks, "validate_context_snapshot_identity", forbidden)
    with pytest.raises(HTTPException) as caught:
        tasks.create_report_generation_task(
            project_id=task_state["project_id"],
            local_task_id="task",
            evidence_snapshot_id=bad,
            create_key="key",
        )
    assert _code(caught.value) == "REPORT_GENERATION_TASK_INPUT_INVALID"
    assert called is False


def test_candidate_seam_missing_cross_project_and_bad_hash_fail_closed_without_insert(task_state, monkeypatch):
    cases = [
        {},
        _candidate(task_state["project_id"] + 1, 1),
        _candidate(task_state["project_id"], 2),
        _candidate(task_state["project_id"], 1, "NOT_A_HASH"),
    ]
    for candidate in cases:
        monkeypatch.setattr(tasks, "validate_context_snapshot_identity", lambda _sid, value=candidate: value)
        with pytest.raises(HTTPException) as caught:
            _create(task_state)
        assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"
        assert _rows(task_state["db_path"]) == (0, 0)


def test_upstream_formal_seam_error_propagates_unchanged(task_state, monkeypatch):
    upstream = HTTPException(status_code=409, detail={"code": "CONTEXT_CANDIDATE_SNAPSHOT_UNSUPPORTED", "message": "x"})

    def fail(_snapshot_id):
        raise upstream

    monkeypatch.setattr(tasks, "validate_context_snapshot_identity", fail)
    with pytest.raises(HTTPException) as caught:
        _create(task_state)
    assert caught.value is upstream
    assert _rows(task_state["db_path"]) == (0, 0)


def test_read_revalidates_persisted_evidence_parent_binding(task_state, monkeypatch):
    created = _create(task_state)
    monkeypatch.setattr(
        tasks,
        "validate_context_snapshot_identity",
        lambda sid: _candidate(task_state["project_id"] + 1, sid),
    )
    with pytest.raises(HTTPException) as caught:
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")
    assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"
    with sqlite3.connect(task_state["db_path"]) as conn:
        assert conn.execute("SELECT state FROM report_generation_tasks WHERE id = ?", (created["id"],)).fetchone()[0] == "queued"


def test_identity_hash_tamper_fails_closed_without_repair(task_state):
    created = _create(task_state)
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute("UPDATE report_generation_tasks SET identity_hash = ? WHERE id = ?", (HASH_B, created["id"]))
    with pytest.raises(HTTPException) as caught:
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")
    assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"
    with sqlite3.connect(task_state["db_path"]) as conn:
        assert conn.execute("SELECT identity_hash FROM report_generation_tasks WHERE id = ?", (created["id"],)).fetchone()[0] == HASH_B


def test_task_attempt_state_disagreement_fails_closed(task_state):
    created = _create(task_state)
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute(
            "UPDATE report_generation_task_attempts SET state = 'running', started_at = 'x' WHERE attempt_id = ?",
            (created["current_attempt_id"],),
        )
    with pytest.raises(HTTPException) as caught:
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")
    assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"


def test_current_attempt_cross_task_missing_wrong_sequence_and_extra_attempt_fail_closed(task_state):
    first = _create(task_state)
    second = _create(
        task_state,
        local_task_id="task-2",
        snapshot_id=2,
        create_key="create-2",
    )
    db_path = task_state["db_path"]

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE report_generation_tasks SET current_attempt_id = ? WHERE id = ?",
            (second["current_attempt_id"], first["id"]),
        )
    with pytest.raises(HTTPException):
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE report_generation_tasks SET current_attempt_id = 'missing' WHERE id = ?",
            (first["id"],),
        )
    with pytest.raises(HTTPException):
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE report_generation_tasks SET current_attempt_id = ? WHERE id = ?",
            (first["current_attempt_id"], first["id"]),
        )
        conn.execute(
            "UPDATE report_generation_task_attempts SET sequence_no = 2 WHERE attempt_id = ?",
            (first["current_attempt_id"],),
        )
    with pytest.raises(HTTPException):
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE report_generation_task_attempts SET sequence_no = 1 WHERE attempt_id = ?",
            (first["current_attempt_id"],),
        )
        conn.execute(
            """
            INSERT INTO report_generation_task_attempts (
                schema_version, attempt_id, task_id, sequence_no, state, created_at, started_at, finished_at, updated_at
            ) VALUES (?, 'extra-attempt', ?, 2, 'queued', 'x', NULL, NULL, 'x')
            """,
            (tasks.SCHEMA_VERSION, first["id"]),
        )
    with pytest.raises(HTTPException):
        tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")


@pytest.mark.parametrize(
    "path",
    [
        [("queued", "running")],
        [("queued", "running"), ("running", "succeeded")],
        [("queued", "running"), ("running", "failed")],
        [("queued", "running"), ("running", "unknown")],
        [("queued", "running"), ("running", "unknown"), ("unknown", "voided")],
    ],
)
def test_every_legal_state_edge_updates_task_and_attempt_atomically(task_state, path):
    current = _create(task_state)
    for old, new in path:
        current = tasks.transition_report_generation_task(
            project_id=task_state["project_id"],
            local_task_id="task-1",
            expected_state=old,
            new_state=new,
        )
        assert current["state"] == new
        assert current["current_attempt"]["state"] == new
        assert current["current_attempt_id"] == current["current_attempt"]["attempt_id"]


@pytest.mark.parametrize(
    "old,new",
    [
        (old, new)
        for old in sorted(tasks.STATES)
        for new in sorted(tasks.STATES)
        if (old, new) not in tasks.LEGAL_TRANSITIONS
    ],
)
def test_every_unfrozen_state_pair_including_self_transition_is_forbidden(task_state, old, new):
    created = _create(task_state)
    _force_state(
        task_state["db_path"], created["id"], created["current_attempt_id"], old
    )
    with pytest.raises(HTTPException) as caught:
        tasks.transition_report_generation_task(
            project_id=task_state["project_id"],
            local_task_id="task-1",
            expected_state=old,
            new_state=new,
        )
    assert _code(caught.value) == "REPORT_GENERATION_TASK_TRANSITION_INVALID"


def test_unknown_survives_restart_and_cannot_auto_retry_or_resurrect(task_state):
    _create(task_state)
    tasks.transition_report_generation_task(
        project_id=task_state["project_id"],
        local_task_id="task-1",
        expected_state="queued",
        new_state="running",
    )
    unknown = tasks.transition_report_generation_task(
        project_id=task_state["project_id"],
        local_task_id="task-1",
        expected_state="running",
        new_state="unknown",
    )
    db.init_db()
    reread = tasks.get_report_generation_task(
        project_id=task_state["project_id"], local_task_id="task-1"
    )
    assert reread["id"] == unknown["id"]
    assert reread["current_attempt_id"] == unknown["current_attempt_id"]
    assert reread["state"] == "unknown"
    with pytest.raises(HTTPException):
        tasks.transition_report_generation_task(
            project_id=task_state["project_id"],
            local_task_id="task-1",
            expected_state="unknown",
            new_state="running",
        )


def test_create_transaction_fault_rolls_back_task_attempt_and_binding(task_state, monkeypatch):
    original = tasks._insert_new_task

    def fail_after_insert(conn, *, row, attempt_id, now):
        original(conn, row=row, attempt_id=attempt_id, now=now)
        raise tasks._stored_invalid("fault")

    monkeypatch.setattr(tasks, "_insert_new_task", fail_after_insert)
    with pytest.raises(HTTPException):
        _create(task_state)
    assert _rows(task_state["db_path"]) == (0, 0)


def test_transition_transaction_fault_rolls_back_both_rows(task_state, monkeypatch):
    created = _create(task_state)
    original = tasks._read_task_by_id
    seen = 0

    def fail_on_post_update(conn, task_id):
        nonlocal seen
        seen += 1
        if seen == 1:
            raise tasks._stored_invalid("fault")
        return original(conn, task_id)

    monkeypatch.setattr(tasks, "_read_task_by_id", fail_on_post_update)
    with pytest.raises(HTTPException):
        tasks.transition_report_generation_task(
            project_id=task_state["project_id"],
            local_task_id="task-1",
            expected_state="queued",
            new_state="running",
        )
    with sqlite3.connect(task_state["db_path"]) as conn:
        task_state_db = conn.execute(
            "SELECT state FROM report_generation_tasks WHERE id = ?", (created["id"],)
        ).fetchone()[0]
        attempt_state_db = conn.execute(
            "SELECT state FROM report_generation_task_attempts WHERE attempt_id = ?",
            (created["current_attempt_id"],),
        ).fetchone()[0]
    assert (task_state_db, attempt_state_db) == ("queued", "queued")


def _collision_wrapper(monkeypatch, winner_transform):
    original = tasks._insert_new_task
    injected = False

    def collide(conn, *, row, attempt_id, now):
        nonlocal injected
        if not injected:
            injected = True
            winner = dict(row)
            winner_transform(winner)
            if winner.get("evidence_snapshot_id") != row["evidence_snapshot_id"]:
                winner["identity_hash"] = tasks._stable_hash(tasks._identity_payload(winner))
            original(conn, row=winner, attempt_id="winner-attempt", now=now)
        return original(conn, row=row, attempt_id=attempt_id, now=now)

    monkeypatch.setattr(tasks, "_insert_new_task", collide)


def test_precise_unique_collision_same_create_key_same_identity_returns_winner(task_state, monkeypatch):
    _collision_wrapper(monkeypatch, lambda winner: None)
    result = _create(task_state)
    assert result["current_attempt_id"] == "winner-attempt"
    assert _rows(task_state["db_path"]) == (1, 1)


def test_precise_unique_collision_same_create_key_identity_drift_is_create_conflict(task_state, monkeypatch):
    _collision_wrapper(monkeypatch, lambda winner: winner.__setitem__("evidence_snapshot_id", 2))
    with pytest.raises(HTTPException) as caught:
        _create(task_state)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_CREATE_CONFLICT"
    assert _rows(task_state["db_path"]) == (0, 0)


def test_precise_unique_collision_different_key_same_logical_identity_is_logical_conflict(task_state, monkeypatch):
    _collision_wrapper(monkeypatch, lambda winner: winner.__setitem__("create_key", "other-key"))
    with pytest.raises(HTTPException) as caught:
        _create(task_state)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_LOGICAL_TASK_CONFLICT"
    assert _rows(task_state["db_path"]) == (0, 0)


def test_precise_unique_collision_same_logical_id_identity_drift_is_logical_identity_conflict(task_state, monkeypatch):
    def drift(winner):
        winner["create_key"] = "other-key"
        winner["evidence_snapshot_id"] = 2

    _collision_wrapper(monkeypatch, drift)
    with pytest.raises(HTTPException) as caught:
        _create(task_state)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_LOGICAL_IDENTITY_CONFLICT"
    assert _rows(task_state["db_path"]) == (0, 0)


def test_voided_history_is_retained_and_not_rewritten(task_state):
    created = _create(task_state)
    tasks.transition_report_generation_task(
        project_id=task_state["project_id"], local_task_id="task-1", expected_state="queued", new_state="running"
    )
    tasks.transition_report_generation_task(
        project_id=task_state["project_id"], local_task_id="task-1", expected_state="running", new_state="unknown"
    )
    voided = tasks.transition_report_generation_task(
        project_id=task_state["project_id"], local_task_id="task-1", expected_state="unknown", new_state="voided"
    )
    assert voided["id"] == created["id"]
    assert voided["current_attempt_id"] == created["current_attempt_id"]
    assert voided["state"] == "voided"
    assert _rows(task_state["db_path"]) == (1, 1)


def test_source_capability_boundary_has_no_transport_worker_or_generation_pipeline_calls():
    source_path = Path(tasks.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"requests", "httpx", "socket", "smtplib", "subprocess"})
    assert "generate_and_persist_anxin_board_report" not in source
    assert "model_call_ledger" not in source
    forbidden_calls = {"post", "get", "request", "sendmail", "popen", "run"}
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names.isdisjoint(forbidden_calls)


def test_changed_file_contract_is_five_files_only():
    expected = {
        "apps/backend/app/db.py",
        "apps/backend/app/report_generation_task_api.py",
        "apps/backend/app/report_generation_tasks.py",
        "tests/backend/test_report_generation_task_api.py",
        "tests/backend/test_report_generation_tasks.py",
    }
    assert len(expected) == 5


@pytest.mark.parametrize("state", sorted(tasks.STATES))
@pytest.mark.parametrize("snapshot", [None, 1])
def test_active_recovery_preserves_durable_state_and_attempt(task_state, state, snapshot):
    original = _create(task_state)
    _force_state(task_state["db_path"], original["id"], original["current_attempt_id"], state)
    before = tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1")
    # New DB connections model process/session restart; no browser token participates.
    for _ in range(2):
        actual = tasks.find_active_report_generation_task(
            project_id=task_state["project_id"], evidence_snapshot_id=snapshot
        )
        assert actual == (before if state in tasks.ACTIVE_STATES else None)
    assert _rows(task_state["db_path"]) == (1, 1)
    assert tasks.get_report_generation_task(project_id=task_state["project_id"], local_task_id="task-1") == before


def test_active_recovery_empty_and_other_snapshot_do_not_create(task_state):
    assert tasks.find_active_report_generation_task(project_id=task_state["project_id"]) is None
    _create(task_state)
    assert tasks.find_active_report_generation_task(project_id=task_state["project_id"], evidence_snapshot_id=2) is None
    assert _rows(task_state["db_path"]) == (1, 1)


def test_active_recovery_does_not_guess_latest_when_multiple_snapshots(task_state):
    first = _create(task_state)
    _create(task_state, local_task_id="second", snapshot_id=2, create_key="second")
    with pytest.raises(HTTPException) as caught:
        tasks.find_active_report_generation_task(project_id=task_state["project_id"])
    assert caught.value.status_code == 409
    assert _code(caught.value) == "REPORT_GENERATION_TASK_ACTIVE_RECOVERY_AMBIGUOUS"
    assert tasks.find_active_report_generation_task(project_id=task_state["project_id"], evidence_snapshot_id=1) == first
    assert _rows(task_state["db_path"]) == (2, 2)


def test_active_recovery_rejects_duplicate_checkpoint_without_repair(task_state):
    _create(task_state)
    second = _create(task_state, local_task_id="second", snapshot_id=2, create_key="second")
    second["evidence_snapshot_id"] = 1
    identity = tasks._stable_hash(tasks._identity_payload(second))
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute("DROP INDEX ux_report_generation_tasks_active_checkpoint")
        conn.execute("UPDATE report_generation_tasks SET evidence_snapshot_id=1, identity_hash=? WHERE id=?", (identity, second["id"]))
    with pytest.raises(HTTPException) as caught:
        tasks.find_active_report_generation_task(project_id=task_state["project_id"], evidence_snapshot_id=1)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_ACTIVE_RECOVERY_AMBIGUOUS"
    assert _rows(task_state["db_path"]) == (2, 2)


def test_active_recovery_validates_snapshot_binding_even_when_empty(task_state, monkeypatch):
    monkeypatch.setattr(tasks, "validate_context_snapshot_identity", lambda snapshot: _candidate(999, snapshot))
    with pytest.raises(HTTPException) as caught:
        tasks.find_active_report_generation_task(project_id=task_state["project_id"], evidence_snapshot_id=1)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"
    assert _rows(task_state["db_path"]) == (0, 0)


def test_active_recovery_rejects_corrupt_attempt_without_mutation(task_state):
    original = _create(task_state)
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute("UPDATE report_generation_task_attempts SET state='running' WHERE attempt_id=?", (original["current_attempt_id"],))
    with pytest.raises(HTTPException) as caught:
        tasks.find_active_report_generation_task(project_id=task_state["project_id"])
    assert _code(caught.value) == "REPORT_GENERATION_TASK_STORED_INVALID"


def test_active_recovery_includes_regeneration_chain(task_state):
    original = _create(task_state)
    original["task_type"] = tasks.REGENERATE_TASK_TYPE
    identity = tasks._stable_hash(tasks._identity_payload(original))
    with sqlite3.connect(task_state["db_path"]) as conn:
        conn.execute("UPDATE report_generation_tasks SET task_type=?, identity_hash=? WHERE id=?", (tasks.REGENERATE_TASK_TYPE, identity, original["id"]))
    recovered = tasks.find_active_report_generation_task(project_id=task_state["project_id"], evidence_snapshot_id=1)
    assert recovered["task_type"] == tasks.REGENERATE_TASK_TYPE
    assert recovered["current_attempt_id"] == original["current_attempt_id"]
    with pytest.raises(HTTPException) as caught:
        _create(task_state, local_task_id="new", create_key="new")
    assert _code(caught.value) == "REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT"


@pytest.mark.parametrize("field", ["project_id", "evidence_snapshot_id"])
@pytest.mark.parametrize("bad", [0, -1, True, "1", 2**63])
def test_active_recovery_rejects_invalid_identity_before_read(task_state, monkeypatch, field, bad):
    monkeypatch.setattr(tasks, "get_connection", lambda: pytest.fail("invalid input reached DB"))
    monkeypatch.setattr(tasks, "validate_context_snapshot_identity", lambda _: pytest.fail("invalid input reached snapshot"))
    kwargs = {"project_id": task_state["project_id"], "evidence_snapshot_id": 1, field: bad}
    with pytest.raises(HTTPException) as caught:
        tasks.find_active_report_generation_task(**kwargs)
    assert _code(caught.value) == "REPORT_GENERATION_TASK_INPUT_INVALID"


def test_active_recovery_never_returns_another_projects_task(task_state):
    _create(task_state)
    assert tasks.find_active_report_generation_task(project_id=task_state["project_id"] + 1) is None
    assert _rows(task_state["db_path"]) == (1, 1)
