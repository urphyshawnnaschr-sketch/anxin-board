"""Read APIs for persisted formal Anxin Board reports."""

import re
import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request, Response

from app import anxin_board_report_store as report_store
from app.db import get_connection
from app.local_session_api import require_local_read_request, require_local_write_request
from app.approved_report_narrative import ApprovedReportNarrativeError, load_approved_report_narrative

from app.anxin_board_report_store import (
    AnxinBoardReportHistoryInputError,
    AnxinBoardReportProjectNotFoundError,
    AnxinBoardReportStoredInvalidError,
    load_anxin_board_report_history,
    load_latest_anxin_board_report,
)
from app.projects import get_project


router = APIRouter()


def _report_not_available() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "ANXIN_BOARD_REPORT_NOT_AVAILABLE",
            "message": "当前还没有可展示的正式安心看板",
        },
    )


def _stored_report_invalid() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={
            "code": "ANXIN_BOARD_REPORT_STORED_INVALID",
            "message": "最新正式安心看板数据无法确认",
        },
    )


def _history_project_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "PROJECT_NOT_FOUND",
            "message": "项目不存在或已被删除",
        },
    )


def _history_input_invalid() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "code": "ANXIN_BOARD_REPORT_HISTORY_INPUT_INVALID",
            "message": "历史安心看板分页参数无效",
        },
    )


def _history_stored_invalid() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={
            "code": "ANXIN_BOARD_REPORT_STORED_INVALID",
            "message": "历史正式安心看板数据无法确认",
        },
    )


@router.get("/api/projects/{project_id}/anxin-board/latest")
def get_latest_anxin_board_report(project_id: int) -> dict:
    """Return only the newest validated persisted formal report."""

    get_project(project_id)
    try:
        report = load_latest_anxin_board_report(project_id=project_id)
    except AnxinBoardReportStoredInvalidError as exc:
        raise _stored_report_invalid() from exc
    if report is None:
        raise _report_not_available()
    return report


