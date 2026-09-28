"""Explicit Human progress corrections, immutable and bound to final approval."""
import json
import sqlite3
import re

from app.ai_contracts import STAGES


class ProgressCorrectionError(ValueError):
    code = "REPORT_PROGRESS_CORRECTION_INVALID"


def normalize(values):
    if values is None:
        return []
    if type(values) is not list or len(values) > 100:
        raise ProgressCorrectionError("功能状态更正格式无效。")
    result = []
    seen = set()
    for value in values:
        if type(value) is not dict or set(value) != {"module_id", "stage", "reason", "evidence_ids"}:
            raise ProgressCorrectionError("功能状态更正字段无效。")
        module_id, reason, refs = value["module_id"], value["reason"], value["evidence_ids"]
        if (type(module_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", module_id)
                or module_id in seen or value["stage"] not in STAGES
                or type(reason) is not str or not reason.strip() or len(reason) > 2000
                or any(ord(ch) < 32 for ch in reason)
                or type(refs) is not list or not 1 <= len(refs) <= 100
                or any(type(ref) is not str or not 1 <= len(ref) <= 500 for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ProgressCorrectionError("更正功能状态时，请选择真实依据并填写原因。")
        seen.add(module_id)
        result.append({**value, "reason": reason.strip(), "evidence_ids": sorted(refs)})
    return sorted(result, key=lambda value: value["module_id"])


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_progress_corrections'").fetchone() is not None


def assert_replay(conn, *, approval, values):
    expected = normalize(values)
    row = None
    if _exists(conn):
        row = conn.execute("SELECT approval_hash,corrections_json FROM report_progress_corrections WHERE approval_snapshot_id=?", (approval["approval_snapshot_id"],)).fetchone()
    if row is None:
        if expected:
            raise ProgressCorrectionError("该报告已经确认，不能追加或替换功能状态更正。")
    elif row[0] != approval["approval_snapshot_hash"] or row[1] != _json(expected):
        raise ProgressCorrectionError("这次更正与已确认内容不同；原报告保持不变。")


def persist(conn, *, approval, values):
    if not conn.in_transaction:
        raise ProgressCorrectionError("功能状态更正必须与报告确认一同保存。")
    values = normalize(values)
    conn.execute("""CREATE TABLE IF NOT EXISTS report_progress_corrections (
        approval_snapshot_id INTEGER PRIMARY KEY, approval_hash TEXT NOT NULL,
        corrections_json TEXT NOT NULL)""")
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS report_progress_corrections_no_{operation.lower()}
        BEFORE {operation} ON report_progress_corrections BEGIN
        SELECT RAISE(ABORT, 'approved progress corrections are immutable'); END""")
    conn.execute("INSERT INTO report_progress_corrections VALUES (?,?,?)",
                 (approval["approval_snapshot_id"], approval["approval_snapshot_hash"], _json(values)))


def as_updates(values):
    return {value["module_id"]: {"stage": value["stage"], "regression_reason": value["reason"],
                                 "evidence_ids": value["evidence_ids"]} for value in normalize(values)}
