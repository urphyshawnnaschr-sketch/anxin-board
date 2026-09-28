from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import main  # noqa: E402
from app import project_profile_generation as core  # noqa: E402
import app.project_profile_generation_api as api  # noqa: E402


def _payload() -> core.GenerateProfilePayload:
    return core.GenerateProfilePayload(authorized=True)


def _request(key: str = "idem-profile-boundary-1"):
    return SimpleNamespace(headers={"local-idempotency-key": key})


def test_local_session_guard_rejects_before_attempt_or_canonical_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"attempt": 0, "delegate": 0}

    def reject_guard(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_SESSION_TEST_REJECTED"})

    def forbidden_attempt(*_args, **_kwargs):
        called["attempt"] += 1
        raise AssertionError("durable attempt must not run before local Human gate")

    def forbidden_delegate(*_args, **_kwargs):
        called["delegate"] += 1
        raise AssertionError("canonical generation must not run before local Human gate")

    monkeypatch.setattr(api, "require_local_write_request", reject_guard)
    monkeypatch.setattr(api, "execute_profile_generation_once", forbidden_attempt)
    monkeypatch.setattr(core, "generate_profile_candidate", forbidden_delegate)

    with pytest.raises(HTTPException) as caught:
        api.generate_profile_candidate(1, _payload(), _request())

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "LOCAL_SESSION_TEST_REJECTED"
    assert called == {"attempt": 0, "delegate": 0}


def test_mounted_wrapper_requires_idempotency_and_delegates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guards = []
    attempts = []
    delegates = []

    def guard(request, *, require_idempotency_key):
        guards.append((request.headers["local-idempotency-key"], require_idempotency_key))

    def delegate(project_id, payload):
        delegates.append((project_id, payload.authorized))
        return {"profile": {"id": 7, "status": "candidate"}, "generation": {"provider": "synthetic"}}

    def execute_once(*, project_id, idempotency_key, operation):
        attempts.append((project_id, idempotency_key))
        return operation()

    monkeypatch.setattr(api, "require_local_write_request", guard)
    monkeypatch.setattr(api, "execute_profile_generation_once", execute_once)
    monkeypatch.setattr(core, "generate_profile_candidate", delegate)

    result = api.generate_profile_candidate(9, _payload(), _request("idem-profile-exact"))

    assert guards == [("idem-profile-exact", True)]
    assert attempts == [(9, "idem-profile-exact")]
    assert delegates == [(9, True)]
    assert result["profile"]["id"] == 7
    assert result["generation"]["provider"] == "synthetic"


def test_attempt_ledger_finalize_failure_is_exposed_only_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "require_local_write_request", lambda *_args, **_kwargs: None)

    def finalize_failure(**_kwargs):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "PROFILE_GENERATION_ATTEMPT_FINALIZE_FAILED",
                "message": "synthetic durable finalize failure",
            },
        )

    monkeypatch.setattr(api, "execute_profile_generation_once", finalize_failure)

    with pytest.raises(HTTPException) as caught:
        api.generate_profile_candidate(9, _payload(), _request("idem-profile-finalize"))

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROFILE_GENERATION_RESULT_UNKNOWN"
    assert "不会自动重发" in caught.value.detail["message"]
    assert "synthetic durable finalize failure" not in caught.value.detail["message"]


def test_unexpected_attempt_storage_failure_is_exposed_only_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api,
        "execute_profile_generation_once",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic sqlite boundary loss")),
    )

    with pytest.raises(HTTPException) as caught:
        api.generate_profile_candidate(9, _payload(), _request("idem-profile-storage"))

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROFILE_GENERATION_RESULT_UNKNOWN"
    assert "synthetic sqlite boundary loss" not in caught.value.detail["message"]


def test_fastapi_mount_has_exactly_one_guarded_generation_route() -> None:
    assert main.project_profile_generation_router is api.router
    matches = [
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/api/projects/{project_id}/profile-candidates/generate"
        and "POST" in (getattr(route, "methods", set()) or set())
    ]

    assert len(matches) == 1
    assert matches[0].endpoint is api.generate_profile_candidate


def test_mounted_wrapper_cannot_reintroduce_provider_specific_transport() -> None:
    source = inspect.getsource(api).lower()

    assert "core.generate_profile_candidate" in source
    assert "execute_profile_generation_once" in source
    assert "require_idempotency_key=true" in source.replace(" ", "")
    assert "_transport" not in source
    assert "_read_deepseek" not in source
    assert "send_deepseek" not in source
    assert "deepseek_transport" not in source
