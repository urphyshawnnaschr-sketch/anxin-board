from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from app.anxin_board_report_v3 import (
    AnxinBoardReportV3Error,
    SCHEMA_VERSION,
    build_anxin_board_report_v3,
    validate_anxin_board_report_v3,
)
from app.plain_language_change_summary import build_plain_language_change_summary


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _profile() -> dict[str, object]:
    content = {
        "schema_version": "project_profile_manual_v1",
        "project_summary": "项目档案",
        "modules": [
            {
                "client_id": f"module-{index}",
                "name": f"业务模块 {index}",
                "description": "",
                "prd_refs": [f"FR-{index}"],
                "requirements": ["完成约定功能"],
                "paths": [
                    {
                        "type": "backend",
                        "pattern": f"apps/backend/app/module_{index}/**",
                        "required": True,
                        "note": "",
                    }
                ],
                "exclusions": [],
            }
            for index in range(1, 4)
        ],
        "domain_glossary": [],
        "exclude_patterns": [],
        "notes": "",
    }
    canonical = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "id": 13,
        "project_id": 1,
        "version_no": 4,
        "source_prd_id": 7,
        "status": "confirmed",
        "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "content": content,
    }


def _profile_v2() -> dict[str, object]:
    modules = [
        {
            "client_id": f"module-{index}",
            "name": f"业务模块 {index}",
            "description": "",
            "prd_refs": [f"FR-{index}"],
            "requirements": ["完成约定功能"],
            "exclusions": [],
        }
        for index in range(1, 4)
    ]
    code_path_1 = {
        "type": "backend",
        "pattern": "apps/backend/app/module_1/**",
        "required": True,
        "note": "",
    }
    code_path_2 = {
        "type": "backend",
        "pattern": "apps/backend/app/module_2/**",
        "required": True,
        "note": "",
    }
    content = {
        "schema_version": "project_profile_v2",
        "project_summary": "全量项目状态基线",
        "planned_modules": modules,
        "implementation_mappings": [
            {
                "planned_module_id": "module-1",
                "status": "partial",
                "exact_head": "9" * 40,
                "evidence_ids": ["repo-code-module-1"],
                "paths": [code_path_1],
                "rationale": "全量代码证据证明已有部分实现。",
            },
            {
                "planned_module_id": "module-2",
                "status": "implemented",
                "exact_head": "9" * 40,
                "evidence_ids": ["repo-code-module-2"],
                "paths": [code_path_2],
                "rationale": "全量代码证据证明实现存在，但不等于完成测试。",
            },
            {
                "planned_module_id": "module-3",
                "status": "unknown",
                "exact_head": "",
                "evidence_ids": [],
                "paths": [],
                "rationale": "",
            },
        ],
        "unplanned_code_features": [],
        "domain_glossary": [],
        "exclude_patterns": [],
        "notes": "",
    }
    canonical = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "id": 21,
        "project_id": 1,
        "version_no": 8,
        "source_prd_id": 7,
        "status": "confirmed",
        "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "content": content,
    }


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


def _approval(profile: dict[str, object]) -> dict[str, object]:
    hashes = {
        "approval_snapshot_hash": "a" * 64,
        "report_content_hash": "b" * 64,
        "validation_result_hash": "c" * 64,
        "evidence_snapshot_hash": "d" * 64,
        "git_facts_hash": "e" * 64,
        "prd_source_hash": "1" * 64,
        "prd_parsed_hash": "2" * 64,
        "prd_structured_hash": "3" * 64,
        "prd_document_fingerprint": "4" * 64,
        "execution_result_hash": "5" * 64,
        "call_identity_hash": "6" * 64,
        "qualification_hash": "7" * 64,
        "authorization_hash": "8" * 64,
    }
    return {
        "approval_snapshot_id": 31,
        **hashes,
        "report_version_id": 3,
        "report_version_no": 2,
        "validation_result_id": 23,
        "evidence_snapshot_id": 11,
        "git_snapshot_id": 29,
        "git_branch": "main",
        "git_from_commit": "1" * 40,
        "git_to_commit": "2" * 40,
        "prd_id": profile["source_prd_id"],
        "profile_id": profile["id"],
        "profile_version_no": profile["version_no"],
        "profile_content_hash": profile["content_hash"],
        "model_execution_result_id": 17,
        "model_call_id": 19,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "actual_model": "deepseek-flash",
        "provider_runtime_fingerprint": "deepseek-runtime-v1",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "benchmark/1",
        "supplement_version_id": None,
        "supplement_content_hash": None,
        "supplement_provided_by": None,
        "supplement_provided_at": None,
        "supplement_provided_timezone": None,
        "supplement_source_type": None,
        "confirmed_by": "张经理",
        "confirmed_at": "2026-09-03T16:30:00+00:00",
        "confirmed_timezone": "Asia/Shanghai",
        "confirmed_utc_offset_minutes": 480,
        "human_acknowledged": True,
    }


