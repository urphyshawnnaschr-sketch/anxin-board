"""项目档案业务：人工候选、版本更新、完整性确认门与历史 API。

安全与事务约束：
- 候选允许不完整保存；确认时每个模块必须有必要组成项和有效路径；
- 三个写接口都在独立连接上先执行 BEGIN IMMEDIATE，再读取任何业务状态；
- 版本检查优先于 no-op；条件更新影响 0 行时整体回滚并区分错误；
- 错误消息与日志不回显 SQLite 原始异常、Python 异常类型、SQL、绝对路径或完整 content。
"""

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from app.db import get_connection
from app.project_profile_v2 import ProjectProfileV2Content, profile_planned_modules

SCHEMA_VERSION = "project_profile_manual_v1"

_PROFILE_COLUMNS = (
    "id, project_id, version_no, source_prd_id, status, content_json, content_hash, "
    "edit_version, created_at, updated_at, confirmed_by, confirmed_at"
)

# ---------- 内容 Schema（F 区冻结） ----------

RefItem = Annotated[str, Field(max_length=200)]
ReqItem = Annotated[str, Field(max_length=500)]
PatItem = Annotated[str, Field(max_length=500)]
AliasItem = Annotated[str, Field(max_length=100)]


def _strip(value):
    if isinstance(value, str):
        return value.strip()
    return value


def _strip_list(value):
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


class ProfilePath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["frontend", "backend", "data", "test", "other"]
    pattern: str = ""
    required: bool = True
    note: str = Field(default="", max_length=500)

    @field_validator("pattern", "note", mode="before")
    @classmethod
    def _normalize(cls, value):
        return _strip(value)


class ProfileModule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    prd_refs: list[RefItem] = Field(default_factory=list, max_length=100)
    requirements: list[ReqItem] = Field(default_factory=list, max_length=100)
    paths: list[ProfilePath] = Field(default_factory=list, max_length=100)
    exclusions: list[PatItem] = Field(default_factory=list, max_length=100)

    @field_validator("client_id", "name", "description", mode="before")
    @classmethod
    def _normalize(cls, value):
        return _strip(value)

    @field_validator("prd_refs", "requirements", "exclusions", mode="before")
    @classmethod
    def _normalize_list(cls, value):
        return _strip_list(value)


class ProfileGlossary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(default="", max_length=100)
    definition: str = Field(default="", max_length=500)
    aliases: list[AliasItem] = Field(default_factory=list, max_length=50)

    @field_validator("term", "definition", mode="before")
    @classmethod
    def _normalize(cls, value):
        return _strip(value)

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalize_list(cls, value):
        return _strip_list(value)


class ProjectProfileContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION]
    project_summary: str = Field(default="", max_length=2000)
    modules: list[ProfileModule] = Field(default_factory=list, max_length=100)
    domain_glossary: list[ProfileGlossary] = Field(default_factory=list, max_length=200)
    exclude_patterns: list[PatItem] = Field(default_factory=list, max_length=100)
    notes: str = Field(default="", max_length=2000)

    @field_validator("project_summary", "notes", mode="before")
    @classmethod
    def _normalize(cls, value):
        return _strip(value)

    @field_validator("exclude_patterns", mode="before")
    @classmethod
    def _normalize_list(cls, value):
        return _strip_list(value)


ProfileContent = ProjectProfileContent | ProjectProfileV2Content

def parse_profile_content(raw):
    model = ProjectProfileV2Content if isinstance(raw, dict) and raw.get("schema_version") == "project_profile_v2" else ProjectProfileContent
    return model.model_validate(raw)


class ProfileUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edit_version: StrictInt
    content: ProfileContent


class ProfileConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edit_version: StrictInt
    confirmed_by: str = "local"


# ---------- 错误构造（H 区冻结） ----------


def _bad_input(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": "INVALID_PROJECT_PROFILE_INPUT", "message": message},
    )


def _profile_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "PROJECT_PROFILE_NOT_FOUND", "message": "项目档案版本不存在或已被删除"},
    )


def _project_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "PROJECT_NOT_FOUND", "message": "项目不存在或已被删除"},
    )


