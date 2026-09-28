"""Low-friction Page 07 validation over already-durable report facts.

Viewing/approving an existing report must not replay Git workspaces or reopen PRD files.
Only durable identity corruption and genuinely unsafe report states remain hard blockers.
Normal project evolution and optional AI contradiction checking are advisories.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from fastapi import HTTPException

from app import report_validation as base
from app.report_review_durable_read import get_review_bundle as get_durable_review_bundle


_ADVISORY_BLOCKER_CODES = {
    "PROJECT_GIT_AUTHORITY_DRIFT",
    "ANALYSIS_LINEAGE_AUTHORITY_DRIFT",
    "CURRENT_PRD_AUTHORITY_DRIFT",
    "CURRENT_PROFILE_AUTHORITY_DRIFT",
    "REPORT_CONTRADICTION_CHECK_REQUIRED",
    "REPORT_CONTRADICTION_BLOCKED",
    "REPORT_STAGE_EVIDENCE_NOT_CLOSED",
}


_VALIDATION_POLICY_VERSION = "page07_low_friction_v5"


def _hard_blockers(blockers):
    return [item for item in blockers if item.get("code") not in _ADVISORY_BLOCKER_CODES]


def _validation_candidate_hash(
    bundle: Mapping[str, object],
    *,
    git_snapshot_id: int,
    git_facts_hash: str,
    candidate_set_hash: str,
) -> str:
    """Version the ValidationResult identity when approval semantics change."""
    return base._stable_hash(
        {
            "validation_policy_version": _VALIDATION_POLICY_VERSION,
            "candidate": base._candidate_payload(
                bundle,
                git_snapshot_id=git_snapshot_id,
                git_facts_hash=git_facts_hash,
                candidate_set_hash=candidate_set_hash,
            ),
        }
    )


def _durable_binding(conn: sqlite3.Connection, bundle: Mapping[str, object]) -> dict[str, object]:
    report = bundle["report_version"]
    snapshot = base._read_snapshot_authority(conn, report["evidence_snapshot_id"])
    git_facts = bundle.get("git_facts")
    if not isinstance(git_facts, Mapping):
        raise base._stored_invalid("冻结 Git facts 不可用。")
    git_snapshot_id = git_facts.get("git_snapshot_id")
    git_facts_hash = git_facts.get("git_facts_hash")
    git_branch = git_facts.get("branch")
    git_from_commit = git_facts.get("from_commit")
    git_to_commit = git_facts.get("to_commit")
    if (
        type(git_snapshot_id) is not int or git_snapshot_id <= 0
        or not base._is_hash(git_facts_hash)
        or any(type(value) is not str or not value for value in (git_branch, git_from_commit, git_to_commit))
    ):
        raise base._stored_invalid("冻结 Git identity 不完整。")
    expected_snapshot = {
        "id": report["evidence_snapshot_id"],
        "project_id": bundle["project_id"],
        "snapshot_hash": report["evidence_snapshot_hash"],
        "git_snapshot_id": git_snapshot_id,
        "git_facts_hash": git_facts_hash,
    }
    if any(snapshot.get(key) != value for key, value in expected_snapshot.items()):
        raise base._stored_invalid("ReportVersion 与冻结 Evidence/Git identity 不一致。")

    model_result = base.get_model_execution_result(report["model_execution_result_id"])
    result_expected = {
        "model_result_id": report["model_execution_result_id"],
        "project_id": bundle["project_id"],
        "snapshot_id": report["evidence_snapshot_id"],
        "execution_result_hash": report["execution_result_hash"],
        "model_call_id": report["model_call_id"],
        "call_identity_hash": report["call_identity_hash"],
        "validated_result_hash": report["report_content_hash"],
    }
    if any(model_result.get(key) != value for key, value in result_expected.items()):
        raise base._stored_invalid("ReportVersion 与 durable ModelExecutionResult identity 不一致。")

    model_call = base.get_model_call(report["model_call_id"])
    call_expected = {
        "model_call_id": report["model_call_id"],
        "project_id": bundle["project_id"],
        "snapshot_id": report["evidence_snapshot_id"],
        "snapshot_hash": report["evidence_snapshot_hash"],
        "call_identity_hash": report["call_identity_hash"],
    }
    if any(model_call.get(key) != value for key, value in call_expected.items()):
        raise base._stored_invalid("ReportVersion 与 durable ModelCall identity 不一致。")
    candidate_set_hash = model_call.get("candidate_set_hash")
    if not base._is_hash(candidate_set_hash):
        raise base._stored_invalid("durable ModelCall candidate_set_hash 无效。")

    profile_row = conn.execute(
        "SELECT project_id, version_no, source_prd_id, content_hash FROM project_profiles WHERE id = ?",
        (snapshot["profile_id"],),
    ).fetchone()
    if (
        profile_row is None
        or profile_row["project_id"] != bundle["project_id"]
        or profile_row["source_prd_id"] != snapshot["prd_id"]
        or profile_row["content_hash"] != snapshot["profile_content_hash"]
        or type(profile_row["version_no"]) is not int
        or profile_row["version_no"] <= 0
    ):
        raise base._stored_invalid("冻结 ProjectProfile identity 不完整。")

    return {
        "snapshot": snapshot,
        "model_result": model_result,
        "model_call": model_call,
        "candidate_set_hash": candidate_set_hash,
        "git_snapshot_id": git_snapshot_id,
        "git_facts_hash": git_facts_hash,
        "git_branch": git_branch,
        "git_from_commit": git_from_commit,
        "git_to_commit": git_to_commit,
        "profile_version_no": profile_row["version_no"],
    }


def _semantic_checks(bundle: Mapping[str, object]):
    checks = []
    blockers = []
    report = bundle["report_version"]
    ai_raw = bundle["ai_raw"]
    lifecycle_ok = report.get("lifecycle") in base._APPROVABLE_REPORT_LIFECYCLES
    checks.append(base._check("report_lifecycle_approvable", lifecycle_ok))
    if not lifecycle_ok:
        blockers.append(base._blocker("REPORT_VERSION_NOT_APPROVABLE", "当前报告版本已失效或已确认。"))

    content = ai_raw.get("content")
    if isinstance(content, Mapping) and ai_raw.get("task_type") == "daily_report_regenerate":
        payload = content.get("new_report")
    else:
        payload = content
    feature_progress = payload.get("feature_progress") if isinstance(payload, Mapping) else None
    stages_ok = type(feature_progress) is list
    if stages_ok:
        for item in feature_progress:
            if not isinstance(item, Mapping):
                stages_ok = False
                break
            if item.get("stage") == "已完成":
                ids = item.get("evidence_ids")
                if type(ids) is not list or not ids:
                    stages_ok = False
                    break
    checks.append(base._check("conservative_stage_evidence", stages_ok))
    if not stages_ok:
        blockers.append(base._blocker("REPORT_STAGE_EVIDENCE_NOT_CLOSED", "报告把功能标记为已完成但没有冻结证据引用。"))

    # PM supplement is human-owned truth. AI contradiction analysis is optional advice,
    # never a prerequisite for accepting the human's explicit final approval.
    checks.append(base._check(
        "supplement_contradiction_optional",
        bundle.get("current_supplement") is None,
    ))
    return checks, blockers


def build_approval_closure(*, conn: sqlite3.Connection, bundle: Mapping[str, object]) -> dict[str, object]:
    report = bundle["report_version"]
    base._assert_current_supplement_identity(conn, bundle)
    binding = _durable_binding(conn, bundle)
    candidate_hash = _validation_candidate_hash(
        bundle,
        git_snapshot_id=binding["git_snapshot_id"],
        git_facts_hash=binding["git_facts_hash"],
        candidate_set_hash=binding["candidate_set_hash"],
    )
    authority, authority_checks, authority_blockers = base._read_current_local_authority(
        conn=conn,
        project_id=int(bundle["project_id"]),
        snapshot=binding["snapshot"],
        model_call=binding["model_call"],
    )
    semantic_checks, semantic_blockers = _semantic_checks(bundle)
    blockers = _hard_blockers([*authority_blockers, *semantic_blockers])
    return {
        "state": "blocked" if blockers else "passed",
        "candidate_hash": candidate_hash,
        "current_authority_hash": base._stable_hash(authority),
        "git_snapshot_id": binding["git_snapshot_id"],
        "git_facts_hash": binding["git_facts_hash"],
        "git_branch": binding["git_branch"],
        "git_from_commit": binding["git_from_commit"],
        "git_to_commit": binding["git_to_commit"],
        "profile_version_no": binding["profile_version_no"],
        "snapshot": binding["snapshot"],
        "model_call": binding["model_call"],
        "current_authority": authority,
        "checks": sorted([*authority_checks, *semantic_checks], key=lambda item: str(item["code"])),
        "blockers": sorted(blockers, key=lambda item: (item["code"], item["message"])),
    }


def create_validation_result(*, project_id: int, report_version_id: int, expected_report_state_version: int) -> dict[str, object]:
    """Local durable integrity check. No Git workspace replay, PRD reopening, or provider call."""
    project_id = base._positive_id(project_id, "project_id")
    report_version_id = base._positive_id(report_version_id, "report_version_id")
    expected_report_state_version = base._positive_id(expected_report_state_version, "expected_report_state_version")
    try:
        base.ensure_report_validation_schema()
    except sqlite3.Error as exc:
        raise base._stored_invalid("ValidationResult schema 初始化失败。") from exc

    bundle = get_durable_review_bundle(project_id=project_id, report_version_id=report_version_id)
    report = bundle["report_version"]
    if (
        report["state_version"] != expected_report_state_version
        or report.get("lifecycle") not in base._APPROVABLE_REPORT_LIFECYCLES
    ):
        raise base._stale()

    candidate_hash = None
    current_authority_hash = None
    try:
        with base.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute(
                "SELECT lifecycle, state_version FROM report_versions WHERE id = ? AND project_id = ?",
                (report_version_id, project_id),
            ).fetchone()
            if (
                fresh is None
                or fresh["state_version"] != expected_report_state_version
                or fresh["lifecycle"] not in base._APPROVABLE_REPORT_LIFECYCLES
            ):
                raise base._stale()
            closure = build_approval_closure(conn=conn, bundle=bundle)
            candidate_hash = closure["candidate_hash"]
            current_authority_hash = closure["current_authority_hash"]
            existing = conn.execute(
                f"SELECT {base._VALIDATION_COLUMNS} FROM {base._VALIDATION_TABLE} WHERE candidate_hash = ? AND current_authority_hash = ?",
                (candidate_hash, current_authority_hash),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"validation_result": base._close_row(existing), "created": False}

            supplement = bundle.get("current_supplement")
            checks = closure["checks"]
            blockers = closure["blockers"]
            stored = {
                "schema_version": base.SCHEMA_VERSION,
                "project_id": project_id,
                "report_version_id": report_version_id,
                "report_state_version": report["state_version"],
                "report_content_hash": report["report_content_hash"],
                "supplement_version_id": None if supplement is None else supplement["supplement_version_id"],
                "supplement_content_hash": None if supplement is None else supplement["content_hash"],
                "evidence_snapshot_id": report["evidence_snapshot_id"],
                "evidence_snapshot_hash": report["evidence_snapshot_hash"],
                "git_snapshot_id": closure["git_snapshot_id"],
                "git_facts_hash": closure["git_facts_hash"],
                "model_execution_result_id": report["model_execution_result_id"],
                "execution_result_hash": report["execution_result_hash"],
                "model_call_id": report["model_call_id"],
                "call_identity_hash": report["call_identity_hash"],
                "candidate_hash": candidate_hash,
                "current_authority_hash": current_authority_hash,
                "state": "blocked" if blockers else "passed",
                "checks": checks,
                "blockers": blockers,
                "created_at": base._now(),
            }
            stored["result_hash"] = base._stable_hash(base._result_hash_payload(stored))
            cursor = conn.execute(
                """
                INSERT INTO report_validation_results (
                    schema_version, project_id, report_version_id, report_state_version,
                    report_content_hash, supplement_version_id, supplement_content_hash,
                    evidence_snapshot_id, evidence_snapshot_hash, git_snapshot_id,
                    git_facts_hash, model_execution_result_id, execution_result_hash,
                    model_call_id, call_identity_hash, candidate_hash,
                    current_authority_hash, state, checks_json, blockers_json,
                    created_at, result_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["schema_version"], stored["project_id"], stored["report_version_id"], stored["report_state_version"],
                    stored["report_content_hash"], stored["supplement_version_id"], stored["supplement_content_hash"],
                    stored["evidence_snapshot_id"], stored["evidence_snapshot_hash"], stored["git_snapshot_id"], stored["git_facts_hash"],
                    stored["model_execution_result_id"], stored["execution_result_hash"], stored["model_call_id"], stored["call_identity_hash"],
                    stored["candidate_hash"], stored["current_authority_hash"], stored["state"],
                    base._canonical_json(checks), base._canonical_json(blockers), stored["created_at"], stored["result_hash"],
                ),
            )
            row = conn.execute(
                f"SELECT {base._VALIDATION_COLUMNS} FROM {base._VALIDATION_TABLE} WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if row is None:
                raise base._stored_invalid("ValidationResult INSERT 后无法回读。")
            conn.commit()
            return {"validation_result": base._close_row(row), "created": True}
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        if candidate_hash is not None and current_authority_hash is not None:
            with base.get_connection() as conn:
                winner = conn.execute(
                    f"SELECT {base._VALIDATION_COLUMNS} FROM {base._VALIDATION_TABLE} WHERE candidate_hash = ? AND current_authority_hash = ?",
                    (candidate_hash, current_authority_hash),
                ).fetchone()
            if winner is not None:
                return {"validation_result": base._close_row(winner), "created": False}
        raise base._stored_invalid("ValidationResult 并发写入冲突无法闭合。")
    except sqlite3.Error as exc:
        raise base._stored_invalid("ValidationResult 持久化失败。") from exc
