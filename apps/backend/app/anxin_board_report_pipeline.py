"""Compose the formal Anxin Board assembler and persistence seams."""

from __future__ import annotations

from collections.abc import Mapping

from app.anxin_board_report import DEFAULT_MANAGER_SUPPLEMENT, build_anxin_board_report
from app.anxin_board_report_store import (
    AnxinBoardReportProjectNotFoundError,
    persist_anxin_board_report,
)
from app.anxin_board_report_v2 import build_anxin_board_report_v2
from app.project_profiles import read_current_confirmed_project_profile


_SQLITE_INTEGER_MAX = 2**63 - 1


def _require_project_id(project_id: int) -> None:
    if (
        type(project_id) is not int
        or project_id <= 0
        or project_id > _SQLITE_INTEGER_MAX
    ):
        raise AnxinBoardReportProjectNotFoundError()


def assemble_and_persist_anxin_board_report(
    *,
    project_id: int,
    project_name: str,
    report_date: str,
    module_summaries: list[Mapping[str, object]],
    daily_change: Mapping[str, object],
    manager_supplement: str = DEFAULT_MANAGER_SUPPLEMENT,
) -> dict[str, object]:
    """Assemble the historical V1 path and persist that exact report once."""

    _require_project_id(project_id)
    report = build_anxin_board_report(
        project_name=project_name,
        report_date=report_date,
        module_summaries=module_summaries,
        daily_change=daily_change,
        manager_supplement=manager_supplement,
    )
    return persist_anxin_board_report(project_id=project_id, report=report)


def assemble_and_persist_anxin_board_report_v2(
    *,
    project_id: int,
    project_name: str,
    report_date: str,
    module_summaries: list[Mapping[str, object]],
    daily_change: Mapping[str, object],
    manager_supplement: str = DEFAULT_MANAGER_SUPPLEMENT,
) -> dict[str, object]:
    """Assemble successor V2 from the current confirmed Profile module authority.

    The caller supplies stage-summary content only. Module id/name/order and bound
    Profile identity are read internally and are rechecked again by persistence.
    """

    _require_project_id(project_id)
    profile = read_current_confirmed_project_profile(project_id)
    report = build_anxin_board_report_v2(
        project_name=project_name,
        report_date=report_date,
        profile=profile,
        module_summaries=module_summaries,
        daily_change=daily_change,
        manager_supplement=manager_supplement,
    )
    return persist_anxin_board_report(project_id=project_id, report=report)
