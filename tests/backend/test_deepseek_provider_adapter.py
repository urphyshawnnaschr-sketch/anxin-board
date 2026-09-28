from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import HTTPException

from app import deepseek_current_authority, deepseek_provider_adapter as subject, deepseek_transport
from app.deepseek_provider_adapter import DeepSeekProviderAdapter
from app.model_provider_contract import ProviderRequest


def _capability(
    *,
    task_type: str = "daily_report_generate",
    output_schema_version: str = "daily-report/1.0",
) -> dict[str, object]:
    return {
        "schema_version": "deepseek_transport_capability_v1",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "endpoint_origin": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions",
        "task_type": task_type,
        "output_schema_version": output_schema_version,
        "response_format": "json_object",
        "stream": False,
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_output_tokens": 384000,
    }


def _qualification_identity(task_type: str) -> tuple[str, str, str]:
    if task_type == "daily_report_regenerate":
        return (
            "page07-regenerate-rules/2.0",
            "daily-report-regenerate/1.0",
            "page07-regenerate-qualification-pack/2.0",
        )
    return ("rules/1.0", "daily-report/1.0", "samples/1.0")


def _authority(
    *, model_call_id: int = 7, task_type: str = "daily_report_generate"
) -> dict[str, object]:
    regenerate = task_type == "daily_report_regenerate"
    purpose = (
        "anxin_board_daily_report_regenerate_v1"
        if regenerate
        else "anxin_board_daily_report_v1"
    )
    rule_version, output_schema_version, sample_pack_version = _qualification_identity(
        task_type
    )
    data_scope_hash = "f" * 64 if regenerate else "b" * 64

    if regenerate:
        record_hash = "a" * 64
        review_ref = "synthetic-review"
        qualification: dict[str, object] = {
            "schema_version": "deepseek_current_registry_qualification_v1",
            "authority_source_id": "product_model_qualification_registry",
            "authority_source_version": "v1",
            "authority_ref": {
                "record_hash": record_hash,
                "review_ref": review_ref,
            },
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "model_version": "DeepSeek-V4.1-Flash",
            "task_type": task_type,
            "ai_contract_schema_version": deepseek_current_authority.AI_CONTRACT_SCHEMA_VERSION,
            "output_schema_version": output_schema_version,
            "prompt_version": "page07-regenerate-prompt/1.0",
            "prompt_contract_hash": "1" * 64,
            "rule_version": rule_version,
            "sample_pack_version": sample_pack_version,
            "sampling_parameters_hash": "2" * 64,
            "qualification_status": "qualified",
            "sample_manifest_hash": "3" * 64,
            "qualification_harness_commit": "synthetic-harness-commit",
            "evidence_manifest_hash": "4" * 64,
            "review_ref": review_ref,
            "record_hash": record_hash,
            "context_window_tokens": 1000000,
            "max_output_tokens": 384000,
        }
    else:
        qualification = {
            "schema_version": "deepseek_current_model_qualification_v1",
            "authority_source_id": "deepseek_official_models_and_metadata",
            "authority_source_version": "v1",
            "authority_ref": {
                "model_list": "deepseek_api_models",
                "model_metadata": "deepseek_api_docs_models_pricing",
            },
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "model_version": "DeepSeek-V4.1-Flash",
            "rule_version": rule_version,
            "output_schema_version": output_schema_version,
            "benchmark_sample_pack_version": sample_pack_version,
            "qualification_status": "qualified",
            "context_window_tokens": 1000000,
            "max_output_tokens": 384000,
            "checked_at": "2026-09-06T00:00:00+00:00",
            "models_source_hash": "5" * 64,
            "metadata_source_hash": "6" * 64,
        }
    qualification["evidence_hash"] = subject._stable_hash(qualification)

    authorization: dict[str, object] = {
        "schema_version": "deepseek_current_data_authorization_v1",
        "authority_source_id": "windows_process_env_human_permit",
        "authority_source_version": "v1",
        "authority_ref": "process-env/exact-scope",
        "provider": "deepseek",
        "authorized": True,
        "valid": True,
        "purpose_id": purpose,
        "data_scope_hash": data_scope_hash,
    }
    authorization["evidence_hash"] = subject._stable_hash(authorization)
    value: dict[str, object] = {
        "schema_version": "deepseek_current_authority_v1",
        "model_call_id": model_call_id,
        "call_identity_hash": "d" * 64,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "context_window_tokens": 1000000,
        "max_output_tokens": 384000,
        "purpose_id": purpose,
        "data_scope_hash": data_scope_hash,
        "qualification": qualification,
        "authorization": authorization,
    }
    value["authority_hash"] = subject._stable_hash(value)
    return value


