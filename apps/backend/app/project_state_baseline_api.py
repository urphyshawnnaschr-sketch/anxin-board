"""Product API for first/full Project State Baseline reconciliation.

The lane composes the existing exact-HEAD repository index, lossless batch planner and
at-most-once provider execution. A successful model run creates an ordinary V2 candidate;
Human confirmation remains mandatory. Baseline-origin confirmation additionally binds the
exact analyzed HEAD as the next incremental Git lineage origin in the same SQLite transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Literal
import unicodedata

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import project_profile_generation as profile_generation
from app import project_profiles as project_profiles_module
from app.db import get_connection
from app.local_session_api import require_local_write_request
from app.git_client import GitClientError
from app.git_workspace_locks import GitOperationInProgress
from app.profile_reconciliation_batches import BatchError
from app.profile_reconciliation_provider import (
    aggregate_authorized_candidate,
    dispatch_authorized_batch,
    prepare_product_execution,
    record_authorization,
)
from app.project_profile_v2 import ProjectProfileV2Content
from app.project_profiles import (
    ProfileConfirmPayload,
    _PROFILE_COLUMNS,
    _canonicalize,
    _completeness_missing,
    _profile_dict,
    _read_active_prd_id,
    _require_project_in_tx,
    _validate_content,
    read_current_confirmed_project_profile,
)


router = APIRouter()
SCHEMA_VERSION = "project_state_baseline_v1"


class BaselinePreflightPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_profile_id: int = Field(gt=0)


class BaselineExecutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_profile_id: int = Field(gt=0)
    authorized: Literal[True]
    authorization_nonce: str = Field(min_length=8, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    preflight_identity_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(code: str, message: str, *, status: int = 409, current: dict | None = None) -> HTTPException:
    detail: dict[str, object] = {"code": code, "message": message}
    if current is not None:
        detail["current"] = current
    return HTTPException(status_code=status, detail=detail)


def _map_batch_error(exc: BatchError, *, phase: str) -> HTTPException:
    code = str(exc)
    if code in {
        "HEAD_CHANGED",
        "PREPARATION_SCOPE_CHANGED",
        "AUTHORIZED_SCOPE_DRIFT",
        "AUTHORIZED_SCOPE_DRIFT_AFTER_CLAIM",
        "PLAN_CHANGED",
    }:
        return _error(
            "PROJECT_STATE_BASELINE_SCOPE_CHANGED",
            "代码、PRD、项目档案、Git 配置或模型选择刚刚发生变化。请重新扫描当前项目后再确认本次全量分析。",
        )
    if code in {
        "PLAN_NOT_CURRENT_CONFIRMED_AUTHORITY",
        "PLAN_SCHEMA_NOT_V2",
        "INVALID_PLAN",
        "INVALID_PLAN_ID",
    }:
        return _error(
            "PROJECT_STATE_BASELINE_PLAN_REQUIRED",
            "请先确认当前 V2 项目档案，再建立当前代码状态基线。",
        )
    if "BUDGET" in code or "PROVIDER_REQUEST_OVER_BUDGET" in code:
        return _error(
            "PROJECT_STATE_BASELINE_BUDGET_UNAVAILABLE",
            "当前模型预算无法安全容纳整仓分批请求。请检查模型设置后重新扫描。",
        )
    if code in {"BATCHES_NOT_COMPLETE", "FULL_BATCH_SET_NOT_AUTHORIZED"}:
        return _error(
            "PROJECT_STATE_BASELINE_INCOMPLETE",
            "本次全量分析尚未完成全部批次，不会生成项目状态候选。",
        )
    if code == "ANALYSIS_EMPTY_NO_CODE_EVIDENCE":
        return _error(
            "PROJECT_STATE_BASELINE_ANALYSIS_EMPTY",
            "模型已完成本次全量代码分析，但没有为任何计划模块返回可核对的代码证据。本次结果被判定为无效，因此不会生成空白候选；请检查当前模型后重新明确发起分析。",
        )
    return _error(
        "PROJECT_STATE_BASELINE_PREPARE_FAILED" if phase == "preflight" else "PROJECT_STATE_BASELINE_EXECUTION_FAILED",
        "无法安全完成当前项目的全量代码分析。没有自动确认，也没有用部分结果冒充完整基线。",
    )


def _prepare_product_scope(project_id: int, plan_profile_id: int, *, phase: str) -> dict[str, object]:
    """Prepare a baseline before any Human authorization or provider credential access.

    Local workspace/runtime failures must stay classified.  In particular, a transient
    Git workspace lock must never escape as FastAPI's opaque 500 response, because the
    browser would then misleadingly blame the user's Git/model configuration.
    """
    try:
        return _freeze_repository_scope(prepare_product_execution(project_id, plan_profile_id))
    except HTTPException:
        raise
    except BatchError as exc:
        raise _map_batch_error(exc, phase=phase) from exc
    except GitOperationInProgress as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_GIT_BUSY",
            "Git 工作区正在完成另一项操作，系统已短暂等待但仍未释放。未调用模型，请稍后重新检查分析批次。",
        ) from exc
    except GitClientError as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_GIT_WORKSPACE_FAILED",
            f"本地受控 Git 工作区无法安全读取：{exc.summary}",
        ) from exc
    except Exception as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_PREPARE_RUNTIME_FAILED",
            "首次分析准备遇到本地运行时错误，系统未调用模型，也未生成不完整结果。请重新打开安心看板后再试。",
            status=500,
        ) from exc


def ensure_project_state_baseline_schema() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_state_baseline_candidates (
                authorization_hash TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                exact_head TEXT NOT NULL,
                git_url TEXT NOT NULL,
                git_branch TEXT NOT NULL,
                source_profile_id INTEGER NOT NULL,
                source_profile_content_hash TEXT NOT NULL,
                source_prd_id INTEGER NOT NULL,
                generated_content_hash TEXT NOT NULL,
                profile_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS trg_project_state_baseline_candidate_no_update
            BEFORE UPDATE ON project_state_baseline_candidates
            BEGIN SELECT RAISE(ABORT, 'project state baseline candidate is append-only'); END;

            CREATE TRIGGER IF NOT EXISTS trg_project_state_baseline_candidate_no_delete
            BEFORE DELETE ON project_state_baseline_candidates
            BEGIN SELECT RAISE(ABORT, 'project state baseline candidate is append-only'); END;
            """
        )
        # Compatibility for an unreleased/intermediate v1 database that may have been
        # created by a development build before Git branch/url were made durable identity.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_state_baseline_candidates)")}
        if "git_url" not in columns:
            conn.execute("ALTER TABLE project_state_baseline_candidates ADD COLUMN git_url TEXT")
        if "git_branch" not in columns:
            conn.execute("ALTER TABLE project_state_baseline_candidates ADD COLUMN git_branch TEXT")


