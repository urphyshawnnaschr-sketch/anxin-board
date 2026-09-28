"""Compose the formal Anxin Board generation seams."""

from __future__ import annotations

from collections.abc import Mapping

from app.anxin_board_module_summaries import build_anxin_board_module_summaries
from app.anxin_board_report import DEFAULT_MANAGER_SUPPLEMENT
from app.anxin_board_report_pipeline import assemble_and_persist_anxin_board_report
from app.plain_language_change_summary_source import (
    build_plain_language_change_summary_from_snapshot,
)


def generate_and_persist_anxin_board_report(
    *,
    project_id: int,
    project_name: str,
    report_date: str,
    git_snapshot_id: int,
    module_stages: Mapping[str, str],
    manager_supplement: str = DEFAULT_MANAGER_SUPPLEMENT,
) -> dict[str, object]:
    """Generate and persist one formal report from explicit frozen inputs."""

    module_summaries = build_anxin_board_module_summaries(
        module_stages=module_stages,
    )
    daily_change = build_plain_language_change_summary_from_snapshot(
        project_id=project_id,
        git_snapshot_id=git_snapshot_id,
    )
    return assemble_and_persist_anxin_board_report(
        project_id=project_id,
        project_name=project_name,
        report_date=report_date,
        module_summaries=module_summaries,
        daily_change=daily_change,
        manager_supplement=manager_supplement,
    )
