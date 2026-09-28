"""Sanitized read-only diagnostics for Project Profile generation attempts."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from app.db import get_connection


router = APIRouter()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.get("/api/projects/{project_id}/profile-generation-attempt/latest")
def latest_profile_generation_attempt(project_id: int) -> dict[str, object]:
    """Return lifecycle/error facts only; never expose idempotency identity or credentials."""

    if type(project_id) is not int or project_id <= 0:
        raise _error(400, "PROJECT_ID_INVALID", "project_id 必须是正整数。")
    try:
        with get_connection() as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='profile_generation_attempts'"
            ).fetchone()
            if exists is None:
                return {"attempt": None}
            row = conn.execute(
                """
                SELECT status, error_status, error_code, error_message, created_at, finished_at
                FROM profile_generation_attempts
                WHERE project_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise _error(
            500,
            "PROFILE_GENERATION_ATTEMPT_DIAGNOSTIC_FAILED",
            "无法读取候选生成诊断状态。",
        ) from exc

    if row is None:
        return {"attempt": None}
    return {
        "attempt": {
            "status": row["status"],
            "error_status": row["error_status"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
        }
    }