def _prd_not_confirmed() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "PRD_PARSE_NOT_CONFIRMED", "message": "项目还没有已确认的 PRD，请先确认 PRD 后再创建候选"},
    )


def _invalid_state() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "PROJECT_PROFILE_INVALID_STATE", "message": "项目档案当前状态不允许该操作"},
    )


def _version_conflict(current: dict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "PROJECT_PROFILE_VERSION_CONFLICT",
            "message": "项目档案候选已被其他操作更新，请重新加载最新内容",
            "current": current,
        },
    )


def _source_prd_stale() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "PROJECT_PROFILE_SOURCE_PRD_STALE",
            "message": "候选引用的 PRD 已失效，请重新创建候选",
        },
    )


def _incomplete(missing: list[dict]) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "PROJECT_PROFILE_INCOMPLETE",
            "message": "项目档案信息不完整，请补充完整后再继续",
            "missing": missing,
        },
    )


def _read_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PROJECT_PROFILE_READ_FAILED", "message": "读取项目档案数据失败"},
    )


def _save_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PROJECT_PROFILE_SAVE_FAILED", "message": "保存失败，原数据未改变"},
    )


# ---------- 路径模式安全规则（F.3） ----------


def _path_pattern_error(pattern: str) -> str | None:
    pattern = pattern.strip()
    if not pattern:
        return "路径模式不能为空"
    if len(pattern) > 500:
        return "路径模式长度不能超过 500"
    if pattern.startswith("/") or pattern.startswith("//") or pattern.startswith("~"):
        return "路径模式不能以 /、// 或 ~ 开头"
    if re.match(r"^[A-Za-z]:", pattern) or ":" in pattern:
        return "路径模式不能包含盘符、反斜杠或冒号"
    if "\\" in pattern:
        return "路径模式不能包含反斜杠"
    if any(seg in (".", "..") for seg in pattern.split("/")):
        return "路径模式不能包含 . 或 .. 路径段"
    if any(unicodedata.category(ch) == "Cc" for ch in pattern):
        return "路径模式不能包含 NUL 或控制字符"
    return None


def _validate_path_pattern(pattern: str) -> None:
    error = _path_pattern_error(pattern)
    if error:
        raise _bad_input(error)


def _validate_content(payload: ProfileContent) -> None:
    if isinstance(payload, ProjectProfileV2Content):
        for item in [*payload.implementation_mappings, *payload.unplanned_code_features]:
            for path in item.paths:
                _validate_path_pattern(path.pattern)
    seen = set()
    for module in profile_planned_modules(payload):
        module = ProfileModule.model_validate(module)
        if module.client_id in seen:
            raise _bad_input(f"重复的 client_id：{module.client_id}")
        seen.add(module.client_id)
        for path in module.paths:
            _validate_path_pattern(path.pattern)


# ---------- canonical JSON 与内容指纹（G 区） ----------


def _canonicalize(payload: ProfileContent) -> tuple[str, str]:
    data = payload.model_dump()
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, content_hash


# ---------- 完整性确认门（I 区） ----------


def _completeness_missing(content: dict) -> list[dict]:
    missing: list[dict] = []
    is_v2 = content.get("schema_version") == "project_profile_v2"
    modules = profile_planned_modules(content) or []
    if not modules:
        missing.append({"scope": "profile", "field": "modules", "message": "至少需要一个功能模块"})
    for module in modules:
        client_id = module.get("client_id", "")
        name = module.get("name", "")
        if not name:
            missing.append(
                {
                    "scope": "module",
                    "module_client_id": client_id,
                    "module_name": name,
                    "field": "name",
                    "message": "模块名称不能为空",
                }
            )
        requirements = module.get("requirements") or []
        if not any(item for item in requirements if item):
            missing.append(
                {
                    "scope": "module",
                    "module_client_id": client_id,
                    "module_name": name,
                    "field": "requirements",
                    "message": "至少需要一项必要组成项",
                }
            )
        if is_v2 and not module.get("prd_refs"):
            missing.append({"scope": "module", "module_client_id": client_id, "field": "prd_refs", "message": "计划功能需要 PRD 引用"})
        paths = module.get("paths") or []
        valid_paths = [
            p for p in paths if isinstance(p, dict) and _path_pattern_error(p.get("pattern", "")) is None
        ]
        if not valid_paths and not is_v2:
            missing.append(
                {
                    "scope": "module",
                    "module_client_id": client_id,
                    "module_name": name,
                    "field": "paths",
                    "message": "至少需要一条有效路径",
                }
            )
    return missing


