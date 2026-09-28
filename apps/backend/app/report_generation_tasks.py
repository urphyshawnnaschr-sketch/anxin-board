"""Report Generation Task Core V1：durable logical task + first attempt，纯本地、Fail Closed。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import uuid

from fastapi import HTTPException

from app.context_snapshot_identity import validate_context_snapshot_identity
from app.db import get_connection


SCHEMA_VERSION = "report_generation_task_core_v1"
TASK_TYPE = "daily_report_generate"
REGENERATE_TASK_TYPE = "daily_report_regenerate"
TASK_TYPES = frozenset({TASK_TYPE, REGENERATE_TASK_TYPE})
STATES = frozenset({"queued", "running", "succeeded", "failed", "unknown", "voided"})
ACTIVE_STATES = frozenset({"queued", "running", "unknown"})
TERMINAL_STATES = STATES - ACTIVE_STATES
LEGAL_TRANSITIONS = frozenset(
    {
        ("queued", "running"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "unknown"),
        ("unknown", "voided"),
    }
)
_SQLITE_MAX_INTEGER = 2**63 - 1
_MAX_OPAQUE_LENGTH = 128
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_COLUMNS = (
    "id, schema_version, project_id, local_task_id, evidence_snapshot_id, task_type, "
    "create_key, current_attempt_id, state, identity_hash, created_at, updated_at"
)
_ATTEMPT_COLUMNS = (
    "id, schema_version, attempt_id, task_id, sequence_no, state, created_at, "
    "started_at, finished_at, updated_at"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "报告生成任务输入无效。") -> HTTPException:
    return _error(400, "REPORT_GENERATION_TASK_INPUT_INVALID", message)


def _stored_invalid(message: str = "报告生成任务持久化身份无法完成自校验。") -> HTTPException:
    return _error(409, "REPORT_GENERATION_TASK_STORED_INVALID", message)


def _create_conflict() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_TASK_CREATE_CONFLICT",
        "同一项目内 create_key 已绑定不同的不可变报告生成任务身份。",
    )


def _logical_conflict(*, identity_drift: bool) -> HTTPException:
    if identity_drift:
        return _error(
            409,
            "REPORT_GENERATION_TASK_LOGICAL_IDENTITY_CONFLICT",
            "同一 logical task identity 已绑定不同的不可变报告生成任务身份。",
        )
    return _error(
        409,
        "REPORT_GENERATION_TASK_LOGICAL_TASK_CONFLICT",
        "同一 logical task identity 已由另一个 create_key 创建。",
    )


def _checkpoint_active_conflict() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT",
        "同一项目与 EvidenceSnapshot/checkpoint 已由另一条 active generation chain 占用。",
    )


def _not_found() -> HTTPException:
    return _error(404, "REPORT_GENERATION_TASK_NOT_FOUND", "指定的报告生成任务不存在。")


def _transition_invalid() -> HTTPException:
    return _error(
        409,
        "REPORT_GENERATION_TASK_TRANSITION_INVALID",
        "报告生成任务状态迁移不在 V1 允许关系内。",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _is_opaque(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= _MAX_OPAQUE_LENGTH


def _require_sqlite_id(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1 or value > _SQLITE_MAX_INTEGER:
        raise _input_invalid(
            f"{field_name} 必须是 1 到 2**63-1 范围内的 SQLite 正整数。"
        )
    return value


def _require_opaque(value: object, field_name: str) -> str:
    if not _is_opaque(value):
        raise _input_invalid(f"{field_name} 必须是长度不超过 128 的非空字符串。")
    return value


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stable_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": row["schema_version"],
        "local_task_id": row["local_task_id"],
        "project_id": row["project_id"],
        "evidence_snapshot_id": row["evidence_snapshot_id"],
        "task_type": row["task_type"],
    }


def _validate_candidate_binding(
    *, project_id: int, evidence_snapshot_id: int
) -> dict[str, object]:
    candidate = validate_context_snapshot_identity(evidence_snapshot_id)
    try:
        candidate_project_id = candidate["project_id"]
        candidate_snapshot_id = candidate["snapshot_id"]
        snapshot_hash = candidate["snapshot_hash"]
    except (KeyError, TypeError) as exc:
        raise _stored_invalid("EvidenceSnapshot formal seam 未返回完整冻结身份。") from exc
    if (
        candidate_project_id != project_id
        or candidate_snapshot_id != evidence_snapshot_id
        or not _is_hash(snapshot_hash)
    ):
        raise _stored_invalid("EvidenceSnapshot parent binding 无法闭合。")
    return candidate


def _read_task_by_key(
    conn: sqlite3.Connection, *, project_id: int, create_key: str
) -> sqlite3.Row | None:
    rows = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM report_generation_tasks "
        "WHERE project_id = ? AND create_key = ?",
        (project_id, create_key),
    ).fetchmany(2)
    if len(rows) > 1:
        raise _stored_invalid("同一 project + create_key 存在多条持久化任务。")
    return rows[0] if rows else None


def _read_task_by_logical(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    local_task_id: str,
    task_type: str = TASK_TYPE,
) -> sqlite3.Row | None:
    rows = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM report_generation_tasks "
        "WHERE project_id = ? AND local_task_id = ? AND task_type = ?",
        (project_id, local_task_id, task_type),
    ).fetchmany(2)
    if len(rows) > 1:
        raise _stored_invalid("同一 logical task identity 存在多条持久化任务。")
    return rows[0] if rows else None


def _read_tasks_by_checkpoint(
    conn: sqlite3.Connection, *, project_id: int, evidence_snapshot_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM report_generation_tasks "
        "WHERE project_id = ? AND evidence_snapshot_id = ? "
        "ORDER BY id",
        (project_id, evidence_snapshot_id),
    ).fetchall()


def _read_task_by_id(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM report_generation_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _read_attempt_by_id(
    conn: sqlite3.Connection, attempt_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM report_generation_task_attempts "
        "WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()


def _close_task_local(
    conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, object]
) -> dict[str, object]:
    task = dict(row)
    if task.get("schema_version") != SCHEMA_VERSION:
        raise _stored_invalid()
    for field in ("id", "project_id", "evidence_snapshot_id"):
        value = task.get(field)
        if type(value) is not int or value < 1 or value > _SQLITE_MAX_INTEGER:
            raise _stored_invalid()
    if (
        not _is_opaque(task.get("local_task_id"))
        or not _is_opaque(task.get("create_key"))
        or not _is_opaque(task.get("current_attempt_id"))
        or task.get("task_type") not in TASK_TYPES
        or task.get("state") not in STATES
        or not _is_hash(task.get("identity_hash"))
        or not _is_opaque(task.get("created_at"))
        or not _is_opaque(task.get("updated_at"))
    ):
        raise _stored_invalid()
    try:
        recomputed = _stable_hash(_identity_payload(task))
    except (KeyError, TypeError, ValueError) as exc:
        raise _stored_invalid() from exc
    if recomputed != task["identity_hash"]:
        raise _stored_invalid("报告生成任务 immutable identity hash 无法闭合。")

    attempt_row = _read_attempt_by_id(conn, task["current_attempt_id"])
    if attempt_row is None:
        raise _stored_invalid("current_attempt_id 指向的 attempt 不存在。")
    attempt = dict(attempt_row)
    if (
        attempt.get("schema_version") != SCHEMA_VERSION
        or type(attempt.get("id")) is not int
        or attempt["id"] <= 0
        or attempt.get("task_id") != task["id"]
        or attempt.get("attempt_id") != task["current_attempt_id"]
        or attempt.get("sequence_no") != 1
        or attempt.get("state") not in STATES
        or attempt.get("state") != task["state"]
        or not _is_opaque(attempt.get("created_at"))
        or not _is_opaque(attempt.get("updated_at"))
    ):
        raise _stored_invalid("Task/current-attempt coherence 无法闭合。")
    count = conn.execute(
        "SELECT COUNT(*) FROM report_generation_task_attempts WHERE task_id = ?",
        (task["id"],),
    ).fetchone()[0]
    if count != 1:
        raise _stored_invalid("V1 logical task 必须且只能保留 first attempt。")

    from app.report_reanalysis_cancellation import cancellation_for_task
    cancellation = cancellation_for_task(conn, task)
    if cancellation is not None and task["state"] != "voided":
        raise _stored_invalid("已撤销的重分析任务不能恢复为可运行状态。")

    if task["state"] == "queued":
        if attempt.get("started_at") is not None or attempt.get("finished_at") is not None:
            raise _stored_invalid()
    elif task["state"] == "running":
        if not _is_opaque(attempt.get("started_at")) or attempt.get("finished_at") is not None:
            raise _stored_invalid()
    elif task["state"] in TERMINAL_STATES:
        if cancellation is not None:
            if (attempt.get("started_at") is not None
                    or attempt.get("finished_at") != cancellation["cancelled_at"]
                    or task.get("updated_at") != cancellation["cancelled_at"]):
                raise _stored_invalid("未发送撤销的 attempt 时间与审计不一致。")
        elif (
            not _is_opaque(attempt.get("started_at"))
            or not _is_opaque(attempt.get("finished_at"))
        ):
            raise _stored_invalid()
    elif task["state"] == "unknown":
        if not _is_opaque(attempt.get("started_at")) or attempt.get("finished_at") is not None:
            raise _stored_invalid()

    result = dict(task)
    result["current_attempt"] = dict(attempt)
    if cancellation is not None:
        result["reanalysis_cancelled"] = True
        result["reanalysis_cancellation"] = cancellation
    return result


def _close_task(
    conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, object]
) -> dict[str, object]:
    closed = _close_task_local(conn, row)
    _validate_candidate_binding(
        project_id=closed["project_id"],
        evidence_snapshot_id=closed["evidence_snapshot_id"],
    )
    return closed


def _classify_existing(
    conn: sqlite3.Connection,
    *,
    expected_identity_hash: str,
    project_id: int,
    local_task_id: str,
    create_key: str,
    task_type: str = TASK_TYPE,
) -> dict[str, object] | None:
    by_key = _read_task_by_key(conn, project_id=project_id, create_key=create_key)
    if by_key is not None:
        closed = _close_task_local(conn, by_key)
        if closed["identity_hash"] == expected_identity_hash:
            return closed
        raise _create_conflict()

    by_logical = _read_task_by_logical(
        conn,
        project_id=project_id,
        local_task_id=local_task_id,
        task_type=task_type,
    )
    if by_logical is not None:
        closed = _close_task_local(conn, by_logical)
        raise _logical_conflict(
            identity_drift=closed["identity_hash"] != expected_identity_hash
        )
    return None


def _classify_checkpoint_occupancy(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    evidence_snapshot_id: int,
    exact_replay: dict[str, object] | None,
) -> dict[str, object] | None:
    closed_rows = [
        _close_task_local(conn, row)
        for row in _read_tasks_by_checkpoint(
            conn,
            project_id=project_id,
            evidence_snapshot_id=evidence_snapshot_id,
        )
    ]
    active_rows = [row for row in closed_rows if row["state"] in ACTIVE_STATES]
    if len(active_rows) > 1:
        raise _stored_invalid(
            "同一 project + EvidenceSnapshot 存在多条 active generation chain。"
        )
    if exact_replay is not None:
        return exact_replay
    if active_rows:
        raise _checkpoint_active_conflict()
    return None


def _classify_admission(
    conn: sqlite3.Connection,
    *,
    expected_identity_hash: str,
    project_id: int,
    local_task_id: str,
    evidence_snapshot_id: int,
    create_key: str,
    task_type: str = TASK_TYPE,
) -> dict[str, object] | None:
    exact_replay = _classify_existing(
        conn,
        expected_identity_hash=expected_identity_hash,
        project_id=project_id,
        local_task_id=local_task_id,
        create_key=create_key,
        task_type=task_type,
    )
    return _classify_checkpoint_occupancy(
        conn,
        project_id=project_id,
        evidence_snapshot_id=evidence_snapshot_id,
        exact_replay=exact_replay,
    )


def _insert_new_task(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, object],
    attempt_id: str,
    now: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO report_generation_tasks (
            schema_version, project_id, local_task_id, evidence_snapshot_id,
            task_type, create_key, current_attempt_id, state, identity_hash,
            created_at, updated_at
        ) VALUES (
            :schema_version, :project_id, :local_task_id, :evidence_snapshot_id,
            :task_type, :create_key, NULL, 'queued', :identity_hash,
            :created_at, :updated_at
        )
        """,
        dict(row),
    )
    task_id = cursor.lastrowid
    if type(task_id) is not int or task_id <= 0:
        raise _stored_invalid("logical task INSERT 未返回有效 row id。")
    conn.execute(
        """
        INSERT INTO report_generation_task_attempts (
            schema_version, attempt_id, task_id, sequence_no, state,
            created_at, started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, 1, 'queued', ?, NULL, NULL, ?)
        """,
        (SCHEMA_VERSION, attempt_id, task_id, now, now),
    )
    updated = conn.execute(
        """
        UPDATE report_generation_tasks
        SET current_attempt_id = ?
        WHERE id = ? AND current_attempt_id IS NULL
        """,
        (attempt_id, task_id),
    )
    if updated.rowcount != 1:
        raise _stored_invalid("logical task 与 first attempt 未能原子闭合。")
    return task_id


