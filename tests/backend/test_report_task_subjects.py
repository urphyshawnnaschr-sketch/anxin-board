"""Page07 3B-2 exact task-subject and credential-safe framing tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import context_redaction, report_task_subjects  # noqa: E402


SYNTHETIC = "SYNTHETIC_CREDENTIAL_123456"
INLINE = "[REDACTED:CREDENTIAL]"


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_report() -> dict[str, object]:
    return {
        "report_version_id": 17,
        "schema_version": "report_version_v1",
        "project_id": 7,
        "version_no": 3,
        "parent_report_version_id": None,
        "model_execution_result_id": 71,
        "execution_result_hash": _hash("execution-71"),
        "formal_response_hash": _hash("formal-71"),
        "validated_result_hash": _hash("validated-71"),
        "model_call_id": 61,
        "call_identity_hash": _hash("call-61"),
        "evidence_snapshot_id": 51,
        "evidence_snapshot_hash": _hash("snapshot-51"),
        "report_content_hash": _hash("validated-71"),
        "lifecycle": "superseded",
        "state_version": 2,
        "created_at": "2026-09-01T00:00:00+00:00",
    }


def _replacement_task() -> dict[str, object]:
    task = {
        "id": 81,
        "schema_version": "report_generation_task_core_v1",
        "project_id": 7,
        "local_task_id": "report-reanalysis-17-abc",
        "evidence_snapshot_id": 51,
        "task_type": "daily_report_regenerate",
        "create_key": "reanalyze-17-abc",
        "current_attempt_id": "attempt-81",
        "state": "queued",
        "created_at": "2026-09-01T00:01:00+00:00",
        "updated_at": "2026-09-01T00:01:00+00:00",
    }
    task["identity_hash"] = _hash(
        {
            "schema_version": task["schema_version"],
            "local_task_id": task["local_task_id"],
            "project_id": task["project_id"],
            "evidence_snapshot_id": task["evidence_snapshot_id"],
            "task_type": task["task_type"],
        }
    )
    return task


def _request_hash_payload(request: dict[str, object]) -> dict[str, object]:
    fields = (
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
    return {field: request[field] for field in fields}


def _regenerate_bundle() -> dict[str, object]:
    report = _source_report()
    task = _replacement_task()
    request = {
        "reanalysis_request_id": 91,
        "schema_version": "report_reanalysis_request_v1",
        "project_id": 7,
        "report_version_id": 17,
        "evidence_snapshot_id": 51,
        "evidence_snapshot_hash": report["evidence_snapshot_hash"],
        "source_report_state_version": 1,
        "source_report_lifecycle": "pending_review",
        "error_location": f"Authorization: Bearer {SYNTHETIC}",
        "corrected_truth": f"应为 Q355。\npassword={SYNTHETIC}",
        "correction_basis": "项目经理复核后的确定事实。",
        "correction_source": f"https://example.test/fact?token={SYNTHETIC}",
        "requested_by": "pm-001",
        "requested_at": "2026-09-01T00:02:00+00:00",
        "requested_timezone": "Asia/Shanghai",
        "idempotency_key": "reanalyze-17-v1",
        "replacement_task_id": task["id"],
        "replacement_local_task_id": task["local_task_id"],
        "replacement_task_identity_hash": task["identity_hash"],
    }
    for field in (
        "error_location",
        "corrected_truth",
        "correction_basis",
        "correction_source",
    ):
        request[f"{field}_hash"] = _text_hash(request[field])
    request["request_hash"] = _hash(_request_hash_payload(request))
    return {
        "reanalysis_request": request,
        "replacement_task": task,
        "source_report_version": report,
    }


def _review_bundle() -> dict[str, object]:
    report = _source_report()
    report["lifecycle"] = "pending_review"
    report["state_version"] = 1
    supplement_text = f"补充事实仍有效。\npassword={SYNTHETIC}"
    return {
        "schema_version": "report_review_bundle_v1",
        "project_id": report["project_id"],
        "report_version": report,
        "ai_raw": {
            "model_execution_result_id": report["model_execution_result_id"],
            "execution_result_hash": report["execution_result_hash"],
            "formal_response_hash": report["formal_response_hash"],
            "validated_result_hash": report["report_content_hash"],
            "model_call_id": report["model_call_id"],
            "call_identity_hash": report["call_identity_hash"],
            "snapshot_id": report["evidence_snapshot_id"],
            "local_task_id": "daily-report-17",
            "task_type": "daily_report_generate",
            "provider": "deepseek",
            "model_id": "model-a",
            "model_version": "version-a",
            "actual_model": "actual-a",
            "provider_runtime_fingerprint": "fingerprint-a",
            "content": {
                "summary": f"AI 原文保留业务结论；Authorization: Bearer {SYNTHETIC}"
            },
        },
        "evidence_snapshot": {
            "snapshot_id": report["evidence_snapshot_id"],
            "snapshot_hash": report["evidence_snapshot_hash"],
        },
        "git_facts": {},
        "evidence_refs": [],
        "current_supplement": {
            "supplement_version_id": 101,
            "schema_version": "report_supplement_version_v1",
            "report_version_id": report["report_version_id"],
            "version_no": 1,
            "expected_previous_version_no": 0,
            "content": supplement_text,
            "content_hash": _text_hash(supplement_text),
            "source_type": "pm_correction",
            "provided_by": "pm-001",
            "provided_at": "2026-09-01T00:03:00+00:00",
            "provided_timezone": "Asia/Shanghai",
            "idempotency_key": "supplement-101",
        },
    }


def _contradiction_request(bundle: dict[str, object]) -> dict[str, object]:
    report = bundle["report_version"]
    supplement = bundle["current_supplement"]
    return {
        "contradiction_request_id": 111,
        "request_hash": _hash("contradiction-request-111"),
        "contradiction_task_id": 121,
        "local_task_id": "report-contradiction-17-101",
        "task_identity_hash": _hash("contradiction-task-121"),
        "task_type": "report_contradiction_check",
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


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def test_t01_structured_redaction_seam_is_deterministic_pure_and_covers_free_text_assignments():
    raw = {
        "ordinary": f"保留结论。\npassword={SYNTHETIC}",
        "nested": {"Authorization": f"Bearer {SYNTHETIC}"},
    }
    first = context_redaction.redact_credential_safe_structured_value(value=raw)
    second = context_redaction.redact_credential_safe_structured_value(value=deepcopy(raw))
    assert first == second
    assert first["schema_version"] == "credential_safe_structured_value_v1"
    assert first["redaction_policy_id"] == context_redaction.POLICY_ID
    assert first["redaction_policy_hash"] == context_redaction.REDACTION_POLICY_HASH
    assert first["redacted_value"]["ordinary"] == f"保留结论。\npassword={INLINE}"
    assert first["redacted_value"]["nested"]["Authorization"] == INLINE
    assert SYNTHETIC not in json.dumps(first, ensure_ascii=False)
    assert first["redacted_value_hash"] == _hash(first["redacted_value"])
    assert first["redaction_match_count"] >= 2


def test_t02_regenerate_subject_is_server_resolved_deterministic_and_model_visible(monkeypatch):
    durable = _regenerate_bundle()
    calls = []

    def resolved(**kwargs):
        calls.append(kwargs)
        return deepcopy(durable)

    monkeypatch.setattr(report_task_subjects.report_review, "get_reanalysis_request", resolved)
    first = report_task_subjects.build_daily_report_regenerate_subject(
        project_id=7, report_version_id=17
    )
    second = report_task_subjects.build_daily_report_regenerate_subject(
        project_id=7, report_version_id=17
    )
    assert first == second
    assert calls == [
        {"project_id": 7, "report_version_id": 17},
        {"project_id": 7, "report_version_id": 17},
    ]
    assert first["subject_schema_version"] == "page07_daily_report_regenerate_subject_v1"
    assert first["task_type"] == "daily_report_regenerate"
    assert first["task_subject_hash"] == _hash(first["subject"])
    correction = first["subject"]["reanalysis_request"]
    assert correction["error_location"] == f"Authorization: {INLINE}"
    assert correction["corrected_truth"] == f"应为 Q355。\npassword={INLINE}"
    assert correction["correction_basis"] == "项目经理复核后的确定事实。"
    assert correction["correction_source"] == f"https://example.test/fact?token={INLINE}"
    assert correction["corrected_truth_hash"] == durable["reanalysis_request"]["corrected_truth_hash"]
    assert SYNTHETIC not in json.dumps(first, ensure_ascii=False)


def test_t03_regenerate_correction_change_never_aliases_same_report_snapshot(monkeypatch):
    first_bundle = _regenerate_bundle()
    second_bundle = deepcopy(first_bundle)
    request = second_bundle["reanalysis_request"]
    request["corrected_truth"] = "应为 Q390。"
    request["corrected_truth_hash"] = _text_hash(request["corrected_truth"])
    request["request_hash"] = _hash(_request_hash_payload(request))

    monkeypatch.setattr(
        report_task_subjects.report_review,
        "get_reanalysis_request",
        lambda **_: deepcopy(first_bundle),
    )
    first = report_task_subjects.build_daily_report_regenerate_subject(
        project_id=7, report_version_id=17
    )
    monkeypatch.setattr(
        report_task_subjects.report_review,
        "get_reanalysis_request",
        lambda **_: deepcopy(second_bundle),
    )
    second = report_task_subjects.build_daily_report_regenerate_subject(
        project_id=7, report_version_id=17
    )
    assert first["subject"]["source_report"] == second["subject"]["source_report"]
    assert first["subject"]["evidence_snapshot"] == second["subject"]["evidence_snapshot"]
    assert first["subject"]["reanalysis_request"]["request_hash"] != second["subject"]["reanalysis_request"]["request_hash"]
    assert first["task_subject_hash"] != second["task_subject_hash"]


def test_t04_regenerate_rejects_corrupt_durable_hash_and_has_no_correction_caller_surface(monkeypatch):
    broken = _regenerate_bundle()
    broken["reanalysis_request"]["corrected_truth_hash"] = "f" * 64
    monkeypatch.setattr(
        report_task_subjects.report_review,
        "get_reanalysis_request",
        lambda **_: deepcopy(broken),
    )
    with pytest.raises(HTTPException) as caught:
        report_task_subjects.build_daily_report_regenerate_subject(
            project_id=7, report_version_id=17
        )
    assert _code(caught) == "REPORT_TASK_SUBJECT_STORED_INVALID"

    signature = inspect.signature(report_task_subjects.build_daily_report_regenerate_subject)
    assert list(signature.parameters) == ["project_id", "report_version_id"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    with pytest.raises(TypeError):
        report_task_subjects.build_daily_report_regenerate_subject(
            project_id=7,
            report_version_id=17,
            corrected_truth="caller override",
        )


def test_t05_contradiction_subject_binds_exact_ai_supplement_request_and_redacts_credentials():
    bundle = _review_bundle()
    request = _contradiction_request(bundle)
    first = report_task_subjects.build_report_contradiction_subject_from_durable_facts(
        review_bundle=deepcopy(bundle), contradiction_request=deepcopy(request)
    )
    second = report_task_subjects.build_report_contradiction_subject_from_durable_facts(
        review_bundle=deepcopy(bundle), contradiction_request=deepcopy(request)
    )
    assert first == second
    assert first["subject_schema_version"] == "page07_report_contradiction_subject_v1"
    assert first["task_type"] == "report_contradiction_check"
    assert first["task_subject_hash"] == _hash(first["subject"])
    subject = first["subject"]
    assert subject["source_report"]["report_version_id"] == 17
    assert subject["source_report"]["model_execution_result_id"] == 71
    assert "AI 原文保留业务结论" in subject["source_report"]["ai_raw"]["summary"]
    assert INLINE in subject["source_report"]["ai_raw"]["summary"]
    assert subject["supplement_version"]["content"] == f"补充事实仍有效。\npassword={INLINE}"
    assert SYNTHETIC not in json.dumps(first, ensure_ascii=False)


def test_t06_changed_supplement_changes_contradiction_subject_and_stale_request_fails_closed():
    bundle = _review_bundle()
    old_request = _contradiction_request(bundle)
    first = report_task_subjects.build_report_contradiction_subject_from_durable_facts(
        review_bundle=deepcopy(bundle), contradiction_request=deepcopy(old_request)
    )

    changed = deepcopy(bundle)
    supplement = changed["current_supplement"]
    supplement["supplement_version_id"] = 102
    supplement["version_no"] = 2
    supplement["expected_previous_version_no"] = 1
    supplement["content"] = "第二版补充事实。"
    supplement["content_hash"] = _text_hash(supplement["content"])
    supplement["idempotency_key"] = "supplement-102"
    with pytest.raises(HTTPException) as stale:
        report_task_subjects.build_report_contradiction_subject_from_durable_facts(
            review_bundle=deepcopy(changed), contradiction_request=deepcopy(old_request)
        )
    assert _code(stale) == "REPORT_TASK_SUBJECT_STORED_INVALID"

    new_request = _contradiction_request(changed)
    new_request["contradiction_request_id"] = 112
    new_request["contradiction_task_id"] = 122
    new_request["local_task_id"] = "report-contradiction-17-102"
    new_request["request_hash"] = _hash("contradiction-request-112")
    new_request["task_identity_hash"] = _hash("contradiction-task-122")
    second = report_task_subjects.build_report_contradiction_subject_from_durable_facts(
        review_bundle=deepcopy(changed), contradiction_request=deepcopy(new_request)
    )
    assert first["task_subject_hash"] != second["task_subject_hash"]


def test_t07_missing_or_corrupt_contradiction_durable_inputs_fail_closed():
    bundle = _review_bundle()
    request = _contradiction_request(bundle)

    missing = deepcopy(bundle)
    missing["current_supplement"] = None
    with pytest.raises(HTTPException) as caught:
        report_task_subjects.build_report_contradiction_subject_from_durable_facts(
            review_bundle=missing, contradiction_request=request
        )
    assert _code(caught) == "REPORT_TASK_SUBJECT_STORED_INVALID"

    drift = deepcopy(request)
    drift["evidence_snapshot_hash"] = "a" * 64
    with pytest.raises(HTTPException) as caught:
        report_task_subjects.build_report_contradiction_subject_from_durable_facts(
            review_bundle=bundle, contradiction_request=drift
        )
    assert _code(caught) == "REPORT_TASK_SUBJECT_STORED_INVALID"


def test_t08_subject_module_has_no_provider_network_db_or_authority_mutation_surface():
    source = inspect.getsource(report_task_subjects)
    for forbidden in (
        "httpx",
        "requests",
        "deepseek_transport",
        "deepseek_execution",
        "deepseek_current_authority",
        "model_gateway",
        "get_connection",
        "sqlite3",
        "mail_",
    ):
        assert forbidden not in source