def _freeze_repository_scope(prepared: dict[str, object]) -> dict[str, object]:
    """Bind product preparation to current Git URL/branch as well as exact HEAD."""
    project, _prd, git_state = profile_generation._read_current_inputs(int(prepared["project_id"]))
    if git_state.get("remote_head") != prepared.get("exact_head"):
        raise _error(
            "PROJECT_STATE_BASELINE_SCOPE_CHANGED",
            "当前 HEAD 已变化，请重新扫描当前项目。",
        )
    git_url = project.get("git_url")
    git_branch = project.get("branch")
    if type(git_url) is not str or not git_url or type(git_branch) is not str or not git_branch:
        raise _error("PROJECT_STATE_BASELINE_SCOPE_CHANGED", "当前 Git 配置无效，请重新检查 Git 连接。")
    value = dict(prepared)
    value["git_url"] = git_url
    value["git_branch"] = git_branch
    return value


def _current_state(
    project_id: int,
    *,
    frozen_git_url: str,
    frozen_git_branch: str,
) -> dict[str, object]:
    """Return provider scope; Git config drift deliberately poisons exact_head closure."""
    try:
        project, prd, git_state = profile_generation._read_current_inputs(project_id)
        with get_connection() as conn:
            profile = read_current_confirmed_project_profile(project_id, conn=conn)
    except Exception:
        return {
            "exact_head": None,
            "prd_id": None,
            "prd_source_hash": None,
            "plan_profile_id": None,
            "plan_content_hash": None,
        }
    if project.get("git_url") != frozen_git_url or project.get("branch") != frozen_git_branch:
        return {
            "exact_head": None,
            "prd_id": prd.get("id"),
            "prd_source_hash": prd.get("source_hash"),
            "plan_profile_id": profile.get("id"),
            "plan_content_hash": profile.get("content_hash"),
        }
    return {
        "exact_head": git_state["remote_head"],
        "prd_id": prd["id"],
        "prd_source_hash": prd["source_hash"],
        "plan_profile_id": profile["id"],
        "plan_content_hash": profile["content_hash"],
    }