# ---------- 序列化 ----------


def _profile_dict(row, include_content: bool = True) -> dict:
    data = dict(row)
    if include_content:
        data["content"] = json.loads(data.pop("content_json"))
    else:
        data.pop("content_json", None)
    return data


router = APIRouter()


def _require_project_in_tx(conn, project_id: int) -> None:
    if conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise _project_not_found()


def _read_active_prd_id(conn, project_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM prd_versions
        WHERE project_id = ? AND status = 'parse_confirmed'
        ORDER BY version_no DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


@router.post("/api/projects/{project_id}/profile-candidates", status_code=201)
def create_candidate(project_id: int, payload: ProfileContent) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    _validate_content(payload)
    canonical, content_hash = _canonicalize(payload)
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _require_project_in_tx(conn, project_id)
            prd_id = _read_active_prd_id(conn, project_id)
            if prd_id is None:
                raise _prd_not_confirmed()
            max_row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            version_no = int(max_row[0]) + 1
            conn.execute(
                "UPDATE project_profiles SET status = 'superseded' WHERE project_id = ? AND status = 'candidate'",
                (project_id,),
            )
            cursor = conn.execute(
                """
                INSERT INTO project_profiles (
                    project_id, version_no, source_prd_id, status, content_json, content_hash,
                    edit_version, created_at, updated_at
                ) VALUES (?, ?, ?, 'candidate', ?, ?, 1, ?, ?)
                """,
                (project_id, version_no, prd_id, canonical, content_hash, created_at, created_at),
            )
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            conn.commit()
    except sqlite3.Error as exc:
        raise _save_failed() from exc
    return _profile_dict(row)


def _generated_baseline_profile_ids(conn, project_id):
    tables = {row['name'] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('brownfield_baseline_tasks','project_state_baseline_candidates')"
    )}
    result = set()
    # Promotion writes this provenance atomically with its candidate. Protect the
    # result even if the later task-completion transaction has not happened.
    if 'project_state_baseline_candidates' in tables:
        result.update(row['profile_id'] for row in conn.execute(
            'SELECT profile_id FROM project_state_baseline_candidates WHERE project_id=?', (project_id,)
        ))
    if 'brownfield_baseline_tasks' in tables:
        result.update(row['profile_id'] for row in conn.execute(
            "SELECT profile_id FROM brownfield_baseline_tasks WHERE project_id=? AND status='succeeded' AND profile_id IS NOT NULL",
            (project_id,),
        ))
    return result


@router.get("/api/profile-candidates/{profile_id}")
def get_profile(profile_id: int) -> dict:
    if profile_id <= 0:
        raise _profile_not_found()
    try:
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            generated = row is not None and profile_id in _generated_baseline_profile_ids(conn, row['project_id'])
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    if row is None:
        raise _profile_not_found()
    return dict(_profile_dict(row), generated_baseline_result=generated)


@router.get("/api/projects/{project_id}/profiles")
def list_profiles(project_id: int) -> list[dict]:
    if project_id <= 0:
        raise _project_not_found()
    try:
        with get_connection() as conn:
            if conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise _project_not_found()
            rows = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE project_id = ? ORDER BY version_no DESC",
                (project_id,),
            ).fetchall()
            generated_ids = _generated_baseline_profile_ids(conn, project_id)
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    result = []
    for row in rows:
        data = _profile_dict(row, include_content=False)
        data["active"] = data["status"] == "confirmed"
        data['generated_baseline_result'] = data['id'] in generated_ids
        result.append(data)
    return result


