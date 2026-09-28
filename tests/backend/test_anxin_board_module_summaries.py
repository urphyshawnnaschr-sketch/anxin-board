from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import app.anxin_board_module_summaries as module_summaries
from app.anxin_board_report import MODULES
from app.client_stage_summary import ClientStageSummaryError, build_client_stage_summary


FORMAL_STAGES = (
    "开发中",
    "等待联调",
    "等待测试",
    "测试中",
    "已完成",
    "暂时无法确认",
)


def _module_stages(stage: str = "开发中") -> dict[str, str]:
    return {module_id: stage for module_id, _module_name in MODULES}


def _mixed_module_stages() -> dict[str, str]:
    return {
        module_id: FORMAL_STAGES[index % len(FORMAL_STAGES)]
        for index, (module_id, _module_name) in enumerate(MODULES)
    }


def _assert_batch_invalid(callable_):
    with pytest.raises(module_summaries.AnxinBoardModuleSummariesError) as caught:
        callable_()
    assert caught.value.code == "ANXIN_BOARD_MODULE_SUMMARIES_INVALID"


def test_signature_is_keyword_only_and_matches_frozen_entrypoint():
    signature = inspect.signature(module_summaries.build_anxin_board_module_summaries)
    assert list(signature.parameters) == ["module_stages"]
    assert signature.parameters["module_stages"].kind is inspect.Parameter.KEYWORD_ONLY


def test_all_formal_modules_return_exact_core_payloads_in_module_order():
    stages = _module_stages()

    result = module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert len(result) == len(MODULES) == 11
    assert result == [
        build_client_stage_summary(module_name=module_name, stage=stages[module_id])
        for module_id, module_name in MODULES
    ]


def test_input_mapping_order_does_not_change_formal_output_order():
    reversed_stages = {
        module_id: "等待测试"
        for module_id, _module_name in reversed(MODULES)
    }

    result = module_summaries.build_anxin_board_module_summaries(
        module_stages=reversed_stages
    )

    assert [item["module_name"] for item in result] == [
        module_name for _module_id, module_name in MODULES
    ]


def test_all_six_formal_stages_are_delegated_to_existing_core():
    stages = _mixed_module_stages()

    result = module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert [item["stage"] for item in result] == [
        stages[module_id] for module_id, _module_name in MODULES
    ]
    assert set(item["stage"] for item in result) == set(FORMAL_STAGES)


def test_missing_module_id_fails_closed():
    stages = _module_stages()
    stages.pop(MODULES[0][0])

    _assert_batch_invalid(
        lambda: module_summaries.build_anxin_board_module_summaries(
            module_stages=stages
        )
    )


def test_extra_module_id_fails_closed():
    stages = _module_stages()
    stages["unexpected_module"] = "开发中"

    _assert_batch_invalid(
        lambda: module_summaries.build_anxin_board_module_summaries(
            module_stages=stages
        )
    )


@pytest.mark.parametrize("value", [None, [], (), "not-a-mapping", 1])
def test_non_mapping_input_fails_closed(value):
    _assert_batch_invalid(
        lambda: module_summaries.build_anxin_board_module_summaries(
            module_stages=value
        )
    )


def test_non_string_module_key_fails_closed():
    stages = _module_stages()
    stages.pop(MODULES[0][0])
    stages[1] = "开发中"

    _assert_batch_invalid(
        lambda: module_summaries.build_anxin_board_module_summaries(
            module_stages=stages
        )
    )


def test_illegal_stage_propagates_formal_core_error_without_batch_wrapping():
    stages = _module_stages()
    stages[MODULES[3][0]] = "尚未开始"

    with pytest.raises(ClientStageSummaryError) as caught:
        module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert caught.value.code == "CLIENT_STAGE_SUMMARY_INVALID"
    assert not isinstance(caught.value, module_summaries.AnxinBoardModuleSummariesError)


def test_explicit_unknown_stage_is_valid_and_not_inferred():
    stages = _module_stages()
    stages[MODULES[5][0]] = "暂时无法确认"

    result = module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert result[5] == build_client_stage_summary(
        module_name=MODULES[5][1],
        stage="暂时无法确认",
    )


def test_core_is_called_exactly_once_per_module_with_formal_name_stage_and_order(
    monkeypatch,
):
    stages = _mixed_module_stages()
    calls: list[tuple[str, str]] = []
    payloads: list[dict[str, object]] = []

    def fake_build_client_stage_summary(*, module_name, stage):
        calls.append((module_name, stage))
        payload = {"call": len(calls), "module_name": module_name, "stage": stage}
        payloads.append(payload)
        return payload

    monkeypatch.setattr(
        module_summaries,
        "build_client_stage_summary",
        fake_build_client_stage_summary,
    )

    result = module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    expected_calls = [
        (module_name, stages[module_id]) for module_id, module_name in MODULES
    ]
    assert calls == expected_calls
    assert len(calls) == len(MODULES) == 11
    assert result == payloads
    assert all(result[index] is payloads[index] for index in range(len(payloads)))


def test_mid_batch_core_failure_stops_immediately_without_later_calls(monkeypatch):
    stages = _module_stages()
    calls: list[str] = []
    fail_at = 4

    def fake_build_client_stage_summary(*, module_name, stage):
        calls.append(module_name)
        if len(calls) == fail_at:
            raise ClientStageSummaryError()
        return {"module_name": module_name, "stage": stage}

    monkeypatch.setattr(
        module_summaries,
        "build_client_stage_summary",
        fake_build_client_stage_summary,
    )

    with pytest.raises(ClientStageSummaryError) as caught:
        module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert caught.value.code == "CLIENT_STAGE_SUMMARY_INVALID"
    assert calls == [module_name for _module_id, module_name in MODULES[:fail_at]]


def test_input_mapping_is_not_modified():
    stages = _mixed_module_stages()
    original = dict(stages)

    module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert stages == original
    assert list(stages) == list(original)


def test_same_input_is_deterministic():
    stages = _mixed_module_stages()

    first = module_summaries.build_anxin_board_module_summaries(module_stages=stages)
    second = module_summaries.build_anxin_board_module_summaries(module_stages=stages)

    assert second == first


def test_batch_error_has_stable_code():
    error = module_summaries.AnxinBoardModuleSummariesError()
    assert str(error) == "ANXIN_BOARD_MODULE_SUMMARIES_INVALID"
    assert error.code == "ANXIN_BOARD_MODULE_SUMMARIES_INVALID"


def test_production_source_reuses_formal_modules_and_core_without_forbidden_io_or_algorithms():
    source = Path(module_summaries.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    upper = source.upper()

    assert "from app.anxin_board_report import MODULES" in source
    assert "from app.client_stage_summary import build_client_stage_summary" in source

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
        "APIRouter",
        "provider",
        "hashlib",
        "client_stage_summary_v1",
        "overall_message",
        "anxin_board_report_hash",
    ):
        assert forbidden.lower() not in lowered
    for statement in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert statement not in upper


def test_public_output_contains_only_existing_core_payload_shape():
    result = module_summaries.build_anxin_board_module_summaries(
        module_stages=_module_stages("测试中")
    )

    expected_keys = tuple(
        build_client_stage_summary(module_name=MODULES[0][1], stage="测试中")
    )
    assert all(tuple(item) == expected_keys for item in result)
    forbidden_keys = {
        "pr",
        "ci",
        "sha",
        "commit",
        "diff",
        "file_path",
        "provider",
        "prompt",
        "context",
        "percentage",
        "完成率",
        "质量评分",
        "个人绩效评分",
    }
    assert all(forbidden_keys.isdisjoint(item) for item in result)
