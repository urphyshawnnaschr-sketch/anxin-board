"""Page 07 exact task subjects: pure credential-safe business framing only."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app import context_redaction, report_review


RESULT_SCHEMA_VERSION = "page07_task_subject_result_v1"
CONTRADICTION_SUBJECT_SCHEMA_VERSION = "page07_report_contradiction_subject_v1"
REGENERATE_SUBJECT_SCHEMA_VERSION = "page07_daily_report_regenerate_subject_v1"
CONTRADICTION_TASK_TYPE = "report_contradiction_check"
REGENERATE_TASK_TYPE = "daily_report_regenerate"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _stored_invalid(message: str = "Page07 task subject durable identity 无法闭合。") -> HTTPException:
    return _error("REPORT_TASK_SUBJECT_STORED_INVALID", message)


def _input_invalid(message: str = "Page07 task subject 输入无效。") -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": "REPORT_TASK_SUBJECT_INPUT_INVALID", "message": message},
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _stored_invalid("Page07 task subject 含不可 canonicalize 的值。") from exc


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _is_positive_id(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_SIGNED_INTEGER_MAX


def _require_positive_id(value: object, field_name: str) -> int:
    if not _is_positive_id(value):
        raise _input_invalid(f"{field_name} 必须是 SQLite signed 范围内的正整数。")
    return value


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _stored_invalid(f"{label} 必须是 server-closed mapping。")
    return value


def _require_identity_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise _stored_invalid(f"{field_name} durable identity/provenance 无效。")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _stored_invalid(f"{field_name} 必须可编码为 UTF-8。") from exc
    return value


def _require_content(value: object, hash_value: object, field_name: str) -> str:
    text = _require_identity_text(value, field_name)
    if not _is_hash(hash_value) or _content_hash(text) != hash_value:
        raise _stored_invalid(f"{field_name} 与 durable content hash 无法闭合。")
    return text


def _close_source_report(value: object) -> dict[str, object]:
    report = dict(_require_mapping(value, "source ReportVersion"))
    for field in (
        "report_version_id",
        "project_id",
        "model_execution_result_id",
        "model_call_id",
        "evidence_snapshot_id",
    ):
        if not _is_positive_id(report.get(field)):
            raise _stored_invalid(f"source ReportVersion {field} 无效。")
    for field in (
        "report_content_hash",
        "execution_result_hash",
        "call_identity_hash",
        "evidence_snapshot_hash",
    ):
        if not _is_hash(report.get(field)):
            raise _stored_invalid(f"source ReportVersion {field} 无效。")
    return report


def _close_regenerate_task(
    value: object,
    *,
    project_id: int,
    evidence_snapshot_id: int,
) -> dict[str, object]:
    task = dict(_require_mapping(value, "replacement regenerate task"))
    if (
        not _is_positive_id(task.get("id"))
        or task.get("project_id") != project_id
        or task.get("evidence_snapshot_id") != evidence_snapshot_id
        or task.get("task_type") != REGENERATE_TASK_TYPE
        or type(task.get("local_task_id")) is not str
        or not task["local_task_id"]
        or not _is_hash(task.get("identity_hash"))
        or type(task.get("schema_version")) is not str
        or not task["schema_version"]
    ):
        raise _stored_invalid("replacement daily_report_regenerate task identity 无法闭合。")
    expected_identity_hash = _stable_hash(
        {
            "schema_version": task["schema_version"],
            "local_task_id": task["local_task_id"],
            "project_id": task["project_id"],
            "evidence_snapshot_id": task["evidence_snapshot_id"],
            "task_type": task["task_type"],
        }
    )
    if expected_identity_hash != task["identity_hash"]:
        raise _stored_invalid("replacement task identity_hash 无法重算。")
    return task


def _reanalysis_hash_payload(value: Mapping[str, object]) -> dict[str, object]:
    required = (
        "schema_version",
        "project_id",
        "report_version_id",
        "evidence_snapshot_id",
        "evidence_snapshot_hash",
        "source_report_state_version",
        "source_report_lifecycle",
        "error_location_hash",
        "corrected_truth_hash",
        "correction_basis_hash",
        "correction_source_hash",
        "requested_by",
        "requested_at",
        "requested_timezone",
        "idempotency_key",
        "replacement_task_id",
        "replacement_local_task_id",
        "replacement_task_identity_hash",
    )
    if any(field not in value for field in required):
        raise _stored_invalid("ReanalysisRequest request_hash payload 缺字段。")
    return {field: value[field] for field in required}


def _close_reanalysis_request(
    value: object,
    *,
    source_report: Mapping[str, object],
    replacement_task: Mapping[str, object],
) -> dict[str, object]:
    request = dict(_require_mapping(value, "ReanalysisRequest"))
    for field in (
        "reanalysis_request_id",
        "project_id",
        "report_version_id",
        "evidence_snapshot_id",
        "source_report_state_version",
        "replacement_task_id",
    ):
        if not _is_positive_id(request.get(field)):
            raise _stored_invalid(f"ReanalysisRequest {field} 无效。")
    if (
        request["project_id"] != source_report["project_id"]
        or request["report_version_id"] != source_report["report_version_id"]
        or request["evidence_snapshot_id"] != source_report["evidence_snapshot_id"]
        or request.get("evidence_snapshot_hash") != source_report["evidence_snapshot_hash"]
        or request["replacement_task_id"] != replacement_task["id"]
        or request.get("replacement_local_task_id") != replacement_task["local_task_id"]
        or request.get("replacement_task_identity_hash") != replacement_task["identity_hash"]
    ):
        raise _stored_invalid("ReanalysisRequest/source report/replacement task cross-binding 漂移。")
    for text_field, hash_field in (
        ("error_location", "error_location_hash"),
        ("corrected_truth", "corrected_truth_hash"),
        ("correction_basis", "correction_basis_hash"),
        ("correction_source", "correction_source_hash"),
    ):
        _require_content(request.get(text_field), request.get(hash_field), text_field)
    for field in (
        "requested_by",
        "requested_at",
        "requested_timezone",
        "idempotency_key",
        "replacement_local_task_id",
        "source_report_lifecycle",
        "schema_version",
    ):
        _require_identity_text(request.get(field), field)
    for field in (
        "evidence_snapshot_hash",
        "replacement_task_identity_hash",
        "request_hash",
    ):
        if not _is_hash(request.get(field)):
            raise _stored_invalid(f"ReanalysisRequest {field} 无效。")
    if _stable_hash(_reanalysis_hash_payload(request)) != request["request_hash"]:
        raise _stored_invalid("ReanalysisRequest request_hash 无法重算。")
    return request


def _close_structured_redaction(value: object) -> dict[str, object]:
    result = context_redaction.redact_credential_safe_structured_value(value=value)
    result = dict(_require_mapping(result, "credential-safe structured redaction"))
    if (
        result.get("schema_version") != "credential_safe_structured_value_v1"
        or result.get("redaction_policy_id") != context_redaction.POLICY_ID
        or result.get("redaction_policy_hash") != context_redaction.REDACTION_POLICY_HASH
        or not _is_hash(result.get("redacted_value_hash"))
        or type(result.get("redaction_match_count")) is not int
        or result["redaction_match_count"] < 0
        or not isinstance(result.get("redaction_stats"), Mapping)
        or "redacted_value" not in result
    ):
        raise _stored_invalid("credential redaction pure seam 返回身份无效。")
    if _stable_hash(result["redacted_value"]) != result["redacted_value_hash"]:
        raise _stored_invalid("credential-safe redacted subject hash 无法闭合。")
    return result


def _subject_result(
    *,
    subject_schema_version: str,
    task_type: str,
    raw_subject: Mapping[str, object],
) -> dict[str, object]:
    redaction = _close_structured_redaction(deepcopy(dict(raw_subject)))
    subject = redaction["redacted_value"]
    if not isinstance(subject, Mapping):
        raise _stored_invalid("credential-safe Page07 subject 必须保持 mapping 形状。")
    subject = deepcopy(dict(subject))
    if (
        subject.get("schema_version") != subject_schema_version
        or subject.get("task_type") != task_type
        or subject.get("redaction_policy_id") != context_redaction.POLICY_ID
        or subject.get("redaction_policy_hash") != context_redaction.REDACTION_POLICY_HASH
    ):
        raise _stored_invalid("credential-safe Page07 subject 固定 identity 漂移。")
    task_subject_hash = _stable_hash(subject)
    if task_subject_hash != redaction["redacted_value_hash"]:
        raise _stored_invalid("task_subject_hash 与 exact credential-safe subject 不一致。")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "subject_schema_version": subject_schema_version,
        "task_type": task_type,
        "subject": subject,
        "task_subject_hash": task_subject_hash,
        "redaction_policy_id": context_redaction.POLICY_ID,
        "redaction_policy_hash": context_redaction.REDACTION_POLICY_HASH,
        "redaction_stats": dict(redaction["redaction_stats"]),
        "redaction_match_count": redaction["redaction_match_count"],
    }


def build_daily_report_regenerate_subject(
    *, project_id: int, report_version_id: int
) -> dict[str, object]:
    """Resolve one exact durable ReanalysisRequest and return only its credential-safe subject.

    Correction truth is never accepted as a caller argument. The durable Page07 review owner is
    re-read server-side on every construction/replay.
    """
    project_id = _require_positive_id(project_id, "project_id")
    report_version_id = _require_positive_id(report_version_id, "report_version_id")
    bundle = report_review.get_reanalysis_request(
        project_id=project_id,
        report_version_id=report_version_id,
    )
    bundle = _require_mapping(bundle, "ReanalysisRequest bundle")
    if bundle.get("cancelled") is True:
        raise _stored_invalid("这次未发送的重分析已撤销，不能重新准备或发送。")
    source_report = _close_source_report(bundle.get("source_report_version"))
    if (
        source_report["project_id"] != project_id
        or source_report["report_version_id"] != report_version_id
    ):
        raise _stored_invalid("server-resolved source ReportVersion 与 requested identity 不一致。")
    replacement_task = _close_regenerate_task(
        bundle.get("replacement_task"),
        project_id=project_id,
        evidence_snapshot_id=source_report["evidence_snapshot_id"],
    )
    request = _close_reanalysis_request(
        bundle.get("reanalysis_request"),
        source_report=source_report,
        replacement_task=replacement_task,
    )

    raw_subject = {
        "schema_version": REGENERATE_SUBJECT_SCHEMA_VERSION,
        "task_type": REGENERATE_TASK_TYPE,
        "project_id": project_id,
        "source_report": {
            "report_version_id": source_report["report_version_id"],
            "report_content_hash": source_report["report_content_hash"],
            "model_execution_result_id": source_report["model_execution_result_id"],
            "execution_result_hash": source_report["execution_result_hash"],
            "model_call_id": source_report["model_call_id"],
            "call_identity_hash": source_report["call_identity_hash"],
        },
        "reanalysis_request": {
            "reanalysis_request_id": request["reanalysis_request_id"],
            "request_hash": request["request_hash"],
            "error_location": request["error_location"],
            "error_location_hash": request["error_location_hash"],
            "corrected_truth": request["corrected_truth"],
            "corrected_truth_hash": request["corrected_truth_hash"],
            "correction_basis": request["correction_basis"],
            "correction_basis_hash": request["correction_basis_hash"],
            "correction_source": request["correction_source"],
            "correction_source_hash": request["correction_source_hash"],
            "requested_by": request["requested_by"],
            "requested_at": request["requested_at"],
            "requested_timezone": request["requested_timezone"],
        },
        "replacement_task": {
            "replacement_task_id": replacement_task["id"],
            "replacement_local_task_id": replacement_task["local_task_id"],
            "replacement_task_identity_hash": replacement_task["identity_hash"],
        },
        "evidence_snapshot": {
            "evidence_snapshot_id": source_report["evidence_snapshot_id"],
            "evidence_snapshot_hash": source_report["evidence_snapshot_hash"],
        },
        "redaction_policy_id": context_redaction.POLICY_ID,
        "redaction_policy_hash": context_redaction.REDACTION_POLICY_HASH,
    }
    return _subject_result(
        subject_schema_version=REGENERATE_SUBJECT_SCHEMA_VERSION,
        task_type=REGENERATE_TASK_TYPE,
        raw_subject=raw_subject,
    )


def _close_contradiction_request(
    value: object,
    *,
    report: Mapping[str, object],
    supplement: Mapping[str, object],
) -> dict[str, object]:
    request = dict(_require_mapping(value, "durable contradiction request"))
    required = (
        "contradiction_request_id",
        "request_hash",
        "contradiction_task_id",
        "local_task_id",
        "task_identity_hash",
        "task_type",
        "project_id",
        "report_version_id",
        "report_content_hash",
        "source_model_execution_result_id",
        "source_execution_result_hash",
        "evidence_snapshot_id",
        "evidence_snapshot_hash",
        "supplement_version_id",
        "supplement_content_hash",
        "supplement_source_type",
        "supplement_provided_by",
        "supplement_provided_at",
        "supplement_provided_timezone",
    )
    if any(field not in request for field in required):
        raise _stored_invalid("durable contradiction request 缺少 3B-2 required binding。")
    for field in ("contradiction_request_id", "contradiction_task_id"):
        if not _is_positive_id(request.get(field)):
            raise _stored_invalid(f"durable contradiction request {field} 无效。")
    for field in ("request_hash", "task_identity_hash"):
        if not _is_hash(request.get(field)):
            raise _stored_invalid(f"durable contradiction request {field} 无效。")
    if request.get("task_type") != CONTRADICTION_TASK_TYPE:
        raise _stored_invalid("durable contradiction request task_type 必须 exact report_contradiction_check。")
    _require_identity_text(request.get("local_task_id"), "contradiction local_task_id")
    expected = {
        "project_id": report["project_id"],
        "report_version_id": report["report_version_id"],
        "report_content_hash": report["report_content_hash"],
        "source_model_execution_result_id": report["model_execution_result_id"],
        "source_execution_result_hash": report["execution_result_hash"],
        "evidence_snapshot_id": report["evidence_snapshot_id"],
        "evidence_snapshot_hash": report["evidence_snapshot_hash"],
        "supplement_version_id": supplement["supplement_version_id"],
        "supplement_content_hash": supplement["content_hash"],
        "supplement_source_type": supplement["source_type"],
        "supplement_provided_by": supplement["provided_by"],
        "supplement_provided_at": supplement["provided_at"],
        "supplement_provided_timezone": supplement["provided_timezone"],
    }
    if any(request.get(field) != expected_value for field, expected_value in expected.items()):
        raise _stored_invalid("durable contradiction request 与 exact report/supplement source 漂移。")
    return request


def build_report_contradiction_subject_from_durable_facts(
    *,
    review_bundle: Mapping[str, object],
    contradiction_request: Mapping[str, object],
) -> dict[str, object]:
    """Pure 3B-2 constructor over already server-closed 3B-1/report-review facts.

    This is intentionally not a browser/API seam and performs no DB/provider/network action.
    The future 3B-1 owner must supply the durable contradiction request; this function refuses
    supplement or AI truth as independent caller fields and cross-binds all supplied identities.
    """
    bundle = dict(_require_mapping(review_bundle, "report review bundle"))
    if bundle.get("schema_version") != "report_review_bundle_v1":
        raise _stored_invalid("report review bundle schema_version 无效。")
    report = _close_source_report(bundle.get("report_version"))
    ai_raw = dict(_require_mapping(bundle.get("ai_raw"), "ReportVersion AI raw"))
    evidence = dict(_require_mapping(bundle.get("evidence_snapshot"), "EvidenceSnapshot projection"))
    supplement = dict(_require_mapping(bundle.get("current_supplement"), "current SupplementVersion"))

    if (
        bundle.get("project_id") != report["project_id"]
        or ai_raw.get("model_execution_result_id") != report["model_execution_result_id"]
        or ai_raw.get("execution_result_hash") != report["execution_result_hash"]
        or ai_raw.get("model_call_id") != report["model_call_id"]
        or ai_raw.get("call_identity_hash") != report["call_identity_hash"]
        or ai_raw.get("validated_result_hash") != report["report_content_hash"]
        or ai_raw.get("snapshot_id") != report["evidence_snapshot_id"]
        or evidence.get("snapshot_id") != report["evidence_snapshot_id"]
        or evidence.get("snapshot_hash") != report["evidence_snapshot_hash"]
    ):
        raise _stored_invalid("review bundle AI raw/ReportVersion/EvidenceSnapshot binding 漂移。")
    if "content" not in ai_raw:
        raise _stored_invalid("review bundle 缺 exact AI raw content。")
    for field in ("supplement_version_id", "report_version_id", "version_no"):
        if not _is_positive_id(supplement.get(field)):
            raise _stored_invalid(f"SupplementVersion {field} 无效。")
    if supplement["report_version_id"] != report["report_version_id"]:
        raise _stored_invalid("SupplementVersion 不属于 exact ReportVersion。")
    _require_content(supplement.get("content"), supplement.get("content_hash"), "supplement content")
    for field in ("source_type", "provided_by", "provided_at", "provided_timezone"):
        _require_identity_text(supplement.get(field), f"SupplementVersion {field}")

    request = _close_contradiction_request(
        contradiction_request,
        report=report,
        supplement=supplement,
    )
    raw_subject = {
        "schema_version": CONTRADICTION_SUBJECT_SCHEMA_VERSION,
        "task_type": CONTRADICTION_TASK_TYPE,
        "project_id": report["project_id"],
        "source_report": {
            "report_version_id": report["report_version_id"],
            "report_content_hash": report["report_content_hash"],
            "model_execution_result_id": report["model_execution_result_id"],
            "execution_result_hash": report["execution_result_hash"],
            "model_call_id": report["model_call_id"],
            "call_identity_hash": report["call_identity_hash"],
            "ai_raw": deepcopy(ai_raw["content"]),
        },
        "supplement_version": {
            "supplement_version_id": supplement["supplement_version_id"],
            "content": supplement["content"],
            "content_hash": supplement["content_hash"],
            "source_type": supplement["source_type"],
            "provided_by": supplement["provided_by"],
            "provided_at": supplement["provided_at"],
            "provided_timezone": supplement["provided_timezone"],
        },
        "contradiction_request": {
            "contradiction_request_id": request["contradiction_request_id"],
            "request_hash": request["request_hash"],
            "contradiction_task_id": request["contradiction_task_id"],
            "local_task_id": request["local_task_id"],
            "task_identity_hash": request["task_identity_hash"],
        },
        "evidence_snapshot": {
            "evidence_snapshot_id": report["evidence_snapshot_id"],
            "evidence_snapshot_hash": report["evidence_snapshot_hash"],
        },
        "redaction_policy_id": context_redaction.POLICY_ID,
        "redaction_policy_hash": context_redaction.REDACTION_POLICY_HASH,
    }
    return _subject_result(
        subject_schema_version=CONTRADICTION_SUBJECT_SCHEMA_VERSION,
        task_type=CONTRADICTION_TASK_TYPE,
        raw_subject=raw_subject,
    )

def build_report_contradiction_subject(*, project_id: int, report_version_id: int) -> dict[str, object]:
                  """Resolve exact current durable contradiction facts; caller cannot inject AI/supplement truth."""
                  from app.report_contradiction import get_current_contradiction_request

                  project_id = _require_positive_id(project_id, "project_id")
                  report_version_id = _require_positive_id(report_version_id, "report_version_id")
                  review_bundle = report_review.get_review_bundle(
                      project_id=project_id, report_version_id=report_version_id
                  )
                  request = get_current_contradiction_request(
                      project_id=project_id, report_version_id=report_version_id
                  )
                  return build_report_contradiction_subject_from_durable_facts(
                      review_bundle=review_bundle, contradiction_request=request
                  )
