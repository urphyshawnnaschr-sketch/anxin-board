"""Context evidence resolvers：从 EvidenceSnapshot V2 冻结身份解析历史 PRD block / Git diff。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from fastapi import HTTPException

from app.db import get_connection
from app.git_analysis import RangeCandidate, _file_evidence_candidates
from app.git_client import (
    GitClient,
    GitClientError,
    _assert_workspace_access,
    _is_reparse,
    _locked_safe_local_config,
    open_workspace_access,
    resolve_workspace_paths,
    validate_workspace_paths,
)
from app.git_snapshots import (
    _FILE_MANIFEST_SCHEMA_VERSION,
    _file_manifest_records,
    _stable_hash as _git_snapshot_stable_hash,
)
from app.git_workspace_locks import GitOperationInProgress, project_workspace_lock
from app.evidence_snapshots import _evidence_items_hash, _git_facts_hash, _stable_hash as _evidence_stable_hash
from app.projects import _normalize_git_url
from app.prd import (
    _STRUCTURED_FORMATS,
    _canonical_json_bytes,
    _document_fingerprint,
    _is_sha256_hex,
    _resolve_storage_target,
    _sha256_hex,
)

_SNAPSHOT_SCHEMA_VERSION_V2 = "evidence_snapshot_core_v2"
_SNAPSHOT_SCHEMA_VERSION = "evidence_snapshot_core_v3"
_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({_SNAPSHOT_SCHEMA_VERSION_V2, _SNAPSHOT_SCHEMA_VERSION})
_STRUCTURED_SCHEMA_VERSION = "prd_structured_evidence_v1"
_STRUCTURED_PARSER_VERSION = "prd-structured-parser-1.0"
_SOURCE_REF_RE = re.compile(r"^prd_structured_block:([1-9][0-9]*):([1-9][0-9]*)$")
_GIT_SOURCE_REF_RE = re.compile(r"^git_file_evidence:([1-9][0-9]*):([1-9][0-9]*)$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_DIFF_BYTE_LIMIT = 5 * 1024 * 1024
_git_client_factory = GitClient


def _error(code: str, message: str, *, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _unsupported() -> HTTPException:
    return _error(
        "CONTEXT_PRD_SNAPSHOT_UNSUPPORTED",
        "该证据快照缺少可验证的结构化 PRD 身份，无法解析历史 PRD 证据。",
    )


def _item_not_found() -> HTTPException:
    return _error(
        "CONTEXT_PRD_ITEM_NOT_FOUND",
        "目标 PRD EvidenceItem 不存在于该证据快照。",
        status=404,
    )


def _item_invalid() -> HTTPException:
    return _error(
        "CONTEXT_PRD_ITEM_INVALID",
        "目标 PRD EvidenceItem 的冻结身份无法闭合。",
    )


def _artifact_required() -> HTTPException:
    return _error(
        "CONTEXT_PRD_ARTIFACT_REQUIRED",
        "该证据快照引用的历史 PRD 缺少可读取的结构化冻结产物。",
    )


def _artifact_invalid() -> HTTPException:
    return _error(
        "CONTEXT_PRD_ARTIFACT_INVALID",
        "该证据快照引用的历史 PRD 结构化产物无法通过身份校验。",
    )


def _read_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, schema_version, project_id, prd_id, prd_source_hash,
               prd_structured_hash, prd_document_fingerprint
        FROM evidence_snapshots
        WHERE id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise _unsupported()
    snapshot = dict(row)
    if snapshot["schema_version"] not in _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise _unsupported()
    if (
        not isinstance(snapshot.get("prd_id"), int)
        or snapshot["prd_id"] <= 0
        or not _is_sha256_hex(snapshot.get("prd_source_hash"))
        or not _is_sha256_hex(snapshot.get("prd_structured_hash"))
        or not _is_sha256_hex(snapshot.get("prd_document_fingerprint"))
    ):
        raise _unsupported()
    return snapshot


def _read_item(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    evidence_id: str,
    snapshot_prd_id: int,
) -> tuple[dict, int]:
    row = conn.execute(
        """
        SELECT snapshot_id, evidence_id, type, source_ref, content_hash,
               selected, redaction_state
        FROM evidence_items
        WHERE snapshot_id = ? AND evidence_id = ?
        """,
        (snapshot_id, evidence_id),
    ).fetchone()
    if row is None:
        raise _item_not_found()
    item = dict(row)
    if (
        item["type"] != "prd_block"
        or item["selected"] != 1
        or item["redaction_state"] != "pending"
        or not _is_sha256_hex(item.get("content_hash"))
    ):
        raise _item_invalid()
    source_ref = item.get("source_ref")
    if not isinstance(source_ref, str):
        raise _item_invalid()
    match = _SOURCE_REF_RE.fullmatch(source_ref)
    if match is None:
        raise _item_invalid()
    prd_id = int(match.group(1))
    ordinal = int(match.group(2))
    if prd_id != snapshot_prd_id:
        raise _item_invalid()
    return item, ordinal


def _read_historical_prd(conn: sqlite3.Connection, *, snapshot: dict) -> dict:
    row = conn.execute(
        """
        SELECT id, project_id, original_filename, source_hash, structured_path,
               structured_hash, structured_schema_version, structured_parser_version,
               document_fingerprint
        FROM prd_versions
        WHERE id = ? AND project_id = ?
        """,
        (snapshot["prd_id"], snapshot["project_id"]),
    ).fetchone()
    if row is None:
        raise _artifact_required()
    prd = dict(row)
    required = (
        "original_filename",
        "source_hash",
        "structured_path",
        "structured_hash",
        "structured_schema_version",
        "structured_parser_version",
        "document_fingerprint",
    )
    if any(prd.get(field) in (None, "") for field in required):
        raise _artifact_required()
    if (
        not _is_sha256_hex(prd.get("source_hash"))
        or not _is_sha256_hex(prd.get("structured_hash"))
        or not _is_sha256_hex(prd.get("document_fingerprint"))
    ):
        raise _artifact_invalid()
    if (
        prd["source_hash"] != snapshot["prd_source_hash"]
        or prd["structured_hash"] != snapshot["prd_structured_hash"]
        or prd["document_fingerprint"] != snapshot["prd_document_fingerprint"]
        or prd["structured_schema_version"] != _STRUCTURED_SCHEMA_VERSION
        or prd["structured_parser_version"] != _STRUCTURED_PARSER_VERSION
    ):
        raise _artifact_invalid()
    return prd


def _read_exact_artifact(snapshot: dict, prd: dict) -> dict:
    # PRD_STORAGE_ESCAPE 必须原样传播，不能吞并为普通 Context 错误。
    target = _resolve_storage_target(prd["structured_path"])
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise _artifact_required() from exc

    if (
        _sha256_hex(raw) != snapshot["prd_structured_hash"]
        or _sha256_hex(raw) != prd["structured_hash"]
    ):
        raise _artifact_invalid()

    try:
        structured = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _artifact_invalid() from exc
    if not isinstance(structured, dict):
        raise _artifact_invalid()
    try:
        if _canonical_json_bytes(structured) != raw:
            raise _artifact_invalid()
    except (TypeError, ValueError) as exc:
        raise _artifact_invalid() from exc

    ext = Path(str(prd["original_filename"])).suffix.lower()
    expected_format = _STRUCTURED_FORMATS.get(ext)
    if expected_format is None or structured.get("source_format") != expected_format:
        raise _artifact_invalid()
    if structured.get("schema_version") != _STRUCTURED_SCHEMA_VERSION:
        raise _artifact_invalid()
    if structured.get("parser_version") != _STRUCTURED_PARSER_VERSION:
        raise _artifact_invalid()
    if structured.get("source_hash") != snapshot["prd_source_hash"]:
        raise _artifact_invalid()
    if structured.get("document_fingerprint") != snapshot["prd_document_fingerprint"]:
        raise _artifact_invalid()

    try:
        fingerprint = _document_fingerprint(
            structured["parser_version"],
            structured["schema_version"],
            structured["source_format"],
            structured["source_hash"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _artifact_invalid() from exc
    if fingerprint != snapshot["prd_document_fingerprint"]:
        raise _artifact_invalid()
    return structured


def _resolve_exact_block(
    *, snapshot: dict, item: dict, ordinal: int, structured: dict
) -> dict:
    blocks = structured.get("blocks")
    if not isinstance(blocks, list) or ordinal > len(blocks):
        raise _artifact_invalid()
    block = blocks[ordinal - 1]
    if not isinstance(block, dict):
        raise _artifact_invalid()
    if type(block.get("ordinal")) is not int or block["ordinal"] != ordinal:
        raise _artifact_invalid()
    if block.get("kind") not in {"heading", "paragraph", "table"}:
        raise _artifact_invalid()
    expected_evidence_id = f"prd:block:{snapshot['prd_document_fingerprint']}:{ordinal}"
    if (
        block.get("evidence_id") != item["evidence_id"]
        or block.get("evidence_id") != expected_evidence_id
        or not _is_sha256_hex(block.get("content_hash"))
        or block.get("content_hash") != item["content_hash"]
    ):
        raise _artifact_invalid()
    return block


def resolve_prd_block(snapshot_id: int, evidence_id: str) -> dict:
    """纯读解析一条 snapshot 内 PRD block；失败时稳定 Fail Closed。"""
    if type(snapshot_id) is not int or snapshot_id <= 0:
        raise _unsupported()
    if not isinstance(evidence_id, str) or not evidence_id:
        raise _item_not_found()

    try:
        with get_connection() as conn:
            snapshot = _read_snapshot(conn, snapshot_id)
            item, ordinal = _read_item(
                conn,
                snapshot_id=snapshot_id,
                evidence_id=evidence_id,
                snapshot_prd_id=snapshot["prd_id"],
            )
            prd = _read_historical_prd(conn, snapshot=snapshot)
            structured = _read_exact_artifact(snapshot, prd)
            block = _resolve_exact_block(
                snapshot=snapshot,
                item=item,
                ordinal=ordinal,
                structured=structured,
            )
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _artifact_invalid() from exc

    return {
        "snapshot_id": snapshot_id,
        "evidence_id": item["evidence_id"],
        "type": "prd_block",
        "source_ref": item["source_ref"],
        "content_hash": item["content_hash"],
        "prd_id": snapshot["prd_id"],
        "ordinal": ordinal,
        "kind": block.get("kind"),
        "text": block.get("text"),
        "heading_level": block.get("heading_level"),
        "table_rows": block.get("table_rows"),
        "page_no": block.get("page_no"),
        "prd_structured_hash": snapshot["prd_structured_hash"],
        "prd_document_fingerprint": snapshot["prd_document_fingerprint"],
    }


# ---------------------------------------------------------------------------
# Context Git Diff Resolver V1
# ---------------------------------------------------------------------------


def _git_snapshot_unsupported() -> HTTPException:
    return _error(
        "CONTEXT_GIT_SNAPSHOT_UNSUPPORTED",
        "该证据快照缺少可验证的冻结 Git 身份，无法解析历史 Git 证据。",
    )


def _git_item_not_found() -> HTTPException:
    return _error(
        "CONTEXT_GIT_ITEM_NOT_FOUND",
        "目标 Git EvidenceItem 不存在于该证据快照。",
        status=404,
    )


def _git_item_invalid() -> HTTPException:
    return _error(
        "CONTEXT_GIT_ITEM_INVALID",
        "目标 Git EvidenceItem 的冻结身份无法闭合。",
    )


def _git_frozen_facts_invalid() -> HTTPException:
    return _error(
        "CONTEXT_GIT_FROZEN_FACTS_INVALID",
        "冻结 Git 事实无法完成身份闭合。",
    )


def _git_workspace_required() -> HTTPException:
    return _error(
        "CONTEXT_GIT_WORKSPACE_REQUIRED",
        "该历史证据对应的受控 Git 工作区不可用。",
    )


def _git_range_unavailable() -> HTTPException:
    return _error(
        "CONTEXT_GIT_RANGE_UNAVAILABLE",
        "冻结 Git 范围所需的本地对象不可用或无法安全重放。",
    )


def _git_attributes_unsafe() -> HTTPException:
    return _error(
        "CONTEXT_GIT_ATTRIBUTES_UNSAFE",
        "本地 Git attributes 会改变历史 Diff 解释，已停止解析。",
    )


def _git_diff_too_large() -> HTTPException:
    return _error(
        "CONTEXT_GIT_DIFF_TOO_LARGE",
        "目标文件的完整历史 Diff 超过 5 MiB 上限，未返回截断正文。",
    )


def _git_diff_invalid() -> HTTPException:
    return _error(
        "CONTEXT_GIT_DIFF_INVALID",
        "目标文件的历史 Diff 无法按冻结 V1 语义完整解析。",
    )


def _git_operation_in_progress() -> HTTPException:
    return _error(
        "GIT_OPERATION_IN_PROGRESS",
        "该项目另有 Git 工作区操作正在进行，请稍后重试。",
    )


def _read_git_snapshot_context(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, schema_version, project_id, git_snapshot_id, branch,
               from_commit, to_commit, project_repository_url,
               project_config_hash, git_facts_hash
        FROM evidence_snapshots
        WHERE id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise _git_snapshot_unsupported()
    snapshot = dict(row)
    if snapshot.get("schema_version") not in _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise _git_snapshot_unsupported()
    if (
        type(snapshot.get("project_id")) is not int
        or snapshot["project_id"] <= 0
        or type(snapshot.get("git_snapshot_id")) is not int
        or snapshot["git_snapshot_id"] <= 0
        or not isinstance(snapshot.get("branch"), str)
        or not snapshot["branch"]
        or not isinstance(snapshot.get("project_repository_url"), str)
        or not snapshot["project_repository_url"]
        or not isinstance(snapshot.get("from_commit"), str)
        or _GIT_COMMIT_RE.fullmatch(snapshot["from_commit"]) is None
        or not isinstance(snapshot.get("to_commit"), str)
        or _GIT_COMMIT_RE.fullmatch(snapshot["to_commit"]) is None
        or not _is_sha256_hex(snapshot.get("project_config_hash"))
        or not _is_sha256_hex(snapshot.get("git_facts_hash"))
    ):
        raise _git_snapshot_unsupported()

    try:
        normalized_repository_url = _normalize_git_url(snapshot["project_repository_url"])
    except HTTPException as exc:
        raise _git_snapshot_unsupported() from exc
    if (
        normalized_repository_url is None
        or normalized_repository_url != snapshot["project_repository_url"]
    ):
        raise _git_snapshot_unsupported()

    expected_config_hash = _evidence_stable_hash(
        {
            "repository_url": normalized_repository_url,
            "branch": snapshot["branch"],
        }
    )
    if expected_config_hash != snapshot["project_config_hash"]:
        raise _git_frozen_facts_invalid()
    return snapshot


def _read_git_item(
    conn: sqlite3.Connection,
    *,
    snapshot: dict,
    snapshot_id: int,
    evidence_id: str,
) -> tuple[dict, int]:
    row = conn.execute(
        """
        SELECT snapshot_id, evidence_id, type, source_ref, content_hash,
               selected, redaction_state
        FROM evidence_items
        WHERE snapshot_id = ? AND evidence_id = ?
        """,
        (snapshot_id, evidence_id),
    ).fetchone()
    if row is None:
        raise _git_item_not_found()
    item = dict(row)
    if (
        item.get("type") != "git_file_fact"
        or item.get("selected") != 1
        or item.get("redaction_state") != "not_applicable"
        or not _is_sha256_hex(item.get("content_hash"))
        or not isinstance(item.get("source_ref"), str)
    ):
        raise _git_item_invalid()
    match = _GIT_SOURCE_REF_RE.fullmatch(item["source_ref"])
    if match is None:
        raise _git_item_invalid()
    source_git_snapshot_id = int(match.group(1))
    ordinal = int(match.group(2))
    if source_git_snapshot_id != snapshot["git_snapshot_id"]:
        raise _git_item_invalid()
    return item, ordinal


def _valid_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _read_historical_git_snapshot(conn: sqlite3.Connection, snapshot: dict) -> tuple[dict, list[str]]:
    row = conn.execute(
        """
        SELECT id, project_id, analysis_lineage_id, branch, from_commit, to_commit,
               commits_json, commit_count, changed_file_count, added_lines,
               deleted_lines, diff_bytes, file_manifest_hash
        FROM git_snapshots
        WHERE id = ? AND project_id = ?
        """,
        (snapshot["git_snapshot_id"], snapshot["project_id"]),
    ).fetchone()
    if row is None:
        raise _git_frozen_facts_invalid()
    git_snapshot = dict(row)
    if (
        git_snapshot.get("id") != snapshot["git_snapshot_id"]
        or git_snapshot.get("project_id") != snapshot["project_id"]
        or git_snapshot.get("branch") != snapshot["branch"]
        or git_snapshot.get("from_commit") != snapshot["from_commit"]
        or git_snapshot.get("to_commit") != snapshot["to_commit"]
        or not _valid_nonnegative_int(git_snapshot.get("changed_file_count"))
        or not _valid_nonnegative_int(git_snapshot.get("added_lines"))
        or not _valid_nonnegative_int(git_snapshot.get("deleted_lines"))
        or not _valid_nonnegative_int(git_snapshot.get("diff_bytes"))
        or type(git_snapshot.get("commit_count")) is not int
        or git_snapshot["commit_count"] <= 0
        or not _is_sha256_hex(git_snapshot.get("file_manifest_hash"))
    ):
        raise _git_frozen_facts_invalid()

    raw_commits = git_snapshot.get("commits_json")
    if not isinstance(raw_commits, str):
        raise _git_frozen_facts_invalid()
    try:
        commits = json.loads(raw_commits)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _git_frozen_facts_invalid() from exc
    if (
        not isinstance(commits, list)
        or len(commits) != git_snapshot["commit_count"]
        or any(
            not isinstance(commit, str) or _GIT_COMMIT_RE.fullmatch(commit) is None
            for commit in commits
        )
        or json.dumps(commits, separators=(",", ":")) != raw_commits
    ):
        raise _git_frozen_facts_invalid()

    try:
        actual_git_facts_hash = _git_facts_hash(git_snapshot)
    except HTTPException as exc:
        raise _git_frozen_facts_invalid() from exc
    if actual_git_facts_hash != snapshot["git_facts_hash"]:
        raise _git_frozen_facts_invalid()
    return git_snapshot, commits


def _read_frozen_file_evidence(
    conn: sqlite3.Connection,
    *,
    git_snapshot: dict,
    item: dict,
    target_ordinal: int,
) -> tuple[list[dict], dict]:
    rows = conn.execute(
        """
        SELECT ordinal, evidence_id, path, added_lines, deleted_lines,
               is_binary, file_facts_hash
        FROM git_file_evidence
        WHERE git_snapshot_id = ?
        ORDER BY ordinal
        """,
        (git_snapshot["id"],),
    ).fetchall()
    if len(rows) != git_snapshot["changed_file_count"]:
        raise _git_frozen_facts_invalid()

    records: list[dict] = []
    target: dict | None = None
    for expected_ordinal, row in enumerate(rows, start=1):
        record = dict(row)
        is_binary_raw = record.get("is_binary")
        is_binary = is_binary_raw == 1
        expected_evidence_id = f"git:file:{git_snapshot['id']}:{expected_ordinal:03d}"
        if (
            record.get("ordinal") != expected_ordinal
            or record.get("evidence_id") != expected_evidence_id
            or not isinstance(record.get("path"), str)
            or not record["path"]
            or "\x00" in record["path"]
            or is_binary_raw not in (0, 1)
            or not _is_sha256_hex(record.get("file_facts_hash"))
        ):
            raise _git_frozen_facts_invalid()
        if is_binary:
            if record.get("added_lines") is not None or record.get("deleted_lines") is not None:
                raise _git_frozen_facts_invalid()
        else:
            if not _valid_nonnegative_int(record.get("added_lines")) or not _valid_nonnegative_int(
                record.get("deleted_lines")
            ):
                raise _git_frozen_facts_invalid()

        facts = {
            "path": record["path"],
            "added_lines": record["added_lines"],
            "deleted_lines": record["deleted_lines"],
            "is_binary": is_binary,
        }
        if _git_snapshot_stable_hash(facts) != record["file_facts_hash"]:
            raise _git_frozen_facts_invalid()
        normalized = {
            "ordinal": expected_ordinal,
            **facts,
            "file_facts_hash": record["file_facts_hash"],
            "evidence_id": record["evidence_id"],
        }
        records.append(normalized)
        if expected_ordinal == target_ordinal:
            target = normalized

    manifest_items = [
        {
            "ordinal": record["ordinal"],
            "path": record["path"],
            "added_lines": record["added_lines"],
            "deleted_lines": record["deleted_lines"],
            "is_binary": record["is_binary"],
            "file_facts_hash": record["file_facts_hash"],
        }
        for record in records
    ]
    manifest_hash = _git_snapshot_stable_hash(
        {"schema_version": _FILE_MANIFEST_SCHEMA_VERSION, "files": manifest_items}
    )
    if manifest_hash != git_snapshot["file_manifest_hash"]:
        raise _git_frozen_facts_invalid()
    if target is None:
        raise _git_frozen_facts_invalid()
    if (
        target["evidence_id"] != item["evidence_id"]
        or target["file_facts_hash"] != item["content_hash"]
    ):
        raise _git_frozen_facts_invalid()
    return records, target


def _git_info_attributes_state(access) -> tuple[int, int, int, int] | None:
    """只读取 .git/info/attributes 元数据；不读取规则正文。"""
    _assert_workspace_access(access)
    info_dir = access.path / ".git" / "info"
    try:
        if info_dir.exists():
            if _is_reparse(info_dir):
                raise GitClientError("GIT_WORKSPACE_ESCAPE")
            if not info_dir.is_dir():
                raise GitClientError("GIT_WORKSPACE_CONFLICT")
        attributes = info_dir / "attributes"
        metadata = attributes.lstat()
    except FileNotFoundError:
        return None
    except GitClientError:
        raise
    except OSError as exc:
        raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc

    if _is_reparse(attributes):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    if not stat.S_ISREG(metadata.st_mode):
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    if metadata.st_size != 0:
        raise _git_attributes_unsafe()
    _assert_workspace_access(access)
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _assert_attributes_stable(access, before: tuple[int, int, int, int] | None) -> None:
    after = _git_info_attributes_state(access)
    if after != before:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")


def _historical_git_prefix(access, to_commit: str) -> list[str]:
    return [
        "--no-optional-locks",
        "-C",
        str(access.path),
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--literal-pathspecs",
        f"--attr-source={to_commit}",
        "-c",
        f"core.attributesFile={os.devnull}",
    ]


def _preserve_workspace_error(exc: GitClientError) -> None:
    if exc.code in {"GIT_WORKSPACE_ESCAPE", "GIT_WORKSPACE_CONFLICT", "GIT_WORKSPACE_DIRTY"}:
        raise _error(exc.code, exc.summary) from exc


def _assert_workspace_clean_read_only(client: GitClient, access) -> None:
    before = _git_info_attributes_state(access)
    try:
        with _locked_safe_local_config(access.path, access.expected_url):
            dirty = client._run(
                [
                    "--no-optional-locks",
                    "--no-replace-objects",
                    "--no-lazy-fetch",
                    "-C",
                    str(access.path),
                    "status",
                    "--porcelain",
                ],
                cwd=access.path,
            )
    except GitClientError as exc:
        _preserve_workspace_error(exc)
        raise _git_range_unavailable() from exc
    _assert_attributes_stable(access, before)
    _assert_workspace_access(access)
    if dirty:
        raise _error("GIT_WORKSPACE_DIRTY", GitClientError("GIT_WORKSPACE_DIRTY").summary)


def _replay_commit_list(client: GitClient, access, git_snapshot: dict) -> list[str]:
    before = _git_info_attributes_state(access)
    try:
        with _locked_safe_local_config(access.path, access.expected_url):
            lines, over = client._run_bounded_process(
                [
                    *_historical_git_prefix(access, git_snapshot["to_commit"]),
                    "rev-list",
                    "--reverse",
                    f"{git_snapshot['from_commit']}..{git_snapshot['to_commit']}",
                ],
                cwd=access.path,
                line_limit=git_snapshot["commit_count"],
                parse_line=True,
                timeout_seconds=client.timeout_seconds,
            )
    except GitClientError as exc:
        _preserve_workspace_error(exc)
        raise _git_range_unavailable() from exc
    _assert_attributes_stable(access, before)
    _assert_workspace_access(access)
    if over or not isinstance(lines, list):
        raise _git_frozen_facts_invalid()
    commits = [line.strip().lower() for line in lines if line.strip()]
    if any(_GIT_COMMIT_RE.fullmatch(commit) is None for commit in commits):
        raise _git_range_unavailable()
    return commits


def _replay_file_evidence(
    client: GitClient, access, git_snapshot: dict
) -> tuple[list[dict], list[tuple[str, str, str]]]:
    before = _git_info_attributes_state(access)
    try:
        with _locked_safe_local_config(access.path, access.expected_url):
            lines, over = client._run_bounded_process(
                [
                    *_historical_git_prefix(access, git_snapshot["to_commit"]),
                    "diff",
                    "--numstat",
                    "--no-renames",
                    git_snapshot["from_commit"],
                    git_snapshot["to_commit"],
                ],
                cwd=access.path,
                line_limit=max(1, git_snapshot["changed_file_count"]),
                parse_line=True,
                timeout_seconds=client.timeout_seconds,
            )
    except GitClientError as exc:
        _preserve_workspace_error(exc)
        raise _git_range_unavailable() from exc
    _assert_attributes_stable(access, before)
    _assert_workspace_access(access)
    if over or not isinstance(lines, list):
        raise _git_frozen_facts_invalid()

    # 继续使用正式 GitClient numstat parser，再复用 producer 的 pathname canonicalization。
    from app.git_client import _parse_numstat_lines

    try:
        rows = _parse_numstat_lines(lines)
        candidates = _file_evidence_candidates(rows)
        replay_candidate = RangeCandidate(
            baseline_commit=git_snapshot["from_commit"],
            remote_head=git_snapshot["to_commit"],
            changed_file_count=len(candidates),
            files=candidates,
        )
        manifest_hash, producer_records = _file_manifest_records(replay_candidate)
    except (GitClientError, HTTPException, ValueError, TypeError) as exc:
        raise _git_frozen_facts_invalid() from exc
    replay_records = [
        {
            "ordinal": record["ordinal"],
            "path": record["path"],
            "added_lines": record["added_lines"],
            "deleted_lines": record["deleted_lines"],
            "is_binary": record["is_binary"],
            "file_facts_hash": record["file_facts_hash"],
        }
        for record in producer_records
    ]
    if manifest_hash != git_snapshot["file_manifest_hash"]:
        raise _git_frozen_facts_invalid()
    return replay_records, rows


def _exact_diff_args(access, git_snapshot: dict, path: str) -> list[str]:
    return [
        *_historical_git_prefix(access, git_snapshot["to_commit"]),
        "diff",
        "--patch",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-color-moved",
        "--ws-error-highlight=none",
        "--text",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
        "--unified=3",
        "--inter-hunk-context=0",
        "--full-index",
        "--default-prefix",
        "--no-relative",
        git_snapshot["from_commit"],
        git_snapshot["to_commit"],
        "--",
        path,
    ]


def _read_exact_diff(client: GitClient, access, git_snapshot: dict, path: str) -> bytes:
    args = _exact_diff_args(access, git_snapshot, path)
    proportional_timeout = max(30, _GIT_DIFF_BYTE_LIMIT // (200 * 1024))
    before = _git_info_attributes_state(access)
    try:
        with _locked_safe_local_config(access.path, access.expected_url):
            measured, over = client._run_bounded_process(
                args,
                cwd=access.path,
                byte_limit=_GIT_DIFF_BYTE_LIMIT,
                timeout_seconds=proportional_timeout,
                parse_line=False,
            )
    except GitClientError as exc:
        _preserve_workspace_error(exc)
        raise _git_diff_invalid() from exc
    _assert_attributes_stable(access, before)
    _assert_workspace_access(access)
    if over:
        raise _git_diff_too_large()
    if type(measured) is not int or measured < 0 or measured > _GIT_DIFF_BYTE_LIMIT:
        raise _git_diff_invalid()

    # 第一次 bounded run 已证明同一 frozen refs / attributes / fixed argv 的输出在硬上限内；
    # 第二次仍走 GitClient._exec 的同一 execution policy / timeout / Job Object，取完整原始 bytes。
    before_capture = _git_info_attributes_state(access)
    try:
        with _locked_safe_local_config(access.path, access.expected_url):
            returncode, raw, _stderr = client._exec(args, cwd=access.path)
    except GitClientError as exc:
        _preserve_workspace_error(exc)
        raise _git_diff_invalid() from exc
    _assert_attributes_stable(access, before_capture)
    _assert_workspace_access(access)
    if returncode != 0 or len(raw) != measured:
        raise _git_diff_invalid()
    if len(raw) > _GIT_DIFF_BYTE_LIMIT:
        raise _git_diff_too_large()
    return raw


def _frozen_records_without_evidence_id(records: list[dict]) -> list[dict]:
    return [
        {
            "ordinal": record["ordinal"],
            "path": record["path"],
            "added_lines": record["added_lines"],
            "deleted_lines": record["deleted_lines"],
            "is_binary": record["is_binary"],
            "file_facts_hash": record["file_facts_hash"],
        }
        for record in records
    ]


def resolve_git_file_fact(snapshot_id: int, evidence_id: str) -> dict:
    """纯读重放 snapshot 内一条 git_file_fact，并返回 deterministic bounded historical Diff。"""
    if type(snapshot_id) is not int or snapshot_id <= 0:
        raise _git_snapshot_unsupported()
    if not isinstance(evidence_id, str) or not evidence_id:
        raise _git_item_not_found()

    try:
        with get_connection() as conn:
            snapshot = _read_git_snapshot_context(conn, snapshot_id)
            item, ordinal = _read_git_item(
                conn,
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                evidence_id=evidence_id,
            )
            git_snapshot, frozen_commits = _read_historical_git_snapshot(conn, snapshot)
            frozen_records, target = _read_frozen_file_evidence(
                conn,
                git_snapshot=git_snapshot,
                item=item,
                target_ordinal=ordinal,
            )
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _git_frozen_facts_invalid() from exc

    access = None
    try:
        with project_workspace_lock(snapshot["project_id"]):
            try:
                paths = resolve_workspace_paths(
                    snapshot["project_id"], uuid.uuid4().hex, create=False
                )
                validate_workspace_paths(paths)
                if not paths.repo.exists():
                    raise _git_workspace_required()
                access = open_workspace_access(paths, snapshot["project_repository_url"])
                client = _git_client_factory()
                client.inspect_workspace(access)
                _assert_workspace_clean_read_only(client, access)
            except HTTPException:
                raise
            except GitClientError as exc:
                _preserve_workspace_error(exc)
                raise _git_range_unavailable() from exc

            replay_commits = _replay_commit_list(client, access, git_snapshot)
            if replay_commits != frozen_commits:
                raise _git_frozen_facts_invalid()

            replay_records, _rows = _replay_file_evidence(client, access, git_snapshot)
            if replay_records != _frozen_records_without_evidence_id(frozen_records):
                raise _git_frozen_facts_invalid()

            result = {
                "snapshot_id": snapshot_id,
                "evidence_id": item["evidence_id"],
                "type": "git_file_fact",
                "source_ref": item["source_ref"],
                "content_hash": item["content_hash"],
                "git_snapshot_id": git_snapshot["id"],
                "ordinal": target["ordinal"],
                "path": target["path"],
                "added_lines": target["added_lines"],
                "deleted_lines": target["deleted_lines"],
                "is_binary": target["is_binary"],
                "from_commit": git_snapshot["from_commit"],
                "to_commit": git_snapshot["to_commit"],
                "git_facts_hash": snapshot["git_facts_hash"],
                "file_manifest_hash": git_snapshot["file_manifest_hash"],
            }

            if target["is_binary"] or _sensitive_target_path(target["path"]):
                return {
                    **result,
                    "diff_text": None,
                    "resolved_content_hash": None,
                    "resolved_diff_bytes": 0,
                    "resolved_content_redaction_state": "not_applicable",
                    "content_kind": ("binary_metadata_only" if target["is_binary"]
                                     else "sensitive_path_metadata_only"),
                }

            raw = _read_exact_diff(client, access, git_snapshot, target["path"])
            try:
                diff_text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise _git_diff_invalid() from exc
            return {
                **result,
                "diff_text": diff_text,
                "resolved_content_hash": hashlib.sha256(raw).hexdigest(),
                "resolved_diff_bytes": len(raw),
                "resolved_content_redaction_state": "pending",
                "content_kind": "text_unified_diff",
            }
    except GitOperationInProgress as exc:
        raise _git_operation_in_progress() from exc
    finally:
        if access is not None:
            access.close()


# ---------------------------------------------------------------------------
# Context Candidate Set V1
# ---------------------------------------------------------------------------

_CANDIDATE_SCHEMA_VERSION = "context_candidate_set_v1"
_CANDIDATE_UNSUPPORTED_SOURCES = ["analysis_rules:not_frozen_in_snapshot_v2"]


def _candidate_snapshot_unsupported() -> HTTPException:
    return _error(
        "CONTEXT_CANDIDATE_SNAPSHOT_UNSUPPORTED",
        "该证据快照不满足 Context Candidate Set V1 的冻结身份要求。",
    )


def _candidate_profile_invalid() -> HTTPException:
    return _error(
        "CONTEXT_CANDIDATE_PROFILE_INVALID",
        "该证据快照引用的历史项目档案无法完成身份闭合。",
    )


def _candidate_item_set_invalid() -> HTTPException:
    return _error(
        "CONTEXT_CANDIDATE_ITEM_SET_INVALID",
        "该证据快照的 EvidenceItem 全集无法与冻结 Git/PRD 证据闭合。",
    )


def _read_candidate_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, schema_version, project_id, git_snapshot_id, analysis_lineage_id,
               branch, from_commit, to_commit, project_repository_url,
               project_config_hash, git_facts_hash, prd_id, prd_source_hash,
               prd_parsed_hash, prd_structured_hash, prd_document_fingerprint,
               profile_id, profile_content_hash, snapshot_hash
        FROM evidence_snapshots
        WHERE id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise _candidate_snapshot_unsupported()
    snapshot = dict(row)
    positive_ids = (
        "id",
        "project_id",
        "git_snapshot_id",
        "analysis_lineage_id",
        "prd_id",
        "profile_id",
    )
    hashes = (
        "project_config_hash",
        "git_facts_hash",
        "prd_source_hash",
        "prd_parsed_hash",
        "prd_structured_hash",
        "prd_document_fingerprint",
        "profile_content_hash",
        "snapshot_hash",
    )
    if (
        snapshot.get("schema_version") not in _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS
        or any(type(snapshot.get(field)) is not int or snapshot[field] <= 0 for field in positive_ids)
        or not isinstance(snapshot.get("branch"), str)
        or not snapshot["branch"]
        or not isinstance(snapshot.get("project_repository_url"), str)
        or not snapshot["project_repository_url"]
        or not isinstance(snapshot.get("from_commit"), str)
        or _GIT_COMMIT_RE.fullmatch(snapshot["from_commit"]) is None
        or not isinstance(snapshot.get("to_commit"), str)
        or _GIT_COMMIT_RE.fullmatch(snapshot["to_commit"]) is None
        or any(not _is_sha256_hex(snapshot.get(field)) for field in hashes)
    ):
        raise _candidate_snapshot_unsupported()

    try:
        normalized_repository_url = _normalize_git_url(snapshot["project_repository_url"])
    except HTTPException as exc:
        raise _candidate_snapshot_unsupported() from exc
    if (
        normalized_repository_url is None
        or normalized_repository_url != snapshot["project_repository_url"]
        or _evidence_stable_hash(
            {"repository_url": normalized_repository_url, "branch": snapshot["branch"]}
        )
        != snapshot["project_config_hash"]
    ):
        raise _candidate_snapshot_unsupported()

    return snapshot


def _validate_candidate_snapshot_integrity(conn: sqlite3.Connection, snapshot: dict) -> None:
    schema_version = snapshot.get("schema_version")
    if schema_version == _SNAPSHOT_SCHEMA_VERSION_V2:
        payload = {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION_V2,
            "project_id": snapshot["project_id"],
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
        }
    elif schema_version == _SNAPSHOT_SCHEMA_VERSION:
        rows = conn.execute(
            """
            SELECT evidence_id, type, source_ref, content_hash, selected, redaction_state
            FROM evidence_items
            WHERE snapshot_id = ?
            ORDER BY type, source_ref, evidence_id
            """,
            (snapshot["id"],),
        ).fetchall()
        payload = {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION,
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
            "evidence_items_hash": _evidence_items_hash([dict(row) for row in rows]),
        }
    else:
        raise _candidate_snapshot_unsupported()
    if _evidence_stable_hash(payload) != snapshot["snapshot_hash"]:
        raise _candidate_snapshot_unsupported()


def _read_candidate_profile(conn: sqlite3.Connection, snapshot: dict) -> dict:
    from app.project_profiles import parse_profile_content, SCHEMA_VERSION as PROFILE_SCHEMA_VERSION

    row = conn.execute(
        """
        SELECT id, project_id, source_prd_id, content_json, content_hash
        FROM project_profiles
        WHERE id = ? AND project_id = ?
        """,
        (snapshot["profile_id"], snapshot["project_id"]),
    ).fetchone()
    if row is None:
        raise _candidate_profile_invalid()
    profile = dict(row)
    if (
        profile.get("id") != snapshot["profile_id"]
        or profile.get("project_id") != snapshot["project_id"]
        or profile.get("source_prd_id") != snapshot["prd_id"]
        or profile.get("content_hash") != snapshot["profile_content_hash"]
        or not _is_sha256_hex(profile.get("content_hash"))
        or not isinstance(profile.get("content_json"), str)
    ):
        raise _candidate_profile_invalid()
    try:
        raw_content = json.loads(profile["content_json"])
        if not isinstance(raw_content, dict):
            raise ValueError("profile content must be an object")
        validated = parse_profile_content(raw_content)
        content = validated.model_dump()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _candidate_profile_invalid() from exc
    if content.get("schema_version") not in {PROFILE_SCHEMA_VERSION, "project_profile_v2"} or content != raw_content:
        raise _candidate_profile_invalid()
    canonical = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if canonical != profile["content_json"]:
        raise _candidate_profile_invalid()
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != profile["content_hash"]:
        raise _candidate_profile_invalid()
    result = {
        "profile_id": profile["id"],
        "profile_content_hash": profile["content_hash"],
        "source_prd_id": profile["source_prd_id"],
        "content": content,
        "resolved_content_redaction_state": "pending",
        "model_send_state": "not_admitted",
    }
    from app.project_progress_context import read_progress_context, ProgressContextError
    try:
        frozen = read_progress_context(conn, snapshot=snapshot)
    except ProgressContextError as exc:
        raise _error(exc.code, str(exc)) from exc
    if frozen is not None:
        result["progress_context_hash"] = frozen["context_hash"]
        result["progress_context"] = frozen
    return result


def _read_candidate_items(
    conn: sqlite3.Connection, snapshot: dict
) -> tuple[dict[int, dict], dict[int, dict]]:
    rows = conn.execute(
        """
        SELECT evidence_id, type, source_ref, content_hash, selected, redaction_state
        FROM evidence_items
        WHERE snapshot_id = ?
        """,
        (snapshot["id"],),
    ).fetchall()
    git_items: dict[int, dict] = {}
    prd_items: dict[int, dict] = {}
    seen_ids: set[str] = set()
    for row in rows:
        item = dict(row)
        evidence_id = item.get("evidence_id")
        source_ref = item.get("source_ref")
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in seen_ids
            or item.get("selected") != 1
            or not isinstance(source_ref, str)
            or not _is_sha256_hex(item.get("content_hash"))
        ):
            raise _candidate_item_set_invalid()
        seen_ids.add(evidence_id)
        if item.get("type") == "git_file_fact":
            if item.get("redaction_state") != "not_applicable":
                raise _candidate_item_set_invalid()
            match = _GIT_SOURCE_REF_RE.fullmatch(source_ref)
            if match is None or int(match.group(1)) != snapshot["git_snapshot_id"]:
                raise _candidate_item_set_invalid()
            ordinal = int(match.group(2))
            if ordinal in git_items:
                raise _candidate_item_set_invalid()
            git_items[ordinal] = item
        elif item.get("type") == "prd_block":
            if item.get("redaction_state") != "pending":
                raise _candidate_item_set_invalid()
            match = _SOURCE_REF_RE.fullmatch(source_ref)
            if match is None or int(match.group(1)) != snapshot["prd_id"]:
                raise _candidate_item_set_invalid()
            ordinal = int(match.group(2))
            if ordinal in prd_items:
                raise _candidate_item_set_invalid()
            prd_items[ordinal] = item
        else:
            raise _candidate_item_set_invalid()
    return git_items, prd_items


def _read_candidate_git_manifest(
    conn: sqlite3.Connection,
    *,
    git_snapshot: dict,
    git_items: dict[int, dict],
) -> list[dict]:
    expected_ordinals = set(range(1, git_snapshot["changed_file_count"] + 1))
    if set(git_items) != expected_ordinals:
        raise _candidate_item_set_invalid()

    identity_rows = conn.execute(
        """
        SELECT ordinal, evidence_id, file_facts_hash
        FROM git_file_evidence
        WHERE git_snapshot_id = ?
        ORDER BY ordinal
        """,
        (git_snapshot["id"],),
    ).fetchall()
    if len(identity_rows) != git_snapshot["changed_file_count"]:
        raise _git_frozen_facts_invalid()

    for expected_ordinal, row in enumerate(identity_rows, start=1):
        identity = dict(row)
        expected_evidence_id = f"git:file:{git_snapshot['id']}:{expected_ordinal:03d}"
        if (
            identity.get("ordinal") != expected_ordinal
            or identity.get("evidence_id") != expected_evidence_id
            or not _is_sha256_hex(identity.get("file_facts_hash"))
        ):
            raise _git_frozen_facts_invalid()
        item = git_items.get(expected_ordinal)
        if (
            item is None
            or item["evidence_id"] != identity["evidence_id"]
            or item["content_hash"] != identity["file_facts_hash"]
            or item["source_ref"]
            != f"git_file_evidence:{git_snapshot['id']}:{expected_ordinal}"
        ):
            raise _candidate_item_set_invalid()

    if not expected_ordinals:
        empty_manifest_hash = _git_snapshot_stable_hash(
            {"schema_version": _FILE_MANIFEST_SCHEMA_VERSION, "files": []}
        )
        if empty_manifest_hash != git_snapshot["file_manifest_hash"]:
            raise _git_frozen_facts_invalid()
        return []

    frozen_records, _target = _read_frozen_file_evidence(
        conn,
        git_snapshot=git_snapshot,
        item=git_items[1],
        target_ordinal=1,
    )
    if len(frozen_records) != len(git_items):
        raise _candidate_item_set_invalid()
    return frozen_records


def _read_candidate_prd_items(
    *,
    snapshot: dict,
    structured: dict,
    prd_items: dict[int, dict],
) -> list[dict]:
    blocks = structured.get("blocks")
    if not isinstance(blocks, list):
        raise _artifact_invalid()
    expected_ordinals = set(range(1, len(blocks) + 1))
    if set(prd_items) != expected_ordinals:
        raise _candidate_item_set_invalid()
    resolved: list[dict] = []
    for ordinal in range(1, len(blocks) + 1):
        item = prd_items[ordinal]
        block = blocks[ordinal - 1]
        expected_evidence_id = f"prd:block:{snapshot['prd_document_fingerprint']}:{ordinal}"
        if (
            item["source_ref"] != f"prd_structured_block:{snapshot['prd_id']}:{ordinal}"
            or not isinstance(block, dict)
            or item["evidence_id"] != expected_evidence_id
            or item["content_hash"] != block.get("content_hash")
        ):
            raise _candidate_item_set_invalid()
        block = _resolve_exact_block(
            snapshot=snapshot,
            item=item,
            ordinal=ordinal,
            structured=structured,
        )
        resolved.append(
            {
                "evidence_id": item["evidence_id"],
                "type": "prd_block",
                "source_ref": item["source_ref"],
                "content_hash": item["content_hash"],
                "prd_id": snapshot["prd_id"],
                "ordinal": ordinal,
                "kind": block.get("kind"),
                "heading_level": block.get("heading_level"),
                "page_no": block.get("page_no"),
                "prd_structured_hash": snapshot["prd_structured_hash"],
                "prd_document_fingerprint": snapshot["prd_document_fingerprint"],
                "resolved_content_redaction_state": "pending",
                "model_send_state": "not_admitted",
            }
        )
    return resolved


def _open_candidate_workspace(snapshot: dict):
    try:
        paths = resolve_workspace_paths(
            snapshot["project_id"], uuid.uuid4().hex, create=False
        )
        validate_workspace_paths(paths)
        if not paths.repo.exists():
            raise _git_workspace_required()
        access = open_workspace_access(paths, snapshot["project_repository_url"])
        try:
            client = _git_client_factory()
            client.inspect_workspace(access)
            _assert_workspace_clean_read_only(client, access)
        except BaseException:
            access.close()
            raise
        return access, client
    except HTTPException:
        raise
    except GitClientError as exc:
        _preserve_workspace_error(exc)
        raise _git_range_unavailable() from exc


def _materialize_candidate_git_items(
    *,
    snapshot: dict,
    git_snapshot: dict,
    frozen_commits: list[str],
    frozen_records: list[dict],
    git_items: dict[int, dict],
) -> list[dict]:
    access = None
    try:
        with project_workspace_lock(snapshot["project_id"]):
            access, client = _open_candidate_workspace(snapshot)
            replay_commits = _replay_commit_list(client, access, git_snapshot)
            if replay_commits != frozen_commits:
                raise _git_frozen_facts_invalid()
            replay_records, _rows = _replay_file_evidence(client, access, git_snapshot)
            if replay_records != _frozen_records_without_evidence_id(frozen_records):
                raise _git_frozen_facts_invalid()

            resolved: list[dict] = []
            for target in frozen_records:
                item = git_items[target["ordinal"]]
                base = {
                    "evidence_id": item["evidence_id"],
                    "type": "git_file_fact",
                    "source_ref": item["source_ref"],
                    "content_hash": item["content_hash"],
                    "git_snapshot_id": git_snapshot["id"],
                    "ordinal": target["ordinal"],
                    "path": target["path"],
                    "added_lines": target["added_lines"],
                    "deleted_lines": target["deleted_lines"],
                    "is_binary": target["is_binary"],
                    "from_commit": git_snapshot["from_commit"],
                    "to_commit": git_snapshot["to_commit"],
                    "git_facts_hash": snapshot["git_facts_hash"],
                    "file_manifest_hash": git_snapshot["file_manifest_hash"],
                    "model_send_state": "not_admitted",
                }
                if target["is_binary"] or _sensitive_target_path(target["path"]):
                    resolved.append(
                        {
                            **base,
                            "resolved_content_hash": None,
                            "resolved_diff_bytes": 0,
                            "resolved_content_redaction_state": "not_applicable",
                            "content_kind": ("binary_metadata_only" if target["is_binary"]
                                     else "sensitive_path_metadata_only"),
                        }
                    )
                    continue
                raw = _read_exact_diff(client, access, git_snapshot, target["path"])
                try:
                    raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise _git_diff_invalid() from exc
                resolved.append(
                    {
                        **base,
                        "resolved_content_hash": hashlib.sha256(raw).hexdigest(),
                        "resolved_diff_bytes": len(raw),
                        "resolved_content_redaction_state": "pending",
                        "content_kind": "text_unified_diff",
                    }
                )
            return resolved
    except GitOperationInProgress as exc:
        raise _git_operation_in_progress() from exc
    finally:
        if access is not None:
            access.close()


def _candidate_hash_payload(result: dict) -> dict:
    profile = result["profile"]
    payload = {
        "schema_version": result["schema_version"],
        "snapshot_id": result["snapshot_id"],
        "snapshot_hash": result["snapshot_hash"],
        "project_id": result["project_id"],
        "profile": {
            "profile_id": profile["profile_id"],
            "profile_content_hash": profile["profile_content_hash"],
            "source_prd_id": profile["source_prd_id"],
            "resolved_content_redaction_state": profile[
                "resolved_content_redaction_state"
            ],
            "model_send_state": profile["model_send_state"],
        },
        "range": result["range"],
        "items": result["items"],
        "unsupported_context_sources": result["unsupported_context_sources"],
    }
    if "progress_context_hash" in profile:
        payload["profile"]["progress_context_hash"] = profile["progress_context_hash"]
    return payload


def build_context_candidate_set(snapshot_id: int) -> dict:
    """纯读构建模型无关、deterministic 的 Context Candidate Set V1。"""
    if type(snapshot_id) is not int or snapshot_id <= 0:
        raise _candidate_snapshot_unsupported()

    try:
        with get_connection() as conn:
            snapshot = _read_candidate_snapshot(conn, snapshot_id)
            profile = _read_candidate_profile(conn, snapshot)
            git_items, prd_items = _read_candidate_items(conn, snapshot)
            git_snapshot, frozen_commits = _read_historical_git_snapshot(conn, snapshot)
            frozen_records = _read_candidate_git_manifest(
                conn,
                git_snapshot=git_snapshot,
                git_items=git_items,
            )
            prd = _read_historical_prd(conn, snapshot=snapshot)
            structured = _read_exact_artifact(snapshot, prd)
            resolved_prd_items = _read_candidate_prd_items(
                snapshot=snapshot,
                structured=structured,
                prd_items=prd_items,
            )
            _validate_candidate_snapshot_integrity(conn, snapshot)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _candidate_snapshot_unsupported() from exc

    resolved_git_items = _materialize_candidate_git_items(
        snapshot=snapshot,
        git_snapshot=git_snapshot,
        frozen_commits=frozen_commits,
        frozen_records=frozen_records,
        git_items=git_items,
    )
    result = {
        "schema_version": _CANDIDATE_SCHEMA_VERSION,
        "snapshot_id": snapshot["id"],
        "snapshot_hash": snapshot["snapshot_hash"],
        "project_id": snapshot["project_id"],
        "profile": profile,
        "range": {
            "git_snapshot_id": git_snapshot["id"],
            "branch": git_snapshot["branch"],
            "from_commit": git_snapshot["from_commit"],
            "to_commit": git_snapshot["to_commit"],
            "commits": list(frozen_commits),
            "commit_count": git_snapshot["commit_count"],
            "git_facts_hash": snapshot["git_facts_hash"],
        },
        "items": [*resolved_git_items, *resolved_prd_items],
        "unsupported_context_sources": list(_CANDIDATE_UNSUPPORTED_SOURCES),
    }
    result["candidate_set_hash"] = _evidence_stable_hash(
        _candidate_hash_payload(result)
    )
    return result


def _sensitive_target_path(path: str) -> bool:
    # Resolve policy lazily to avoid the resolver/redaction import cycle.
    from app.model_send_admission import _first_match
    return _first_match(path) is not None
