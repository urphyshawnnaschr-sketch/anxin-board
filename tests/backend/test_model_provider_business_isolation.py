from __future__ import annotations

import inspect

from app import main, project_profile_generation, report_generation_execution, report_generation_preparation
from app.model_provider_contract import ProviderCapability


def _synthetic_capability() -> ProviderCapability:
    return ProviderCapability(
        provider="synthetic-zero-network",
        model_id="synthetic-model",
        model_version="synthetic-model-v1",
        task_type="daily_report_generate",
        output_schema_version="daily-report/1.0",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
    )


def test_synthetic_provider_capability_flows_through_generic_preparation_policy() -> None:
    capability = report_generation_preparation._validate_capability(_synthetic_capability())
    qualification, authorization = report_generation_preparation._preparation_assertions(
        capability
    )
    budget = report_generation_preparation._budget_record(capability)

    assert qualification["provider"] == capability.provider
    assert qualification["model_id"] == capability.model_id
    assert qualification["model_version"] == capability.model_version
    assert qualification["qualification_status"] == "qualified"
    assert authorization == {
        "provider": capability.provider,
        "authorized": True,
        "valid": True,
    }
    assert budget["provider"] == capability.provider
    assert budget["model_id"] == capability.model_id
    assert budget["model_version"] == capability.model_version
    assert budget["context_window_tokens"] == capability.context_window_tokens
    assert budget["max_output_tokens"] == capability.max_output_tokens
    assert budget["budget_policy_version"] == "daily-report-synthetic-model/1.0"
    assert budget["reserved_output_tokens"] <= capability.max_output_tokens


def test_report_business_state_machine_has_no_provider_specific_dispatch() -> None:
    for module in (report_generation_preparation, report_generation_execution):
        source = inspect.getsource(module).lower()
        assert "deepseek" not in source
        assert "anthropic" not in source
        assert "openai" not in source

    preparation_source = inspect.getsource(report_generation_preparation)
    execution_source = inspect.getsource(report_generation_execution)

    assert "resolve_default_model_provider_adapter" in preparation_source
    assert "model_execution.execute_model_call" in execution_source
    assert "resolve_model_provider_adapter" not in execution_source
    assert "send_deepseek" not in execution_source
    assert "deepseek_execution" not in execution_source


def test_profile_generation_and_composition_root_do_not_bypass_provider_adapter() -> None:
    profile_source = inspect.getsource(project_profile_generation).lower()
    main_source = inspect.getsource(main).lower()

    assert "resolve_default_model_provider_adapter" in profile_source
    assert "providercredentialrequest" in profile_source
    assert "providerreceipt" in profile_source
    assert "deepseek_transport" not in profile_source
    assert "send_deepseek" not in profile_source

    assert "deepseek_transport" not in main_source
    assert "send_deepseek" not in main_source
    assert "._transport =" not in main_source
