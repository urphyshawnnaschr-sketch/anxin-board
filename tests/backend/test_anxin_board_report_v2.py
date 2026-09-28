from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from app import main
import app.anxin_board_report_pipeline as pipeline
from app.anxin_board_report import MODULES, build_anxin_board_report
from app.anxin_board_report_store import (
    AnxinBoardReportApprovalRequiredError,
    AnxinBoardReportStoredInvalidError,
    load_anxin_board_report_history,
    load_latest_anxin_board_report,
    persist_anxin_board_report,
)
from app.anxin_board_report_v2 import (
    AnxinBoardReportV2Error,
    SCHEMA_VERSION,
    build_anxin_board_report_v2,
    validate_anxin_board_report_v2,
)
from app.client_stage_summary import build_client_stage_summary
from app.db import get_connection
from app.plain_language_change_summary import build_plain_language_change_summary
from app.project_profiles import read_current_confirmed_project_profile
from prd_fixtures import make_md


def _seed_historical_report(*, project_id: int, report: dict[str, object]) -> dict[str, object]:
    created_at = "2026-08-27T00:00:00+00:00"
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO anxin_board_reports "
            "(project_id, schema_version, report_date, report_hash, report_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                project_id,
                report["schema_version"],
                report["report_date"],
                report["anxin_board_report_hash"],
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
    return {
        "id": int(cursor.lastrowid),
        "project_id": project_id,
        "schema_version": report["schema_version"],
        "report_date": report["report_date"],
        "report_hash": report["anxin_board_report_hash"],
        "created_at": created_at,
        "report": deepcopy(report),
    }


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _profile(module_count: int = 3, *, status: str = "confirmed") -> dict[str, object]:
    content = {
        "schema_version": "project_profile_manual_v1",
        "project_summary": "项目档案",
        "modules": [
            {
                "client_id": f"module-{index + 1}",
                "name": f"业务模块 {index + 1}",
                "description": "",
                "prd_refs": [f"FR-{index + 1}"],
                "requirements": ["完成约定功能"],
                "paths": [
                    {
                        "type": "backend",
                        "pattern": f"apps/backend/app/module_{index + 1}/**",
                        "required": True,
                        "note": "",
                    }
                ],
                "exclusions": [],
            }
            for index in range(module_count)
        ],
        "domain_glossary": [],
        "exclude_patterns": [],
        "notes": "",
    }
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "id": 101,
        "project_id": 7,
        "version_no": 4,
        "source_prd_id": 55,
        "status": status,
        "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "content": content,
    }