def _create_task_in_connection(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    local_task_id: str,
    evidence_snapshot_id: int,
    create_key: str,
    task_type: str,
) -> dict[str, object]:
    if task_type not in TASK_TYPES:
        raise _input_invalid("task_type 不在冻结的报告生成任务类型内。")
    _validate_candidate_binding(
        project_id=project_id, evidence_snapshot_id=evidence_snapshot_id
    )
    now = _now()
    row: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "local_task_id": local_task_id,
        "evidence_snapshot_id": evidence_snapshot_id,
        "task_type": task_type,
        "create_key": create_key,
        "created_at": now,
        "updated_at": now,
    }
    row["identity_hash"] = _stable_hash(_identity_payload(row))
    attempt_id = uuid.uuid4().hex

    existing = _classify_admission(
        conn,
        expected_identity_hash=row["identity_hash"],
        project_id=project_id,
        local_task_id=local_task_id,
        evidence_snapshot_id=evidence_snapshot_id,
        create_key=create_key,
        task_type=task_type,
    )
    if existing is not None:
        _validate_candidate_binding(
            project_id=existing["project_id"],
            evidence_snapshot_id=existing["evidence_snapshot_id"],
        )
        return existing
    try:
        task_id = _insert_new_task(conn, row=row, attempt_id=attempt_id, now=now)
    except sqlite3.IntegrityError as exc:
        winner = _classify_admission(
            conn,
            expected_identity_hash=row["identity_hash"],
            project_id=project_id,
            local_task_id=local_task_id,
            evidence_snapshot_id=evidence_snapshot_id,
            create_key=create_key,
            task_type=task_type,
        )
        if winner is None:
            raise _stored_invalid(
                "并发 UNIQUE collision 后无法闭合 winner row。"
            ) from exc
        _validate_candidate_binding(
            project_id=winner["project_id"],
            evidence_snapshot_id=winner["evidence_snapshot_id"],
        )
        return winner

    inserted = _read_task_by_id(conn, task_id)
    if inserted is None:
        raise _stored_invalid("logical task 写入后无法回读。")
    return _close_task(conn, inserted)


