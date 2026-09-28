"""Lightweight frozen EvidenceSnapshot identity closure for task/status paths.

This seam intentionally validates only persisted snapshot identity/integrity.  It must not
materialize historical Git bodies or acquire the project Git workspace lock.  Full Git replay
remains owned by Context Candidate / model-call preparation before any provider send gate.
"""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app.context_resolver import (
    _read_candidate_snapshot,
    _validate_candidate_snapshot_integrity,
)
from app.db import get_connection


def validate_context_snapshot_identity(snapshot_id: int) -> dict[str, object]:
    """Validate one frozen snapshot from durable DB facts without touching Git workspace."""
    try:
        with get_connection() as conn:
            snapshot = _read_candidate_snapshot(conn, snapshot_id)
            _validate_candidate_snapshot_integrity(conn, snapshot)
    except HTTPException:
        raise
    except sqlite3.Error:
        raise

    return {
        "project_id": snapshot["project_id"],
        "snapshot_id": snapshot["id"],
        "snapshot_hash": snapshot["snapshot_hash"],
    }