@router.get("/api/projects/{project_id}/anxin-board/history")
def get_anxin_board_report_history(
    project_id: int,
    before_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return one validated history page from the formal History Read seam."""

    try:
        return load_anxin_board_report_history(
            project_id=project_id,
            before_id=before_id,
            limit=limit,
        )
    except AnxinBoardReportProjectNotFoundError as exc:
        raise _history_project_not_found() from exc
    except AnxinBoardReportHistoryInputError as exc:
        raise _history_input_invalid() from exc
    except AnxinBoardReportStoredInvalidError as exc:
        raise _history_stored_invalid() from exc


@router.get("/api/projects/{project_id}/anxin-board/reports/{report_version_id}/narrative")
def get_approved_narrative(project_id: int, report_version_id: int, report_hash: str, request: Request, response: Response) -> dict:
    """Read exact approved work details without modifying a historical V3 report."""
    require_local_read_request(request)
    response.headers["Cache-Control"] = "no-store"
    if (project_id <= 0 or project_id > 2**63 - 1 or report_version_id <= 0
            or report_version_id > 2**63 - 1 or not re.fullmatch(r"[0-9a-f]{64}", report_hash)):
        raise HTTPException(400, detail={"code": "APPROVED_REPORT_NARRATIVE_INPUT_INVALID", "message": "报告版本标识无效。"})
    try:
        with closing(get_connection()) as conn:
            conn.execute("BEGIN")
            project = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise _history_project_not_found()
            columns = report_store._read_columns(conn)
            rows = conn.execute(
                f"SELECT {columns} FROM anxin_board_reports WHERE project_id = ? AND report_hash = ?",
                (project_id, report_hash),
            ).fetchall()
            if len(rows) != 1:
                raise HTTPException(404, detail={"code": "APPROVED_REPORT_NARRATIVE_NOT_AVAILABLE", "message": "没有找到与此版本对应的已审核工作说明。"})
            _, report = report_store._validate_stored_row(
                rows[0], conn=conn, expected_project_id=project_id,
                expected_project_name=project["name"], require_current_profile=False,
            )
            if report.get("schema_version") != "anxin_board_report_v3" or report.get("report_version_id") != report_version_id:
                raise HTTPException(409, detail={"code": "APPROVED_REPORT_NARRATIVE_IDENTITY_MISMATCH", "message": "工作说明与当前报告版本不一致，请重新读取。"})
            approval = report_store._read_approval_snapshot_for_v3(
                conn, project_id=project_id, approval_snapshot_id=report["approval_snapshot_id"],
                approval_snapshot_hash=report["approval_snapshot_hash"],
            )
            return load_approved_report_narrative(
                project_id=project_id, report=report, approval_snapshot=approval, conn=conn,
            )
    except ApprovedReportNarrativeError as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": "本版具体工作说明暂时无法安全读取，未使用其他版本内容替代。"}) from exc
    except (AnxinBoardReportStoredInvalidError, sqlite3.Error) as exc:
        raise HTTPException(409, detail={"code": "APPROVED_REPORT_NARRATIVE_INVALID", "message": "本版具体工作说明与审核记录无法核对，请检查报告状态。"}) from exc


def _module_narrative_failure(exc):
    from app.approved_module_narrative import ApprovedModuleNarrativeError
    code = exc.code if isinstance(exc, ApprovedModuleNarrativeError) else 'APPROVED_MODULE_NARRATIVE_INVALID'
    status = 400 if code.endswith('INPUT_INVALID') else 404 if code.endswith('NOT_AVAILABLE') else 409
    return HTTPException(status, detail={'code': code, 'message': '模块说明与本版报告或已确认来源无法核对，未使用其他版本内容替代。'})


@router.get('/api/projects/{project_id}/anxin-board/reports/{report_version_id}/module-narrative')
def get_confirmed_module_narrative(project_id: int, report_version_id: int, report_hash: str, request: Request, response: Response):
    from app.approved_module_narrative import get_approved_module_narrative, load_module_narrative_target
    require_local_read_request(request)
    response.headers['Cache-Control'] = 'no-store'
    try:
        with closing(get_connection()) as conn:
            conn.execute('BEGIN')
            report, approval, profile = load_module_narrative_target(conn, project_id, report_version_id, report_hash)
            return get_approved_module_narrative(project_id, report, approval, profile, conn=conn)
    except Exception as exc:
        raise _module_narrative_failure(exc) from exc


@router.post('/api/projects/{project_id}/anxin-board/reports/{report_version_id}/module-narrative')
def confirm_module_narrative(project_id: int, report_version_id: int, payload: dict, request: Request, response: Response):
    from app.approved_module_narrative import confirm_approved_module_narrative
    # The immutable domain record closes the body idempotency key to all supplied text and identities.
    require_local_write_request(request, require_idempotency_key=False)
    response.headers['Cache-Control'] = 'no-store'
    try:
        return confirm_approved_module_narrative(project_id=project_id, report_version_id=report_version_id, payload=payload)
    except Exception as exc:
        raise _module_narrative_failure(exc) from exc


@router.get('/api/projects/{project_id}/anxin-board/reports/{report_version_id}/git-metrics')
def get_approved_git_metrics(project_id: int, report_version_id: int, report_hash: str, request: Request, response: Response):
    from app.approved_module_narrative import load_module_narrative_target
    from app.approved_report_git_metrics import ApprovedReportGitMetricsError, load_approved_report_git_metrics
    require_local_read_request(request)
    response.headers['Cache-Control'] = 'no-store'
    try:
        with closing(get_connection()) as conn:
            conn.execute('BEGIN')
            report, approval, _ = load_module_narrative_target(conn, project_id, report_version_id, report_hash)
            return load_approved_report_git_metrics(project_id, report, approval, conn=conn)
    except Exception as exc:
        code = exc.code if isinstance(exc, ApprovedReportGitMetricsError) else 'APPROVED_REPORT_GIT_METRICS_INVALID'
        raise HTTPException(409, detail={'code': code, 'message': '本版已冻结的代码变动数据无法核对，未使用零值或其他版本数据替代。'}) from exc
