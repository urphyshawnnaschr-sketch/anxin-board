"""Durable at-most-once stage ledger. Callers supply redacted, validated results.

Every mutation owns and commits its transaction; dispatch may start only after a
claim returns claimed_now=True. A surviving claim must be reconciled as unknown.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import uuid


class BrownfieldStoreError(ValueError):
    pass


_TERMINAL = {"succeeded", "failed_pre_send", "failed_after_send", "unknown"}
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_:-]{0,159}$")


def _fail(code):
    raise BrownfieldStoreError("BROWNFIELD_STORE_" + code)


def _json(value):
    def validate(item):
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                _fail("JSON_INVALID")
            for child in item.values():
                validate(child)
        elif type(item) is list:
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            _fail("JSON_INVALID")
    try:
        validate(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("JSON_INVALID")


def _text(value):
    if type(value) is not str or not value.strip():
        _fail("INPUT_INVALID")


def _error_code(value):
    if value is not None and (type(value) is not str or not _SAFE_CODE.fullmatch(value)):
        _fail("ERROR_CODE_INVALID")


@contextmanager
def _write(conn):
    if conn.in_transaction:
        _fail("TRANSACTION_ACTIVE")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except BrownfieldStoreError:
        conn.rollback()
        raise
    except sqlite3.Error:
        conn.rollback()
        _fail("DATABASE_CONFLICT")
    except Exception:
        conn.rollback()
        raise


def ensure_schema(conn):
    try:
        _ensure_schema(conn)
    except sqlite3.Error:
        conn.rollback()
        _fail("DATABASE_CONFLICT")


def _ensure_schema(conn):
    if conn.in_transaction:
        _fail("TRANSACTION_ACTIVE")
    conn.executescript("""
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS brownfield_baseline_tasks (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL UNIQUE,
        project_id INTEGER NOT NULL,
        authorization_nonce TEXT NOT NULL UNIQUE,
        identity_json TEXT NOT NULL,
        identity_hash TEXT NOT NULL,
        max_calls INTEGER NOT NULL CHECK(max_calls > 0),
        status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed_pre_send','failed_after_send','unknown')),
        profile_id INTEGER,
        error_code TEXT,
        created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS brownfield_identity_live_unique
      ON brownfield_baseline_tasks(project_id,identity_hash)
      WHERE status IN ('queued','running','unknown','succeeded');
    CREATE TABLE IF NOT EXISTS brownfield_stage_claims (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES brownfield_baseline_tasks(task_id),
        stage_key TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        wire_bytes INTEGER NOT NULL CHECK(wire_bytes > 0),
        created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE(task_id,stage_key)
    );
    CREATE TABLE IF NOT EXISTS brownfield_stage_results (
        task_id TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('succeeded','failed_pre_send','failed_after_send','unknown')),
        result_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(task_id,stage_key),
        FOREIGN KEY(task_id,stage_key) REFERENCES brownfield_stage_claims(task_id,stage_key)
    );
    CREATE TRIGGER IF NOT EXISTS brownfield_tasks_no_delete BEFORE DELETE ON brownfield_baseline_tasks
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_TASK'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_tasks_identity_immutable BEFORE UPDATE ON brownfield_baseline_tasks
    WHEN NEW.task_id IS NOT OLD.task_id OR NEW.project_id IS NOT OLD.project_id
      OR NEW.authorization_nonce IS NOT OLD.authorization_nonce OR NEW.identity_json IS NOT OLD.identity_json
      OR NEW.identity_hash IS NOT OLD.identity_hash OR NEW.max_calls IS NOT OLD.max_calls
      OR NEW.created_at IS NOT OLD.created_at OR NEW.sequence IS NOT OLD.sequence
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_TASK'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_tasks_terminal_immutable BEFORE UPDATE ON brownfield_baseline_tasks
    WHEN OLD.status IN ('succeeded','failed_pre_send','failed_after_send','unknown')
    BEGIN SELECT RAISE(ABORT,'TERMINAL_TASK'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_claims_no_update BEFORE UPDATE ON brownfield_stage_claims
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_CLAIM'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_claims_no_delete BEFORE DELETE ON brownfield_stage_claims
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_CLAIM'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_results_no_update BEFORE UPDATE ON brownfield_stage_results
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_RESULT'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_results_no_delete BEFORE DELETE ON brownfield_stage_results
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_RESULT'); END;
    CREATE TABLE IF NOT EXISTS brownfield_baseline_outputs (
        task_id TEXT PRIMARY KEY REFERENCES brownfield_baseline_tasks(task_id),
        output_json TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );
    CREATE TRIGGER IF NOT EXISTS brownfield_outputs_no_update BEFORE UPDATE ON brownfield_baseline_outputs
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_OUTPUT'); END;
    CREATE TRIGGER IF NOT EXISTS brownfield_outputs_no_delete BEFORE DELETE ON brownfield_baseline_outputs
    BEGIN SELECT RAISE(ABORT,'IMMUTABLE_OUTPUT'); END;
    COMMIT;
    """)


def _task(row):
    if row is None:
        return None
    value = dict(row)
    value["identity"] = json.loads(value.pop("identity_json"))
    return value


def get_task(conn, task_id):
    return _task(conn.execute("SELECT * FROM brownfield_baseline_tasks WHERE task_id=?", (task_id,)).fetchone())


def latest_task(conn, project_id):
    return _task(conn.execute("SELECT * FROM brownfield_baseline_tasks WHERE project_id=? ORDER BY sequence DESC LIMIT 1", (project_id,)).fetchone())


def create_task(conn, *, project_id: int, authorization_nonce: str, identity: dict, max_calls: int) -> dict:
    if type(project_id) is not int or project_id <= 0 or type(identity) is not dict or type(max_calls) is not int or max_calls <= 0:
        _fail("INPUT_INVALID")
    _text(authorization_nonce)
    encoded = _json(identity)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with _write(conn):
        existing = conn.execute("SELECT * FROM brownfield_baseline_tasks WHERE authorization_nonce=?", (authorization_nonce,)).fetchone()
        if existing:
            if (existing["project_id"], existing["identity_hash"], existing["identity_json"], existing["max_calls"]) != (project_id, digest, encoded, max_calls):
                _fail("AUTHORIZATION_MISMATCH")
            return _task(existing)
        if conn.execute("SELECT 1 FROM brownfield_baseline_tasks WHERE project_id=? AND identity_hash=? AND status IN ('queued','running','unknown','succeeded')", (project_id, digest)).fetchone():
            _fail("IDENTITY_ALREADY_AUTHORIZED")
        task_id = "baseline-" + uuid.uuid4().hex
        conn.execute("INSERT INTO brownfield_baseline_tasks(task_id,project_id,authorization_nonce,identity_json,identity_hash,max_calls,status) VALUES(?,?,?,?,?,?,'queued')", (task_id, project_id, authorization_nonce, encoded, digest, max_calls))
        return get_task(conn, task_id)


def get_stage(conn, task_id, stage_key):
    row = conn.execute("""SELECT c.*, COALESCE(r.status,'claimed') AS status,
        r.result_json,r.error_code,r.created_at AS finished_at FROM brownfield_stage_claims c
        LEFT JOIN brownfield_stage_results r ON c.task_id=r.task_id AND c.stage_key=r.stage_key
        WHERE c.task_id=? AND c.stage_key=?""", (task_id, stage_key)).fetchone()
    if row is None:
        return None
    value = dict(row)
    encoded = value.pop("result_json")
    value["result"] = json.loads(encoded) if encoded is not None else None
    return value


def list_stages(conn, task_id):
    return [get_stage(conn, task_id, row["stage_key"]) for row in conn.execute("SELECT stage_key FROM brownfield_stage_claims WHERE task_id=? ORDER BY sequence", (task_id,)).fetchall()]


def claim_stage(conn, *, task_id, stage_key, input_hash, wire_bytes) -> dict:
    for value in (task_id, stage_key, input_hash):
        _text(value)
    if type(wire_bytes) is not int or wire_bytes <= 0:
        _fail("INPUT_INVALID")
    with _write(conn):
        task = get_task(conn, task_id)
        if task is None:
            _fail("TASK_NOT_FOUND")
        stage = get_stage(conn, task_id, stage_key)
        if stage:
            if (stage["input_hash"], stage["wire_bytes"]) != (input_hash, wire_bytes):
                _fail("STAGE_INPUT_MISMATCH")
            if stage["status"] == "succeeded":
                return dict(stage, claimed_now=False)
            _fail("STAGE_ALREADY_CLAIMED")
        if task["status"] not in {"queued", "running"}:
            _fail("TASK_TERMINAL")
        if conn.execute("SELECT 1 FROM brownfield_stage_results WHERE task_id=? AND status!='succeeded'", (task_id,)).fetchone():
            _fail("PRIOR_STAGE_FAILED")
        if conn.execute("""SELECT 1 FROM brownfield_stage_claims c
            LEFT JOIN brownfield_stage_results r ON c.task_id=r.task_id AND c.stage_key=r.stage_key
            WHERE c.task_id=? AND r.task_id IS NULL""", (task_id,)).fetchone():
            _fail("STAGE_IN_FLIGHT")
        count = conn.execute("SELECT count(*) FROM brownfield_stage_claims WHERE task_id=?", (task_id,)).fetchone()[0]
        if count >= task["max_calls"]:
            _fail("CALL_BUDGET_EXHAUSTED")
        conn.execute("INSERT INTO brownfield_stage_claims(task_id,stage_key,input_hash,wire_bytes) VALUES(?,?,?,?)", (task_id, stage_key, input_hash, wire_bytes))
        conn.execute("UPDATE brownfield_baseline_tasks SET status='running' WHERE task_id=?", (task_id,))
        return dict(get_stage(conn, task_id, stage_key), claimed_now=True)


def finish_stage(conn, *, task_id, stage_key, status, result, error_code=None) -> dict:
    if type(status) is not str or status not in _TERMINAL or (result is not None and type(result) not in (dict, list)):
        _fail("RESULT_INVALID")
    if status == "succeeded" and (result is None or error_code is not None):
        _fail("RESULT_INVALID")
    _error_code(error_code)
    encoded = _json(result) if result is not None else None
    with _write(conn):
        stage = get_stage(conn, task_id, stage_key)
        if stage is None:
            _fail("STAGE_NOT_CLAIMED")
        if stage["status"] != "claimed":
            if (stage["status"], _json(stage["result"]), stage["error_code"]) == (status, _json(result), error_code):
                return stage
            _fail("STAGE_TERMINAL")
        if get_task(conn, task_id)["status"] not in {"queued", "running"}:
            _fail("TASK_TERMINAL")
        conn.execute("INSERT INTO brownfield_stage_results(task_id,stage_key,status,result_json,error_code) VALUES(?,?,?,?,?)", (task_id, stage_key, status, encoded, error_code))
        return get_stage(conn, task_id, stage_key)


def finish_task(conn, task_id, *, status, profile_id=None, error_code=None):
    if type(status) is not str or status not in _TERMINAL or (profile_id is not None and (type(profile_id) is not int or profile_id <= 0)):
        _fail("STATUS_INVALID")
    _error_code(error_code)
    if status == "succeeded" and error_code is not None:
        _fail("STATUS_INVALID")
    with _write(conn):
        task = get_task(conn, task_id)
        if task is None:
            _fail("TASK_NOT_FOUND")
        if task["status"] in _TERMINAL:
            if (task["status"], task["profile_id"], task["error_code"]) == (status, profile_id, error_code):
                return task
            _fail("TASK_TERMINAL")
        if status == "succeeded" and any(stage["status"] != "succeeded" for stage in list_stages(conn, task_id)):
            _fail("STAGES_INCOMPLETE")
        conn.execute("UPDATE brownfield_baseline_tasks SET status=?,profile_id=?,error_code=? WHERE task_id=?", (status, profile_id, error_code, task_id))
        return get_task(conn, task_id)


def task_status(conn, task_id) -> dict:
    task = get_task(conn, task_id)
    if task is None:
        _fail("TASK_NOT_FOUND")
    stages = list_stages(conn, task_id)
    return dict(task, claimed_calls=len(stages), remaining_calls=task["max_calls"] - len(stages), stages=stages)


def get_output(conn, task_id) -> dict | None:
    row = conn.execute("SELECT * FROM brownfield_baseline_outputs WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["output"] = json.loads(value.pop("output_json"))
    return value


def save_output(conn, task_id, *, output: dict) -> dict:
    """Persist caller-validated local aggregation; never a provider body or source."""
    if type(output) is not dict:
        _fail("OUTPUT_INVALID")
    encoded = _json(output)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with _write(conn):
        task = get_task(conn, task_id)
        if task is None:
            _fail("TASK_NOT_FOUND")
        existing = get_output(conn, task_id)
        if existing is not None:
            if existing["output_hash"] != digest or _json(existing["output"]) != encoded:
                _fail("OUTPUT_CONFLICT")
            return existing
        if task["status"] not in {"queued", "running"}:
            _fail("TASK_TERMINAL")
        conn.execute("INSERT INTO brownfield_baseline_outputs(task_id,output_json,output_hash) VALUES(?,?,?)", (task_id, encoded, digest))
        return get_output(conn, task_id)


def set_local_error(conn, task_id, error_code):
    _error_code(error_code)
    with _write(conn):
        task = get_task(conn, task_id)
        if task is None:
            _fail("TASK_NOT_FOUND")
        if task["status"] not in {"queued", "running"}:
            _fail("TASK_TERMINAL")
        conn.execute("UPDATE brownfield_baseline_tasks SET error_code=? WHERE task_id=?", (error_code, task_id))
        return get_task(conn, task_id)


def reuse_successful_stage(conn, *, task_id, stage_key, input_hash, wire_bytes) -> dict | None:
    """Copy an exact successful stage from a known-failed prior authorization.

    This is local cache reuse only. The caller must separately authorize the new
    task; unknown tasks never supply cached results. Claim and result commit together.
    """
    for value in (task_id, stage_key, input_hash):
        _text(value)
    if type(wire_bytes) is not int or wire_bytes <= 0:
        _fail("INPUT_INVALID")
    with _write(conn):
        task = get_task(conn, task_id)
        if task is None:
            _fail("TASK_NOT_FOUND")
        if task["status"] not in {"queued", "running"}:
            _fail("TASK_TERMINAL")
        existing = get_stage(conn, task_id, stage_key)
        if existing is not None:
            if (existing["input_hash"], existing["wire_bytes"]) != (input_hash, wire_bytes):
                _fail("STAGE_INPUT_MISMATCH")
            if existing["status"] == "succeeded":
                return dict(existing, claimed_now=False)
            _fail("STAGE_ALREADY_CLAIMED")
        if conn.execute("SELECT 1 FROM brownfield_stage_results WHERE task_id=? AND status!='succeeded'", (task_id,)).fetchone():
            _fail("PRIOR_STAGE_FAILED")
        if conn.execute("""SELECT 1 FROM brownfield_stage_claims c
            LEFT JOIN brownfield_stage_results r ON c.task_id=r.task_id AND c.stage_key=r.stage_key
            WHERE c.task_id=? AND r.task_id IS NULL""", (task_id,)).fetchone():
            _fail("STAGE_IN_FLIGHT")
        prior = conn.execute("""SELECT t.task_id,r.result_json FROM brownfield_baseline_tasks t
            JOIN brownfield_stage_claims c ON c.task_id=t.task_id
            JOIN brownfield_stage_results r ON r.task_id=c.task_id AND r.stage_key=c.stage_key
            WHERE t.project_id=? AND t.identity_hash=? AND t.identity_json=? AND t.sequence<?
              AND t.status IN ('failed_pre_send','failed_after_send')
              AND c.stage_key=? AND c.input_hash=? AND c.wire_bytes=? AND r.status='succeeded'
            ORDER BY t.sequence DESC LIMIT 1""",
            (task["project_id"], task["identity_hash"], _json(task["identity"]), task["sequence"], stage_key, input_hash, wire_bytes)).fetchone()
        if prior is None:
            return None
        count = conn.execute("SELECT count(*) FROM brownfield_stage_claims WHERE task_id=?", (task_id,)).fetchone()[0]
        if count >= task["max_calls"]:
            _fail("CALL_BUDGET_EXHAUSTED")
        try:
            result = json.loads(prior["result_json"])
        except (TypeError, ValueError, RecursionError):
            _fail("REUSE_RESULT_INVALID")
        if type(result) is not dict:
            _fail("REUSE_RESULT_INVALID")
        result.update(reused_from_task_id=prior["task_id"], reused_from_stage_key=stage_key)
        encoded = _json(result)
        conn.execute("INSERT INTO brownfield_stage_claims(task_id,stage_key,input_hash,wire_bytes) VALUES(?,?,?,?)", (task_id, stage_key, input_hash, wire_bytes))
        conn.execute("INSERT INTO brownfield_stage_results(task_id,stage_key,status,result_json,error_code) VALUES(?,?,'succeeded',?,NULL)", (task_id, stage_key, encoded))
        conn.execute("UPDATE brownfield_baseline_tasks SET status='running' WHERE task_id=?", (task_id,))
        return dict(get_stage(conn, task_id, stage_key), claimed_now=False)