def _receipt() -> dict[str, object]:
    return {
        "provider": "deepseek",
        "provider_response_id": "response-1",
        "actual_model": "deepseek-flash",
        "provider_runtime_fingerprint": "runtime-1",
        "finish_reason": "stop",
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "result": {"status": "ok"},
    }


def _request(
    *,
    task_type: str = "daily_report_generate",
    output_schema_version: str = "daily-report/1.0",
) -> ProviderRequest:
    return ProviderRequest(
        model_call_id=7,
        call_identity_hash="d" * 64,
        provider="deepseek",
        model_id="deepseek-flash",
        model_version="DeepSeek-V4.1-Flash",
        task_type=task_type,
        output_schema_version=output_schema_version,
        messages=({"role": "user", "content": "synthetic-only"},),
        max_output_tokens=100,
    )


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail.get("code"))


def test_capability_delegates_to_existing_transport_without_io(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []

    def fake_capability(*, task_type: str, output_schema_version: str):
        observed.append((task_type, output_schema_version))
        return _capability(
            task_type=task_type,
            output_schema_version=output_schema_version,
        )

    monkeypatch.setattr(deepseek_transport, "get_deepseek_transport_capability", fake_capability)

    capability = DeepSeekProviderAdapter().get_capability(
        task_type="daily_report_generate",
        output_schema_version="daily-report/1.0",
    )

    assert observed == [("daily_report_generate", "daily-report/1.0")]
    assert capability.provider == "deepseek"
    assert capability.context_window_tokens == 1000000
    assert capability.max_output_tokens == 384000


def test_provider_specific_capability_shape_drift_fails_closed(monkeypatch) -> None:
    bad = _capability()
    bad["endpoint_origin"] = "https://unexpected.example"
    monkeypatch.setattr(
        deepseek_transport,
        "get_deepseek_transport_capability",
        lambda **_kwargs: bad,
    )

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().get_capability(
            task_type="daily_report_generate",
            output_schema_version="daily-report/1.0",
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"


def test_generate_authority_delegates_to_existing_authority(monkeypatch) -> None:
    observed: list[dict[str, object]] = []

    def fake_authority(**kwargs):
        observed.append(kwargs)
        return _authority(model_call_id=int(kwargs["model_call_id"]))

    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_current_authority",
        fake_authority,
    )

    authority = DeepSeekProviderAdapter().resolve_current_authority(
        model_call_id=7,
        final_context_manifest_hash="1" * 64,
        framed_payload_hash="2" * 64,
        task_type="daily_report_generate",
    )

    assert observed == [
        {
            "model_call_id": 7,
            "final_context_manifest_hash": "1" * 64,
            "framed_payload_hash": "2" * 64,
        }
    ]
    assert authority.provider == "deepseek"
    assert authority.model_call_id == 7
    assert authority.qualification_rule_version == "rules/1.0"
    assert authority.qualification_output_schema_version == "daily-report/1.0"
    assert authority.qualification_benchmark_sample_pack_version == "samples/1.0"
    assert authority.qualification_status == "qualified"
    assert authority.authorization_authorized is True
    assert authority.authorization_valid is True
    assert authority.authorization_evidence_hash == _authority()["authorization"]["evidence_hash"]


def test_provider_specific_qualification_source_drift_fails_closed(monkeypatch) -> None:
    bad = deepcopy(_authority())
    bad["qualification"]["authority_source_id"] = "unexpected-source"
    bad["qualification"]["evidence_hash"] = subject._stable_hash(
        {
            key: deepcopy(bad["qualification"][key])
            for key in bad["qualification"]
            if key != "evidence_hash"
        }
    )
    bad["authority_hash"] = subject._stable_hash(
        {key: deepcopy(bad[key]) for key in bad if key != "authority_hash"}
    )
    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_current_authority",
        lambda **_kwargs: bad,
    )

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().resolve_current_authority(
            model_call_id=7,
            final_context_manifest_hash="1" * 64,
            framed_payload_hash="2" * 64,
            task_type="daily_report_generate",
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"


def test_regenerate_authority_requires_and_forwards_exact_identity(monkeypatch) -> None:
    observed: list[dict[str, object]] = []

    def fake_authority(**kwargs):
        observed.append(kwargs)
        return _authority(
            model_call_id=int(kwargs["model_call_id"]),
            task_type="daily_report_regenerate",
        )

    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_regenerate_current_authority",
        fake_authority,
    )

    authority = DeepSeekProviderAdapter().resolve_current_authority(
        model_call_id=7,
        final_context_manifest_hash="1" * 64,
        framed_payload_hash="2" * 64,
        task_type="daily_report_regenerate",
        request_envelope_hash="3" * 64,
        prompt_contract_hash="4" * 64,
        sampling_parameters_hash="5" * 64,
    )

    assert observed[0]["request_envelope_hash"] == "3" * 64
    assert observed[0]["prompt_contract_hash"] == "4" * 64
    assert observed[0]["sampling_parameters_hash"] == "5" * 64
    assert authority.purpose_id == "anxin_board_daily_report_regenerate_v1"
    assert authority.qualification_rule_version == "page07-regenerate-rules/2.0"
    assert authority.qualification_output_schema_version == "daily-report-regenerate/1.0"
    assert (
        authority.qualification_benchmark_sample_pack_version
        == "page07-regenerate-qualification-pack/2.0"
    )