def create_report_generation_task(
    *,
    project_id: int,
    local_task_id: str,
    evidence_snapshot_id: int,
    create_key: str,
) -> dict[str, object]:
    """原子创建 durable daily_report_generate logical task + first attempt；不会调用 provider。"""
    project_id = _require_sqlite_id(project_id, "project_id")
    evidence_snapshot_id = _require_sqlite_id(
        evidence_snapshot_id, "evidence_snapshot_id"
    )
    local_task_id = _require_opaque(local_task_id, "local_task_id")
    create_key = _require_opaque(create_key, "create_key")

    try:
        with get_connection() as conn:
            return _create_task_in_connection(
                conn,
                project_id=project_id,
                local_task_id=local_task_id,
                evidence_snapshot_id=evidence_snapshot_id,
                create_key=create_key,
                task_type=TASK_TYPE,
            )
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def create_report_regeneration_task_in_transaction(
    *,
    conn: sqlite3.Connection,
    project_id: int,
    local_task_id: str,
    evidence_snapshot_id: int,
    create_key: str,
) -> dict[str, object]:
    """在调用方既有 SQLite 事务内创建 daily_report_regenerate；不 commit、不调用 provider。"""
    if not isinstance(conn, sqlite3.Connection):
        raise _input_invalid("conn 必须是有效 SQLite connection。")
    project_id = _require_sqlite_id(project_id, "project_id")
    evidence_snapshot_id = _require_sqlite_id(
        evidence_snapshot_id, "evidence_snapshot_id"
    )
    local_task_id = _require_opaque(local_task_id, "local_task_id")
    create_key = _require_opaque(create_key, "create_key")
    return _create_task_in_connection(
        conn,
        project_id=project_id,
        local_task_id=local_task_id,
        evidence_snapshot_id=evidence_snapshot_id,
        create_key=create_key,
        task_type=REGENERATE_TASK_TYPE,
    )