def _profile_modules(profile: dict[str, object]) -> list[dict[str, object]]:
    content = profile["content"]
    if content["schema_version"] == "project_profile_v2":
        return content["planned_modules"]
    return content["modules"]


def _ai_raw(profile: dict[str, object], *, task_type: str = "daily_report_generate"):
    progress = [
        {
            "feature": module["name"],
            "stage": stage,
            "summary": f"{module['name']} 当前说明",
            "evidence_ids": [f"E-{index}"],
        }
        for index, (module, stage) in enumerate(
            zip(
                _profile_modules(profile),
                ("开发中", "等待测试", "已完成"),
                strict=True,
            ),
            start=1,
        )
    ]
    content = {"feature_progress": progress}
    if task_type == "daily_report_regenerate":
        content = {"new_report": content}
    return {"task_type": task_type, "content": content}


def test_cumulative_report_completion_does_not_invent_checks_and_validates():
    from app.project_progress import compute_snapshot, to_module_summaries
    profile = _profile_v2()
    approval = _approval(profile)
    progress = compute_snapshot(scope={"project_id": 1, "git_url": "https://example.test/repo.git",
        "branch": "main", "profile_id": profile["id"], "profile_content_hash": profile["content_hash"],
        "analysis_lineage_id": 1}, profile=profile, report={
        "report_version_id": 3, "report_content_hash": "b"*64, "approval_snapshot_id": 31,
        "approval_snapshot_hash": "a"*64, "parent_head_sha": "9"*40, "head_sha": "2"*40}, updates={})
    report = build_anxin_board_report_v3(project_name="客户项目", profile=profile, ai_raw=_ai_raw(profile),
        daily_change=_daily_change(), manager_supplement="继续推进剩余功能。", approval_snapshot=approval,
        module_summaries=to_module_summaries(progress))
    assert report["completed_module_count"] == 1
    assert "开发和检查" not in report["overall_message"]
    assert validate_anxin_board_report_v3(report, profile=profile, approval_snapshot=approval) == report


def test_v3_uses_profile_machine_identity_and_approved_stage_only():
    profile = _profile()
    approval = _approval(profile)
    report = build_anxin_board_report_v3(
        project_name="正式项目",
        profile=profile,
        ai_raw=_ai_raw(profile),
        daily_change=_daily_change(),
        manager_supplement="同意大模型的日报。",
        approval_snapshot=approval,
    )

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["report_date"] == "2026-09-04"
    assert [item["module_id"] for item in report["modules"]] == [
        item["client_id"] for item in profile["content"]["modules"]
    ]
    assert [item["name"] for item in report["modules"]] == [
        item["name"] for item in profile["content"]["modules"]
    ]
    assert [item["stage"] for item in report["modules"]] == [
        "开发中",
        "等待测试",
        "已完成",
    ]
    assert report["approval_snapshot_id"] == 31
    assert report["approval_snapshot_hash"] == "a" * 64
    assert report["profile_version_no"] == 4
    assert report["provider_runtime_fingerprint"] == "deepseek-runtime-v1"
    assert report["human_acknowledged"] is True
    assert validate_anxin_board_report_v3(
        report,
        profile=profile,
        approval_snapshot=approval,
    ) == report