def _rehash_profile(profile: dict[str, object]) -> None:
    canonical = json.dumps(
        profile["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    profile["content_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _daily_change() -> dict[str, object]:
    evidence = {
        "schema_version": "development_change_evidence_v1",
        "evidence_state": "complete",
        "has_change": True,
        "commit_count": 1,
        "changed_file_count": 2,
        "added_lines": 10,
        "deleted_lines": 2,
        "line_change_total": 12,
        "line_change_metric_role": "supporting_evidence_only",
        "production_change_file_count": 1,
        "test_change_file_count": 1,
        "ci_change_file_count": 0,
        "documentation_change_file_count": 0,
        "operations_change_file_count": 0,
        "other_change_file_count": 0,
        "affected_areas": [
            {"area": "backend", "file_count": 1},
            {"area": "backend_tests", "file_count": 1},
        ],
    }
    evidence["development_change_evidence_hash"] = _hash(evidence)
    return build_plain_language_change_summary(evidence=evidence)


def _summaries(profile: dict[str, object]) -> list[dict[str, object]]:
    return [
        build_client_stage_summary(module_name=item["name"], stage="开发中")
        for item in profile["content"]["modules"]
    ]


def _report(profile: dict[str, object]) -> dict[str, object]:
    return build_anxin_board_report_v2(
        project_name="动态模块项目",
        report_date="2026-08-27",
        profile=profile,
        module_summaries=_summaries(profile),
        daily_change=_daily_change(),
    )


@pytest.mark.parametrize("module_count", [3, 17])
def test_dynamic_profile_module_count_id_name_and_order_are_exact(module_count):
    profile = _profile(module_count)
    report = _report(profile)

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["module_count"] == module_count
    assert [item["module_id"] for item in report["modules"]] == [
        item["client_id"] for item in profile["content"]["modules"]
    ]
    assert [item["name"] for item in report["modules"]] == [
        item["name"] for item in profile["content"]["modules"]
    ]
    assert validate_anxin_board_report_v2(report, profile=profile) == report


def test_profile_identity_is_hash_bound_and_tamper_fails_closed():
    profile = _profile()
    report = _report(profile)

    assert report["profile_id"] == profile["id"]
    assert report["profile_version_no"] == profile["version_no"]
    assert report["profile_content_hash"] == profile["content_hash"]
    assert report["source_prd_id"] == profile["source_prd_id"]

    for key, bad in (
        ("profile_id", 999),
        ("profile_version_no", 999),
        ("profile_content_hash", "0" * 64),
        ("source_prd_id", 999),
    ):
        changed = deepcopy(report)
        changed[key] = bad
        payload = {
            name: deepcopy(value)
            for name, value in changed.items()
            if name != "anxin_board_report_hash"
        }
        changed["anxin_board_report_hash"] = _hash(payload)
        with pytest.raises(AnxinBoardReportV2Error):
            validate_anxin_board_report_v2(changed, profile=profile)


def test_profile_content_hash_drift_and_duplicate_client_id_fail_closed():
    profile = _profile()
    changed = deepcopy(profile)
    changed["content"]["modules"][0]["name"] = "被篡改名称"
    with pytest.raises(AnxinBoardReportV2Error):
        _report(changed)

    duplicate = _profile()
    duplicate["content"]["modules"][1]["client_id"] = duplicate["content"]["modules"][0]["client_id"]
    _rehash_profile(duplicate)
    with pytest.raises(AnxinBoardReportV2Error):
        _report(duplicate)


def test_v2_recloses_profile_completeness_and_path_safety_even_for_internal_callers():
    incomplete = _profile()
    incomplete["content"]["modules"][0]["requirements"] = []
    _rehash_profile(incomplete)
    with pytest.raises(AnxinBoardReportV2Error):
        _report(incomplete)

    unsafe_path = _profile()
    unsafe_path["content"]["modules"][0]["paths"][0]["pattern"] = "../secret/**"
    _rehash_profile(unsafe_path)
    with pytest.raises(AnxinBoardReportV2Error):
        _report(unsafe_path)


def test_new_report_requires_confirmed_profile_but_history_can_validate_superseded_bound_profile():
    profile = _profile()
    report = _report(profile)

    historical = deepcopy(profile)
    historical["status"] = "superseded"
    assert validate_anxin_board_report_v2(report, profile=historical) == report

    with pytest.raises(AnxinBoardReportV2Error):
        build_anxin_board_report_v2(
            project_name="动态模块项目",
            report_date="2026-08-27",
            profile=historical,
            module_summaries=_summaries(historical),
            daily_change=_daily_change(),
        )

    candidate = deepcopy(profile)
    candidate["status"] = "candidate"
    with pytest.raises(AnxinBoardReportV2Error):
        validate_anxin_board_report_v2(report, profile=candidate)


def test_same_client_id_survives_name_change_without_machine_identity_reconstruction():
    before = _profile()
    before_report = _report(before)

    after = deepcopy(before)
    after["version_no"] = 5
    after["content"]["modules"][0]["name"] = "重命名后的模块"
    _rehash_profile(after)
    after_report = _report(after)

    assert before_report["modules"][0]["module_id"] == after_report["modules"][0]["module_id"]
    assert after_report["modules"][0]["name"] == "重命名后的模块"


def test_caller_cannot_substitute_module_name_or_order_through_stage_summary():
    profile = _profile()
    summaries = _summaries(profile)
    summaries[0] = build_client_stage_summary(module_name="另一套模块真相", stage="开发中")
    with pytest.raises(AnxinBoardReportV2Error):
        build_anxin_board_report_v2(
            project_name="动态模块项目",
            report_date="2026-08-27",
            profile=profile,
            module_summaries=summaries,
            daily_change=_daily_change(),
        )

    reversed_summaries = list(reversed(_summaries(profile)))
    with pytest.raises(AnxinBoardReportV2Error):
        build_anxin_board_report_v2(
            project_name="动态模块项目",
            report_date="2026-08-27",
            profile=profile,
            module_summaries=reversed_summaries,
            daily_change=_daily_change(),
        )


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(tmp_path / "prdroot"))
    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client


def _create_project_with_confirmed_prd(client: TestClient) -> int:
    created = client.post("/api/projects", json={"name": "动态模块项目"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/prd-versions",
        files={"file": ("prd.md", make_md("PRD 正文"), "application/octet-stream")},
    )
    assert upload.status_code == 201, upload.text
    confirm_prd = client.post(
        f"/api/prd-versions/{upload.json()['id']}/confirm",
        json={"confirmed_by": "项目经理"},
    )
    assert confirm_prd.status_code == 200, confirm_prd.text
    return project_id


def _create_confirmed_profile(client: TestClient, module_count: int = 3, *, project_id=None):
    if project_id is None:
        project_id = _create_project_with_confirmed_prd(client)

    content = _profile(module_count)["content"]
    candidate = client.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert candidate.status_code == 201, candidate.text
    confirmed = client.post(
        f"/api/profile-candidates/{candidate.json()['id']}/confirm",
        json={"edit_version": candidate.json()["edit_version"], "confirmed_by": "项目经理"},
    )
    assert confirmed.status_code == 200, confirmed.text
    return project_id, confirmed.json()


def test_internal_current_confirmed_readback_recloses_exact_database_authority(api_env):
    project_id, confirmed = _create_confirmed_profile(api_env, 3)
    authority = read_current_confirmed_project_profile(project_id)

    assert authority["id"] == confirmed["id"]
    assert authority["version_no"] == confirmed["version_no"]
    assert authority["source_prd_id"] == confirmed["source_prd_id"]
    assert authority["content_hash"] == confirmed["content_hash"]
    assert [item["client_id"] for item in authority["content"]["modules"]] == [
        "module-1",
        "module-2",
        "module-3",
    ]


def test_v2_pipeline_cannot_create_new_formal_report_after_v3_admission(api_env):
    project_id, _ = _create_confirmed_profile(api_env, 3)
    authority = read_current_confirmed_project_profile(project_id)

    with pytest.raises(AnxinBoardReportApprovalRequiredError):
        pipeline.assemble_and_persist_anxin_board_report_v2(
            project_id=project_id,
            project_name="动态模块项目",
            report_date="2026-08-27",
            module_summaries=_summaries(authority),
            daily_change=_daily_change(),
        )
    assert load_latest_anxin_board_report(project_id=project_id) is None



def test_mixed_v1_v2_history_dispatches_explicitly_and_old_v2_survives_profile_supersession(api_env):
    project_id, _ = _create_confirmed_profile(api_env, 3)
    authority = read_current_confirmed_project_profile(project_id)

    v1 = build_anxin_board_report(
        project_name="动态模块项目",
        report_date="2026-08-26",
        module_summaries=[
            build_client_stage_summary(module_name=name, stage="开发中")
            for _, name in MODULES
        ],
        daily_change=_daily_change(),
    )
    _seed_historical_report(project_id=project_id, report=v1)

    v2 = build_anxin_board_report_v2(
        project_name="动态模块项目",
        report_date="2026-08-27",
        profile=authority,
        module_summaries=_summaries(authority),
        daily_change=_daily_change(),
    )
    v2_record = _seed_historical_report(project_id=project_id, report=v2)
    assert [item["schema_version"] for item in load_anxin_board_report_history(project_id=project_id)] == [
        SCHEMA_VERSION,
        "anxin_board_report_v1",
    ]

    _create_confirmed_profile(api_env, 4, project_id=project_id)
    history = load_anxin_board_report_history(project_id=project_id)
    assert history[0]["report_hash"] == v2_record["report_hash"]
    assert history[0]["report"]["module_count"] == 3



def test_never_confirmed_superseded_candidate_cannot_masquerade_as_v2_history(api_env):
    project_id = _create_project_with_confirmed_prd(api_env)
    content = _profile(3)["content"]
    first = api_env.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert first.status_code == 201, first.text

    fake_authority = deepcopy(first.json())
    fake_authority["status"] = "confirmed"
    forged_report = build_anxin_board_report_v2(
        project_name="动态模块项目",
        report_date="2026-08-27",
        profile=fake_authority,
        module_summaries=_summaries(fake_authority),
        daily_change=_daily_change(),
    )

    second = api_env.post(f"/api/projects/{project_id}/profile-candidates", json=content)
    assert second.status_code == 201, second.text
    refreshed_first = api_env.get(f"/api/profile-candidates/{first.json()['id']}")
    assert refreshed_first.status_code == 200
    assert refreshed_first.json()["status"] == "superseded"
    assert refreshed_first.json()["confirmed_at"] is None

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO anxin_board_reports (
                project_id, schema_version, report_date, report_hash, report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                forged_report["schema_version"],
                forged_report["report_date"],
                forged_report["anxin_board_report_hash"],
                json.dumps(forged_report, ensure_ascii=False, separators=(",", ":")),
                "2026-08-27T00:00:00+00:00",
            ),
        )

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_anxin_board_report_history(project_id=project_id)


def test_profile_identity_drift_between_build_and_persist_fails_closed(api_env):
    project_id, _ = _create_confirmed_profile(api_env, 3)
    authority = read_current_confirmed_project_profile(project_id)
    report = build_anxin_board_report_v2(
        project_name="动态模块项目",
        report_date="2026-08-27",
        profile=authority,
        module_summaries=_summaries(authority),
        daily_change=_daily_change(),
    )

    _create_confirmed_profile(api_env, 4, project_id=project_id)
    with pytest.raises(AnxinBoardReportStoredInvalidError):
        persist_anxin_board_report(project_id=project_id, report=report)
