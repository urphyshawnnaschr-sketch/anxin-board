"""Owner-only Phase 3 live preflight: real Product state, zero provider POST/API-key read.

It may read the selected model id from the existing Windows credential store through the
reviewed live adapter. It never reads the DeepSeek API key and never executes a provider
request. Output is sanitized identity/budget metadata only; no PRD/source bodies.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from fastapi import HTTPException  # noqa: E402
from app.db import get_db_path  # noqa: E402
from app.profile_reconciliation_batches import BatchError  # noqa: E402
from app.profile_reconciliation_provider import (  # noqa: E402
    prepare_product_execution,
    select_smallest_evidence_smoke_batch,
)

SCHEMA_VERSION = "profile_reconciliation_provider_preflight_v1"


def _discover_confirmed_v2() -> list[dict[str, object]]:
    db = get_db_path()
    if not db.exists():
        return []
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id,project_id,source_prd_id,content_hash,content_json "
            "FROM project_profiles WHERE status='confirmed' ORDER BY project_id,id"
        ).fetchall()
    finally:
        conn.close()
    found = []
    for row in rows:
        try:
            content = json.loads(row["content_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(content, dict) or content.get("schema_version") != "project_profile_v2":
            continue
        found.append(
            {
                "project_id": int(row["project_id"]),
                "plan_profile_id": int(row["id"]),
                "source_prd_id": int(row["source_prd_id"]),
                "plan_content_hash": str(row["content_hash"]),
                "planned_module_count": len(content.get("planned_modules") or []),
            }
        )
    return found


def _safe_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return {"type": "HTTPException", "status": exc.status_code, "code": str(detail.get("code") or "UNKNOWN")}
    if isinstance(exc, BatchError):
        return {"type": "BatchError", "code": str(exc)}
    if isinstance(exc, sqlite3.Error):
        return {"type": type(exc).__name__, "code": "PRODUCT_DB_SCHEMA_UNAVAILABLE"}
    return {"type": type(exc).__name__, "code": "UNCLASSIFIED_PREFLIGHT_FAILURE"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--plan-profile-id", type=int)
    parser.add_argument("--auto-current", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "provider_calls": 0,
        "provider_api_key_read": False,
        "provider_network_calls": 0,
    }
    try:
        candidates = _discover_confirmed_v2()
    except sqlite3.Error as exc:
        candidates = []
        result["status"] = "needs_desktop_user_context"
        result["error"] = _safe_error(exc)
    result["confirmed_v2_candidates"] = candidates
    project_id, plan_profile_id = args.project_id, args.plan_profile_id
    if args.auto_current and result.get("status") is None:
        if len(candidates) != 1:
            result["status"] = "needs_explicit_identity"
        else:
            project_id = int(candidates[0]["project_id"])
            plan_profile_id = int(candidates[0]["plan_profile_id"])
    if result.get("status") is None and (not project_id or not plan_profile_id):
        result["status"] = "needs_explicit_identity"
    if result.get("status") is None:
        try:
            prepared = prepare_product_execution(int(project_id), int(plan_profile_id))
            summary = prepared["summary"]
            smoke_index = select_smallest_evidence_smoke_batch(prepared["execution_plan"])
            request = next(item for item in summary["requests"] if item["batch_index"] == smoke_index)
            result.update(
                status="ready",
                project_id=int(project_id),
                prd_id=prepared["prd_id"],
                prd_source_hash=prepared["prd_source_hash"],
                plan_profile_id=prepared["plan_profile_id"],
                plan_content_hash=prepared["plan_content_hash"],
                exact_head=prepared["exact_head"],
                execution_identity_hash=summary["execution_identity_hash"],
                batch_set_hash=summary["batch_set_hash"],
                batch_count=summary["batch_count"],
                provider=summary["provider"],
                model_id=summary["model_id"],
                model_version=summary["model_version"],
                max_output_tokens=summary["max_output_tokens"],
                max_input_bytes=summary["max_input_bytes"],
                total_wire_bytes=summary["total_wire_bytes"],
                tracked_files=summary.get("tracked_files"),
                safe_text_bytes=summary.get("safe_text_bytes"),
                coverage=summary.get("coverage"),
                recommended_smoke_batch_index=smoke_index,
                recommended_smoke_request=request,
            )
        except Exception as exc:  # sanitized owner preflight; never echo provider/private bodies
            result["status"] = "blocked"
            result["error"] = _safe_error(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": result.get("status"),
        "candidate_count": len(candidates),
        "project_id": result.get("project_id"),
        "plan_profile_id": result.get("plan_profile_id"),
        "exact_head": result.get("exact_head"),
        "batch_count": result.get("batch_count"),
        "total_wire_bytes": result.get("total_wire_bytes"),
        "recommended_smoke_batch_index": result.get("recommended_smoke_batch_index"),
        "error": result.get("error"),
        "provider_calls": 0,
        "provider_api_key_read": False,
        "provider_network_calls": 0,
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
