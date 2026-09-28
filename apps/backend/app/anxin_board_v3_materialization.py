"""Bounded Page08 V3 materialization inside the Page07 approval transaction."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3

from app.anxin_board_report import DEFAULT_MANAGER_SUPPLEMENT
from app.anxin_board_report_store import persist_anxin_board_report_v3_in_transaction
from app.anxin_board_report_v3 import build_anxin_board_report_v3
from app.project_progress import to_module_summaries
from app.project_progress_materialization import materialize_progress, ProgressMaterializationError
from app.project_profiles import (
    ProjectProfileAuthorityError,
    read_bound_project_profile_for_report,
)


class AnxinBoardV3MaterializationError(RuntimeError):
    code = "ANXIN_BOARD_V3_MATERIALIZATION_BLOCKED"


def _fail(message: str) -> AnxinBoardV3MaterializationError:
    return AnxinBoardV3MaterializationError(message)


def materialize_approved_anxin_board_v3_in_transaction(
    *,
    conn: sqlite3.Connection,
    project_id: int,
    review_bundle: Mapping[str, object],
    approval_snapshot: Mapping[str, object],
    daily_change: Mapping[str, object],
    corrections: dict | None = None,
) -> dict[str, object]:
    """Create the one formal V3 row; caller owns commit/rollback."""
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise _fail("正式安心看板必须与最终确认处于同一数据库事务。")
    if review_bundle.get("project_id") != project_id:
        raise _fail("报告审阅项目身份无法闭合。")

    report = review_bundle.get("report_version")
    ai_raw = review_bundle.get("ai_raw")
    if not isinstance(report, Mapping) or not isinstance(ai_raw, Mapping):
        raise _fail("报告审阅数据不完整。")
    if (
        report.get("report_version_id") != approval_snapshot.get("report_version_id")
        or report.get("report_content_hash") != approval_snapshot.get("report_content_hash")
    ):
        raise _fail("ApprovalSnapshot 与 ReportVersion 不一致。")

    try:
        profile = read_bound_project_profile_for_report(
            approval_snapshot.get("profile_id"),
            conn=conn,
        )
    except ProjectProfileAuthorityError as exc:
        raise _fail("最终确认绑定的冻结 ProjectProfile 无法读取。") from exc
    if (
        profile.get("project_id") != project_id
        or profile.get("id") != approval_snapshot.get("profile_id")
        or profile.get("version_no") != approval_snapshot.get("profile_version_no")
        or profile.get("content_hash") != approval_snapshot.get("profile_content_hash")
        or profile.get("source_prd_id") != approval_snapshot.get("prd_id")
    ):
        raise _fail("冻结 ProjectProfile 与最终确认快照无法闭合。")

    project = conn.execute(
        "SELECT name FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if project is None or type(project["name"]) is not str or not project["name"].strip():
        raise _fail("项目名称 authority 无法读取。")

    supplement = review_bundle.get("current_supplement")
    if supplement is None:
        manager_supplement = DEFAULT_MANAGER_SUPPLEMENT
    elif isinstance(supplement, Mapping):
        manager_supplement = supplement.get("content")
        if type(manager_supplement) is not str or not manager_supplement.strip():
            raise _fail("人工补充内容无法闭合。")
        if (
            supplement.get("supplement_version_id")
            != approval_snapshot.get("supplement_version_id")
            or supplement.get("content_hash")
            != approval_snapshot.get("supplement_content_hash")
        ):
            raise _fail("人工补充与最终确认快照不一致。")
    else:
        raise _fail("人工补充 provenance 无效。")

    try:
        cumulative = materialize_progress(conn=conn, project_id=project_id, profile=profile, ai_raw=ai_raw,
                                          approval_snapshot=approval_snapshot, corrections=corrections)
        formal_report = build_anxin_board_report_v3(
            project_name=project["name"],
            profile=profile,
            ai_raw=ai_raw,
            daily_change=daily_change,
            manager_supplement=manager_supplement,
            approval_snapshot=approval_snapshot,
            module_summaries=to_module_summaries(cumulative),
        )
        return persist_anxin_board_report_v3_in_transaction(
            conn=conn,
            project_id=project_id,
            report=formal_report,
            approval_snapshot_id=approval_snapshot["approval_snapshot_id"],
            approval_snapshot_hash=approval_snapshot["approval_snapshot_hash"],
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ProgressMaterializationError):
            error = AnxinBoardV3MaterializationError(str(exc))
            error.code = exc.code
            error.cause_code = exc.code
            raise error from exc
        if isinstance(exc, AnxinBoardV3MaterializationError):
            raise
        raise _fail("正式安心看板 V3 无法从当前确认事实完整物化。") from exc
