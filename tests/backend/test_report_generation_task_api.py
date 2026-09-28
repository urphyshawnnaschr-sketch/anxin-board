"""HTTP contract tests for Report Generation Task admission/observation V1."""

import importlib
import inspect
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import main  # noqa: E402
from app import report_generation_task_api as task_api  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def _task(**overrides) -> dict[str, object]:
    task: dict[str, object] = {
        "id": 17,
        "schema_version": "report_generation_task_v1",
        "project_id": 41,
        "local_task_id": "daily-2026-08-25",
        "evidence_snapshot_id": 23,
        "task_type": "daily_report_generate",
        "create_key": "create-2026-08-25",
        "current_attempt_id": "attempt-1",
        "state": "queued",
        "identity_hash": "a" * 64,
        "created_at": "2026-08-25T12:00:00Z",
        "updated_at": "2026-08-25T12:00:00Z",
        "current_attempt": {"attempt_id": "attempt-1", "state": "queued"},
    }
    task.update(overrides)
    return task


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "local_task_id": "daily-2026-08-25",
        "evidence_snapshot_id": 23,
        "create_key": "create-2026-08-25",
    }
    payload.update(overrides)
    return payload


def test_create_forwards_exact_admission_inputs_once_and_returns_201(client, monkeypatch):
    calls: list[dict[str, object]] = []
    record = _task()

    def fake_create(**kwargs):
        calls.append(kwargs)
        return record

    monkeypatch.setattr(task_api, "create_report_generation_task", fake_create)

    response = client.post(
        "/api/projects/41/report-generation-tasks",
        json=_payload(),
    )

    assert response.status_code == 201
    assert response.json() == record
    assert calls == [
        {
            "project_id": 41,
            "local_task_id": "daily-2026-08-25",
            "evidence_snapshot_id": 23,
            "create_key": "create-2026-08-25",
        }
    ]


def test_create_returns_exact_core_object_without_adapter_copy(monkeypatch):
    record = _task()
    monkeypatch.setattr(
        task_api,
        "create_report_generation_task",
        lambda **_kwargs: record,
    )

    payload = task_api.CreateReportGenerationTaskRequest(**_payload())
    result = task_api.create_report_generation_task_http(41, payload)

    assert result is record


def test_create_replay_keeps_201_and_same_durable_truth(client, monkeypatch):
    record = _task()
    calls = 0

    def fake_create(**_kwargs):
        nonlocal calls
        calls += 1
        return record

    monkeypatch.setattr(task_api, "create_report_generation_task", fake_create)

    first = client.post("/api/projects/41/report-generation-tasks", json=_payload())
    second = client.post("/api/projects/41/report-generation-tasks", json=_payload())

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json() == record
    assert calls == 2


def test_read_forwards_exact_logical_identity_once_and_returns_200(client, monkeypatch):
    calls: list[dict[str, object]] = []
    record = _task()

    def fake_get(**kwargs):
        calls.append(kwargs)
        return record

    monkeypatch.setattr(task_api, "get_report_generation_task", fake_get)

    response = client.get(
        "/api/projects/41/report-generation-tasks/daily-2026-08-25"
    )

    assert response.status_code == 200
    assert response.json() == record
    assert calls == [
        {"project_id": 41, "local_task_id": "daily-2026-08-25"}
    ]


def test_read_preserves_opaque_identity_containing_slash(client, monkeypatch):
    calls: list[dict[str, object]] = []
    opaque_id = "daily/2026-08-25"
    record = _task(local_task_id=opaque_id)

    def fake_get(**kwargs):
        calls.append(kwargs)
        return record

    monkeypatch.setattr(task_api, "get_report_generation_task", fake_get)

    response = client.get(
        "/api/projects/41/report-generation-tasks/daily/2026-08-25"
    )

    assert response.status_code == 200
    assert response.json() == record
    assert calls == [{"project_id": 41, "local_task_id": opaque_id}]


