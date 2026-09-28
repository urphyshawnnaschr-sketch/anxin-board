from __future__ import annotations

from app import report_generation_recovery_api as api


def test_finalize_existing_route_only_runs_local_finalizer(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        api,
        "require_local_write_request",
        lambda request, require_idempotency_key: seen.update(
            request=request, require_idempotency_key=require_idempotency_key
        ),
    )
    monkeypatch.setattr(
        api,
        "finalize_report_generation_from_existing_task_result",
        lambda **kwargs: {
            "task_state": "succeeded",
            "execution_source": "existing_verified_result",
            "provider_retry_state": "not_retried",
            **kwargs,
        },
    )

    request = object()
    result = api.finalize_existing_report_generation_http(
        project_id=7,
        local_task_id="task-recover-1",
        request=request,
    )

    assert seen == {"request": request, "require_idempotency_key": False}
    assert result["project_id"] == 7
    assert result["local_task_id"] == "task-recover-1"
    assert result["task_state"] == "succeeded"
    assert result["provider_retry_state"] == "not_retried"
