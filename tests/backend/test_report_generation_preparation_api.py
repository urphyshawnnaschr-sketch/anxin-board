from __future__ import annotations

from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import report_generation_preparation_api as api  # noqa: E402


def test_prepare_http_forwards_only_path_identity_and_strict_authorization(monkeypatch):
    marker = {"preparation_state": "prepared", "provider_send_state": "not_attempted"}
    seen = {}

    def prepare(**kwargs):
        seen.update(kwargs)
        return marker

    monkeypatch.setattr(api, "prepare_report_generation_model_call", prepare)
    payload = api.PrepareReportGenerationRequest(preparation_authorized=True)

    result = api.prepare_report_generation_model_call_http(
        project_id=7,
        local_task_id="task-real-1",
        payload=payload,
    )

    assert result is marker
    assert seen == {
        "project_id": 7,
        "local_task_id": "task-real-1",
        "preparation_authorized": True,
    }


def test_request_schema_rejects_extra_fields():
    try:
        api.PrepareReportGenerationRequest(
            preparation_authorized=True,
            provider_send_authorized=True,
        )
    except Exception:
        return
    raise AssertionError("extra provider-send authority field must be rejected")
