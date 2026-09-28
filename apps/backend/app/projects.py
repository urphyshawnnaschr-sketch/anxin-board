"""项目草稿业务：校验、错误构造、CRUD 与 API 路由。"""

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictInt

from app.db import get_connection

_PROJECT_COLUMNS = "id, name, git_url, branch, status, created_at, updated_at, version"


class ProjectCreate(BaseModel):
    name: str


class ProjectUpdate(BaseModel):
    name: str
    git_url: str | None = None
    branch: str
    version: StrictInt


def _bad_input(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": "INVALID_PROJECT_INPUT", "message": message},
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "PROJECT_NOT_FOUND", "message": "项目不存在或已被删除"},
    )


def _read_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PROJECT_READ_FAILED", "message": "读取项目数据失败"},
    )


def _save_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PROJECT_SAVE_FAILED", "message": "保存失败，原数据未改变"},
    )


def _validate_name(raw: str) -> str:
    name = raw.strip()
    if not 1 <= len(name) <= 100:
        raise _bad_input("项目名称长度必须为 1 到 100 个字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise _bad_input("项目名称不能包含控制字符")
    return name


_GIT_URL_BAD_CHAR_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_SCP_STYLE_URL_RE = re.compile(r"^[^@\s:]+@[^/\s:]+:.+$")


def _normalize_git_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        return None
    if len(value) > 500:
        raise _bad_input("Git 仓库地址不能超过 500 个字符")
    if _GIT_URL_BAD_CHAR_RE.search(value):
        raise _bad_input("Git 仓库地址不能包含空白或控制字符")
    if value.startswith("https://") or value.startswith("ssh://"):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
        except ValueError as exc:
            raise _bad_input("Git 仓库地址格式不正确") from exc
        if not host or not parsed.path or parsed.path == "/":
            raise _bad_input("Git 仓库地址格式不正确")
        if "?" in value or "#" in value:
            raise _bad_input("Git 仓库地址不能包含查询参数或片段")
        if parsed.scheme == "https" and (parsed.username is not None or parsed.password is not None):
            raise _bad_input("HTTPS Git 仓库地址不能包含用户名、密码或 Token")
        if parsed.scheme == "ssh" and parsed.password is not None:
            raise _bad_input("SSH Git 仓库地址不能包含密码")
    elif not _SCP_STYLE_URL_RE.fullmatch(value):
        raise _bad_input("Git 仓库地址格式不正确")
    return value


def _validate_branch(raw: str) -> str:
    branch = raw.strip()
    if not 1 <= len(branch) <= 255:
        raise _bad_input("活动分支长度必须为 1 到 255 个字符")
    if branch.startswith("-"):
        raise _bad_input("活动分支不能以连字符开头")
    if re.search(r"[\s\x00-\x1f\x7f]", branch):
        raise _bad_input("活动分支不能包含空格或控制字符")
    if branch.startswith("/") or branch.endswith("/"):
        raise _bad_input("活动分支不能以 / 开头或结尾")
    if branch.endswith(".lock"):
        raise _bad_input("活动分支不能以 .lock 结尾")
    for bad in ("..", "//", "@{"):
        if bad in branch:
            raise _bad_input("活动分支包含非法字符组合")
    if re.search(r"[\\~^:?*\[\]]", branch):
        raise _bad_input("活动分支包含不允许的字符")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise _bad_input("活动分支只能包含字母、数字、点、下划线、斜杠和连字符")
    return branch


def _conflict(current: dict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "VERSION_CONFLICT",
            "message": "项目已在其他操作中更新，请重新加载最新内容",
            "current": current,
        },
    )


router = APIRouter()


@router.get("/api/projects")
def list_projects() -> list[dict]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY id DESC"
            ).fetchall()
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    return [dict(row) for row in rows]


@router.get("/api/projects/{project_id}")
def get_project(project_id: int) -> dict:
    if project_id <= 0:
        raise _not_found()
    try:
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    if row is None:
        raise _not_found()
    return dict(row)


@router.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate) -> dict:
    name = _validate_name(payload.name)
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO projects (name, status, created_at, git_url, branch, updated_at, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, "draft", created_at, None, "main", created_at, 1),
            )
            row = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise _save_failed() from exc
    return dict(row)


@router.put("/api/projects/{project_id}")
def update_project(project_id: int, payload: ProjectUpdate) -> dict:
    if project_id <= 0:
        raise _not_found()
    name = _validate_name(payload.name)
    git_url = _normalize_git_url(payload.git_url)
    branch = _validate_branch(payload.branch)
    version = payload.version
    if version < 1:
        raise _bad_input("版本号必须大于等于 1")

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if current is None:
                raise _not_found()
            current_obj = dict(current)
            if current_obj["version"] != version:
                raise _conflict(current_obj)
            no_op = (
                current_obj["name"] == name
                and current_obj["git_url"] == git_url
                and current_obj["branch"] == branch
            )
            if no_op:
                conn.commit()
                return {"project": current_obj, "changed": False}
            updated_at = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                UPDATE projects
                SET name = ?, git_url = ?, branch = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (name, git_url, branch, updated_at, project_id, version),
            )
            if cursor.rowcount != 1:
                fresh = conn.execute(
                    f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if fresh is None:
                    raise _not_found()
                raise _conflict(dict(fresh))
            if current_obj["git_url"] != git_url or current_obj["branch"] != branch:
                conn.execute(
                    "DELETE FROM project_git_connections WHERE project_id = ?",
                    (project_id,),
                )
                conn.execute(
                    """
                    UPDATE analysis_lineages
                    SET status = 'closed', break_reason = 'git_config_changed', closed_at = ?
                    WHERE project_id = ? AND status = 'active'
                    """,
                    (updated_at, project_id),
                )
            updated = conn.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            conn.commit()
            return {"project": dict(updated), "changed": True}
    except sqlite3.Error as exc:
        raise _save_failed() from exc