@pytest.mark.parametrize(
    "extra_field",
    [
        "state",
        "attempt_id",
        "task_type",
        "report_date",
        "git_snapshot_id",
        "module_stages",
        "manager_supplement",
        "provider",
        "model",
        "worker",
        "project_name",
        "unexpected",
    ],
)
def test_create_rejects_forbidden_transport_fields_before_core(
    client, monkeypatch, extra_field
):
    called = False

    def fail_if_called(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Task Core must not run for rejected transport fields")

    monkeypatch.setattr(task_api, "create_report_generation_task", fail_if_called)

    response = client.post(
        "/api/projects/41/report-generation-tasks",
        json=_payload(**{extra_field: "caller-value"}),
    )

    assert response.status_code == 422
    assert called is False


@pytest.mark.parametrize("bad_value", ["23", True, 23.5, None])
def test_create_requires_strict_evidence_snapshot_integer_before_core(
    client, monkeypatch, bad_value
):
    called = False

    def fail_if_called(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Task Core must not run for malformed transport integer")

    monkeypatch.setattr(task_api, "create_report_generation_task", fail_if_called)
    response = client.post(
        "/api/projects/41/report-generation-tasks",
        json=_payload(evidence_snapshot_id=bad_value),
    )

    assert response.status_code == 422
    assert called is False


def test_malformed_project_path_remains_framework_422(client, monkeypatch):
    called = False

    def fail_if_called(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Task Core must not run for malformed path identity")

    monkeypatch.setattr(task_api, "create_report_generation_task", fail_if_called)
    response = client.post(
        "/api/projects/not-an-int/report-generation-tasks",
        json=_payload(),
    )

    assert response.status_code == 422
    assert called is False


@pytest.mark.parametrize(
    ("code", "core_status", "expected_status"),
    [
        ("REPORT_GENERATION_TASK_INPUT_INVALID", 400, 400),
        ("REPORT_GENERATION_TASK_NOT_FOUND", 404, 404),
        ("REPORT_GENERATION_TASK_CREATE_CONFLICT", 409, 409),
        ("REPORT_GENERATION_TASK_LOGICAL_TASK_CONFLICT", 409, 409),
        ("REPORT_GENERATION_TASK_LOGICAL_IDENTITY_CONFLICT", 409, 409),
        ("REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT", 500, 409),
        ("REPORT_GENERATION_TASK_STORED_INVALID", 409, 500),
        ("REPORT_GENERATION_TASK_TRANSITION_INVALID", 409, 500),
    ],
)
def test_create_maps_only_frozen_task_core_codes(
    client, monkeypatch, code, core_status, expected_status
):
    detail = {"code": code, "message": "stable"}

    def raise_core(**_kwargs):
        raise HTTPException(status_code=core_status, detail=detail)

    monkeypatch.setattr(task_api, "create_report_generation_task", raise_core)
    response = client.post("/api/projects/41/report-generation-tasks", json=_payload())

    assert response.status_code == expected_status
    assert response.json()["detail"] == detail


def test_read_maps_not_found_and_stored_invalid(client, monkeypatch):
    cases = [
        ("REPORT_GENERATION_TASK_NOT_FOUND", 404, 404),
        ("REPORT_GENERATION_TASK_STORED_INVALID", 409, 500),
    ]

    for code, core_status, expected_status in cases:
        detail = {"code": code, "message": "stable"}

        def raise_core(**_kwargs):
            raise HTTPException(status_code=core_status, detail=detail)

        monkeypatch.setattr(task_api, "get_report_generation_task", raise_core)
        response = client.get("/api/projects/41/report-generation-tasks/task-1")
        assert response.status_code == expected_status
        assert response.json()["detail"] == detail


def test_unmapped_upstream_http_exception_is_preserved(client, monkeypatch):
    detail = {
        "code": "CONTEXT_CANDIDATE_PROFILE_INVALID",
        "message": "formal upstream truth",
    }

    def raise_upstream(**_kwargs):
        raise HTTPException(status_code=409, detail=detail)

    monkeypatch.setattr(task_api, "create_report_generation_task", raise_upstream)
    response = client.post("/api/projects/41/report-generation-tasks", json=_payload())

    assert response.status_code == 409
    assert response.json()["detail"] == detail


def test_request_model_is_transport_only_and_forbids_extra_fields():
    assert set(task_api.CreateReportGenerationTaskRequest.model_fields) == {
        "local_task_id",
        "evidence_snapshot_id",
        "create_key",
    }
    assert task_api.CreateReportGenerationTaskRequest.model_config["extra"] == "forbid"


def test_task_api_source_remains_thin_admission_observation_only():
    source = inspect.getsource(task_api)

    for forbidden in (
        "transition_report_generation_task",
        "generate_and_persist_anxin_board_report",
        "anxin_board_generation_api",
        "get_connection",
        "sqlite3",
        "subprocess",
        "smtplib",
        "httpx",
        "requests",
        "DEEPSEEK_API_KEY",
        "module_stages",
        "manager_supplement",
    ):
        assert forbidden not in source

    assert source.count("create_report_generation_task(") == 1
    assert source.count("get_report_generation_task(") == 1


def test_main_mounts_task_routes_without_regressing_report_routes():
    matching = [
        (route.path, frozenset(route.methods or set()))
        for route in main.app.routes
        if "report-generation-tasks" in route.path
    ]

    assert matching == [
        (
            "/api/projects/{project_id}/report-generation-tasks",
            frozenset({"POST"}),
        ),
        (
            "/api/projects/{project_id}/report-generation-tasks/active",
            frozenset({"GET"}),
        ),
        (
            "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}",
            frozenset({"GET"}),
        ),
        (
            "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/prepare-model-call",
            frozenset({"POST"}),
        ),
        (
            "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/send-authorization-preview",
            frozenset({"POST"}),
        ),
        (
            "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/authorize-send",
            frozenset({"POST"}),
        ),
        (
            "/api/projects/{project_id}/report-generation-tasks/{local_task_id:path}/execute-authorized",
            frozenset({"POST"}),
        ),
    ]

    paths = [route.path for route in main.app.routes]
    assert paths.count("/api/projects/{project_id}/anxin-board/generate") == 1
    assert paths.count("/api/projects/{project_id}/anxin-board/latest") == 1
    assert paths.count("/api/projects/{project_id}/anxin-board/history") == 1


@pytest.mark.parametrize("snapshot", [None, 23])
@pytest.mark.parametrize("record", [None, _task()])
def test_active_lookup_route_precedes_opaque_id_and_returns_nullable_task(client, monkeypatch, snapshot, record):
    calls = []
    def lookup(**kwargs):
        calls.append(kwargs)
        return record
    monkeypatch.setattr(task_api, "find_active_report_generation_task", lookup)
    monkeypatch.setattr(task_api, "get_report_generation_task", lambda **kwargs: pytest.fail("catchall used"))
    suffix = "" if snapshot is None else "?evidence_snapshot_id=23"
    response = client.get("/api/projects/41/report-generation-tasks/active" + suffix)
    assert response.status_code == 200
    assert response.json() == {"task": record}
    assert calls == [{"project_id": 41, "evidence_snapshot_id": snapshot}]


def test_active_lookup_ambiguity_is_fail_closed_409(client, monkeypatch):
    def lookup(**kwargs):
        raise HTTPException(409, {"code": "REPORT_GENERATION_TASK_ACTIVE_RECOVERY_AMBIGUOUS", "message": "ambiguous"})
    monkeypatch.setattr(task_api, "find_active_report_generation_task", lookup)
    response = client.get("/api/projects/41/report-generation-tasks/active")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REPORT_GENERATION_TASK_ACTIVE_RECOVERY_AMBIGUOUS"