def _preflight_identity_hash(prepared: dict[str, object]) -> str:
    """Bind the Human-visible preflight to the exact scope that may later be authorized."""
    execution_plan = prepared.get("execution_plan")
    if not isinstance(execution_plan, dict):
        raise _error("PROJECT_STATE_BASELINE_PREFLIGHT_IDENTITY_INVALID", "首次分析准备身份无效，未调用模型。", status=500)
    execution_identity = execution_plan.get("execution_identity_hash")
    if type(execution_identity) is not str or len(execution_identity) != 64:
        execution_identity = hashlib.sha256(
            json.dumps(execution_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
    payload = {
        "schema_version": "project_state_baseline_preflight_identity_v1",
        "project_id": prepared["project_id"],
        "prd_id": prepared["prd_id"],
        "prd_source_hash": prepared["prd_source_hash"],
        "plan_profile_id": prepared["plan_profile_id"],
        "plan_content_hash": prepared["plan_content_hash"],
        "exact_head": prepared["exact_head"],
        "git_url": prepared["git_url"],
        "git_branch": prepared["git_branch"],
        "execution_identity_hash": execution_identity,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_summary(prepared: dict[str, object]) -> dict[str, object]:
    summary = dict(prepared["summary"])
    requests = summary.get("requests")
    if not isinstance(requests, list):
        requests = []
    return {
        "schema_version": SCHEMA_VERSION,
        "preflight_identity_hash": _preflight_identity_hash(prepared),
        "project_id": prepared["project_id"],
        "plan_profile_id": prepared["plan_profile_id"],
        "exact_head": prepared["exact_head"],
        "git_branch": prepared["git_branch"],
        "provider": summary.get("provider"),
        "model_id": summary.get("model_id"),
        "model_version": summary.get("model_version"),
        "batch_count": len(prepared["batches"]),
        "total_wire_bytes": summary.get("total_wire_bytes"),
        "tracked_files": summary.get("tracked_files"),
        "safe_text_bytes": summary.get("safe_text_bytes"),
        "coverage": summary.get("coverage"),
        "provider_calls": 0,
        "credential_read": False,
        "candidate_ready": False,
        "auto_confirm": False,
        "batches": [
            {
                "batch_index": item.get("batch_index"),
                "wire_bytes": item.get("wire_bytes"),
                "evidence_count": item.get("evidence_count"),
                "module_count": len(item.get("module_ids") or []),
            }
            for item in requests
        ],
    }


def _require_exact_head_reconciliation_coverage(
    stored_content: object, *, exact_head: str
) -> None:
    """Require current planned semantics to remain fully assessed at the analyzed HEAD."""
    try:
        model = ProjectProfileV2Content.model_validate(stored_content)
    except (TypeError, ValueError) as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_RECONCILIATION_STALE",
            "当前项目档案已经不再是本次全量分析对应的完整 V2 对账结果。请重新运行全量代码分析后再确认。",
        ) from exc

    planned_ids = [module.client_id for module in model.planned_modules]
    mapping_ids = [mapping.planned_module_id for mapping in model.implementation_mappings]
    if len(mapping_ids) != len(planned_ids) or set(mapping_ids) != set(planned_ids):
        raise _error(
            "PROJECT_STATE_BASELINE_RECONCILIATION_STALE",
            "计划功能在全量分析后发生修改，旧代码对账已不能覆盖全部模块。请重新运行全量代码分析后再确认。",
        )
    if any(mapping.exact_head != exact_head for mapping in model.implementation_mappings):
        raise _error(
            "PROJECT_STATE_BASELINE_RECONCILIATION_STALE",
            "项目档案中的实现对账不再绑定本次全量分析的 exact HEAD。请重新运行全量代码分析后再确认。",
        )
    if any(feature.exact_head != exact_head for feature in model.unplanned_code_features):
        raise _error(
            "PROJECT_STATE_BASELINE_RECONCILIATION_STALE",
            "项目档案中的计划外代码功能不再绑定本次全量分析的 exact HEAD。请重新运行全量代码分析后再确认。",
        )


def _promote_generated_candidate(
    *,
    project_id: int,
    prepared: dict[str, object],
    authorization_hash: str,
    content: dict[str, object],
) -> dict[str, object]:
    """Idempotently copy isolated provider output into normal Human review flow."""
    ensure_project_state_baseline_schema()
    model = ProjectProfileV2Content.model_validate(content)
    _validate_content(model)
    canonical, generated_content_hash = _canonicalize(model)
    created_at = _now()

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT profile_id, generated_content_hash FROM project_state_baseline_candidates WHERE authorization_hash = ?",
                (authorization_hash,),
            ).fetchone()
            if existing is not None:
                row = conn.execute(
                    f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ? AND project_id = ?",
                    (existing["profile_id"], project_id),
                ).fetchone()
                if row is None or existing["generated_content_hash"] != generated_content_hash:
                    raise _error(
                        "PROJECT_STATE_BASELINE_PROMOTION_INVALID",
                        "已完成的全量分析记录无法与项目档案候选闭合。为避免重复生成，系统已停止。",
                    )
                conn.commit()
                return _profile_dict(row)

            _require_project_in_tx(conn, project_id)
            active_prd_id = _read_active_prd_id(conn, project_id)
            if active_prd_id != prepared["prd_id"]:
                raise _error("PROJECT_STATE_BASELINE_SCOPE_CHANGED", "PRD 已发生变化，请重新扫描当前项目。")
            current_profile = read_current_confirmed_project_profile(project_id, conn=conn)
            if (
                current_profile["id"] != prepared["plan_profile_id"]
                or current_profile["content_hash"] != prepared["plan_content_hash"]
                or current_profile["source_prd_id"] != prepared["prd_id"]
            ):
                raise _error("PROJECT_STATE_BASELINE_SCOPE_CHANGED", "项目档案已发生变化，请重新扫描当前项目。")

            project = conn.execute("SELECT git_url, branch FROM projects WHERE id = ?", (project_id,)).fetchone()
            connection = conn.execute(
                "SELECT status, checked_url_hash, checked_branch, remote_head FROM project_git_connections WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            expected_url_hash = hashlib.sha256(str(prepared["git_url"]).encode("utf-8")).hexdigest()
            if (
                project is None
                or project["git_url"] != prepared["git_url"]
                or project["branch"] != prepared["git_branch"]
                or connection is None
                or connection["status"] != "connected"
                or connection["checked_url_hash"] != expected_url_hash
                or connection["checked_branch"] != prepared["git_branch"]
                or connection["remote_head"] != prepared["exact_head"]
            ):
                raise _error("PROJECT_STATE_BASELINE_SCOPE_CHANGED", "Git 配置或 HEAD 已变化，请重新扫描当前项目。")

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
                (project_id, version_no, prepared["prd_id"], canonical, generated_content_hash, created_at, created_at),
            )
            profile_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO project_state_baseline_candidates (
                    authorization_hash, schema_version, project_id, exact_head, git_url, git_branch,
                    source_profile_id, source_profile_content_hash, source_prd_id,
                    generated_content_hash, profile_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization_hash,
                    SCHEMA_VERSION,
                    project_id,
                    prepared["exact_head"],
                    prepared["git_url"],
                    prepared["git_branch"],
                    prepared["plan_profile_id"],
                    prepared["plan_content_hash"],
                    prepared["prd_id"],
                    generated_content_hash,
                    profile_id,
                    created_at,
                ),
            )
            row = conn.execute(f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)).fetchone()
            conn.commit()
            return _profile_dict(row)
    except HTTPException:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_PROMOTION_FAILED",
            "全量分析已经完成，但保存项目状态候选失败。系统不会自动重复调用模型。",
            status=500,
        ) from exc


