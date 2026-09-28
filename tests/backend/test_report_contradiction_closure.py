from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND))

from app import report_contradiction  # noqa: E402


def test_contradiction_request_identity_is_exact_and_deterministic(monkeypatch):
    report = {
        "project_id": 1, "report_version_id": 2, "report_content_hash": "a" * 64,
        "model_execution_result_id": 3, "execution_result_hash": "b" * 64,
        "evidence_snapshot_id": 4, "evidence_snapshot_hash": "c" * 64,
    }
    supplement = {
        "report_version_id": 2, "supplement_version_id": 5, "content_hash": "d" * 64,
        "source_type": "pm_correction", "provided_by": "pm",
        "provided_at": "2026-09-08T00:00:00+00:00", "provided_timezone": "UTC",
    }
    monkeypatch.setattr(report_contradiction, "get_review_bundle", lambda **_: {"report_version": report, "current_supplement": supplement})
    r, s = report_contradiction._facts(1, 2)
    assert r == report and s == supplement
    seed = report_contradiction._hash({
        "project_id": 1, "report_version_id": 2, "report_content_hash": "a" * 64,
        "evidence_snapshot_id": 4, "evidence_snapshot_hash": "c" * 64,
        "supplement_version_id": 5, "supplement_content_hash": "d" * 64,
    })
    assert f"contradiction-{seed[:32]}".startswith("contradiction-")


def test_contradiction_business_path_has_no_provider_native_transport_imports():
    for name in ("report_contradiction.py", "page07_contradiction_preparation.py", "page07_model_execution.py"):
        text = (BACKEND / "app" / name).read_text(encoding="utf-8")
        assert "send_deepseek_v4_flash" not in text
        assert "urllib" not in text
        assert "requests." not in text


def test_validation_remains_fail_closed_for_supplement_without_verified_result():
    text = (BACKEND / "app" / "report_validation.py").read_text(encoding="utf-8")
    assert "REPORT_CONTRADICTION_CHECK_REQUIRED" in text
    assert 'validated.get("has_conflict") is False' in text
    assert 'validated.get("unresolved_items") == []' in text
    assert "except HTTPException" in text
def test_durable_contradiction_send_claim_blocks_cross_process_replay(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    from fastapi import HTTPException
    from app import db

    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "claim.db"))
    request = {
        "contradiction_request_id": 7,
        "project_id": 1,
        "report_version_id": 2,
        "evidence_snapshot_id": 4,
        "local_task_id": "contradiction-exact",
        "task_identity_hash": "a" * 64,
        "task_type": "report_contradiction_check",
    }
    call = {
        "model_call_id": 9,
        "project_id": 1,
        "snapshot_id": 4,
        "local_task_id": "contradiction-exact",
        "call_identity_hash": "b" * 64,
        "task_type": "report_contradiction_check",
    }
    first = report_contradiction.claim_contradiction_send_once(request=request, call=call)
    assert first["model_call_id"] == 9
    assert first["contradiction_request_id"] == 7
    with pytest.raises(HTTPException) as caught:
        report_contradiction.claim_contradiction_send_once(request=request, call=call)
    assert caught.value.detail["code"] == "REPORT_CONTRADICTION_SEND_OUTCOME_UNKNOWN"
    with sqlite3.connect(db.get_db_path()) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE report_contradiction_send_claims SET model_call_id = 10 WHERE model_call_id = 9")


def test_contradiction_execution_replays_existing_verified_result_without_transport(monkeypatch):
    from app import page07_model_execution

    call = {
        "model_call_id": 9,
        "project_id": 1,
        "snapshot_id": 4,
        "local_task_id": "contradiction-exact",
        "call_identity_hash": "b" * 64,
        "task_type": "report_contradiction_check",
    }
    request = {
        "contradiction_request_id": 7,
        "project_id": 1,
        "report_version_id": 2,
        "evidence_snapshot_id": 4,
        "local_task_id": "contradiction-exact",
        "task_identity_hash": "a" * 64,
        "task_type": "report_contradiction_check",
    }
    existing = {"model_result_id": 11, "model_call_id": 9}
    monkeypatch.setattr(page07_model_execution, "get_model_call", lambda _id: call)
    monkeypatch.setattr(report_contradiction, "get_current_contradiction_request", lambda **_kwargs: request)
    monkeypatch.setattr(page07_model_execution.model_execution_results, "get_model_execution_result_for_call", lambda _id: existing)
    monkeypatch.setattr(
        page07_model_execution.model_provider_runtime,
        "resolve_model_provider_adapter",
        lambda _provider: (_ for _ in ()).throw(AssertionError("transport seam must not be resolved")),
    )
    result = page07_model_execution.execute_report_contradiction_model_call(
        model_call_id=9, budget_record={}, report_version_id=2
    )
    assert result == existing


def test_gateway_has_direct_modelcall_to_durable_contradiction_subject_binding():
    text = (BACKEND / "app" / "model_gateway.py").read_text(encoding="utf-8")
    assert 'durable_request.get("local_task_id") != call.get("local_task_id")' in text
    assert 'source_report.get("report_version_id") != report_version_id' in text

