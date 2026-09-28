"""Page 07 deterministic ValidationResult Slice 3.

Owns immutable validation results and deterministic current-authority drift closure.
It never executes a model/provider, mutates AI raw, captures new evidence, or
creates an ApprovalSnapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Mapping

from fastapi import HTTPException

from app.context_resolver import build_context_candidate_set
from app.db import ensure_report_validation_schema, get_connection
from app.model_call_ledger import get_model_call
from app.model_execution_results import get_model_execution_result
from app.project_profiles import (
    ProjectProfileAuthorityError,
    read_current_confirmed_project_profile,
)
from app.projects import _normalize_git_url
from app.report_review import get_review_bundle


SCHEMA_VERSION = "report_validation_result_v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_STATES = {"passed", "blocked"}
_APPROVABLE_REPORT_LIFECYCLES = {"pending_review", "blocked"}
_VALIDATION_TABLE = "report_validation_results"

_VALIDATION_COLUMNS = (
    "id, schema_version, project_id, report_version_id, report_state_version, "
    "report_content_hash, supplement_version_id, supplement_content_hash, "
    "evidence_snapshot_id, evidence_snapshot_hash, git_snapshot_id, git_facts_hash, "
    "model_execution_result_id, execution_result_hash, model_call_id, call_identity_hash, "
    "candidate_hash, current_authority_hash, state, checks_json, blockers_json, "
    "created_at, result_hash"
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str) -> HTTPException:
    return _error(400, "REPORT_VALIDATION_INPUT_INVALID", message)


def _stale() -> HTTPException:
    return _error(
        409,
        "REPORT_VALIDATION_STALE_CANDIDATE",
        "报告审阅候选已变化，请重新加载后再验证。",
    )


def _stored_invalid(message: str = "ValidationResult 持久化事实无法完成自校验。") -> HTTPException:
    return _error(409, "REPORT_VALIDATION_STORED_INVALID", message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _stored_invalid("ValidationResult canonical JSON 无法生成。") from exc


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _positive_id(value: object, field: str) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise _input_invalid(f"{field} 必须是 SQLite signed 范围内的正整数。")
    return value


def _check(code: str, passed: bool) -> dict[str, object]:
    return {"code": code, "passed": bool(passed)}


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _read_snapshot_authority(
    conn: sqlite3.Connection, snapshot_id: int
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT id, project_id, git_snapshot_id, analysis_lineage_id, branch,
               project_repository_url, project_config_hash, git_facts_hash,
               prd_id, prd_source_hash, prd_parsed_hash, prd_structured_hash,
               prd_document_fingerprint, profile_id, profile_content_hash, snapshot_hash
        FROM evidence_snapshots WHERE id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise _stored_invalid("EvidenceSnapshot authority row 不存在。")
    return dict(row)


def _assert_frozen_source_binding(
    *,
    bundle: Mapping[str, object],
    snapshot: Mapping[str, object],
    candidate: Mapping[str, object],
    model_result: Mapping[str, object],
    model_call: Mapping[str, object],
) -> tuple[int, str, str]:
    report = bundle["report_version"]
    git_facts = bundle.get("git_facts")
    if not isinstance(git_facts, Mapping):
        raise _stored_invalid("review bundle 缺少 frozen Git facts。")

    git_snapshot_id = git_facts.get("git_snapshot_id")
    git_facts_hash = git_facts.get("git_facts_hash")
    if type(git_snapshot_id) is not int or git_snapshot_id <= 0 or not _is_hash(git_facts_hash):
        raise _stored_invalid("review bundle frozen Git identity 无效。")

    snapshot_expected = {
        "id": report.get("evidence_snapshot_id"),
        "project_id": bundle.get("project_id"),
        "snapshot_hash": report.get("evidence_snapshot_hash"),
        "git_snapshot_id": git_snapshot_id,
        "git_facts_hash": git_facts_hash,
    }
    if any(snapshot.get(field) != expected for field, expected in snapshot_expected.items()):
        raise _stored_invalid("EvidenceSnapshot/Git source identity 与 ReportVersion 不一致。")

    candidate_range = candidate.get("range")
    if (
        candidate.get("snapshot_id") != snapshot.get("id")
        or candidate.get("snapshot_hash") != snapshot.get("snapshot_hash")
        or candidate.get("project_id") != bundle.get("project_id")
        or not isinstance(candidate_range, Mapping)
        or candidate_range.get("git_snapshot_id") != git_snapshot_id
        or candidate_range.get("git_facts_hash") != git_facts_hash
    ):
        raise _stored_invalid("Context Candidate Set 无法重闭合 frozen Git/Evidence identity。")

    candidate_set_hash = candidate.get("candidate_set_hash")
    model_call_candidate_set_hash = model_call.get("candidate_set_hash")
    if (
        type(candidate_set_hash) is not str
        or _HASH_RE.fullmatch(candidate_set_hash) is None
        or type(model_call_candidate_set_hash) is not str
        or _HASH_RE.fullmatch(model_call_candidate_set_hash) is None
    ):
        raise _stored_invalid("Context Candidate Set/Model Call candidate_set_hash 无效。")
    if candidate_set_hash != model_call_candidate_set_hash:
        raise _stored_invalid("Context Candidate Set identity 与 durable Model Call 不一致。")

    result_expected = {
        "model_result_id": report.get("model_execution_result_id"),
        "project_id": bundle.get("project_id"),
        "snapshot_id": report.get("evidence_snapshot_id"),
        "execution_result_hash": report.get("execution_result_hash"),
        "model_call_id": report.get("model_call_id"),
        "call_identity_hash": report.get("call_identity_hash"),
        "validated_result_hash": report.get("report_content_hash"),
    }
    if any(model_result.get(field) != expected for field, expected in result_expected.items()):
        raise _stored_invalid("verified ModelExecutionResult 与 ReportVersion identity 不一致。")

    call_expected = {
        "model_call_id": report.get("model_call_id"),
        "project_id": bundle.get("project_id"),
        "snapshot_id": report.get("evidence_snapshot_id"),
        "snapshot_hash": report.get("evidence_snapshot_hash"),
        "call_identity_hash": report.get("call_identity_hash"),
    }
    if any(model_call.get(field) != expected for field, expected in call_expected.items()):
        raise _stored_invalid("durable Model Call 与 ReportVersion/EvidenceSnapshot identity 不一致。")
    return git_snapshot_id, git_facts_hash, candidate_set_hash


def _assert_current_supplement_identity(
    conn: sqlite3.Connection, bundle: Mapping[str, object]
) -> None:
    report = bundle["report_version"]
    expected = bundle.get("current_supplement")
    row = conn.execute(
        """
        SELECT id, version_no, content_hash
        FROM report_supplement_versions
        WHERE report_version_id = ?
        ORDER BY version_no DESC LIMIT 1
        """,
        (report["report_version_id"],),
    ).fetchone()
    if expected is None:
        if row is not None:
            raise _stale()
        return
    if row is None:
        raise _stale()
    if (
        row["id"] != expected.get("supplement_version_id")
        or row["version_no"] != expected.get("version_no")
        or row["content_hash"] != expected.get("content_hash")
    ):
        raise _stale()


def _read_current_local_authority(
    *,
    conn: sqlite3.Connection,
    project_id: int,
    snapshot: Mapping[str, object],
    model_call: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, str]]]:
    checks: list[dict[str, object]] = []
    blockers: list[dict[str, str]] = []

    project_row = conn.execute(
        "SELECT id, git_url, branch, version FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    project: dict[str, object] | None = None
    if project_row is None:
        checks.append(_check("project_git_identity", False))
        blockers.append(_blocker("PROJECT_AUTHORITY_MISSING", "当前项目 authority 不存在。"))
    else:
        project = dict(project_row)
        try:
            normalized_url = _normalize_git_url(project.get("git_url"))
        except HTTPException:
            normalized_url = None
        project_git_ok = (
            type(normalized_url) is str
            and normalized_url == snapshot.get("project_repository_url")
            and project.get("branch") == snapshot.get("branch")
        )
        checks.append(_check("project_git_identity", project_git_ok))
        if not project_git_ok:
            blockers.append(
                _blocker(
                    "PROJECT_GIT_AUTHORITY_DRIFT",
                    "当前项目 Git 仓库或活动分支与冻结 EvidenceSnapshot 不一致。",
                )
            )

    lineage_rows = conn.execute(
        """
        SELECT id, branch, status FROM analysis_lineages
        WHERE project_id = ? AND status = 'active'
        ORDER BY sequence_no DESC
        """,
        (project_id,),
    ).fetchall()
    lineage_ok = (
        len(lineage_rows) == 1
        and lineage_rows[0]["id"] == snapshot.get("analysis_lineage_id")
        and lineage_rows[0]["branch"] == snapshot.get("branch")
    )
    checks.append(_check("analysis_lineage_identity", lineage_ok))
    if not lineage_ok:
        blockers.append(
            _blocker(
                "ANALYSIS_LINEAGE_AUTHORITY_DRIFT",
                "当前 active analysis lineage 与冻结 EvidenceSnapshot 不一致。",
            )
        )

    prd_rows = conn.execute(
        """
        SELECT id, source_hash, parsed_hash, structured_hash, document_fingerprint
        FROM prd_versions
        WHERE project_id = ? AND status = 'parse_confirmed'
        ORDER BY version_no DESC
        """,
        (project_id,),
    ).fetchall()
    prd = dict(prd_rows[0]) if len(prd_rows) == 1 else None
    prd_ok = bool(
        prd
        and prd.get("id") == snapshot.get("prd_id")
        and prd.get("source_hash") == snapshot.get("prd_source_hash")
        and prd.get("parsed_hash") == snapshot.get("prd_parsed_hash")
        and prd.get("structured_hash") == snapshot.get("prd_structured_hash")
        and prd.get("document_fingerprint") == snapshot.get("prd_document_fingerprint")
    )
    checks.append(_check("current_prd_identity", prd_ok))
    if not prd_ok:
        blockers.append(
            _blocker(
                "CURRENT_PRD_AUTHORITY_DRIFT",
                "当前唯一已确认 PRD 与冻结 EvidenceSnapshot 的 PRD identity/hash 不一致。",
            )
        )

    profile: dict[str, object] | None
    try:
        profile = read_current_confirmed_project_profile(project_id, conn=conn)
    except ProjectProfileAuthorityError:
        profile = None
    profile_ok = bool(
        profile
        and profile.get("id") == snapshot.get("profile_id")
        and profile.get("content_hash") == snapshot.get("profile_content_hash")
        and profile.get("source_prd_id") == snapshot.get("prd_id")
    )
    checks.append(_check("current_profile_identity", profile_ok))
    if not profile_ok:
        blockers.append(
            _blocker(
                "CURRENT_PROFILE_AUTHORITY_DRIFT",
                "当前唯一已确认 Project Profile 与冻结 EvidenceSnapshot 不一致。",
            )
        )

    model_ok = (
        model_call.get("project_id") == project_id
        and model_call.get("snapshot_id") == snapshot.get("id")
        and model_call.get("snapshot_hash") == snapshot.get("snapshot_hash")
        and model_call.get("qualification_status") == "qualified"
        and model_call.get("authorization_authorized") == 1
        and model_call.get("authorization_valid") == 1
        and _is_hash(model_call.get("qualification_hash"))
        and _is_hash(model_call.get("authorization_hash"))
        and type(model_call.get("rule_version")) is str
        and bool(model_call.get("rule_version"))
        and type(model_call.get("output_schema_version")) is str
        and bool(model_call.get("output_schema_version"))
    )
    checks.append(_check("frozen_model_authority_identity", model_ok))
    if not model_ok:
        blockers.append(
            _blocker(
                "MODEL_AUTHORITY_IDENTITY_INVALID",
                "冻结 Model Call 的 qualification/authorization/rule/output identity 无法闭合。",
            )
        )

    authority = {
        "project": None if project is None else {
            "id": project.get("id"),
            "git_url": project.get("git_url"),
            "branch": project.get("branch"),
            "version": project.get("version"),
        },
        "active_lineage": None if len(lineage_rows) != 1 else {
            "id": lineage_rows[0]["id"],
            "branch": lineage_rows[0]["branch"],
            "status": lineage_rows[0]["status"],
        },
        "current_prd": prd,
        "current_profile": None if profile is None else {
            "id": profile.get("id"),
            "version_no": profile.get("version_no"),
            "source_prd_id": profile.get("source_prd_id"),
            "content_hash": profile.get("content_hash"),
        },
        "model_call": {
            "model_call_id": model_call.get("model_call_id"),
            "call_identity_hash": model_call.get("call_identity_hash"),
            "provider": model_call.get("provider"),
            "model_id": model_call.get("model_id"),
            "model_version": model_call.get("model_version"),
            "rule_version": model_call.get("rule_version"),
            "output_schema_version": model_call.get("output_schema_version"),
            "benchmark_sample_pack_version": model_call.get("benchmark_sample_pack_version"),
            "qualification_hash": model_call.get("qualification_hash"),
            "authorization_hash": model_call.get("authorization_hash"),
        },
    }
    return authority, checks, blockers


def _report_semantic_checks(
    bundle: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    checks: list[dict[str, object]] = []
    blockers: list[dict[str, str]] = []
    report = bundle["report_version"]
    ai_raw = bundle["ai_raw"]
    supplement = bundle.get("current_supplement")

    lifecycle_ok = report.get("lifecycle") in _APPROVABLE_REPORT_LIFECYCLES
    checks.append(_check("report_lifecycle_approvable", lifecycle_ok))
    if not lifecycle_ok:
        blockers.append(
            _blocker(
                "REPORT_VERSION_NOT_APPROVABLE",
                "当前 ReportVersion 已 superseded/approved/voided，不能形成新的绿色 ValidationResult。",
            )
        )

    content = ai_raw.get("content")
    if isinstance(content, Mapping) and ai_raw.get("task_type") == "daily_report_regenerate":
        report_payload = content.get("new_report")
    else:
        report_payload = content
    feature_progress = (
        report_payload.get("feature_progress")
        if isinstance(report_payload, Mapping)
        else None
    )
    stages_ok = type(feature_progress) is list
    if stages_ok:
        for item in feature_progress:
            if not isinstance(item, Mapping):
                stages_ok = False
                break
            if item.get("stage") == "已完成":
                evidence_ids = item.get("evidence_ids")
                if type(evidence_ids) is not list or not evidence_ids:
                    stages_ok = False
                    break
    checks.append(_check("conservative_stage_evidence", stages_ok))
    if not stages_ok:
        blockers.append(
            _blocker(
                "REPORT_STAGE_EVIDENCE_NOT_CLOSED",
                "报告阶段无法证明保持正式阶段词和保守证据约束。",
            )
        )

    if supplement is None:
        checks.append(_check("supplement_contradiction_input", True))
    else:
        contradiction_ok = False
        contradiction_code = "REPORT_CONTRADICTION_CHECK_REQUIRED"
        contradiction_message = "当前候选包含 PM supplement；没有与该 supplement exact identity 绑定的 verified contradiction result，不能推断无冲突。"
        try:
            from app.model_execution_results import get_model_execution_result_for_call
            from app.model_call_ledger import get_model_call
            from app.report_contradiction import get_current_contradiction_request

            request = get_current_contradiction_request(
                project_id=int(bundle["project_id"]),
                report_version_id=int(report["report_version_id"]),
            )
            with get_connection() as conn:
                call_row = conn.execute(
                    "SELECT id FROM model_calls WHERE project_id = ? AND local_task_id = ? AND task_type = 'report_contradiction_check' ORDER BY id DESC LIMIT 2",
                    (bundle["project_id"], request["local_task_id"]),
                ).fetchall()
            if len(call_row) == 1:
                call = get_model_call(int(call_row[0]["id"]))
                result = get_model_execution_result_for_call(int(call["model_call_id"]))
                validated = result.get("validated_result")
                contradiction_ok = (
                    call.get("snapshot_id") == report["evidence_snapshot_id"]
                    and call.get("local_task_id") == request["local_task_id"]
                    and call.get("task_type") == "report_contradiction_check"
                    and result.get("task_type") == "report_contradiction_check"
                    and isinstance(validated, Mapping)
                    and validated.get("has_conflict") is False
                    and validated.get("conflicts") == []
                    and validated.get("unresolved_items") == []
                )
                if not contradiction_ok:
                    contradiction_code = "REPORT_CONTRADICTION_BLOCKED"
                    contradiction_message = "Exact contradiction result 报告冲突、未决项或 identity closure 失败，不能确认。"
        except HTTPException:
            contradiction_ok = False
        checks.append(_check("supplement_contradiction_input", contradiction_ok))
        if not contradiction_ok:
            blockers.append(_blocker(contradiction_code, contradiction_message))
    return checks, blockers


def _candidate_payload(
    bundle: Mapping[str, object],
    *,
    git_snapshot_id: int,
    git_facts_hash: str,
    candidate_set_hash: str,
) -> dict[str, object]:
    report = bundle["report_version"]
    ai_raw = bundle["ai_raw"]
    supplement = bundle.get("current_supplement")
    return {
        "project_id": bundle["project_id"],
        "report_version_id": report["report_version_id"],
        "report_state_version": report["state_version"],
        "report_content_hash": report["report_content_hash"],
        "supplement": None if supplement is None else {
            "supplement_version_id": supplement["supplement_version_id"],
            "version_no": supplement["version_no"],
            "content_hash": supplement["content_hash"],
            "source_type": supplement["source_type"],
            "provided_by": supplement["provided_by"],
            "provided_at": supplement["provided_at"],
            "provided_timezone": supplement["provided_timezone"],
        },
        "evidence_snapshot_id": report["evidence_snapshot_id"],
        "evidence_snapshot_hash": report["evidence_snapshot_hash"],
        "git_snapshot_id": git_snapshot_id,
        "git_facts_hash": git_facts_hash,
        "candidate_set_hash": candidate_set_hash,
        "model_execution_result_id": report["model_execution_result_id"],
        "execution_result_hash": report["execution_result_hash"],
        "model_call_id": report["model_call_id"],
        "call_identity_hash": report["call_identity_hash"],
        "validated_result_hash": ai_raw["validated_result_hash"],
    }


def _result_hash_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": value["schema_version"],
        "project_id": value["project_id"],
        "report_version_id": value["report_version_id"],
        "report_state_version": value["report_state_version"],
        "report_content_hash": value["report_content_hash"],
        "supplement_version_id": value["supplement_version_id"],
        "supplement_content_hash": value["supplement_content_hash"],
        "evidence_snapshot_id": value["evidence_snapshot_id"],
        "evidence_snapshot_hash": value["evidence_snapshot_hash"],
        "git_snapshot_id": value["git_snapshot_id"],
        "git_facts_hash": value["git_facts_hash"],
        "model_execution_result_id": value["model_execution_result_id"],
        "execution_result_hash": value["execution_result_hash"],
        "model_call_id": value["model_call_id"],
        "call_identity_hash": value["call_identity_hash"],
        "candidate_hash": value["candidate_hash"],
        "current_authority_hash": value["current_authority_hash"],
        "state": value["state"],
        "checks": value["checks"],
        "blockers": value["blockers"],
        "created_at": value["created_at"],
    }


def _close_row(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
    value = dict(row)
    if value.get("schema_version") != SCHEMA_VERSION or value.get("state") not in _VALID_STATES:
        raise _stored_invalid()
    for field in (
        "id",
        "project_id",
        "report_version_id",
        "report_state_version",
        "evidence_snapshot_id",
        "git_snapshot_id",
        "model_execution_result_id",
        "model_call_id",
    ):
        if type(value.get(field)) is not int or value[field] <= 0:
            raise _stored_invalid()
    for field in (
        "report_content_hash",
        "evidence_snapshot_hash",
        "git_facts_hash",
        "execution_result_hash",
        "call_identity_hash",
        "candidate_hash",
        "current_authority_hash",
        "result_hash",
    ):
        if not _is_hash(value.get(field)):
            raise _stored_invalid()
    if value.get("supplement_version_id") is None:
        if value.get("supplement_content_hash") is not None:
            raise _stored_invalid()
    elif (
        type(value.get("supplement_version_id")) is not int
        or value["supplement_version_id"] <= 0
        or not _is_hash(value.get("supplement_content_hash"))
    ):
        raise _stored_invalid()
    try:
        checks = json.loads(value["checks_json"])
        blockers = json.loads(value["blockers_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise _stored_invalid() from exc
    if type(checks) is not list or type(blockers) is not list:
        raise _stored_invalid()
    if _canonical_json(checks) != value["checks_json"] or _canonical_json(blockers) != value["blockers_json"]:
        raise _stored_invalid()
    if value["state"] == "passed" and blockers:
        raise _stored_invalid("passed ValidationResult 不允许存在 blocker。")
    if value["state"] == "blocked" and not blockers:
        raise _stored_invalid("blocked ValidationResult 必须包含 blocker。")
    result = dict(value)
    result["validation_result_id"] = result.pop("id")
    result.pop("checks_json")
    result.pop("blockers_json")
    result["checks"] = checks
    result["blockers"] = blockers
    if _stable_hash(_result_hash_payload(result)) != result["result_hash"]:
        raise _stored_invalid("ValidationResult result_hash 无法闭合。")
    return result


def create_validation_result(
    *,
    project_id: int,
    report_version_id: int,
    expected_report_state_version: int,
) -> dict[str, object]:
    """Create/replay one immutable deterministic ValidationResult for the exact candidate."""
    project_id = _positive_id(project_id, "project_id")
    report_version_id = _positive_id(report_version_id, "report_version_id")
    expected_report_state_version = _positive_id(
        expected_report_state_version, "expected_report_state_version"
    )
    try:
        ensure_report_validation_schema()
    except sqlite3.Error as exc:
        raise _stored_invalid("ValidationResult schema 初始化失败。") from exc

    # Initial verified projection. A second closure happens under BEGIN IMMEDIATE below.
    bundle = get_review_bundle(project_id=project_id, report_version_id=report_version_id)
    report = bundle["report_version"]
    if (
        report["state_version"] != expected_report_state_version
        or report.get("lifecycle") not in _APPROVABLE_REPORT_LIFECYCLES
    ):
        raise _stale()

    candidate_hash: str | None = None
    current_authority_hash: str | None = None
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh_report = conn.execute(
                "SELECT lifecycle, state_version FROM report_versions "
                "WHERE id = ? AND project_id = ?",
                (report_version_id, project_id),
            ).fetchone()
            if (
                fresh_report is None
                or fresh_report["state_version"] != expected_report_state_version
                or fresh_report["lifecycle"] not in _APPROVABLE_REPORT_LIFECYCLES
            ):
                raise _stale()
            _assert_current_supplement_identity(conn, bundle)

            # Writers are now excluded. Re-close every immutable source owner against
            # the exact committed state used to derive this ValidationResult.
            candidate_now = build_context_candidate_set(report["evidence_snapshot_id"])
            model_result_now = get_model_execution_result(
                report["model_execution_result_id"]
            )
            model_call_now = get_model_call(report["model_call_id"])
            snapshot = _read_snapshot_authority(conn, report["evidence_snapshot_id"])
            git_snapshot_id, git_facts_hash, candidate_set_hash = _assert_frozen_source_binding(
                bundle=bundle,
                snapshot=snapshot,
                candidate=candidate_now,
                model_result=model_result_now,
                model_call=model_call_now,
            )
            candidate_hash = _stable_hash(
                _candidate_payload(
                    bundle,
                    git_snapshot_id=git_snapshot_id,
                    git_facts_hash=git_facts_hash,
                    candidate_set_hash=candidate_set_hash,
                )
            )

            authority, authority_checks, authority_blockers = _read_current_local_authority(
                conn=conn,
                project_id=project_id,
                snapshot=snapshot,
                model_call=model_call_now,
            )
            semantic_checks, semantic_blockers = _report_semantic_checks(bundle)
            checks = sorted(
                [*authority_checks, *semantic_checks],
                key=lambda item: str(item["code"]),
            )
            blockers = sorted(
                [*authority_blockers, *semantic_blockers],
                key=lambda item: (item["code"], item["message"]),
            )
            current_authority_hash = _stable_hash(authority)

            existing = conn.execute(
                f"SELECT {_VALIDATION_COLUMNS} FROM {_VALIDATION_TABLE} "
                "WHERE candidate_hash = ? AND current_authority_hash = ?",
                (candidate_hash, current_authority_hash),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"validation_result": _close_row(existing), "created": False}

            supplement = bundle.get("current_supplement")
            stored: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "report_version_id": report_version_id,
                "report_state_version": report["state_version"],
                "report_content_hash": report["report_content_hash"],
                "supplement_version_id": None if supplement is None else supplement["supplement_version_id"],
                "supplement_content_hash": None if supplement is None else supplement["content_hash"],
                "evidence_snapshot_id": report["evidence_snapshot_id"],
                "evidence_snapshot_hash": report["evidence_snapshot_hash"],
                "git_snapshot_id": git_snapshot_id,
                "git_facts_hash": git_facts_hash,
                "model_execution_result_id": report["model_execution_result_id"],
                "execution_result_hash": report["execution_result_hash"],
                "model_call_id": report["model_call_id"],
                "call_identity_hash": report["call_identity_hash"],
                "candidate_hash": candidate_hash,
                "current_authority_hash": current_authority_hash,
                "state": "blocked" if blockers else "passed",
                "checks": checks,
                "blockers": blockers,
                "created_at": _now(),
            }
            stored["result_hash"] = _stable_hash(_result_hash_payload(stored))
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
                    stored["schema_version"], stored["project_id"], stored["report_version_id"],
                    stored["report_state_version"], stored["report_content_hash"],
                    stored["supplement_version_id"], stored["supplement_content_hash"],
                    stored["evidence_snapshot_id"], stored["evidence_snapshot_hash"],
                    stored["git_snapshot_id"], stored["git_facts_hash"],
                    stored["model_execution_result_id"], stored["execution_result_hash"],
                    stored["model_call_id"], stored["call_identity_hash"],
                    stored["candidate_hash"], stored["current_authority_hash"],
                    stored["state"], _canonical_json(checks), _canonical_json(blockers),
                    stored["created_at"], stored["result_hash"],
                ),
            )
            row = conn.execute(
                f"SELECT {_VALIDATION_COLUMNS} FROM {_VALIDATION_TABLE} WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if row is None:
                raise _stored_invalid("ValidationResult INSERT 后无法回读。")
            conn.commit()
            return {"validation_result": _close_row(row), "created": True}
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        if candidate_hash is not None and current_authority_hash is not None:
            try:
                with get_connection() as conn:
                    winner = conn.execute(
                        f"SELECT {_VALIDATION_COLUMNS} FROM {_VALIDATION_TABLE} "
                        "WHERE candidate_hash = ? AND current_authority_hash = ?",
                        (candidate_hash, current_authority_hash),
                    ).fetchone()
                if winner is not None:
                    return {"validation_result": _close_row(winner), "created": False}
            except sqlite3.Error as exc:
                raise _stored_invalid() from exc
        raise _stored_invalid("ValidationResult 并发写入冲突无法闭合。")
    except sqlite3.Error as exc:
        raise _stored_invalid("ValidationResult 持久化失败。") from exc


def get_latest_validation_result(
    *, project_id: int, report_version_id: int
) -> dict[str, object] | None:
    """Pure read: return latest immutable result, or None before the first validation write."""
    project_id = _positive_id(project_id, "project_id")
    report_version_id = _positive_id(report_version_id, "report_version_id")
    try:
        with get_connection() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_VALIDATION_TABLE,),
            ).fetchone()
            if table is None:
                return None
            row = conn.execute(
                f"SELECT {_VALIDATION_COLUMNS} FROM {_VALIDATION_TABLE} "
                "WHERE project_id = ? AND report_version_id = ? ORDER BY id DESC LIMIT 1",
                (project_id, report_version_id),
            ).fetchone()
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
    return None if row is None else _close_row(row)