def _confirm_baseline_candidate(profile_id: int, payload: ProfileConfirmPayload) -> dict[str, object] | None:
    """Confirm baseline-origin candidate + exact Git lineage atomically; return None for ordinary candidates."""
    ensure_project_state_baseline_schema()
    with get_connection() as conn:
        origin = conn.execute(
            "SELECT * FROM project_state_baseline_candidates WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
    if origin is None:
        return None

    if profile_id <= 0 or payload.edit_version <= 0:
        raise _error("PROJECT_PROFILE_BAD_INPUT", "项目档案确认参数不合法。", status=400)
    confirmed_by = (payload.confirmed_by or "").strip()
    if not 1 <= len(confirmed_by) <= 100 or any(unicodedata.category(ch) == "Cc" for ch in confirmed_by):
        raise _error("PROJECT_PROFILE_BAD_INPUT", "确认人信息不合法。", status=400)
    confirmed_at = _now()

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            origin = conn.execute(
                "SELECT * FROM project_state_baseline_candidates WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            row = conn.execute(f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)).fetchone()
            if origin is None or row is None:
                raise _error("PROJECT_PROFILE_NOT_FOUND", "项目档案版本不存在或已被删除。", status=404)
            current = dict(row)
            if current["status"] != "candidate":
                raise _error("PROJECT_PROFILE_INVALID_STATE", "当前项目档案已经不是可确认候选。")
            if current["edit_version"] != payload.edit_version:
                raise _error(
                    "PROJECT_PROFILE_VERSION_CONFLICT",
                    "候选已被其他操作更新，请重新加载最新内容。",
                    current=_profile_dict(current),
                )
            prd_id = _read_active_prd_id(conn, current["project_id"])
            if prd_id is None or prd_id != current["source_prd_id"] or prd_id != origin["source_prd_id"]:
                raise _error("PROJECT_PROFILE_SOURCE_PRD_STALE", "PRD 已更新，请重新建立项目状态基线。")
            try:
                stored_content = json.loads(current["content_json"])
            except json.JSONDecodeError as exc:
                raise _error("PROJECT_PROFILE_READ_FAILED", "项目档案内容无法读取。", status=500) from exc
            missing = _completeness_missing(stored_content)
            if missing:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "PROJECT_PROFILE_INCOMPLETE", "message": "项目档案信息不完整。", "missing": missing},
                )

            _require_exact_head_reconciliation_coverage(
                stored_content,
                exact_head=str(origin["exact_head"]),
            )

            project = conn.execute("SELECT git_url, branch FROM projects WHERE id = ?", (current["project_id"],)).fetchone()
            connection = conn.execute(
                "SELECT status, checked_url_hash, checked_branch FROM project_git_connections WHERE project_id = ?",
                (current["project_id"],),
            ).fetchone()
            expected_url_hash = hashlib.sha256(str(origin["git_url"]).encode("utf-8")).hexdigest()
            if (
                project is None
                or type(origin["git_url"]) is not str
                or type(origin["git_branch"]) is not str
                or project["git_url"] != origin["git_url"]
                or project["branch"] != origin["git_branch"]
                or connection is None
                or connection["status"] != "connected"
                or connection["checked_url_hash"] != expected_url_hash
                or connection["checked_branch"] != origin["git_branch"]
            ):
                raise _error(
                    "PROJECT_STATE_BASELINE_GIT_SCOPE_STALE",
                    "Git 地址或分支已变化。为避免把旧仓库状态设成新增量起点，请重新检查 Git 并重新建立当前代码状态基线。",
                )

            active = conn.execute(
                "SELECT id, branch, baseline_commit FROM analysis_lineages WHERE project_id = ? AND status = 'active' ORDER BY sequence_no DESC LIMIT 1",
                (current["project_id"],),
            ).fetchone()
            same_lineage = (
                active is not None
                and active["branch"] == origin["git_branch"]
                and active["baseline_commit"] == origin["exact_head"]
            )
            if active is not None and not same_lineage:
                conn.execute(
                    """
                    UPDATE analysis_lineages
                    SET status = 'closed', break_reason = 'project_state_baseline_rebound', closed_at = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (confirmed_at, active["id"]),
                )
            if not same_lineage:
                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) FROM analysis_lineages WHERE project_id = ?",
                    (current["project_id"],),
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO analysis_lineages (
                        project_id, sequence_no, branch, baseline_commit, status, break_reason,
                        source_git_checked_at, created_at, closed_at
                    ) VALUES (?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                    """,
                    (
                        current["project_id"],
                        int(sequence) + 1,
                        origin["git_branch"],
                        origin["exact_head"],
                        origin["created_at"],
                        confirmed_at,
                    ),
                )

            conn.execute(
                "UPDATE project_profiles SET status = 'superseded' WHERE project_id = ? AND status = 'confirmed'",
                (current["project_id"],),
            )
            cursor = conn.execute(
                """
                UPDATE project_profiles
                SET status = 'confirmed', confirmed_by = ?, confirmed_at = ?, edit_version = edit_version + 1
                WHERE id = ? AND status = 'candidate' AND edit_version = ?
                """,
                (confirmed_by, confirmed_at, profile_id, payload.edit_version),
            )
            if cursor.rowcount != 1:
                raise _error("PROJECT_PROFILE_VERSION_CONFLICT", "候选已被其他操作更新，请重新加载最新内容。")
            updated = conn.execute(f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?", (profile_id,)).fetchone()
            conn.commit()
            return _profile_dict(updated)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_CONFIRM_FAILED",
            "确认当前代码状态基线失败，项目档案和增量起点均未改变。",
            status=500,
        ) from exc


