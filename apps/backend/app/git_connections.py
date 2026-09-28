"""项目 Git 连接 API、attempt 状态机与受控工作区编排。"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from app.db import get_connection
from app.git_client import (
    GIT_ERROR_SUMMARIES,
    GitClientError,
    cleanup_attempt_workspace,
    open_workspace_access,
    promote_attempt_workspace,
    reserve_attempt_workspace,
    resolve_workspace_paths,
    validate_workspace_paths,
)
from app.git_ssh_client import SshEnabledGitClient
from app.git_workspace_locks import GitOperationInProgress, project_workspace_lock
from app.projects import _normalize_git_url, _validate_branch


router = APIRouter()
_TESTING_FRESH_FOR = timedelta(minutes=15)
_git_client_factory = SshEnabledGitClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status: int, code: str, message: str, current: dict | None = None) -> HTTPException:
    detail: dict = {"code": code, "message": message}
    if current is not None:
        detail["current"] = current
    return HTTPException(status_code=status, detail=detail)


def _project_not_found() -> HTTPException:
    return _error(404, "PROJECT_NOT_FOUND", "项目不存在或已被删除")


def _public_state(row: sqlite3.Row | dict | None, branch: str | None = None) -> dict:
    if row is None:
        return {
            "status": "not_tested",
            "checked_branch": branch,
            "git_version": None,
            "remote_head": None,
            "local_head": None,
            "last_checked_at": None,
            "error_code": None,
            "error_summary": None,
        }
    item = dict(row)
    return {
        "status": item["status"],
        "checked_branch": item["checked_branch"],
        "git_version": item["git_version"],
        "remote_head": item["remote_head"],
        "local_head": item["local_head"],
        "last_checked_at": item["last_checked_at"],
        "error_code": item["error_code"],
        "error_summary": item["error_summary"],
    }


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _is_fresh_testing(row: sqlite3.Row) -> bool:
    if row["status"] != "testing" or not row["started_at"]:
        return False
    try:
        started = datetime.fromisoformat(row["started_at"])
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < _TESTING_FRESH_FOR


def _begin_attempt(project_id: int) -> tuple[str, str, str]:
    attempt_id = uuid.uuid4().hex
    started_at = _now()
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
            existing = conn.execute(
                "SELECT * FROM project_git_connections WHERE project_id = ?", (project_id,)
            ).fetchone()
            if existing is not None and _is_fresh_testing(existing):
                raise _error(
                    409,
                    "GIT_CHECK_IN_PROGRESS",
                    "该项目已有 Git 检查正在进行，请稍后查看。",
                    _public_state(existing),
                )
            conn.execute(
                """
                INSERT INTO project_git_connections (
                    project_id, status, attempt_id, checked_url_hash, checked_branch,
                    git_version, remote_head, local_head, workspace_rel_path,
                    started_at, last_checked_at, error_code, error_summary
                ) VALUES (?, 'testing', ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL)
                ON CONFLICT(project_id) DO UPDATE SET
                    status = 'testing', attempt_id = excluded.attempt_id,
                    checked_url_hash = excluded.checked_url_hash,
                    checked_branch = excluded.checked_branch, git_version = NULL,
                    remote_head = NULL, local_head = NULL, workspace_rel_path = NULL,
                    started_at = excluded.started_at, last_checked_at = NULL,
                    error_code = NULL, error_summary = NULL
                """,
                (project_id, attempt_id, _url_hash(url), branch, started_at),
            )
            conn.commit()
            return attempt_id, url, branch
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(500, "GIT_CHECK_SAVE_FAILED", "无法保存 Git 检查状态，未标记连接成功。") from exc


def _finish_attempt(
    project_id: int,
    attempt_id: str,
    *,
    status: str,
    branch: str,
    git_version: str | None = None,
    remote_head: str | None = None,
    local_head: str | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> dict:
    checked_at = _now()
    workspace = "repo" if status == "connected" else None
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE project_git_connections
                SET status = ?, attempt_id = NULL, checked_branch = ?, git_version = ?,
                    remote_head = ?, local_head = ?, workspace_rel_path = ?,
                    last_checked_at = ?, error_code = ?, error_summary = ?
                WHERE project_id = ? AND attempt_id = ? AND status = 'testing'
                """,
                (
                    status,
                    branch,
                    git_version,
                    remote_head,
                    local_head,
                    workspace,
                    checked_at,
                    error_code,
                    error_summary,
                    project_id,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError("attempt ownership changed")
            row = conn.execute(
                "SELECT * FROM project_git_connections WHERE project_id = ?", (project_id,)
            ).fetchone()
            conn.commit()
    except sqlite3.Error as exc:
        raise _error(500, "GIT_CHECK_SAVE_FAILED", "无法保存 Git 检查结果，未标记连接成功。") from exc
    return _public_state(row)


@router.get("/api/projects/{project_id}/git/status")
def get_git_status(project_id: int) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    try:
        with get_connection() as conn:
            project = conn.execute(
                "SELECT git_url, branch FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise _project_not_found()
            row = conn.execute(
                "SELECT * FROM project_git_connections WHERE project_id = ?", (project_id,)
            ).fetchone()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(500, "GIT_CHECK_READ_FAILED", "读取 Git 连接状态失败。") from exc

    if row is not None and row["status"] == "connected":
        url = _normalize_git_url(project["git_url"])
        branch = _validate_branch(project["branch"])
        if not url or row["checked_url_hash"] != _url_hash(url) or row["checked_branch"] != branch:
            return _public_state(None, branch)
    return _public_state(row, project["branch"])


@router.post("/api/projects/{project_id}/git/check")
def check_git_connection(project_id: int) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    try:
        with project_workspace_lock(project_id):
            return _check_git_connection_locked(project_id)
    except GitOperationInProgress:
        current = None
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM project_git_connections WHERE project_id = ?", (project_id,)
                ).fetchone()
                if row is not None:
                    project = conn.execute(
                        "SELECT branch FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                    current = _public_state(row, project["branch"] if project else None)
        except sqlite3.Error:
            pass
        raise _error(
            409,
            "GIT_OPERATION_IN_PROGRESS",
            "该项目另有 Git 工作区操作正在进行，请稍后再检查连接。",
            current,
        )


def _check_git_connection_locked(project_id: int) -> dict:
    attempt_id, url, branch = _begin_attempt(project_id)
    if not url.startswith("https://") and not getattr(_git_client_factory, "supports_ssh", False):
        return _finish_attempt(
            project_id,
            attempt_id,
            status="failed",
            branch=branch,
            error_code="GIT_PROTOCOL_NOT_ENABLED",
            error_summary="当前 Git 客户端未启用 SSH 传输，请检查本机 Git/OpenSSH 配置。",
        )

    paths = None
    temp_ownership = None
    workspace_access = None
    git_version = None
    try:
        client = _git_client_factory()
        git_version = client.get_version()
        client.validate_branch(branch)
        paths = resolve_workspace_paths(project_id, attempt_id, create=True)
        validate_workspace_paths(paths)
        if paths.repo.exists():
            workspace_access = open_workspace_access(paths, url)
            client.inspect_workspace(workspace_access)
            client.assert_clean(workspace_access)
            # One explicit human check gets one remote authentication opportunity.
            # The fetch itself proves repository access + branch existence and updates
            # the pinned remote-tracking ref; all remaining checks are local reads.
            client.fetch_branch(workspace_access, branch)
            remote_head = client.get_remote_head_ref(workspace_access, branch)
            client.checkout_remote_head(workspace_access, branch)
            client.assert_size_limit(workspace_access)
            local_head = client.get_local_head(workspace_access)
        else:
            temp_ownership = reserve_attempt_workspace(paths, attempt_id)
            workspace_access = open_workspace_access(paths, url, temp_ownership)
            # clone --single-branch --branch is the sole remote operation for a fresh
            # workspace. Do not follow it with fetch/ls-remote, which would reopen GCM.
            client.clone_branch(url, branch, workspace_access)
            validate_workspace_paths(paths)
            client.inspect_workspace(workspace_access)
            client.assert_clean(workspace_access)
            remote_head = client.get_remote_head_ref(workspace_access, branch)
            client.checkout_remote_head(workspace_access, branch)
            client.assert_size_limit(workspace_access)
            local_head = client.get_local_head(workspace_access)

        validate_workspace_paths(paths)
        if local_head != remote_head:
            raise GitClientError("GIT_CHECK_FAILED")
        if temp_ownership is not None:
            promote_attempt_workspace(paths, temp_ownership)
            temp_ownership = None
        return _finish_attempt(
            project_id,
            attempt_id,
            status="connected",
            branch=branch,
            git_version=git_version,
            remote_head=remote_head,
            local_head=local_head,
        )
    except HTTPException:
        raise
    except GitClientError as exc:
        code = exc.code
        summary = exc.summary
        if paths is not None and temp_ownership is not None:
            try:
                cleanup_attempt_workspace(paths, temp_ownership)
            except (GitClientError, OSError):
                code = "GIT_CHECK_FAILED"
                summary = GIT_ERROR_SUMMARIES[code]
        return _finish_attempt(
            project_id,
            attempt_id,
            status="failed",
            branch=branch,
            git_version=git_version,
            error_code=code,
            error_summary=summary,
        )
    except Exception:
        if paths is not None and temp_ownership is not None:
            try:
                cleanup_attempt_workspace(paths, temp_ownership)
            except (GitClientError, OSError):
                pass
        return _finish_attempt(
            project_id,
            attempt_id,
            status="failed",
            branch=branch,
            git_version=git_version,
            error_code="GIT_CHECK_FAILED",
            error_summary=GIT_ERROR_SUMMARIES["GIT_CHECK_FAILED"],
        )
    finally:
        if workspace_access is not None:
            workspace_access.close()
