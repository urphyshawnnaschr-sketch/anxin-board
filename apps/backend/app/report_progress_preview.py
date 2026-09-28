"""Read-only whole-project progress preview for the Human report review page."""
import sqlite3

from fastapi import HTTPException
from app.db import get_connection
from app.project_profiles import read_bound_project_profile_for_report, ProjectProfileAuthorityError
from app.project_profile_v2 import profile_planned_modules
from app.project_progress import ProjectProgressError
from app.project_progress_context import ProgressContextError
from app.project_progress_materialization import compute_updates, read_progress_inputs, ProgressMaterializationError


def build_preview(bundle):
    report = bundle["report_version"]
    if report.get("lifecycle") not in {"pending_review", "blocked"}:
        return None
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM evidence_snapshots WHERE id=?", (report["evidence_snapshot_id"],)).fetchone()
            if row is None or row["snapshot_hash"] != report["evidence_snapshot_hash"]:
                raise ProgressMaterializationError("PROJECT_PROGRESS_EVIDENCE_INVALID")
            profile = read_bound_project_profile_for_report(row["profile_id"], conn=conn)
            inputs = read_progress_inputs(conn=conn, project_id=bundle["project_id"], profile=profile, evidence_snapshot=row)
            updates = compute_updates(profile=profile, ai_raw=bundle["ai_raw"], previous=inputs["previous"],
                                      allowed_git_refs=inputs["allowed_git_refs"],
                                      allowed_evidence_refs=inputs["allowed_evidence_refs"])
            prior = {item["module_id"]: item for item in inputs["previous"]["modules"]} if inputs["previous"] else {}
            baseline = {item["planned_module_id"]: item for item in profile["content"].get("implementation_mappings", [])}
            modules = []
            for item in profile_planned_modules(profile["content"]):
                module_id = item["client_id"]
                source = prior.get(module_id, baseline.get(module_id, {}))
                previous_stage = source.get("stage") or {"implemented": "已完成", "partial": "开发中"}.get(source.get("status"), "暂时无法确认")
                update = updates.get(module_id, {})
                modules.append({"module_id": module_id, "name": item["name"], "previous_stage": previous_stage,
                                "stage": update.get("stage", previous_stage),
                                "evidence_ids": update.get("evidence_ids", source.get("evidence_ids", []))})
            refs = conn.execute("SELECT evidence_id,type,source_ref FROM evidence_items WHERE snapshot_id=? AND type='git_file_fact' ORDER BY evidence_id", (report["evidence_snapshot_id"],)).fetchall()
            return {"state": "ready", "modules": modules,
                    "evidence_refs": [dict(ref) for ref in refs if ref["evidence_id"] in inputs["allowed_git_refs"]]}
    except (ProjectProfileAuthorityError, ProjectProgressError, ProgressContextError, ProgressMaterializationError) as exc:
        return {"state": "unavailable", "code": exc.code,
                "message": str(exc) if isinstance(exc, (ProgressMaterializationError, ProgressContextError)) else
                "累计进度暂时无法核对，请重新读取当前代码范围。原报告和已确认进度均已保留。"}
    except (sqlite3.Error, KeyError, TypeError, HTTPException):
        return {"state": "unavailable", "code": "PROJECT_PROGRESS_PREVIEW_UNAVAILABLE",
                "message": "累计进度的已保存依据无法读取。原报告正文仍可查看，请先核对报告来源。"}
