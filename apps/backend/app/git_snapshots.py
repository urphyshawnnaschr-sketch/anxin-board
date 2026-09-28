"""项目经理确认 Git 范围时的原子事实冻结；不推进任何分析基线或检查点。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictInt

from app.db import get_connection
from app.git_analysis import RangeCandidate, compute_range_candidate
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
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_FILE_MANIFEST_SCHEMA_VERSION = "git_file_manifest_v1"
_SNAPSHOT_COLUMNS = (
    "id, project_id, analysis_lineage_id, branch, from_commit, to_commit, "
    "commits_json, commit_count, changed_file_count, added_lines, deleted_lines, "
    "diff_bytes, frozen_at"
)


class GitSnapshotConfirm(BaseModel):
    expected_lineage_id: StrictInt
    expected_from_commit: str
    expected_to_commit: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _project_not_found() -> HTTPException:
    return _error(404, "PROJECT_NOT_FOUND", "项目不存在或已被删除")


def _validate_payload(payload: GitSnapshotConfirm) -> tuple[int, str, str]:
    if payload.expected_lineage_id <= 0:
        raise _error(400, "INVALID_GIT_SNAPSHOT_CONFIRMATION", "分析链路 ID 必须大于 0。")
    expected_from = payload.expected_from_commit.strip()
    expected_to = payload.expected_to_commit.strip()
    if not _COMMIT_RE.fullmatch(expected_from) or not _COMMIT_RE.fullmatch(expected_to):
        raise _error(
            400,
            "INVALID_GIT_SNAPSHOT_CONFIRMATION",
            "确认范围必须携带完整的小写 Git commit ID。",
        )
    return payload.expected_lineage_id, expected_from, expected_to


def _snapshot_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    return {
        "id": item["id"],
        "project_id": item["project_id"],
        "analysis_lineage_id": item["analysis_lineage_id"],
        "branch": item["branch"],
        "from_commit": item["from_commit"],
        "to_commit": item["to_commit"],
        "commits": json.loads(item["commits_json"]),
        "commit_count": item["commit_count"],
        "changed_file_count": item["changed_file_count"],
        "added_lines": item["added_lines"],
        "deleted_lines": item["deleted_lines"],
        "diff_bytes": item["diff_bytes"],
        "frozen_at": item["frozen_at"],
    }


def _stable_hash(value: dict) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_manifest_records(candidate: RangeCandidate) -> tuple[str, list[dict]]:
    if len(candidate.files) != candidate.changed_file_count:
        raise _error(
            500,
            "GIT_SNAPSHOT_FILE_FACTS_INVALID",
            "Git 文件事实数量与范围统计不一致，未保存 GitSnapshot。",
        )

    records: list[dict] = []
    manifest_items: list[dict] = []
    for ordinal, file_fact in enumerate(candidate.files, start=1):
        facts = {
            "path": file_fact.path,
            "added_lines": file_fact.added_lines,
            "deleted_lines": file_fact.deleted_lines,
            "is_binary": file_fact.is_binary,
        }
        file_facts_hash = _stable_hash(facts)
        record = {
            "ordinal": ordinal,
            **facts,
            "file_facts_hash": file_facts_hash,
        }
        records.append(record)
        manifest_items.append(record.copy())

    manifest_hash = _stable_hash(
        {
            "schema_version": _FILE_MANIFEST_SCHEMA_VERSION,
            "files": manifest_items,
        }
    )
    return manifest_hash, records


def _read_context(conn: sqlite3.Connection, project_id: int) -> tuple[dict, dict | None]:
    project = conn.execute(
        "SELECT git_url, branch FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        raise _project_not_found()
    url = _normalize_git_url(project["git_url"])
    branch = _validate_branch(project["branch"])
    if not url:
        raise _error(409, "GIT_CONFIG_REQUIRED", "请先保存 Git 仓库地址和活动分支。")
    lineage = conn.execute(
        """
        SELECT id, project_id, branch, baseline_commit
        FROM analysis_lineages
        WHERE project_id = ? AND status = 'active'
        ORDER BY sequence_no DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return {"git_url": url, "branch": branch}, dict(lineage) if lineage else None


