"""Anxin Board read API tests for persisted formal reports."""

from datetime import datetime, timezone
import importlib
import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import anxin_board_reports as anxin_board_reports_api  # noqa: E402
from app import main  # noqa: E402
from app.anxin_board_report import MODULES, build_anxin_board_report  # noqa: E402
from app.client_stage_summary import build_client_stage_summary  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.plain_language_change_summary import build_plain_language_change_summary  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _evidence() -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "development_change_evidence_v1",
        "evidence_state": "complete_no_change",
        "has_change": False,
        "commit_count": 0,
        "changed_file_count": 0,
        "added_lines": 0,
        "deleted_lines": 0,
        "line_change_total": 0,
        "line_change_metric_role": "supporting_evidence_only",
        "production_change_file_count": 0,
        "test_change_file_count": 0,
        "ci_change_file_count": 0,
        "documentation_change_file_count": 0,
        "operations_change_file_count": 0,
        "other_change_file_count": 0,
        "affected_areas": [],
    }
    result["development_change_evidence_hash"] = _hash(result)
    return result


def _formal_report(project_name: str, supplement: str = "同意大模型的日报。") -> dict:
    return build_anxin_board_report(
        project_name=project_name,
        report_date="2026-08-22",
        module_summaries=[
            build_client_stage_summary(module_name=name, stage="开发中")
            for _, name in MODULES
        ],
        daily_change=build_plain_language_change_summary(evidence=_evidence()),
        manager_supplement=supplement,
    )


def _create_project(client: TestClient, name: str = "安心看板测试项目") -> dict:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def _seed_legacy_report(*, project_id: int, report: dict[str, object]) -> dict[str, object]:
    """Test-local historical fixture seam; production V1/V2 creation remains fail-closed."""
    created_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO anxin_board_reports
            (project_id, schema_version, report_date, report_hash, report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                report["schema_version"],
                report["report_date"],
                report["anxin_board_report_hash"],
                json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                created_at,
            ),
        )
        row_id = int(cursor.lastrowid)
    return {
        "id": row_id,
        "project_id": project_id,
        "schema_version": report["schema_version"],
        "report_date": report["report_date"],
        "report_hash": report["anxin_board_report_hash"],
        "created_at": created_at,
        "report": report,
    }


def _persist_revisions(project: dict, supplements: list[str]) -> list[dict[str, object]]:
    return [
        _seed_legacy_report(
            project_id=project["id"],
            report=_formal_report(project["name"], supplement),
        )
        for supplement in supplements
    ]


def test_existing_project_without_formal_report_is_explicitly_unavailable(client):
    project = _create_project(client)

    response = client.get(f"/api/projects/{project['id']}/anxin-board/latest")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "ANXIN_BOARD_REPORT_NOT_AVAILABLE",
            "message": "当前还没有可展示的正式安心看板",
        }
    }


def test_missing_project_preserves_project_not_found(client):
    response = client.get("/api/projects/99999/anxin-board/latest")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_latest_endpoint_returns_exact_persisted_formal_report(client):
    project = _create_project(client)
    report = _formal_report(project["name"])
    _seed_legacy_report(project_id=project["id"], report=report)

    response = client.get(f"/api/projects/{project['id']}/anxin-board/latest")

    assert response.status_code == 200
    assert response.json() == report


def test_latest_endpoint_returns_newest_same_day_revision(client):
    project = _create_project(client, "纠正版 API 项目")
    older = _formal_report(project["name"], "第一版结论。")
    newest = _formal_report(project["name"], "第二版结论。")
    _seed_legacy_report(project_id=project["id"], report=older)
    _seed_legacy_report(project_id=project["id"], report=newest)

    response = client.get(f"/api/projects/{project['id']}/anxin-board/latest")

    assert response.status_code == 200
    assert response.json() == newest


def test_corrupt_latest_is_fail_visible_and_does_not_fallback(client):
    project = _create_project(client, "损坏 API 项目")
    older = _formal_report(project["name"], "第一版结论。")
    newest = _formal_report(project["name"], "第二版结论。")
    _seed_legacy_report(project_id=project["id"], report=older)
    latest_record = _seed_legacy_report(project_id=project["id"], report=newest)
    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ? WHERE id = ?",
            ("{}", latest_record["id"]),
        )

    response = client.get(f"/api/projects/{project['id']}/anxin-board/latest")

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "ANXIN_BOARD_REPORT_STORED_INVALID",
            "message": "最新正式安心看板数据无法确认",
        }
    }


def test_report_read_seam_does_not_create_or_mutate_project(client):
    project = _create_project(client)
    before = client.get(f"/api/projects/{project['id']}").json()

    response = client.get(f"/api/projects/{project['id']}/anxin-board/latest")
    after = client.get(f"/api/projects/{project['id']}").json()

    assert response.status_code == 404
    assert after == before


def test_history_existing_project_without_reports_returns_empty_list(client):
    project = _create_project(client, "空历史项目")

    response = client.get(f"/api/projects/{project['id']}/anxin-board/history")

    assert response.status_code == 200
    assert response.json() == []


def test_history_returns_exact_formal_record_shape(client):
    project = _create_project(client, "单条历史项目")
    record = _seed_legacy_report(
        project_id=project["id"],
        report=_formal_report(project["name"], "单条历史。"),
    )

    response = client.get(f"/api/projects/{project['id']}/anxin-board/history")

    assert response.status_code == 200
    assert response.json() == [record]
    assert set(response.json()[0]) == {
        "id",
        "project_id",
        "schema_version",
        "report_date",
        "report_hash",
        "created_at",
        "report",
    }