def find_active_report_generation_task(
    *, project_id: int, evidence_snapshot_id: int | None = None
) -> dict[str, object] | None:
    """Recover the unique durable active chain without creating or transitioning it."""
    project_id = _require_sqlite_id(project_id, "project_id")
    if evidence_snapshot_id is not None:
        evidence_snapshot_id = _require_sqlite_id(evidence_snapshot_id, "evidence_snapshot_id")
        _validate_candidate_binding(
            project_id=project_id, evidence_snapshot_id=evidence_snapshot_id
        )
    try:
        with get_connection() as conn:
            conn.execute("BEGIN")
            if evidence_snapshot_id is None:
                rows = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM report_generation_tasks "
                    "WHERE project_id = ? ORDER BY id", (project_id,)
                ).fetchall()
            else:
                rows = _read_tasks_by_checkpoint(
                    conn, project_id=project_id, evidence_snapshot_id=evidence_snapshot_id
                )
            closed = [_close_task_local(conn, row) for row in rows]
            active = [row for row in closed if row["state"] in ACTIVE_STATES]
            if len(active) > 1:
                raise _error(
                    409, "REPORT_GENERATION_TASK_ACTIVE_RECOVERY_AMBIGUOUS",
                    "发现多条 active generation task，无法唯一恢复；请检查持久化状态。",
                )
            if not active:
                return None
            task = active[0]
            _validate_candidate_binding(
                project_id=project_id, evidence_snapshot_id=task["evidence_snapshot_id"]
            )
            return _with_batch_summary(task)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def get_report_generation_task(
    *, project_id: int, local_task_id: str
) -> dict[str, object]:
    """Read the exact durable generate/regenerate task without changing its state."""
    project_id = _require_sqlite_id(project_id, "project_id")
    local_task_id = _require_opaque(local_task_id, "local_task_id")
    try:
        with get_connection() as conn:
            row = _read_task_by_logical(
                conn,
                project_id=project_id,
                local_task_id=local_task_id,
                task_type=TASK_TYPE,
            )
            if row is None:
                row = _read_task_by_logical(conn, project_id=project_id,
                    local_task_id=local_task_id, task_type=REGENERATE_TASK_TYPE)
                if row is None:
                    raise _not_found()
                return _close_task(conn, row)
            return _with_batch_summary(_close_task(conn, row))
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def _with_batch_summary(task):
    from app.report_generation_batches import task_summary
    batch = task_summary(task)
    return {**task, "batch_plan": batch} if batch is not None else task


