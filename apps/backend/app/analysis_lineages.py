"""Git 初始基线、连续链路 API 与数据库事务；不直接拼装任意 Git 子进程命令。

前置条件检查、数据库事务、状态映射在这里完成。受控 Git 命令由 git_client 提供，
纯范围计算与容量判断由 git_analysis 提供，同项目工作区互斥由 git_workspace_locks 提供。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.db import get_connection
from app.git_analysis import RangeCandidate, candidate_status, compute_range_candidate
from app.git_client import (
    GIT_ERROR_SUMMARIES,
    GitClient,
    GitClientError,
    open_workspace_access,
    resolve_workspace_paths,
    validate_workspace_paths,
)
from app.git_workspace_locks import GitOperationInProgress, project_workspace_lock
from app.projects import _normalize_git_url, _validate_branch


router = APIRouter()
_git_client_factory = GitClient

_LINEAGE_COLUMNS = (
    "id, project_id, sequence_no, branch, baseline_commit, status, break_reason, "
    "source_git_checked_at, created_at, closed_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status: int, code: str, message: str, current: dict | None = None) -> HTTPException:
    detail: dict = {"code": code, "message": message}
    if current is not None:
        detail["current"] = current
    return HTTPException(status_code=status, detail=detail)


def _project_not_found() -> HTTPException:
    return _error(404, "PROJECT_NOT_FOUND", "项目不存在或已被删除")


def _read_failed() -> HTTPException:
    return _error(500, "LINEAGE_READ_FAILED", "读取分析链路数据失败")


def _save_failed() -> HTTPException:
    return _error(500, "LINEAGE_SAVE_FAILED", "保存分析链路失败，原数据未改变")


def _lineage_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    return {
        "id": item["id"],
        "project_id": item["project_id"],
        "sequence_no": item["sequence_no"],
        "branch": item["branch"],
        "baseline_commit": item["baseline_commit"],
        "status": item["status"],
        "break_reason": item["break_reason"],
        "source_git_checked_at": item["source_git_checked_at"],
        "created_at": item["created_at"],
        "closed_at": item["closed_at"],
    }


def _read_active_lineage(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_LINEAGE_COLUMNS} FROM analysis_lineages "
        "WHERE project_id = ? AND status = 'active' ORDER BY sequence_no DESC LIMIT 1",
        (project_id,),
    ).fetchone()


@router.get("/api/projects/{project_id}/analysis-lineage")
def get_analysis_lineage(project_id: int) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    try:
        with get_connection() as conn:
            if conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise _project_not_found()
            row = _read_active_lineage(conn, project_id)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    lineage = _lineage_dict(row)
    if lineage is None:
        return {"status": "no_lineage", "lineage": None}
    return {"status": "active", "lineage": lineage}


@router.post("/api/projects/{project_id}/analysis-lineages", status_code=201)
def create_analysis_lineage(project_id: int) -> dict:
    if project_id <= 0:
        raise _project_not_found()

    try:
        with project_workspace_lock(project_id):
            return _create_lineage_locked(project_id)
    except GitOperationInProgress:
        raise _error(
            409,
            "GIT_OPERATION_IN_PROGRESS",
            "该项目另有 Git 工作区操作正在进行，请稍后再建立基线。",
        )


def _create_lineage_locked(project_id: int) -> dict:
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute(
                "SELECT git_url, branch FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise _project_not_found()
            url = _normalize_git_url(project["git_url"])
            branch = _validate_branch(project["branch"])
            if not url:
                raise _error(409, "GIT_CONFIG_REQUIRED", "请先保存 Git 仓库地址和活动分支。")

            connection = conn.execute(
                "SELECT * FROM project_git_connections WHERE project_id = ?", (project_id,)
            ).fetchone()
            if connection is None or connection["status"] != "connected":
                raise _error(409, "GIT_NOT_CONNECTED", "请先测试 Git 连接并确认连接成功。")
            if (
                connection["checked_url_hash"] != _url_hash(url)
                or connection["checked_branch"] != branch
            ):
                raise _error(
                    409,
                    "GIT_CONFIG_CHANGED",
                    "Git 地址或分支已修改，请重新测试连接后再建立基线。",
                )
            remote_head = connection["remote_head"]
            local_head = connection["local_head"]
            if not _is_valid_head(remote_head) or not _is_valid_head(local_head):
                raise _error(409, "GIT_HEAD_INVALID", "连接记录缺少有效的远端或本地 HEAD。")

            existing = _read_active_lineage(conn, project_id)
            if existing is not None:
                conn.commit()
                return {"lineage": _lineage_dict(existing), "created": False}

            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM analysis_lineages WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            created_at = _now()
            cursor = conn.execute(
                """
                INSERT INTO analysis_lineages (
                    project_id, sequence_no, branch, baseline_commit, status, break_reason,
                    source_git_checked_at, created_at, closed_at
                ) VALUES (?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                """,
                (
                    project_id,
                    int(sequence) + 1,
                    branch,
                    remote_head,
                    connection["last_checked_at"],
                    created_at,
                ),
            )
            row = conn.execute(
                f"SELECT {_LINEAGE_COLUMNS} FROM analysis_lineages WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            conn.commit()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _save_failed() from exc
    return {"lineage": _lineage_dict(row), "created": True}


def _url_hash(url: str) -> str:
    import hashlib

    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _is_valid_head(value: str | None) -> bool:
    return bool(value) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


@router.post("/api/projects/{project_id}/git/range-candidate")
def refresh_range_candidate(project_id: int) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    try:
        with project_workspace_lock(project_id):
            return _refresh_candidate_locked(project_id)
    except GitOperationInProgress:
        return {"status": "git_operation_in_progress", "candidate": None}


def _refresh_candidate_locked(project_id: int) -> dict:
    try:
        project, connection, lineage = _load_refresh_context(project_id)
    except HTTPException:
        raise
    if lineage is None:
        return {"status": "no_lineage", "candidate": None}
    if lineage["branch"] != project["branch"]:
        return {"status": "branch_changed", "candidate": None}

    access = None
    try:
        client = _git_client_factory()
        attempt_id = uuid.uuid4().hex
        paths = resolve_workspace_paths(project_id, attempt_id, create=False)
        validate_workspace_paths(paths)
        if not paths.repo.exists():
            return {"status": "git_failed", "candidate": None}
        access = open_workspace_access(paths, project["git_url"])
        client.inspect_workspace(access)
        client.fetch_branch(access, project["branch"])
        candidate = compute_range_candidate(
            client,
            access,
            branch=project["branch"],
            baseline_commit=lineage["baseline_commit"],
        )
        return {
            "status": candidate_status(candidate),
            "candidate": _candidate_dict(candidate),
        }
    except GitClientError as exc:
        return {
            "status": "git_failed",
            "candidate": None,
            "error_code": exc.code,
            "error_summary": GIT_ERROR_SUMMARIES.get(exc.code, exc.summary),
        }
    finally:
        if access is not None:
            access.close()


def _load_refresh_context(project_id: int) -> tuple[dict, dict | None, dict | None]:
    try:
        with get_connection() as conn:
            project = conn.execute(
                "SELECT git_url, branch FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise _project_not_found()
            url = _normalize_git_url(project["git_url"])
            branch = _validate_branch(project["branch"])
            if not url:
                raise _error(409, "GIT_CONFIG_REQUIRED", "请先保存 Git 仓库地址和活动分支。")
            connection_row = conn.execute(
                "SELECT * FROM project_git_connections WHERE project_id = ?", (project_id,)
            ).fetchone()
            connection = dict(connection_row) if connection_row is not None else None
            lineage = _lineage_dict(_read_active_lineage(conn, project_id))
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    return {"git_url": url, "branch": branch}, connection, lineage


def _candidate_dict(candidate: RangeCandidate) -> dict:
    return {
        "baseline_commit": candidate.baseline_commit,
        "remote_head": candidate.remote_head,
        "commits": candidate.commits,
        "commit_count": candidate.commit_count,
        "changed_file_count": candidate.changed_file_count,
        "added_lines": candidate.added_lines,
        "deleted_lines": candidate.deleted_lines,
        "diff_bytes": candidate.diff_bytes,
        "continuity": candidate.continuity,
        "capacity": candidate.capacity,
        "batch_required": candidate.batch_required,
        "statistics_complete": candidate.statistics_complete,
        "capacity_reasons": list(candidate.capacity_reasons),
        "capacity_limits": candidate.capacity_limits,
    }