"""EvidenceSnapshot Core V2：冻结 structured PRD identity，不触发 Context、模型或报告。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictInt

from app.db import get_connection
from app.prd import (
    _STRUCTURED_FORMATS,
    _canonical_json_bytes,
    _document_fingerprint,
    _is_sha256_hex,
    _resolve_storage_target,
    _sha256_hex,
)
from app.projects import _normalize_git_url, _validate_branch


router = APIRouter()
SCHEMA_VERSION = "evidence_snapshot_core_v3"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PRD_STRUCTURED_SCHEMA_VERSION = "prd_structured_evidence_v1"
_PRD_STRUCTURED_PARSER_VERSION = "prd-structured-parser-1.0"
_SNAPSHOT_COLUMNS = (
    "id, schema_version, project_id, git_snapshot_id, analysis_lineage_id, "
    "branch, from_commit, to_commit, project_repository_url, project_config_hash, "
    "git_facts_hash, prd_id, prd_source_hash, prd_parsed_hash, "
    "prd_structured_hash, prd_document_fingerprint, profile_id, "
    "profile_content_hash, snapshot_hash, frozen_at"
)


class EvidenceSnapshotConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_git_snapshot_id: StrictInt
    expected_lineage_id: StrictInt
    expected_prd_id: StrictInt
    expected_profile_id: StrictInt


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _project_not_found() -> HTTPException:
    return _error(404, "PROJECT_NOT_FOUND", "项目不存在或已被删除")


def _prd_structured_required() -> HTTPException:
    return _error(
        409,
        "EVIDENCE_SNAPSHOT_PRD_STRUCTURED_REQUIRED",
        "当前已确认 PRD 缺少可验证的结构化证据，请重新导入并确认后再创建证据快照。",
    )


def _prd_structured_invalid() -> HTTPException:
    return _error(
        409,
        "EVIDENCE_SNAPSHOT_PRD_STRUCTURED_INVALID",
        "当前已确认 PRD 的结构化证据无法验证，请重新导入并确认后再创建证据快照。",
    )


def _validate_payload(payload: EvidenceSnapshotConfirm) -> tuple[int, int, int, int]:
    values = (
        payload.expected_git_snapshot_id,
        payload.expected_lineage_id,
        payload.expected_prd_id,
        payload.expected_profile_id,
    )
    if any(value <= 0 for value in values):
        raise _error(
            400,
            "INVALID_EVIDENCE_SNAPSHOT_CONFIRMATION",
            "EvidenceSnapshot 确认身份必须全部为正整数。",
        )
    return values


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snapshot_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _source_data_invalid() -> HTTPException:
    return _error(
        500,
        "EVIDENCE_SNAPSHOT_SOURCE_DATA_INVALID",
        "已确认的基础事实记录不完整，无法冻结 EvidenceSnapshot。",
    )


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise _source_data_invalid()
    return value


def _git_facts_hash(git_snapshot: dict) -> str:
    try:
        commits = json.loads(git_snapshot["commits_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise _source_data_invalid() from exc
    if not isinstance(commits, list) or not all(isinstance(item, str) for item in commits):
        raise _source_data_invalid()
    return _stable_hash(
        {
            "branch": git_snapshot["branch"],
            "from_commit": git_snapshot["from_commit"],
            "to_commit": git_snapshot["to_commit"],
            "commits": commits,
            "commit_count": git_snapshot["commit_count"],
            "changed_file_count": git_snapshot["changed_file_count"],
            "added_lines": git_snapshot["added_lines"],
            "deleted_lines": git_snapshot["deleted_lines"],
            "diff_bytes": git_snapshot["diff_bytes"],
        }
    )


def _read_existing(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    git_snapshot_id: int,
    lineage_id: int,
    prd_id: int,
    profile_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM evidence_snapshots "
        "WHERE schema_version = ? AND project_id = ? AND git_snapshot_id = ? "
        "AND analysis_lineage_id = ? AND prd_id = ? AND profile_id = ?",
        (SCHEMA_VERSION, project_id, git_snapshot_id, lineage_id, prd_id, profile_id),
    ).fetchone()


def _read_prd_structured_blocks(prd: dict) -> tuple[list[dict], str, str]:
    structured_fields = (
        "structured_path",
        "structured_hash",
        "structured_schema_version",
        "structured_parser_version",
        "document_fingerprint",
    )
    if any(not prd.get(field) for field in structured_fields):
        raise _prd_structured_required()

    # 路径安全错误必须保留 PRD_STORAGE_ESCAPE，不在此转换为普通 structured invalid。
    target = _resolve_storage_target(prd["structured_path"])
    try:
        structured_bytes = target.read_bytes()
    except OSError as exc:
        raise _prd_structured_invalid() from exc

    if not _is_sha256_hex(prd.get("structured_hash")):
        raise _prd_structured_invalid()
    if _sha256_hex(structured_bytes) != prd["structured_hash"]:
        raise _prd_structured_invalid()

    try:
        structured = json.loads(structured_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _prd_structured_invalid() from exc
    if not isinstance(structured, dict):
        raise _prd_structured_invalid()
    try:
        if _canonical_json_bytes(structured) != structured_bytes:
            raise _prd_structured_invalid()
    except (TypeError, ValueError) as exc:
        raise _prd_structured_invalid() from exc

    source_format = structured.get("source_format")
    expected_source_format = _STRUCTURED_FORMATS.get(
        Path(str(prd.get("original_filename") or "")).suffix.lower()
    )
    if expected_source_format is None or source_format != expected_source_format:
        raise _prd_structured_invalid()
    if structured.get("source_hash") != prd["source_hash"]:
        raise _prd_structured_invalid()

    if prd.get("structured_schema_version") != _PRD_STRUCTURED_SCHEMA_VERSION:
        raise _prd_structured_invalid()
    if structured.get("schema_version") != _PRD_STRUCTURED_SCHEMA_VERSION:
        raise _prd_structured_invalid()
    if structured.get("schema_version") != prd["structured_schema_version"]:
        raise _prd_structured_invalid()

    if prd.get("structured_parser_version") != _PRD_STRUCTURED_PARSER_VERSION:
        raise _prd_structured_invalid()
    if structured.get("parser_version") != _PRD_STRUCTURED_PARSER_VERSION:
        raise _prd_structured_invalid()
    if structured.get("parser_version") != prd["structured_parser_version"]:
        raise _prd_structured_invalid()

    document_fingerprint = prd.get("document_fingerprint")
    artifact_fingerprint = structured.get("document_fingerprint")
    if not _is_sha256_hex(document_fingerprint) or not _is_sha256_hex(artifact_fingerprint):
        raise _prd_structured_invalid()
    if artifact_fingerprint != document_fingerprint:
        raise _prd_structured_invalid()

    try:
        expected_fingerprint = _document_fingerprint(
            structured["parser_version"],
            structured["schema_version"],
            source_format,
            structured["source_hash"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _prd_structured_invalid() from exc
    if expected_fingerprint != artifact_fingerprint:
        raise _prd_structured_invalid()
    if expected_fingerprint != document_fingerprint:
        raise _prd_structured_invalid()

    blocks = structured.get("blocks")
    if not isinstance(blocks, list):
        raise _prd_structured_invalid()
    seen_evidence_ids: set[str] = set()
    for ordinal, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise _prd_structured_invalid()
        if type(block.get("ordinal")) is not int or block["ordinal"] != ordinal:
            raise _prd_structured_invalid()
        if block.get("kind") not in {"heading", "paragraph", "table"}:
            raise _prd_structured_invalid()
        evidence_id = block.get("evidence_id")
        expected_evidence_id = f"prd:block:{document_fingerprint}:{ordinal}"
        if not isinstance(evidence_id, str) or evidence_id != expected_evidence_id:
            raise _prd_structured_invalid()
        if evidence_id in seen_evidence_ids:
            raise _prd_structured_invalid()
        seen_evidence_ids.add(evidence_id)
        if not _is_sha256_hex(block.get("content_hash")):
            raise _prd_structured_invalid()
    return blocks, prd["structured_hash"], document_fingerprint


def _snapshot_integrity_invalid() -> HTTPException:
    return _error(
        500,
        "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID",
        "已冻结的 EvidenceSnapshot 完整性校验失败，已拒绝复用该快照。",
    )


def _evidence_items_hash(items: list[dict[str, object]]) -> str:
    canonical_items = sorted(
        (
            {
                "evidence_id": item["evidence_id"],
                "type": item["type"],
                "source_ref": item["source_ref"],
                "content_hash": item["content_hash"],
                "selected": item["selected"],
                "redaction_state": item["redaction_state"],
            }
            for item in items
        ),
        key=lambda item: (
            str(item["type"]),
            str(item["source_ref"]),
            str(item["evidence_id"]),
        ),
    )
    return _stable_hash({"schema_version": "evidence_items_commitment_v1", "items": canonical_items})


def _expected_snapshot_hash(snapshot: dict, evidence_items_hash: str) -> str:
    return _stable_hash(
        {
            "schema_version": snapshot["schema_version"],
            "project_id": snapshot["project_id"],
            "project_repository_url": snapshot["project_repository_url"],
            "project_config_hash": snapshot["project_config_hash"],
            "git_snapshot_id": snapshot["git_snapshot_id"],
            "analysis_lineage_id": snapshot["analysis_lineage_id"],
            "branch": snapshot["branch"],
            "from_commit": snapshot["from_commit"],
            "to_commit": snapshot["to_commit"],
            "git_facts_hash": snapshot["git_facts_hash"],
            "prd_id": snapshot["prd_id"],
            "prd_source_hash": snapshot["prd_source_hash"],
            "prd_parsed_hash": snapshot["prd_parsed_hash"],
            "prd_structured_hash": snapshot["prd_structured_hash"],
            "prd_document_fingerprint": snapshot["prd_document_fingerprint"],
            "profile_id": snapshot["profile_id"],
            "profile_content_hash": snapshot["profile_content_hash"],
            "evidence_items_hash": evidence_items_hash,
        }
    )


def _validate_existing_snapshot_integrity(
    conn: sqlite3.Connection, existing: sqlite3.Row
) -> None:
    snapshot = dict(existing)
    item_rows = conn.execute(
        """
        SELECT evidence_id, type, source_ref, content_hash, selected, redaction_state
        FROM evidence_items
        WHERE snapshot_id = ?
        ORDER BY type, source_ref, evidence_id
        """,
        (snapshot["id"],),
    ).fetchall()
    evidence_items_hash = _evidence_items_hash([dict(row) for row in item_rows])
    if snapshot.get("snapshot_hash") != _expected_snapshot_hash(snapshot, evidence_items_hash):
        raise _snapshot_integrity_invalid()


@router.post("/api/projects/{project_id}/evidence-snapshots", status_code=201)
def confirm_evidence_snapshot(project_id: int, payload: EvidenceSnapshotConfirm) -> dict:
    if project_id <= 0:
        raise _project_not_found()
    git_snapshot_id, lineage_id, prd_id, profile_id = _validate_payload(payload)

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project_row = conn.execute(
                "SELECT id, git_url, branch FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project_row is None:
                raise _project_not_found()

            existing = _read_existing(
                conn,
                project_id=project_id,
                git_snapshot_id=git_snapshot_id,
                lineage_id=lineage_id,
                prd_id=prd_id,
                profile_id=profile_id,
            )
            if existing is not None:
                _validate_existing_snapshot_integrity(conn, existing)
                conn.commit()
                return {"snapshot": _snapshot_dict(existing), "created": False}

            repository_url = _normalize_git_url(project_row["git_url"])
            branch = _validate_branch(project_row["branch"])
            if not repository_url:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_GIT_CONFIG_REQUIRED",
                    "请先保存有效的 Git 仓库地址和活动分支。",
                )

            git_row = conn.execute(
                """
                SELECT id, project_id, analysis_lineage_id, branch, from_commit, to_commit,
                       commits_json, commit_count, changed_file_count, added_lines,
                       deleted_lines, diff_bytes, file_manifest_hash
                FROM git_snapshots
                WHERE id = ? AND project_id = ?
                """,
                (git_snapshot_id, project_id),
            ).fetchone()
            if git_row is None:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_GIT_SNAPSHOT_NOT_FOUND",
                    "指定的 GitSnapshot 不存在或不属于当前项目。",
                )
            git_snapshot = dict(git_row)
            if git_snapshot["analysis_lineage_id"] != lineage_id:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_LINEAGE_STALE",
                    "GitSnapshot 所属分析链路与确认身份不一致。",
                )

            lineage_row = conn.execute(
                """
                SELECT id, branch, baseline_commit
                FROM analysis_lineages
                WHERE project_id = ? AND status = 'active'
                """,
                (project_id,),
            ).fetchone()
            if lineage_row is None or lineage_row["id"] != lineage_id:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_LINEAGE_STALE",
                    "当前活动分析链路已变化，请重新选择基础事实。",
                )
            if lineage_row["baseline_commit"] != git_snapshot["from_commit"]:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_LINEAGE_STALE",
                    "当前分析基线已变化，请重新选择基础事实。",
                )
            if (
                lineage_row["branch"] != git_snapshot["branch"]
                or branch != git_snapshot["branch"]
            ):
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_GIT_CONFIG_STALE",
                    "当前 Git 分支与已冻结的 GitSnapshot 不一致。",
                )

            prd_rows = conn.execute(
                """
                SELECT id, original_filename, source_hash, parsed_hash,
                       structured_path, structured_hash, structured_schema_version,
                       structured_parser_version, document_fingerprint
                FROM prd_versions
                WHERE project_id = ? AND status = 'parse_confirmed'
                """,
                (project_id,),
            ).fetchall()
            if not prd_rows:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PRD_NOT_CONFIRMED",
                    "当前项目没有已确认的 PRD。",
                )
            if len(prd_rows) != 1:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PRD_STATE_INVALID",
                    "当前项目的有效 PRD 状态不唯一，无法冻结 EvidenceSnapshot。",
                )
            prd = dict(prd_rows[0])
            if prd["id"] != prd_id:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PRD_STALE",
                    "当前有效 PRD 已变化，请重新选择基础事实。",
                )
            prd_source_hash = _require_hash(prd["source_hash"])
            prd_parsed_hash = _require_hash(prd["parsed_hash"])

            profile_rows = conn.execute(
                """
                SELECT id, source_prd_id, content_hash
                FROM project_profiles
                WHERE project_id = ? AND status = 'confirmed'
                """,
                (project_id,),
            ).fetchall()
            if not profile_rows:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PROFILE_NOT_CONFIRMED",
                    "当前项目没有已确认的项目档案。",
                )
            if len(profile_rows) != 1:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PROFILE_STATE_INVALID",
                    "当前项目的 confirmed 项目档案状态不唯一。",
                )
            profile = dict(profile_rows[0])
            if profile["id"] != profile_id:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PROFILE_STALE",
                    "当前 confirmed 项目档案已变化，请重新选择基础事实。",
                )
            if profile["source_prd_id"] != prd_id:
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_PROFILE_PRD_MISMATCH",
                    "当前项目档案不是基于当前有效 PRD。",
                )
            profile_content_hash = _require_hash(profile["content_hash"])

            file_evidence_rows = conn.execute(
                """
                SELECT ordinal, evidence_id, file_facts_hash
                FROM git_file_evidence
                WHERE git_snapshot_id = ?
                ORDER BY ordinal
                """,
                (git_snapshot_id,),
            ).fetchall()
            if (
                git_snapshot["file_manifest_hash"] is None
                or len(file_evidence_rows) != git_snapshot["changed_file_count"]
            ):
                raise _error(
                    409,
                    "EVIDENCE_SNAPSHOT_FILE_EVIDENCE_INCOMPLETE",
                    "当前 GitSnapshot 不满足 EvidenceItem 映射前置条件，请重新生成符合当前 FileEvidence 合同的新 GitSnapshot。",
                )

            (
                prd_blocks,
                prd_structured_hash,
                prd_document_fingerprint,
            ) = _read_prd_structured_blocks(prd)

            project_config_hash = _stable_hash(
                {"repository_url": repository_url, "branch": branch}
            )
            git_facts_hash = _git_facts_hash(git_snapshot)
            planned_items = [
                {
                    "evidence_id": row["evidence_id"],
                    "type": "git_file_fact",
                    "source_ref": f"git_file_evidence:{git_snapshot_id}:{row['ordinal']}",
                    "content_hash": row["file_facts_hash"],
                    "selected": 1,
                    "redaction_state": "not_applicable",
                }
                for row in file_evidence_rows
            ]
            planned_items.extend(
                {
                    "evidence_id": block["evidence_id"],
                    "type": "prd_block",
                    "source_ref": f"prd_structured_block:{prd_id}:{block['ordinal']}",
                    "content_hash": block["content_hash"],
                    "selected": 1,
                    "redaction_state": "pending",
                }
                for block in prd_blocks
            )
            evidence_items_hash = _evidence_items_hash(planned_items)
            snapshot_hash = _stable_hash(
                {
                    "schema_version": SCHEMA_VERSION,
                    "project_id": project_id,
                    "project_repository_url": repository_url,
                    "project_config_hash": project_config_hash,
                    "git_snapshot_id": git_snapshot_id,
                    "analysis_lineage_id": lineage_id,
                    "branch": git_snapshot["branch"],
                    "from_commit": git_snapshot["from_commit"],
                    "to_commit": git_snapshot["to_commit"],
                    "git_facts_hash": git_facts_hash,
                    "prd_id": prd_id,
                    "prd_source_hash": prd_source_hash,
                    "prd_parsed_hash": prd_parsed_hash,
                    "prd_structured_hash": prd_structured_hash,
                    "prd_document_fingerprint": prd_document_fingerprint,
                    "profile_id": profile_id,
                    "profile_content_hash": profile_content_hash,
                    "evidence_items_hash": evidence_items_hash,
                }
            )

            cursor = conn.execute(
                """
                INSERT INTO evidence_snapshots (
                    schema_version, project_id, git_snapshot_id, analysis_lineage_id,
                    branch, from_commit, to_commit, project_repository_url,
                    project_config_hash, git_facts_hash, prd_id, prd_source_hash,
                    prd_parsed_hash, prd_structured_hash, prd_document_fingerprint,
                    profile_id, profile_content_hash, snapshot_hash, frozen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    project_id,
                    git_snapshot_id,
                    lineage_id,
                    git_snapshot["branch"],
                    git_snapshot["from_commit"],
                    git_snapshot["to_commit"],
                    repository_url,
                    project_config_hash,
                    git_facts_hash,
                    prd_id,
                    prd_source_hash,
                    prd_parsed_hash,
                    prd_structured_hash,
                    prd_document_fingerprint,
                    profile_id,
                    profile_content_hash,
                    snapshot_hash,
                    _now(),
                ),
            )
            snapshot_id = cursor.lastrowid
            for file_evidence in file_evidence_rows:
                conn.execute(
                    """
                    INSERT INTO evidence_items (
                        snapshot_id, evidence_id, type, source_ref, content_hash,
                        selected, redaction_state
                    ) VALUES (?, ?, 'git_file_fact', ?, ?, 1, 'not_applicable')
                    """,
                    (
                        snapshot_id,
                        file_evidence["evidence_id"],
                        f"git_file_evidence:{git_snapshot_id}:{file_evidence['ordinal']}",
                        file_evidence["file_facts_hash"],
                    ),
                )
            for block in prd_blocks:
                conn.execute(
                    """
                    INSERT INTO evidence_items (
                        snapshot_id, evidence_id, type, source_ref, content_hash,
                        selected, redaction_state
                    ) VALUES (?, ?, 'prd_block', ?, ?, 1, 'pending')
                    """,
                    (
                        snapshot_id,
                        block["evidence_id"],
                        f"prd_structured_block:{prd_id}:{block['ordinal']}",
                        block["content_hash"],
                    ),
                )
            row = conn.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM evidence_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            from app.project_progress_context import freeze_progress_context, ProgressContextError
            try:
                freeze_progress_context(
                    conn, snapshot=dict(row), commits=json.loads(git_snapshot["commits_json"]),
                )
            except ProgressContextError as exc:
                raise _error(409, exc.code, str(exc)) from exc
            conn.commit()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(
            500,
            "EVIDENCE_SNAPSHOT_SAVE_FAILED",
            "保存 EvidenceSnapshot 失败，数据库原状态未改变。",
        ) from exc
    return {"snapshot": _snapshot_dict(row), "created": True}