def _baseline_semantics_identity(raw_content: object) -> str:
    """Canonical identity for fields whose change invalidates full-code reconciliation."""
    model = ProjectProfileV2Content.model_validate(raw_content)
    payload = {
        "planned_modules": [item.model_dump() for item in model.planned_modules],
        "implementation_mappings": [item.model_dump() for item in model.implementation_mappings],
        "unplanned_code_features": [item.model_dump() for item in model.unplanned_code_features],
        "exclude_patterns": list(model.exclude_patterns),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@router.get("/api/projects/{project_id}/project-state-baseline/status")
def get_project_state_baseline_status(project_id: int) -> dict[str, object]:
    """Return whether the current project has a confirmed, provenance-backed full-code baseline."""
    if project_id <= 0:
        raise _error("PROJECT_NOT_FOUND", "项目不存在或已被删除。", status=404)
    try:
        with get_connection() as conn:
            project = conn.execute(
                "SELECT id, git_url, branch FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise _error("PROJECT_NOT_FOUND", "项目不存在或已被删除。", status=404)

            pending = conn.execute(
                """
                SELECT p.id, p.version_no, b.exact_head
                FROM project_profiles p
                JOIN project_state_baseline_candidates b ON b.profile_id = p.id
                WHERE p.project_id = ? AND p.status = 'candidate'
                ORDER BY p.version_no DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if pending is not None:
                return {
                    "status": "candidate_pending",
                    "has_confirmed_baseline": False,
                    "profile_id": int(pending["id"]),
                    "profile_version": int(pending["version_no"]),
                    "profile_schema_version": "project_profile_v2",
                    "exact_head": pending["exact_head"],
                }

            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE project_id = ? AND status = 'confirmed' ORDER BY version_no DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return {
                    "status": "profile_required",
                    "has_confirmed_baseline": False,
                    "profile_id": None,
                    "profile_version": None,
                    "profile_schema_version": None,
                    "exact_head": None,
                }
            current = dict(row)
            try:
                content = json.loads(current["content_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise _error(
                    "PROJECT_STATE_BASELINE_STATUS_READ_FAILED",
                    "当前项目档案无法安全读取。",
                    status=500,
                ) from exc
            schema_version = content.get("schema_version") if isinstance(content, dict) else None
            common = {
                "profile_id": int(current["id"]),
                "profile_version": int(current["version_no"]),
                "profile_schema_version": schema_version,
            }
            if schema_version != "project_profile_v2":
                return {
                    "status": "upgrade_required",
                    "has_confirmed_baseline": False,
                    "exact_head": None,
                    **common,
                }
            try:
                ProjectProfileV2Content.model_validate(content)
            except Exception as exc:
                raise _error(
                    "PROJECT_STATE_BASELINE_STATUS_READ_FAILED",
                    "当前 V2 项目档案无法安全读取。",
                    status=500,
                ) from exc

            active = conn.execute(
                """
                SELECT branch, baseline_commit
                FROM analysis_lineages
                WHERE project_id = ? AND status = 'active'
                ORDER BY sequence_no DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            origins = conn.execute(
                """
                SELECT * FROM project_state_baseline_candidates
                WHERE project_id = ? AND source_prd_id = ?
                ORDER BY created_at DESC
                """,
                (project_id, current["source_prd_id"]),
            ).fetchall()
            current_semantics = _baseline_semantics_identity(content)
            for origin in origins:
                origin_profile_row = conn.execute(
                    f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ? AND project_id = ?",
                    (origin["profile_id"], project_id),
                ).fetchone()
                try:
                    origin_profile = project_profiles_module._reclose_authority_row(
                        origin_profile_row,
                        allowed_statuses={"confirmed", "superseded"},
                    )
                    origin_semantics = _baseline_semantics_identity(origin_profile["content"])
                except (project_profiles_module.ProjectProfileAuthorityError, TypeError, ValueError):
                    continue
                if origin_semantics != current_semantics:
                    continue
                if (
                    project["git_url"] != origin["git_url"]
                    or project["branch"] != origin["git_branch"]
                    or active is None
                    or active["branch"] != origin["git_branch"]
                    or active["baseline_commit"] != origin["exact_head"]
                ):
                    continue
                try:
                    _require_exact_head_reconciliation_coverage(
                        content,
                        exact_head=str(origin["exact_head"]),
                    )
                except HTTPException:
                    continue
                return {
                    "status": "established",
                    "has_confirmed_baseline": True,
                    "exact_head": origin["exact_head"],
                    "git_branch": origin["git_branch"],
                    **common,
                }
            return {
                "status": "missing",
                "has_confirmed_baseline": False,
                "exact_head": None,
                **common,
            }
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_STATUS_READ_FAILED",
            "读取项目分析台账状态失败。",
            status=500,
        ) from exc


@router.post("/api/projects/{project_id}/project-state-baseline/preflight")
def preflight_project_state_baseline(
    project_id: int,
    payload: BaselinePreflightPayload,
    request: Request,
) -> dict[str, object]:
    """Build exact-HEAD full-repository batches locally; no API key or provider send."""
    try:
        # Preflight is protected by the local browser session, but it creates no durable
        # provider attempt and therefore does not require an idempotency key.
        require_local_write_request(request, require_idempotency_key=False)
        prepared = _prepare_product_scope(project_id, payload.plan_profile_id, phase="preflight")
        summary = _public_summary(prepared)
        # Validate response serialization inside this boundary so FastAPI cannot turn a
        # late serialization issue into an opaque HTTP 500.
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"status": "ready", "preflight": summary}
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(
            "PROJECT_STATE_BASELINE_PREFLIGHT_RUNTIME_FAILED",
            "首次分析准备遇到本地运行时错误，系统未调用模型，也未生成不完整结果。请重新打开安心看板后再试。",
            status=500,
        ) from exc


@router.post("/api/projects/{project_id}/project-state-baseline/execute", status_code=201)
def execute_project_state_baseline(
    project_id: int,
    payload: BaselineExecutePayload,
    request: Request,
) -> dict[str, object]:
    """Authorize frozen batches once, execute at-most-once per batch, then create a candidate."""
    require_local_write_request(request, require_idempotency_key=True)
    idempotency_key = request.headers.get("local-idempotency-key")
    if idempotency_key != payload.authorization_nonce:
        raise _error(
            "PROJECT_STATE_BASELINE_AUTHORIZATION_IDENTITY_MISMATCH",
            "本次授权身份与浏览器幂等身份不一致，未发送模型请求。",
            status=400,
        )
    prepared = _prepare_product_scope(project_id, payload.plan_profile_id, phase="execute")
    current_preflight_identity_hash = _preflight_identity_hash(prepared)
    if current_preflight_identity_hash != payload.preflight_identity_hash:
        raise _error(
            "PROJECT_STATE_BASELINE_PREFLIGHT_SCOPE_CHANGED",
            "你刚才确认的代码范围、PRD、功能档案、Git 配置、模型或分析批次已经变化。系统未读取 API Key，也未发送模型请求；请重新检查当前代码后再确认。",
        )
    execution_plan = prepared["execution_plan"]
    try:
        with get_connection() as conn:
            authorization = record_authorization(
                conn,
                execution_plan=execution_plan,
                project_id=project_id,
                prd_id=prepared["prd_id"],
                prd_source_hash=prepared["prd_source_hash"],
                plan_content_hash=prepared["plan_content_hash"],
                authorization_nonce=payload.authorization_nonce,
                authorized=payload.authorized,
            )

            cached_credential: list[str] = []

            def credential_reader() -> str:
                if not cached_credential:
                    cached_credential.append(profile_generation._read_provider_credential())
                return cached_credential[0]

            def current_state() -> dict[str, object]:
                return _current_state(
                    project_id,
                    frozen_git_url=str(prepared["git_url"]),
                    frozen_git_branch=str(prepared["git_branch"]),
                )

            for batch in prepared["batches"]:
                result = dispatch_authorized_batch(
                    conn,
                    batch,
                    authorization_hash=authorization["authorization_hash"],
                    adapter=prepared["adapter"],
                    credential_reader=credential_reader,
                    current_state=current_state,
                )
                status = result.get("status")
                if status == "unknown":
                    raise _error(
                        "PROJECT_STATE_BASELINE_BATCH_UNKNOWN",
                        "有一个模型批次的外部执行结果无法确认。系统不会自动重发；请保留当前操作并核对后再决定。",
                        current={"batch_index": batch.get("batch_index"), "authorization_hash": authorization["authorization_hash"], "error_code": result.get("error_code")},
                    )
                if status != "succeeded":
                    raise _error(
                        "PROJECT_STATE_BASELINE_BATCH_FAILED",
                        "本次全量分析有批次明确失败，因此没有生成不完整的项目状态候选。修正模型设置后可由你重新明确发起。",
                        current={"batch_index": batch.get("batch_index"), "status": status, "error_code": result.get("error_code")},
                    )

            isolated = aggregate_authorized_candidate(
                conn,
                prepared["batches"],
                prepared["plan"],
                authorization_hash=authorization["authorization_hash"],
                current_state=current_state,
            )

        profile = _promote_generated_candidate(
            project_id=project_id,
            prepared=prepared,
            authorization_hash=authorization["authorization_hash"],
            content=isolated["content"],
        )
        return {
            "status": "candidate_ready",
            "schema_version": SCHEMA_VERSION,
            "exact_head": prepared["exact_head"],
            "git_branch": prepared["git_branch"],
            "authorization_hash": authorization["authorization_hash"],
            "batch_count": len(prepared["batches"]),
            "profile": profile,
            "auto_confirm": False,
        }
    except HTTPException:
        raise
    except BatchError as exc:
        raise _map_batch_error(exc, phase="execute") from exc


@router.post("/api/profile-candidates/{profile_id}/confirm-with-state-baseline")
def confirm_profile_candidate_with_state_baseline(profile_id: int, payload: ProfileConfirmPayload) -> dict[str, object]:
    """Use ordinary confirmation unless this candidate originated from full-code baseline."""
    result = _confirm_baseline_candidate(profile_id, payload)
    if result is not None:
        return result
    return project_profiles_module.confirm_candidate(profile_id, payload)