def test_history_preserves_store_id_desc_order(client):
    project = _create_project(client, "排序历史项目")
    records = _persist_revisions(project, ["第一版。", "第二版。", "第三版。"])

    response = client.get(f"/api/projects/{project['id']}/anxin-board/history")

    assert response.status_code == 200
    assert response.json() == [records[2], records[1], records[0]]


@pytest.mark.parametrize("limit", [1, 20, 50])
def test_history_accepts_frozen_valid_limits(client, limit):
    project = _create_project(client, f"limit-{limit}")

    response = client.get(
        f"/api/projects/{project['id']}/anxin-board/history?limit={limit}"
    )

    assert response.status_code == 200
    assert response.json() == []


def test_history_exclusive_cursor_pages_without_duplicates(client):
    project = _create_project(client, "游标分页项目")
    records = _persist_revisions(
        project,
        ["第一版。", "第二版。", "第三版。", "第四版。", "第五版。"],
    )

    first = client.get(
        f"/api/projects/{project['id']}/anxin-board/history?limit=2"
    )
    assert first.status_code == 200
    assert first.json() == [records[4], records[3]]

    cursor = first.json()[-1]["id"]
    second = client.get(
        f"/api/projects/{project['id']}/anxin-board/history"
        f"?before_id={cursor}&limit=2"
    )

    assert second.status_code == 200
    assert second.json() == [records[2], records[1]]
    assert {row["id"] for row in first.json()}.isdisjoint(
        {row["id"] for row in second.json()}
    )


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=51",
        "before_id=0",
        "before_id=-1",
        f"before_id={2**63}",
    ],
)
def test_history_domain_invalid_pagination_maps_to_stable_400(client, query):
    project = _create_project(client, f"非法分页-{query}")

    response = client.get(
        f"/api/projects/{project['id']}/anxin-board/history?{query}"
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "ANXIN_BOARD_REPORT_HISTORY_INPUT_INVALID",
            "message": "历史安心看板分页参数无效",
        }
    }


@pytest.mark.parametrize("query", ["limit=abc", "before_id=abc"])
def test_history_transport_unparseable_query_remains_fastapi_422(client, query):
    project = _create_project(client, f"transport-{query}")

    response = client.get(
        f"/api/projects/{project['id']}/anxin-board/history?{query}"
    )

    assert response.status_code == 422


@pytest.mark.parametrize("project_id", [99999, 2**63])
def test_history_missing_or_out_of_domain_project_maps_to_stable_404(client, project_id):
    response = client.get(f"/api/projects/{project_id}/anxin-board/history")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "PROJECT_NOT_FOUND",
            "message": "项目不存在或已被删除",
        }
    }


def test_history_corrupt_selected_row_is_500_without_partial_success(client):
    project = _create_project(client, "损坏历史项目")
    records = _persist_revisions(project, ["第一版。", "第二版。", "第三版。"])
    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ? WHERE id = ?",
            ("{}", records[1]["id"]),
        )

    response = client.get(
        f"/api/projects/{project['id']}/anxin-board/history?limit=3"
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "ANXIN_BOARD_REPORT_STORED_INVALID",
            "message": "历史正式安心看板数据无法确认",
        }
    }


def test_history_endpoint_calls_formal_seam_once_and_returns_exact_list(monkeypatch):
    payload = [{"opaque": "formal-history-record"}]
    calls = []

    def fake_history(*, project_id, before_id, limit):
        calls.append(
            {
                "project_id": project_id,
                "before_id": before_id,
                "limit": limit,
            }
        )
        return payload

    def forbidden_get_project(*_args, **_kwargs):
        raise AssertionError("History API must not pre-call get_project")

    monkeypatch.setattr(
        anxin_board_reports_api,
        "load_anxin_board_report_history",
        fake_history,
    )
    monkeypatch.setattr(anxin_board_reports_api, "get_project", forbidden_get_project)

    result = anxin_board_reports_api.get_anxin_board_report_history(
        project_id=7,
        before_id=11,
        limit=13,
    )

    assert result is payload
    assert calls == [{"project_id": 7, "before_id": 11, "limit": 13}]


def test_history_endpoint_source_is_only_thin_formal_seam_adapter():
    source = inspect.getsource(
        anxin_board_reports_api.get_anxin_board_report_history
    )
    module_source = Path(anxin_board_reports_api.__file__).read_text(encoding="utf-8")
    upper = source.upper()
    lowered_module = module_source.lower()

    assert source.count("load_anxin_board_report_history(") == 1
    assert "get_project(" not in source
    assert "get_connection" not in source
    assert "2**63" not in source
    assert "_SQLITE" not in source
    assert "OFFSET" not in upper
    for statement in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert statement not in upper
    for forbidden in ("requests", "httpx", "provider", "smtp", "subprocess"):
        assert forbidden not in lowered_module


def test_no_unapproved_public_write_route_exists_for_formal_reports(client):
    allowed_write_routes = {
        ("/api/projects/{project_id}/anxin-board/generate", "POST"),
        (
            "/api/projects/{project_id}/anxin-board/reports/{report_version_id}/module-narrative",
            "POST",
        ),
    }
    forbidden_methods = {"POST", "PUT", "PATCH", "DELETE"}
    offending = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        if "/anxin-board" not in path:
            continue
        for method in methods & forbidden_methods:
            if (path, method) not in allowed_write_routes:
                offending.append((path, method))
    assert offending == []