def _assert_expected_lineage(
    lineage: dict | None,
    *,
    expected_lineage_id: int,
    expected_from_commit: str,
) -> None:
    if lineage is None or lineage["id"] != expected_lineage_id:
        raise _error(
            409,
            "ANALYSIS_LINEAGE_MISMATCH",
            "当前活动分析链路已变化，请重新查看候选范围后再确认。",
        )
    if lineage["baseline_commit"] != expected_from_commit:
        raise _error(
            409,
            "GIT_SNAPSHOT_FROM_COMMIT_STALE",
            "当前分析基线已变化，请重新查看候选范围后再确认。",
        )


def _read_current_candidate(project_id: int, project: dict, lineage: dict) -> RangeCandidate:
    access = None
    try:
        client = _git_client_factory()
        paths = resolve_workspace_paths(project_id, uuid.uuid4().hex, create=False)
        validate_workspace_paths(paths)
        if not paths.repo.exists():
            raise _error(409, "GIT_WORKSPACE_REQUIRED", "Git 工作区不存在，请先重新测试连接。")
        access = open_workspace_access(paths, project["git_url"])
        client.inspect_workspace(access)
        client.fetch_branch(access, project["branch"])
        return compute_range_candidate(
            client,
            access,
            branch=project["branch"],
            baseline_commit=lineage["baseline_commit"],
        )
    except HTTPException:
        raise
    except GitClientError as exc:
        summary = GIT_ERROR_SUMMARIES.get(exc.code, exc.summary)
        raise _error(409, exc.code, summary) from exc
    finally:
        if access is not None:
            access.close()


def _assert_candidate_can_freeze(candidate: RangeCandidate, expected_to_commit: str) -> None:
    if candidate.remote_head != expected_to_commit:
        raise _error(
            409,
            "GIT_SNAPSHOT_TO_COMMIT_STALE",
            "远端 HEAD 已变化，请重新查看候选范围后再确认。",
        )
    if candidate.continuity == "no_new_commit":
        raise _error(409, "GIT_SNAPSHOT_EMPTY_RANGE", "当前范围没有可冻结的新提交。")
    if candidate.continuity == "checkpoint_unreachable":
        raise _error(
            409,
            "GIT_SNAPSHOT_CHECKPOINT_UNREACHABLE",
            "当前分析基线与远端 HEAD 已断链，不能冻结该范围。",
        )
    if candidate.capacity == "capacity_exceeded":
        raise _error(
            409,
            "GIT_SNAPSHOT_CAPACITY_EXCEEDED",
            "当前候选范围超过本地资源硬上限，不能冻结不完整范围。"
            + "；".join(
                f"{label}上限 {candidate.capacity_limits.get(key, '未知')}"
                for reason, key, label in (
                    ("changed_files_hard_limit", "changed_files", "变化文件数"),
                    ("line_changes_hard_limit", "line_changes", "变化行数"),
                    ("commits_hard_limit", "commits", "提交数"),
                    ("diff_bytes_hard_limit", "diff_bytes", "差异字节数"),
                ) if reason in candidate.capacity_reasons
            ),
        )


