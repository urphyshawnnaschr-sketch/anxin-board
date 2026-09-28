from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from app import (
    deepseek_current_authority,
    deepseek_transport,
    model_gateway,
    model_provider_gateway,
)
from app.deepseek_provider_adapter import DeepSeekProviderAdapter


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail.get("code"))


def test_deepseek_request_byte_accounting_matches_frozen_provider_shape() -> None:
    adapter = DeepSeekProviderAdapter()
    measured = adapter.estimate_request_utf8_bytes(
        messages=({"role": "user", "content": "synthetic-only"},),
        max_output_tokens=100,
    )
    assert type(measured) is int and measured > 0


def test_deepseek_request_byte_accounting_preserves_transport_validation() -> None:
    adapter = DeepSeekProviderAdapter()
    with pytest.raises(HTTPException) as exc_info:
        adapter.estimate_request_utf8_bytes(
            messages=({"role": "user", "content": "synthetic-only"},),
            max_output_tokens=deepseek_transport.MAX_OUTPUT_TOKENS + 1,
        )
    assert _code(exc_info.value) == "DEEPSEEK_TRANSPORT_INPUT_INVALID"


def test_generic_gateway_preserves_deepseek_identity_and_binds_allowed_evidence() -> None:
    payload = b"synthetic deepseek compatibility payload"
    manifest = {
        "model_call_id": 7,
        "call_identity_hash": "1" * 64,
        "provider": deepseek_transport.PROVIDER,
        "model_id": deepseek_transport.MODEL_ID,
        "model_version": deepseek_transport.MODEL_VERSION,
        "task_type": deepseek_transport.TASK_TYPE,
        "output_schema_version": deepseek_transport.OUTPUT_SCHEMA_VERSION,
        "framed_payload_hash": hashlib.sha256(payload).hexdigest(),
        "budget_profile_hash": "2" * 64,
        "admitted_targets": [{"target": "synthetic-evidence", "target_type": "git_file_fact"}],
    }
    budget = {
        "reserved_output_tokens": 100,
        "safety_margin_tokens": 10,
    }

    legacy = model_gateway._build_gateway_request_plan(
        manifest=manifest, payload=payload, budget_record=budget
    )
    adapter = DeepSeekProviderAdapter()
    generic = model_provider_gateway._build_gateway_request_plan(
        manifest=manifest, payload=payload, budget_record=budget, adapter=adapter
    )

    assert generic["task_envelope"] == legacy["task_envelope"]
    assert generic["framed_payload_hash"] == legacy["framed_payload_hash"]
    assert generic["budget_profile_hash"] == legacy["budget_profile_hash"]
    assert generic["formal_result_contract_hash"] == legacy["formal_result_contract_hash"]
    assert "synthetic-evidence" in generic["messages"][0]["content"]
    assert generic["messages_hash"] != legacy["messages_hash"]
    assert generic["provider_request_utf8_bytes"] == adapter.estimate_request_utf8_bytes(
        messages=tuple(generic["messages"]), max_output_tokens=budget["reserved_output_tokens"]
    )
    assert len(generic["request_envelope_hash"]) == 64


@pytest.mark.parametrize(
    ("source_code", "expected_code"),
    [
        ("DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE", "MODEL_PROVIDER_AUTHORITY_UNAVAILABLE"),
        ("DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED", "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT"),
        ("DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED", "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT"),
    ],
)
def test_deepseek_authority_failure_is_normalized_at_adapter_boundary(
    monkeypatch, source_code: str, expected_code: str
) -> None:
    def fail(**_kwargs):
        raise HTTPException(status_code=409, detail={"code": source_code, "message": "synthetic"})

    monkeypatch.setattr(deepseek_current_authority, "resolve_deepseek_current_authority", fail)

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().resolve_current_authority(
            model_call_id=7,
            final_context_manifest_hash="1" * 64,
            framed_payload_hash="2" * 64,
            task_type="daily_report_generate",
        )

    assert _code(exc_info.value) == expected_code