def get_report_regeneration_task(
    *, project_id: int, local_task_id: str
) -> dict[str, object]:
    """读取 durable daily_report_regenerate task；只读自校验，不执行 task。"""
    project_id = _require_sqlite_id(project_id, "project_id")
    local_task_id = _require_opaque(local_task_id, "local_task_id")
    try:
        with get_connection() as conn:
            row = _read_task_by_logical(
                conn,
                project_id=project_id,
                local_task_id=local_task_id,
                task_type=REGENERATE_TASK_TYPE,
            )
            if row is None:
                raise _not_found()
            return _close_task(conn, row)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def transition_report_generation_task(
    *,
    project_id: int,
    local_task_id: str,
    expected_state: str,
    new_state: str,
) -> dict[str, object]:
    """同一事务原子镜像 daily_report_generate task/current-attempt state；禁止任何未冻结 edge。"""
    project_id = _require_sqlite_id(project_id, "project_id")
    local_task_id = _require_opaque(local_task_id, "local_task_id")
    if expected_state not in STATES or new_state not in STATES:
        raise _transition_invalid()
    if (expected_state, new_state) not in LEGAL_TRANSITIONS:
        raise _transition_invalid()

    current = get_report_generation_task(
        project_id=project_id, local_task_id=local_task_id
    )
    if current["state"] != expected_state:
        raise _transition_invalid()

    now = _now()
    try:
        with get_connection() as conn:
            row = _read_task_by_logical(
                conn,
                project_id=project_id,
                local_task_id=local_task_id,
                task_type=TASK_TYPE,
            )
            if row is None:
                raise _not_found()
            closed = _close_task_local(conn, row)
            if closed["state"] != expected_state:
                raise _transition_invalid()
            attempt = closed["current_attempt"]

            started_at = attempt["started_at"]
            finished_at = attempt["finished_at"]
            if expected_state == "queued" and new_state == "running":
                started_at = now
            if new_state in TERMINAL_STATES:
                finished_at = now

            attempt_update = conn.execute(
                """
                UPDATE report_generation_task_attempts
                SET state = ?, started_at = ?, finished_at = ?, updated_at = ?
                WHERE attempt_id = ? AND task_id = ? AND state = ?
                """,
                (
                    new_state,
                    started_at,
                    finished_at,
                    now,
                    closed["current_attempt_id"],
                    closed["id"],
                    expected_state,
                ),
            )
            if attempt_update.rowcount != 1:
                raise _stored_invalid("current attempt transition 未能闭合 expected old state。")

            task_update = conn.execute(
                """
                UPDATE report_generation_tasks
                SET state = ?, updated_at = ?
                WHERE id = ? AND current_attempt_id = ? AND state = ?
                """,
                (
                    new_state,
                    now,
                    closed["id"],
                    closed["current_attempt_id"],
                    expected_state,
                ),
            )
            if task_update.rowcount != 1:
                raise _stored_invalid("logical task transition 未能闭合 expected old state。")

            updated = _read_task_by_id(conn, closed["id"])
            if updated is None:
                raise _stored_invalid("transition 后 logical task 无法回读。")
            return _close_task(conn, updated)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