def test_execute_delegates_exactly_once_to_existing_single_send_transport(monkeypatch) -> None:
    observed: list[dict[str, object]] = []

    monkeypatch.setattr(
        deepseek_transport,
        "get_deepseek_transport_capability",
        lambda **kwargs: _capability(**kwargs),
    )

    def fake_send(**kwargs):
        observed.append(kwargs)
        return _receipt()

    monkeypatch.setattr(deepseek_transport, "send_deepseek_v4_flash", fake_send)

    receipt = DeepSeekProviderAdapter().execute(_request())

    assert len(observed) == 1
    assert observed[0]["max_tokens"] == 100
    assert observed[0]["messages"] == [{"role": "user", "content": "synthetic-only"}]
    assert receipt.provider == "deepseek"
    assert receipt.total_tokens == 14
    assert receipt.result == {"status": "ok"}


def test_unsupported_task_fails_before_authority_or_transport(monkeypatch) -> None:
    authority_calls = 0
    transport_calls = 0

    def unexpected_authority(**_kwargs):
        nonlocal authority_calls
        authority_calls += 1
        return _authority()

    def unexpected_transport(**_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        return _receipt()

    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_current_authority",
        unexpected_authority,
    )
    monkeypatch.setattr(deepseek_transport, "send_deepseek_v4_flash", unexpected_transport)

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().resolve_current_authority(
            model_call_id=7,
            final_context_manifest_hash="1" * 64,
            framed_payload_hash="2" * 64,
            task_type="unsupported-task",
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_UNSUPPORTED_IDENTITY"
    assert authority_calls == 0
    assert transport_calls == 0


def test_malformed_authority_fails_closed(monkeypatch) -> None:
    bad = deepcopy(_authority())
    bad["provider"] = "other-provider"
    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_current_authority",
        lambda **_kwargs: bad,
    )

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().resolve_current_authority(
            model_call_id=7,
            final_context_manifest_hash="1" * 64,
            framed_payload_hash="2" * 64,
            task_type="daily_report_generate",
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"


def test_tampered_provider_evidence_hash_fails_closed(monkeypatch) -> None:
    bad = deepcopy(_authority())
    bad["qualification"]["evidence_hash"] = "0" * 64
    bad["authority_hash"] = subject._stable_hash(
        {key: deepcopy(bad[key]) for key in bad if key != "authority_hash"}
    )
    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_current_authority",
        lambda **_kwargs: bad,
    )

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().resolve_current_authority(
            model_call_id=7,
            final_context_manifest_hash="1" * 64,
            framed_payload_hash="2" * 64,
            task_type="daily_report_generate",
        )
    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"