def test_regenerate_uses_only_new_report_payload():
    profile = _profile()
    approval = _approval(profile)
    raw = _ai_raw(profile, task_type="daily_report_regenerate")
    raw["content"]["feature_progress"] = [
        {"feature": "伪旧报告", "stage": "已完成"}
    ]

    report = build_anxin_board_report_v3(
        project_name="正式项目",
        profile=profile,
        ai_raw=raw,
        daily_change=_daily_change(),
        manager_supplement="同意大模型的日报。",
        approval_snapshot=approval,
    )
    assert [item["name"] for item in report["modules"]] == [
        item["name"] for item in profile["content"]["modules"]
    ]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", ["开发中", "等待测试", "暂时无法确认"]),
        ("duplicate", ["暂时无法确认", "暂时无法确认", "已完成"]),
        ("extra", ["开发中", "等待测试", "已完成"]),
        ("renamed", ["开发中", "暂时无法确认", "已完成"]),
        ("invalid_stage", ["开发中", "暂时无法确认", "已完成"]),
        ("completed_without_evidence", ["开发中", "等待测试", "暂时无法确认"]),
    ],
)
def test_incomplete_or_ambiguous_feature_progress_degrades_per_module(mutation, expected):
    profile = _profile()
    approval = _approval(profile)
    raw = _ai_raw(profile)
    progress = raw["content"]["feature_progress"]
    if mutation == "missing":
        progress.pop()
    elif mutation == "duplicate":
        progress[1]["feature"] = progress[0]["feature"]
    elif mutation == "extra":
        progress.append({"feature": "额外模块", "stage": "开发中"})
    elif mutation == "renamed":
        progress[1]["feature"] = "猜出来的模块名"
    elif mutation == "invalid_stage":
        progress[1]["stage"] = "模型自创阶段"
    else:
        progress[2]["evidence_ids"] = []

    report = build_anxin_board_report_v3(
        project_name="正式项目",
        profile=profile,
        ai_raw=raw,
        daily_change=_daily_change(),
        manager_supplement="同意大模型的日报。",
        approval_snapshot=approval,
    )
    assert [item["name"] for item in report["modules"]] == [
        item["name"] for item in profile["content"]["modules"]
    ]
    assert [item["stage"] for item in report["modules"]] == expected


def test_v2_reconciliation_baseline_carries_safe_lower_bound_when_daily_ai_is_missing():
    profile = _profile_v2()
    approval = _approval(profile)
    raw = _ai_raw(profile)
    raw["content"]["feature_progress"] = []

    report = build_anxin_board_report_v3(
        project_name="正式项目",
        profile=profile,
        ai_raw=raw,
        daily_change=_daily_change(),
        manager_supplement="同意大模型的日报。",
        approval_snapshot=approval,
    )

    assert [item["stage"] for item in report["modules"]] == [
        "开发中",
        "等待测试",
        "暂时无法确认",
    ]
    assert report["completed_module_count"] == 0
    assert report["active_module_count"] == 2
    assert report["unknown_module_count"] == 1
    assert validate_anxin_board_report_v3(report, profile=profile, approval_snapshot=approval) == report


def test_daily_exact_stage_overrides_baseline_but_unsupported_completed_never_does():
    profile = _profile_v2()
    approval = _approval(profile)
    raw = _ai_raw(profile)
    progress = raw["content"]["feature_progress"]
    progress[0]["stage"] = "测试中"
    progress[1]["stage"] = "已完成"
    progress[1]["evidence_ids"] = []
    progress[2]["stage"] = "已完成"
    progress[2]["evidence_ids"] = ["today-evidence"]

    report = build_anxin_board_report_v3(
        project_name="正式项目",
        profile=profile,
        ai_raw=raw,
        daily_change=_daily_change(),
        manager_supplement="同意大模型的日报。",
        approval_snapshot=approval,
    )

    # Exact daily evidence may advance a module.  Missing evidence cannot turn the
    # historical implementation baseline into a formal completion claim.
    assert [item["stage"] for item in report["modules"]] == [
        "测试中",
        "等待测试",
        "已完成",
    ]


def test_profile_or_approval_provenance_tamper_fails_even_when_report_hash_is_rebuilt():
    profile = _profile()
    approval = _approval(profile)
    report = build_anxin_board_report_v3(
        project_name="正式项目",
        profile=profile,
        ai_raw=_ai_raw(profile),
        daily_change=_daily_change(),
        manager_supplement="同意大模型的日报。",
        approval_snapshot=approval,
    )

    tampered = deepcopy(report)
    tampered["git_branch"] = "other"
    payload = {
        key: deepcopy(value)
        for key, value in tampered.items()
        if key != "anxin_board_report_hash"
    }
    tampered["anxin_board_report_hash"] = _hash(payload)
    with pytest.raises(AnxinBoardReportV3Error):
        validate_anxin_board_report_v3(
            tampered,
            profile=profile,
            approval_snapshot=approval,
        )

    wrong_profile = deepcopy(profile)
    wrong_profile["version_no"] = 5
    with pytest.raises(AnxinBoardReportV3Error):
        validate_anxin_board_report_v3(
            report,
            profile=wrong_profile,
            approval_snapshot=approval,
        )