@router.put("/api/profile-candidates/{profile_id}")
def update_candidate(profile_id: int, payload: ProfileUpdatePayload) -> dict:
    if profile_id <= 0:
        raise _profile_not_found()
    edit_version = payload.edit_version
    if edit_version <= 0:
        raise _bad_input("edit_version 必须大于 0")
    _validate_content(payload.content)
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                raise _profile_not_found()
            current = dict(row)
            if current["status"] != "candidate":
                raise _invalid_state()
            if current["edit_version"] != edit_version:
                raise _version_conflict(_profile_dict(current))
            content_to_save = payload.content
            if isinstance(content_to_save, ProjectProfileV2Content):
                previous = json.loads(current['content_json'])
                previous_modules = {m['client_id']: m for m in previous.get('planned_modules', [])}
                changed = {m.client_id for m in content_to_save.planned_modules if previous_modules.get(m.client_id) != m.model_dump()}
                # Assessment belongs to the precise plan semantics, not merely its ID.
                content_to_save = content_to_save.model_copy(update={'implementation_mappings': [m for m in content_to_save.implementation_mappings if m.planned_module_id not in changed]})
            canonical, content_hash = _canonicalize(content_to_save)
            if current["content_hash"] == content_hash:
                conn.commit()
                return {"profile": _profile_dict(current), "changed": False}
            if profile_id in _generated_baseline_profile_ids(conn, current['project_id']):
                raise HTTPException(status_code=409, detail={
                    'code': 'PROJECT_PROFILE_BASELINE_RESULT_READ_ONLY',
                    'message': '分析结果保留原文，不能直接修改。请核对后确认；调整功能计划请返回项目准备。',
                })
            updated_at = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                UPDATE project_profiles
                SET content_json = ?, content_hash = ?, updated_at = ?, edit_version = edit_version + 1
                WHERE id = ? AND status = 'candidate' AND edit_version = ?
                """,
                (canonical, content_hash, updated_at, profile_id, edit_version),
            )
            if cursor.rowcount != 1:
                fresh = conn.execute(
                    f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
                ).fetchone()
                if fresh is None:
                    raise _profile_not_found()
                fresh_obj = dict(fresh)
                if fresh_obj["status"] != "candidate":
                    raise _invalid_state()
                raise _version_conflict(_profile_dict(fresh_obj))
            updated = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            conn.commit()
            return {"profile": _profile_dict(updated), "changed": True}
    except sqlite3.Error as exc:
        raise _save_failed() from exc


@router.post("/api/profile-candidates/{profile_id}/confirm")
def confirm_candidate(profile_id: int, payload: ProfileConfirmPayload) -> dict:
    if profile_id <= 0:
        raise _profile_not_found()
    edit_version = payload.edit_version
    if edit_version <= 0:
        raise _bad_input("edit_version 必须大于 0")
    confirmed_by = (payload.confirmed_by or "").strip()
    if not 1 <= len(confirmed_by) <= 100 or any(unicodedata.category(ch) == "Cc" for ch in confirmed_by):
        raise _bad_input("确认人信息不合法")
    confirmed_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                raise _profile_not_found()
            current = dict(row)
            if current["status"] != "candidate":
                raise _invalid_state()
            if current["edit_version"] != edit_version:
                raise _version_conflict(_profile_dict(current))
            prd_id = _read_active_prd_id(conn, current["project_id"])
            if prd_id is None or prd_id != current["source_prd_id"]:
                raise _source_prd_stale()
            try:
                stored_content = json.loads(current["content_json"])
            except json.JSONDecodeError as exc:
                raise _read_failed() from exc
            missing = _completeness_missing(stored_content)
            if missing:
                raise _incomplete(missing)
            conn.execute(
                "UPDATE project_profiles SET status = 'superseded' WHERE project_id = ? AND status = 'confirmed'",
                (current["project_id"],),
            )
            cursor = conn.execute(
                """
                UPDATE project_profiles
                SET status = 'confirmed', confirmed_by = ?, confirmed_at = ?,
                    edit_version = edit_version + 1
                WHERE id = ? AND status = 'candidate' AND edit_version = ?
                """,
                (confirmed_by, confirmed_at, profile_id, edit_version),
            )
            if cursor.rowcount != 1:
                fresh = conn.execute(
                    f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
                ).fetchone()
                if fresh is None:
                    raise _profile_not_found()
                fresh_obj = dict(fresh)
                if fresh_obj["status"] != "candidate":
                    raise _invalid_state()
                raise _version_conflict(_profile_dict(fresh_obj))
            updated = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            conn.commit()
    except sqlite3.Error as exc:
        raise _save_failed() from exc
    return _profile_dict(updated)


# ---------- 内部 confirmed Profile authority readback（FR-11） ----------


class ProjectProfileAuthorityError(RuntimeError):
    code = "PROJECT_PROFILE_AUTHORITY_INVALID"


def _authority_fail() -> ProjectProfileAuthorityError:
    return ProjectProfileAuthorityError(ProjectProfileAuthorityError.code)


def _reclose_authority_row(row, *, allowed_statuses: set[str]) -> dict:
    if row is None:
        raise _authority_fail()
    data = dict(row)
    if data.get("status") not in allowed_statuses:
        raise _authority_fail()
    try:
        raw_content = json.loads(data["content_json"])
        content = parse_profile_content(raw_content)
    except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise _authority_fail() from exc

    seen: set[str] = set()
    for raw_module in profile_planned_modules(content):
        module = ProfileModule.model_validate(raw_module)
        if module.client_id in seen:
            raise _authority_fail()
        seen.add(module.client_id)
        for path in module.paths:
            if _path_pattern_error(path.pattern) is not None:
                raise _authority_fail()
    if _completeness_missing(content.model_dump()):
        raise _authority_fail()

    _, content_hash = _canonicalize(content)
    if data.get("content_hash") != content_hash:
        raise _authority_fail()

    for key in ("id", "project_id", "version_no", "source_prd_id"):
        if type(data.get(key)) is not int or data[key] <= 0 or data[key] > 2**63 - 1:
            raise _authority_fail()

    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "version_no": data["version_no"],
        "source_prd_id": data["source_prd_id"],
        "status": data["status"],
        "content_hash": data["content_hash"],
        "content": content.model_dump(),
    }


def read_current_confirmed_project_profile(project_id: int, *, conn=None) -> dict:
    """Read and re-close the one current confirmed Profile for generation.

    When ``conn`` is supplied, the caller controls the transaction so report
    persistence can revalidate Profile identity in the same SQLite write snapshot.
    """

    if type(project_id) is not int or project_id <= 0 or project_id > 2**63 - 1:
        raise _authority_fail()

    def _read(active_conn):
        rows = active_conn.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE project_id = ? AND status = 'confirmed' ORDER BY version_no DESC",
            (project_id,),
        ).fetchall()
        if len(rows) != 1:
            raise _authority_fail()
        authority = _reclose_authority_row(rows[0], allowed_statuses={"confirmed"})
        active_prd_id = _read_active_prd_id(active_conn, project_id)
        if active_prd_id is None or active_prd_id != authority["source_prd_id"]:
            raise _authority_fail()
        return authority

    if conn is not None:
        return _read(conn)
    try:
        with get_connection() as owned_conn:
            owned_conn.execute("BEGIN")
            authority = _read(owned_conn)
            owned_conn.commit()
            return authority
    except sqlite3.Error as exc:
        raise _authority_fail() from exc


def read_bound_project_profile_for_report(profile_id: int, *, conn=None) -> dict:
    """Re-close a report-bound confirmed/superseded Profile for history validation."""

    if type(profile_id) is not int or profile_id <= 0 or profile_id > 2**63 - 1:
        raise _authority_fail()

    def _read(active_conn):
        row = active_conn.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        authority = _reclose_authority_row(row, allowed_statuses={"confirmed", "superseded"})
        return authority

    if conn is not None:
        return _read(conn)
    try:
        with get_connection() as owned_conn:
            owned_conn.execute("BEGIN")
            authority = _read(owned_conn)
            owned_conn.commit()
            return authority
    except sqlite3.Error as exc:
        raise _authority_fail() from exc