def _save_snapshot(
    project_id: int,
    project: dict,
    lineage: dict,
    candidate: RangeCandidate,
    *,
    expected_lineage_id: int,
    expected_from_commit: str,
    expected_to_commit: str,
    require_existing: bool = False,
) -> dict:
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_project, current_lineage = _read_context(conn, project_id)
            _assert_expected_lineage(
                current_lineage,
                expected_lineage_id=expected_lineage_id,
                expected_from_commit=expected_from_commit,
            )
            if (
                current_project["git_url"] != project["git_url"]
                or current_project["branch"] != project["branch"]
                or current_lineage["branch"] != project["branch"]
            ):
                raise _error(
                    409,
                    "GIT_SNAPSHOT_CONFIG_STALE",
                    "Git 地址或活动分支已变化，请重新测试连接并查看候选范围。",
                )

            existing = conn.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM git_snapshots "
                "WHERE project_id = ? AND analysis_lineage_id = ? "
                "AND from_commit = ? AND to_commit = ?",
                (
                    project_id,
                    expected_lineage_id,
                    expected_from_commit,
                    expected_to_commit,
                ),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"snapshot": _snapshot_dict(existing), "created": False}
            if require_existing:
                raise _error(
                    409,
                    "GIT_SNAPSHOT_TO_COMMIT_STALE",
                    "远端 HEAD 已变化，请重新查看候选范围后再确认。",
                )

            file_manifest_hash, file_records = _file_manifest_records(candidate)
            frozen_at = _now()
            cursor = conn.execute(
                """
                INSERT INTO git_snapshots (
                    project_id, analysis_lineage_id, branch, from_commit, to_commit,
                    commits_json, commit_count, changed_file_count, added_lines,
                    deleted_lines, diff_bytes, file_manifest_hash, frozen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    expected_lineage_id,
                    project["branch"],
                    expected_from_commit,
                    candidate.remote_head,
                    json.dumps(candidate.commits, separators=(",", ":")),
                    candidate.commit_count,
                    candidate.changed_file_count,
                    candidate.added_lines,
                    candidate.deleted_lines,
                    candidate.diff_bytes,
                    file_manifest_hash,
                    frozen_at,
                ),
            )
            git_snapshot_id = cursor.lastrowid
            for record in file_records:
                evidence_id = f"git:file:{git_snapshot_id}:{record['ordinal']:03d}"
                conn.execute(
                    """
                    INSERT INTO git_file_evidence (
                        git_snapshot_id, ordinal, evidence_id, path, added_lines,
                        deleted_lines, is_binary, file_facts_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        git_snapshot_id,
                        record["ordinal"],
                        evidence_id,
                        record["path"],
                        record["added_lines"],
                        record["deleted_lines"],
                        1 if record["is_binary"] else 0,
                        record["file_facts_hash"],
                    ),
                )
            row = conn.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM git_snapshots WHERE id = ?",
                (git_snapshot_id,),
            ).fetchone()
            conn.commit()
    except HTTPException:
        raise
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise _error(
            500,
            "GIT_SNAPSHOT_SAVE_FAILED",
            "保存 GitSnapshot 失败，数据库原状态未改变。",
        ) from exc
    return {"snapshot": _snapshot_dict(row), "created": True}


@router.post("/api/projects/{project_id}/git-snapshots", status_code=201)
def confirm_git_snapshot(project_id: int, payload: GitSnapshotConfirm) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    expected_lineage_id, expected_from, expected_to = _validate_payload(payload)
    try:
        with project_workspace_lock(project_id):
            try:
                with get_connection() as conn:
                    project, lineage = _read_context(conn, project_id)
            except HTTPException:
                raise
            except sqlite3.Error as exc:
                raise _error(
                    500,
                    "GIT_SNAPSHOT_READ_FAILED",
                    "读取 GitSnapshot 前置数据失败。",
                ) from exc

            _assert_expected_lineage(
                lineage,
                expected_lineage_id=expected_lineage_id,
                expected_from_commit=expected_from,
            )
            if lineage["branch"] != project["branch"]:
                raise _error(
                    409,
                    "GIT_SNAPSHOT_CONFIG_STALE",
                    "Git 地址或活动分支已变化，请重新测试连接并查看候选范围。",
                )
            candidate = _read_current_candidate(project_id, project, lineage)
            require_existing = candidate.remote_head != expected_to
            if not require_existing:
                _assert_candidate_can_freeze(candidate, expected_to)
            return _save_snapshot(
                project_id,
                project,
                lineage,
                candidate,
                expected_lineage_id=expected_lineage_id,
                expected_from_commit=expected_from,
                expected_to_commit=expected_to,
                require_existing=require_existing,
            )
    except GitOperationInProgress:
        raise _error(
            409,
            "GIT_OPERATION_IN_PROGRESS",
            "该项目另有 Git 工作区操作正在进行，请稍后再确认范围。",
        )
