from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import app.anxin_board_generation_pipeline as pipeline
from app.anxin_board_report import DEFAULT_MANAGER_SUPPLEMENT, MODULES


FORMAL_STAGES = (
    "开发中",
    "等待联调",
    "等待测试",
    "测试中",
    "已完成",
    "暂时无法确认",
)


def _call(**overrides):
    values = {
        "project_id": 7,
        "project_name": "安心看板项目",
        "report_date": "2026-08-24",
        "git_snapshot_id": 13,
        "module_stages": {"opaque": "input"},
    }
    values.update(overrides)
    return pipeline.generate_and_persist_anxin_board_report(**values)


def test_signature_is_keyword_only_and_matches_frozen_entrypoint():
    signature = inspect.signature(pipeline.generate_and_persist_anxin_board_report)

    assert list(signature.parameters) == [
        "project_id",
        "project_name",
        "report_date",
        "git_snapshot_id",
        "module_stages",
        "manager_supplement",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert (
        signature.parameters["manager_supplement"].default
        == DEFAULT_MANAGER_SUPPLEMENT
    )


def test_three_formal_seams_run_exactly_once_in_frozen_order_with_exact_objects(
    monkeypatch,
):
    module_stages = {"formal": "stages"}
    module_summaries = [{"formal": "module-summary"}]
    daily_change = {"formal": "daily-change"}
    persistence_record = {"formal": "stored-record"}
    calls: list[tuple[str, object]] = []

    def fake_modules(*, module_stages):
        calls.append(("modules", module_stages))
        assert module_stages is globals_module_stages
        return module_summaries

    def fake_daily(*, project_id, git_snapshot_id):
        calls.append(("daily", (project_id, git_snapshot_id)))
        return daily_change

    def fake_persist(**kwargs):
        calls.append(("persist", kwargs))
        assert kwargs["module_summaries"] is module_summaries
        assert kwargs["daily_change"] is daily_change
        return persistence_record

    globals_module_stages = module_stages
    monkeypatch.setattr(pipeline, "build_anxin_board_module_summaries", fake_modules)
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        fake_daily,
    )
    monkeypatch.setattr(pipeline, "assemble_and_persist_anxin_board_report", fake_persist)

    result = _call(
        project_id=17,
        project_name="项目 A",
        report_date="2026-08-25",
        git_snapshot_id=23,
        module_stages=module_stages,
        manager_supplement="明确补充",
    )

    assert result is persistence_record
    assert [name for name, _payload in calls] == ["modules", "daily", "persist"]
    assert calls[1][1] == (17, 23)
    assert calls[2][1] == {
        "project_id": 17,
        "project_name": "项目 A",
        "report_date": "2026-08-25",
        "module_summaries": module_summaries,
        "daily_change": daily_change,
        "manager_supplement": "明确补充",
    }


def test_default_manager_supplement_is_forwarded_exactly(monkeypatch):
    observed = {}

    monkeypatch.setattr(
        pipeline,
        "build_anxin_board_module_summaries",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        lambda **_kwargs: {},
    )

    def fake_persist(**kwargs):
        observed.update(kwargs)
        return {"stored": True}

    monkeypatch.setattr(pipeline, "assemble_and_persist_anxin_board_report", fake_persist)

    _call()

    assert observed["manager_supplement"] is DEFAULT_MANAGER_SUPPLEMENT


def test_explicit_manager_supplement_is_forwarded_exactly(monkeypatch):
    supplement = "项目经理明确补充"
    observed = {}

    monkeypatch.setattr(
        pipeline,
        "build_anxin_board_module_summaries",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        lambda **_kwargs: {},
    )

    def fake_persist(**kwargs):
        observed.update(kwargs)
        return {"stored": True}

    monkeypatch.setattr(pipeline, "assemble_and_persist_anxin_board_report", fake_persist)

    _call(manager_supplement=supplement)

    assert observed["manager_supplement"] is supplement


def test_step1_error_propagates_and_steps2_and3_do_not_run(monkeypatch):
    error = RuntimeError("step1")

    def fail_step1(**_kwargs):
        raise error

    def forbidden(**_kwargs):
        raise AssertionError("later seam must not run after Step 1 failure")

    monkeypatch.setattr(pipeline, "build_anxin_board_module_summaries", fail_step1)
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        forbidden,
    )
    monkeypatch.setattr(pipeline, "assemble_and_persist_anxin_board_report", forbidden)

    with pytest.raises(RuntimeError) as caught:
        _call()

    assert caught.value is error


