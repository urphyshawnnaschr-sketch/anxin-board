"""Freeze approved cumulative progress before context selection/redaction/budgeting.

Legacy EvidenceSnapshots have no companion and retain their original context bytes.
New snapshots record an explicit prior state (including no prior state) once. Reads
never consult the latest progress and never write or invoke a provider.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

from app.project_progress import read_latest


class ProgressContextError(ValueError):
    code = "PROJECT_PROGRESS_CONTEXT_INVALID"


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def progress_scope(snapshot):
    return {"project_id": snapshot["project_id"],
            "git_url": snapshot["project_repository_url"],
            "branch": snapshot["branch"],
            "analysis_lineage_id": snapshot["analysis_lineage_id"],
            "profile_id": snapshot["profile_id"],
            "profile_content_hash": snapshot["profile_content_hash"]}


def _identity(snapshot):
    return {"evidence_snapshot_id": snapshot["id"],
            "evidence_snapshot_hash": snapshot["snapshot_hash"],
            "scope": progress_scope(snapshot)}


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_progress_contexts'").fetchone() is not None


def read_progress_context(conn: sqlite3.Connection, *, snapshot: dict):
    if not _exists(conn):
        return None
    row = conn.execute("SELECT content_json, content_hash FROM evidence_progress_contexts WHERE evidence_snapshot_id=?", (snapshot["id"],)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["content_json"])
        if (_hash(value) != row["content_hash"] or
                value.get("schema_version") != "project_progress_context_v1" or
                any(value.get(key) != val for key, val in _identity(snapshot).items())):
            raise ProgressContextError("累计进度与本次分析范围不一致，请重新读取范围。")
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ProgressContextError("已保存的累计进度无法读取。") from exc
    return {**value, "context_hash": row["content_hash"]}


def freeze_progress_context(conn: sqlite3.Connection, *, snapshot: dict, commits: list[str]):
    if not conn.in_transaction:
        raise ProgressContextError("累计进度必须与分析范围一同保存。")
    existing = read_progress_context(conn, snapshot=snapshot)
    if existing is not None:
        return existing
    previous = read_latest(conn, scope=progress_scope(snapshot))
    if previous is not None and previous["report"]["head_sha"] not in {snapshot["from_commit"], *commits}:
        raise ProgressContextError("本次代码范围没有接续上次已确认进度，请重新选择连续范围。")
    conn.execute("""CREATE TABLE IF NOT EXISTS evidence_progress_contexts (
        evidence_snapshot_id INTEGER PRIMARY KEY,
        content_json TEXT NOT NULL, content_hash TEXT NOT NULL)""")
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS evidence_progress_contexts_no_{operation.lower()}
            BEFORE {operation} ON evidence_progress_contexts BEGIN
            SELECT RAISE(ABORT, 'frozen progress context is immutable'); END""")
    value = {"schema_version": "project_progress_context_v1", **_identity(snapshot),
             "previous": deepcopy(previous)}
    content_hash = _hash(value)
    conn.execute("INSERT INTO evidence_progress_contexts VALUES (?, ?, ?)",
                 (snapshot["id"], _json(value), content_hash))
    return {**value, "context_hash": content_hash}
