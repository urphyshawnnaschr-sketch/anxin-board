"""HTTP contract tests for the frozen R2 direct-formal generation fail-closed gate."""

import importlib
import inspect
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import anxin_board_generation_api as generation_api  # noqa: E402
from app import main  # noqa: E402
from app.anxin_board_report import MODULES  # noqa: E402
from app.db import get_connection  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "report_date": "2026-08-27",
        "git_snapshot_id": 123,
        "module_stages": {"legacy": "开发中"},
        "manager_supplement": "人工补充。",
    }
    payload.update(overrides)
    return payload


def _legacy_11_stages() -> dict[str, str]:
    return {module_id: "开发中" for module_id, _module_name in MODULES}


def _create_project(client: TestClient, name: str = "直通旁路封锁测试项目") -> dict:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def _formal_row_count(project_id: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM anxin_board_reports WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
        )


def test_valid_existing_project_fails_closed_before_formal_persistence(client):
    project = _create_project(client)
    before = _formal_row_count(project["id"])

    response = client.post(
        f"/api/projects/{project['id']}/anxin-board/generate",
        json=_payload(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "ANXIN_BOARD_FORMAL_GENERATION_NOT_READY",
            "message": "正式报告主链尚未完成，当前禁止直接生成正式报告。",
        }
    }
    assert _formal_row_count(project["id"]) == before == 0


def test_complete_legacy_fixed_11_module_payload_is_still_blocked(client):
    project = _create_project(client, "旧十一模块旁路测试项目")
    stages = _legacy_11_stages()
    assert len(stages) == 11

    response = client.post(
        f"/api/projects/{project['id']}/anxin-board/generate",
        json=_payload(module_stages=stages),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANXIN_BOARD_FORMAL_GENERATION_NOT_READY"
    assert _formal_row_count(project["id"]) == 0


def test_generation_pipeline_zero_call_even_if_fail_if_called_seam_is_injected(client, monkeypatch):
    project = _create_project(client, "旧生成流水线零调用测试项目")
    calls = 0

    def forbidden_pipeline(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("legacy generation pipeline must never be called")

    monkeypatch.setattr(
        generation_api,
        "generate_and_persist_anxin_board_report",
        forbidden_pipeline,
        raising=False,
    )

    response = client.post(
        f"/api/projects/{project['id']}/anxin-board/generate",
        json=_payload(module_stages=_legacy_11_stages()),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANXIN_BOARD_FORMAL_GENERATION_NOT_READY"
    assert calls == 0
    assert _formal_row_count(project["id"]) == 0


@pytest.mark.parametrize(
    "payload",
    [
        _payload(module_stages={}),
        _payload(module_stages=["caller", "supplied", "stages"]),
        _payload(manager_supplement="另一份人工输入。"),
        _payload(report_date={"caller": "controlled"}),
    ],
)
def test_syntactically_valid_legacy_payload_variants_cannot_bypass_gate(client, payload):
    project = _create_project(client, "旁路变体测试项目")

    response = client.post(
        f"/api/projects/{project['id']}/anxin-board/generate",
        json=payload,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANXIN_BOARD_FORMAL_GENERATION_NOT_READY"
    assert _formal_row_count(project["id"]) == 0


def test_missing_project_preserves_existing_404_precedence(client):
    response = client.post("/api/projects/999999/anxin-board/generate", json=_payload())

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize("missing", ["report_date", "git_snapshot_id", "module_stages"])
def test_missing_required_transport_field_remains_framework_422(client, missing):
    payload = _payload()
    payload.pop(missing)

    response = client.post("/api/projects/1/anxin-board/generate", json=payload)

    assert response.status_code == 422


def test_malformed_transport_values_remain_framework_422(client):
    response = client.post(
        "/api/projects/not-an-int/anxin-board/generate",
        json=_payload(),
    )
    assert response.status_code == 422

    response = client.post(
        "/api/projects/1/anxin-board/generate",
        json=_payload(git_snapshot_id="not-an-int"),
    )
    assert response.status_code == 422


def test_extra_or_legacy_transport_fields_cannot_bypass_gate(client):
    project = _create_project(client, "额外字段测试项目")

    for extra in (
        {"project_name": "伪造项目名"},
        {"approved": True},
        {"formal": True},
        {"model_id": "caller-model"},
    ):
        response = client.post(
            f"/api/projects/{project['id']}/anxin-board/generate",
            json=_payload(**extra),
        )
        assert response.status_code == 422

    assert _formal_row_count(project["id"]) == 0


def test_generation_request_model_keeps_legacy_transport_shape_and_forbids_extra_fields():
    assert set(generation_api.GenerateAnxinBoardRequest.model_fields) == {
        "report_date",
        "git_snapshot_id",
        "module_stages",
        "manager_supplement",
    }
    assert generation_api.GenerateAnxinBoardRequest.model_config["extra"] == "forbid"


def test_generation_api_source_contains_no_formal_pipeline_or_external_side_effect_seam():
    source = inspect.getsource(generation_api)

    for forbidden in (
        "generate_and_persist_anxin_board_report",
        "persist_anxin_board_report",
        "assemble_and_persist_anxin_board_report",
        "get_connection",
        "sqlite3",
        "subprocess",
        "smtplib",
        "httpx",
        "requests",
        "socket",
        "model_gateway",
        "deepseek_transport",
    ):
        assert forbidden not in source

    assert source.count("get_project(project_id)") == 1
    assert source.count("FORMAL_GENERATION_NOT_READY") >= 2


def test_read_routes_remain_mounted_and_usable_after_write_gate(client):
    project = _create_project(client, "历史读取保留项目")
    paths = [route.path for route in main.app.routes]

    assert paths.count("/api/projects/{project_id}/anxin-board/generate") == 1
    assert paths.count("/api/projects/{project_id}/anxin-board/latest") == 1
    assert paths.count("/api/projects/{project_id}/anxin-board/history") == 1

    history = client.get(f"/api/projects/{project['id']}/anxin-board/history")
    assert history.status_code == 200
    assert history.json() == []

    latest = client.get(f"/api/projects/{project['id']}/anxin-board/latest")
    assert latest.status_code == 404
    assert latest.json()["detail"]["code"] == "ANXIN_BOARD_REPORT_NOT_AVAILABLE"
    assert _formal_row_count(project["id"]) == 0