def test_step2_error_propagates_and_step3_does_not_run(monkeypatch):
    error = RuntimeError("step2")
    module_summaries = []
    step1_calls = 0

    def step1(**_kwargs):
        nonlocal step1_calls
        step1_calls += 1
        return module_summaries

    def fail_step2(**_kwargs):
        raise error

    def forbidden_step3(**_kwargs):
        raise AssertionError("Step 3 must not run after Step 2 failure")

    monkeypatch.setattr(pipeline, "build_anxin_board_module_summaries", step1)
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        fail_step2,
    )
    monkeypatch.setattr(
        pipeline,
        "assemble_and_persist_anxin_board_report",
        forbidden_step3,
    )

    with pytest.raises(RuntimeError) as caught:
        _call()

    assert caught.value is error
    assert step1_calls == 1


def test_step3_error_propagates_without_wrapping(monkeypatch):
    error = RuntimeError("step3")

    monkeypatch.setattr(
        pipeline,
        "build_anxin_board_module_summaries",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        lambda **_kwargs: {},
    )

    def fail_step3(**_kwargs):
        raise error

    monkeypatch.setattr(pipeline, "assemble_and_persist_anxin_board_report", fail_step3)

    with pytest.raises(RuntimeError) as caught:
        _call()

    assert caught.value is error


def test_scalar_inputs_are_forwarded_only_to_their_frozen_destinations(monkeypatch):
    observed = {"modules": [], "daily": [], "persist": []}
    module_stages = {"opaque": "stages"}
    module_summaries = []
    daily_change = {}

    def fake_modules(**kwargs):
        observed["modules"].append(kwargs)
        return module_summaries

    def fake_daily(**kwargs):
        observed["daily"].append(kwargs)
        return daily_change

    def fake_persist(**kwargs):
        observed["persist"].append(kwargs)
        return {"stored": True}

    monkeypatch.setattr(pipeline, "build_anxin_board_module_summaries", fake_modules)
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        fake_daily,
    )
    monkeypatch.setattr(pipeline, "assemble_and_persist_anxin_board_report", fake_persist)

    _call(
        project_id=31,
        project_name="绑定项目",
        report_date="2026-08-26",
        git_snapshot_id=37,
        module_stages=module_stages,
    )

    assert observed["modules"] == [{"module_stages": module_stages}]
    assert observed["modules"][0]["module_stages"] is module_stages
    assert observed["daily"] == [{"project_id": 31, "git_snapshot_id": 37}]
    assert observed["persist"] == [
        {
            "project_id": 31,
            "project_name": "绑定项目",
            "report_date": "2026-08-26",
            "module_summaries": module_summaries,
            "daily_change": daily_change,
            "manager_supplement": DEFAULT_MANAGER_SUPPLEMENT,
        }
    ]


def test_orchestration_is_deterministic_when_formal_seams_are_deterministic(monkeypatch):
    module_summaries = [{"same": "modules"}]
    daily_change = {"same": "daily"}
    record = {"same": "record"}

    monkeypatch.setattr(
        pipeline,
        "build_anxin_board_module_summaries",
        lambda **_kwargs: module_summaries,
    )
    monkeypatch.setattr(
        pipeline,
        "build_plain_language_change_summary_from_snapshot",
        lambda **_kwargs: daily_change,
    )
    monkeypatch.setattr(
        pipeline,
        "assemble_and_persist_anxin_board_report",
        lambda **_kwargs: record,
    )

    first = _call()
    second = _call()

    assert first is record
    assert second is record


def test_production_source_reuses_only_formal_seams_without_forbidden_capabilities():
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    upper = source.upper()

    assert (
        "from app.anxin_board_module_summaries import "
        "build_anxin_board_module_summaries"
    ) in source
    assert "from app.anxin_board_report import DEFAULT_MANAGER_SUPPLEMENT" in source
    assert (
        "from app.anxin_board_report_pipeline import "
        "assemble_and_persist_anxin_board_report"
    ) in source
    assert "build_plain_language_change_summary_from_snapshot" in source

    for module_id, module_name in MODULES:
        assert f'"{module_id}"' not in source
        assert f'"{module_name}"' not in source
    for stage in FORMAL_STAGES:
        assert f'"{stage}"' not in source

    for forbidden in (
        "get_connection",
        "sqlite",
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "git_client",
        "apirouter",
        "provider",
        "smtp",
        "scheduler",
        "hashlib",
        "client_stage_summary_v1",
        "plain_language_change_summary_v1",
        "anxin_board_report_v1",
        "overall_message",
    ):
        assert forbidden not in lowered
    for statement in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert statement not in upper
