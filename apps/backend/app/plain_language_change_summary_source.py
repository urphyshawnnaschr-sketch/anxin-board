"""Compose frozen development-change evidence with the plain-language summary core."""

from __future__ import annotations

from app.development_change_evidence_source import (
    build_development_change_evidence_from_snapshot,
)
from app.plain_language_change_summary import build_plain_language_change_summary


def build_plain_language_change_summary_from_snapshot(
    *,
    project_id: int,
    git_snapshot_id: int,
) -> dict[str, object]:
    """Build one exact client-facing summary from one explicit frozen GitSnapshot."""

    evidence = build_development_change_evidence_from_snapshot(
        project_id=project_id,
        git_snapshot_id=git_snapshot_id,
    )
    return build_plain_language_change_summary(evidence=evidence)
