"""Owner-only Quality V1 preflight: real Product state, zero provider POST/API-key read.

This script proves what exact-HEAD repository evidence the Quality V1 software would
prepare for a selected module. It may read only the selected model id through the
reviewed adapter. It never reads the DeepSeek API key and never sends a provider request.
No PRD/source bodies are emitted; only sanitized identities, paths, roles and hashes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from fastapi import HTTPException  # noqa: E402
from app.db import get_db_path  # noqa: E402
from app.profile_reconciliation_batches import BatchError  # noqa: E402
from app.profile_reconciliation_provider_quality import prepare_product_quality_execution  # noqa: E402

SCHEMA_VERSION = "profile_reconciliation_provider_quality_preflight_v1"
DEFAULT_MODULE_ID = "FR-03-prd-preview"


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
    return {"type": type(exc).__name__, "code": "UNCLASSIFIED_QUALITY_PREFLIGHT_FAILURE"}


def _find_module_batch(prepared: dict[str, object], module_id: str) -> dict[str, object]:
    matches = [
        batch
        for batch in prepared["batches"]
        if len(batch.get("planned_modules", [])) == 1
        and batch["planned_modules"][0].get("client_id") == module_id
    ]
    if len(matches) != 1:
        raise BatchError("QUALITY_PREFLIGHT_MODULE_NOT_UNIQUE")
    return matches[0]


def _sanitized_batch(batch: dict[str, object], summary: dict[str, object]) -> dict[str, object]:
    request = next(
        (item for item in summary["requests"] if item.get("batch_id") == batch.get("batch_id")),
        None,
    )
    if request is None:
        raise BatchError("QUALITY_PREFLIGHT_REQUEST_NOT_FOUND")
    evidence = []
    for item in batch.get("repo_evidence", []):
        evidence.append(
            {
                "evidence_id": item.get("evidence_id"),
                "path": item.get("path"),
                "role": item.get("quality_evidence_role"),
                "role_binding_id": item.get("quality_role_binding_id"),
                "content_hash": item.get("content_hash"),
            }
        )
    counts = Counter(str(item.get("role")) for item in evidence)
    retrieval = dict(batch.get("quality_retrieval") or {})
    requirement_count = int(retrieval.get("requirement_count") or 0)
    candidates_without_source = int(retrieval.get("requirements_without_source_matches") or 0)
    packed_missing = retrieval.get("requirements_without_selected_source")
    packed_without_source = requirement_count if packed_missing is None else int(packed_missing)
    packed_with_source = int(retrieval.get("requirements_with_selected_source") or 0)
    selected_source_indexes = list(retrieval.get("selected_source_requirement_indexes") or [])
    source_count = counts.get("source_code", 0)
    test_count = counts.get("test_code", 0)
    ready = (
        requirement_count > 0
        and candidates_without_source == 0
        and packed_without_source == 0
        and packed_with_source == requirement_count
        and len(selected_source_indexes) == requirement_count
        and source_count > 0
    )
    return {
        "batch_index": batch.get("batch_index"),
        "batch_id": batch.get("batch_id"),
        "module_id": batch["planned_modules"][0].get("client_id"),
        "wire_bytes": request.get("wire_bytes"),
        "requirement_count": requirement_count,
        "requirements_with_source_matches": retrieval.get("requirements_with_source_matches"),
        "requirements_without_source_matches": candidates_without_source,
        "requirements_with_selected_source": packed_with_source,
        "requirements_without_selected_source": packed_without_source,
        "selected_source_requirement_indexes": selected_source_indexes,
        "selected_evidence_count": len(evidence),
        "selected_role_counts": dict(sorted(counts.items())),
        "source_code_evidence_count": source_count,
        "test_code_evidence_count": test_count,
        "budget_omitted_count": int(retrieval.get("budget_omitted_count") or 0),
        "software_evidence_readiness": "ready" if ready else "insufficient_source_evidence",
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--plan-profile-id", type=int)
    parser.add_argument("--module-id", default=DEFAULT_MODULE_ID)
    parser.add_argument("--auto-current", action="store_true")
    parser.add_argument("--include-coverage-plan", action="store_true",
                        help="Locally plan uncovered safe text; does not authorize or dispatch a provider")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "provider_calls": 0,
        "provider_api_key_read": False,
        "provider_network_calls": 0,
        "candidate_aggregation_performed": False,
        "authorization_created": False,
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
            prepared = prepare_product_quality_execution(int(project_id), int(plan_profile_id),
                                                        include_coverage_plan=args.include_coverage_plan)
            summary = prepared["summary"]
            batch = _find_module_batch(prepared, args.module_id)
            target = _sanitized_batch(batch, summary)
            result.update(
                status="ready" if target["software_evidence_readiness"] == "ready" else "blocked",
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
                retrieval_coverage=summary.get("retrieval_coverage"),
                coverage_supplement=summary.get("coverage_supplement"),
                target_module=target,
            )
        except Exception as exc:  # sanitized owner preflight; never echo PRD/private source bodies
            result["status"] = "blocked"
            result["error"] = _safe_error(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    target = result.get("target_module") if isinstance(result.get("target_module"), dict) else {}
    print(json.dumps({
        "status": result.get("status"),
        "candidate_count": len(candidates),
        "project_id": result.get("project_id"),
        "plan_profile_id": result.get("plan_profile_id"),
        "exact_head": result.get("exact_head"),
        "batch_count": result.get("batch_count"),
        "module_id": target.get("module_id"),
        "software_evidence_readiness": target.get("software_evidence_readiness"),
        "source_code_evidence_count": target.get("source_code_evidence_count"),
        "test_code_evidence_count": target.get("test_code_evidence_count"),
        "requirements_without_source_matches": target.get("requirements_without_source_matches"),
        "requirements_without_selected_source": target.get("requirements_without_selected_source"),
        "error": result.get("error"),
        "provider_calls": 0,
        "provider_api_key_read": False,
        "provider_network_calls": 0,
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
