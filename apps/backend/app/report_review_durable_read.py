"""Read-only Page 07 projection from already-durable report facts.

Basic report viewing must not replay Git workspaces or reopen PRD artifacts. The
irreversible/send-time paths keep their stronger source authority checks elsewhere.
"""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app import context_resolver
from app import report_review as base
from app.db import get_connection
from app.model_call_ledger import get_model_call
from app.model_execution_results import get_model_execution_result


def _load_durable_report(report_version_id: int):
    """Close ReportVersion against durable ModelCall/ModelExecutionResult only."""
    try:
        with get_connection() as conn:
            row = base._read_report_by_id(conn, report_version_id)
            if row is None:
                raise base._report_not_found()
            model_execution_result_id = row["model_execution_result_id"]
            model_call_id = row["model_call_id"]
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise base._stored_invalid() from exc

    try:
        model_result = get_model_execution_result(model_execution_result_id)
    except HTTPException as exc:
        raise base._stored_invalid(
            "ReportVersion 引用的 ModelExecutionResult 无法完成 durable verified read。"
        ) from exc
    try:
        call = get_model_call(model_call_id)
    except HTTPException as exc:
        raise base._stored_invalid(
            "ReportVersion 引用的 ModelCall 无法完成 durable verified read。"
        ) from exc

    if (
        call.get("model_call_id") != model_call_id
        or call.get("project_id") != row["project_id"]
        or call.get("snapshot_id") != row["evidence_snapshot_id"]
        or call.get("call_identity_hash") != row["call_identity_hash"]
    ):
        raise base._stored_invalid("ReportVersion 与 durable ModelCall 身份发生漂移。")

    report = base._close_report_row(
        row,
        model_result=model_result,
        candidate={"snapshot_hash": call["snapshot_hash"]},
    )
    return report, model_result, call


def _frozen_git_projection(report):
    """Read hash-checked frozen Git metadata from SQLite only; never touches a workspace."""
    try:
        with get_connection() as conn:
            snapshot = context_resolver._read_git_snapshot_context(
                conn, report["evidence_snapshot_id"]
            )
            if snapshot.get("project_id") != report["project_id"]:
                return None
            git_snapshot, commits = context_resolver._read_historical_git_snapshot(
                conn, snapshot
            )
    except (HTTPException, sqlite3.Error):
        return None
    return {
        "git_snapshot_id": git_snapshot["id"],
        "branch": git_snapshot["branch"],
        "from_commit": git_snapshot["from_commit"],
        "to_commit": git_snapshot["to_commit"],
        "commits": list(commits),
        "commit_count": git_snapshot["commit_count"],
        "changed_file_count": git_snapshot["changed_file_count"],
        "added_lines": git_snapshot["added_lines"],
        "deleted_lines": git_snapshot["deleted_lines"],
        "git_facts_hash": snapshot["git_facts_hash"],
        "file_manifest_hash": git_snapshot["file_manifest_hash"],
    }


def _durable_evidence_refs(ai_content):
    """Expose only evidence IDs already frozen inside the verified model result."""
    return [
        {
            "evidence_id": evidence_id,
            "type": "frozen_reference",
            "source_ref": None,
            "content_hash": None,
            "redaction_state": "not_replayed",
        }
        for evidence_id in sorted(base._collect_evidence_ids(ai_content))
    ]


def get_review_bundle(*, project_id: int, report_version_id: int) -> dict[str, object]:
    """Read an already-generated report without requiring live Git/PRD replay."""
    project_id = base._require_positive_id(project_id, "project_id")
    report_version_id = base._require_positive_id(report_version_id, "report_version_id")
    report, model_result, _call = _load_durable_report(report_version_id)
    if report["project_id"] != project_id:
        raise base._report_not_found()

    try:
        with get_connection() as conn:
            latest = base._read_latest_supplement(conn, report_version_id)
            current_supplement = None if latest is None else base._close_supplement(latest)
    except sqlite3.Error as exc:
        raise base._stored_invalid() from exc

    git_facts = _frozen_git_projection(report)
    details_state = "frozen_git" if git_facts is not None else "unavailable"
    source_message = (
        "报告正文已从持久化且哈希闭合的模型结果读取；Git 范围来自冻结数据库事实。"
        "本页不会为了展示报告重新占用 Git 工作区或重读 PRD 文件。"
        if git_facts is not None
        else
        "报告正文已从持久化且哈希闭合的模型结果读取。原始 Git/PRD 技术详情当前不可展开，"
        "但不会因此隐藏已经生成的报告；需要校验、重分析或真实发送时再重新检查来源。"
    )

    ai_raw = {
        "model_execution_result_id": model_result["model_result_id"],
        "execution_result_hash": model_result["execution_result_hash"],
        "formal_response_hash": model_result["formal_response_hash"],
        "validated_result_hash": model_result["validated_result_hash"],
        "model_call_id": model_result["model_call_id"],
        "call_identity_hash": model_result["call_identity_hash"],
        "snapshot_id": model_result["snapshot_id"],
        "local_task_id": model_result["local_task_id"],
        "task_type": model_result["task_type"],
        "provider": model_result["provider"],
        "model_id": model_result["model_id"],
        "model_version": model_result["model_version"],
        "actual_model": model_result["actual_model"],
        "provider_runtime_fingerprint": model_result["provider_runtime_fingerprint"],
        "content": model_result["validated_result"],
    }
    return {
        "schema_version": base.REVIEW_BUNDLE_SCHEMA_VERSION,
        "project_id": project_id,
        "report_version": report,
        "ai_raw": ai_raw,
        "evidence_snapshot": {
            "snapshot_id": report["evidence_snapshot_id"],
            "snapshot_hash": report["evidence_snapshot_hash"],
        },
        "git_facts": git_facts or {},
        "evidence_refs": _durable_evidence_refs(model_result["validated_result"]),
        "current_supplement": current_supplement,
        "source_read": {
            "state": "durable_verified",
            "details_state": details_state,
            "message": source_message,
        },
    }


def get_current_review_bundle(*, project_id: int) -> dict[str, object]:
    """Read current immutable ReportVersion from durable facts only."""
    project_id = base._require_positive_id(project_id, "project_id")
    try:
        with get_connection() as conn:
            report_version_id = base._read_current_report_id(conn, project_id)
    except sqlite3.Error as exc:
        raise base._stored_invalid() from exc
    if report_version_id is None:
        raise base._report_not_found()
    return get_review_bundle(
        project_id=project_id,
        report_version_id=report_version_id,
    )
