from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import deepseek_current_authority, deepseek_provider_adapter as subject
from app.deepseek_provider_adapter import DeepSeekProviderAdapter


def _raw_regenerate_authority_without_task_type() -> dict[str, object]:
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
        # Intentionally missing task_type; every other reviewed-registry field is present.
        "ai_contract_schema_version": deepseek_current_authority.AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": "daily-report-regenerate/1.0",
        "prompt_version": "page07-regenerate-prompt/1.0",
        "prompt_contract_hash": "1" * 64,
        "rule_version": "page07-regenerate-rules/2.0",
        "sample_pack_version": "page07-regenerate-qualification-pack/2.0",
        "sampling_parameters_hash": "2" * 64,
        "qualification_status": "qualified",
        "sample_manifest_hash": "3" * 64,
        "qualification_harness_commit": "synthetic-harness-commit",
        "evidence_manifest_hash": "4" * 64,
        "review_ref": review_ref,
        "record_hash": record_hash,
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
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
        "purpose_id": "anxin_board_daily_report_regenerate_v1",
        "data_scope_hash": "b" * 64,
    }
    authorization["evidence_hash"] = subject._stable_hash(authorization)

    raw: dict[str, object] = {
        "schema_version": "deepseek_current_authority_v1",
        "model_call_id": 7,
        "call_identity_hash": "c" * 64,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "purpose_id": "anxin_board_daily_report_regenerate_v1",
        "data_scope_hash": "b" * 64,
        "qualification": qualification,
        "authorization": authorization,
    }
    raw["authority_hash"] = subject._stable_hash(raw)
    return raw


def test_regenerate_authority_requires_exact_frozen_task_identity(monkeypatch) -> None:
    raw = _raw_regenerate_authority_without_task_type()
    monkeypatch.setattr(
        deepseek_current_authority,
        "resolve_deepseek_regenerate_current_authority",
        lambda **_kwargs: raw,
    )

    with pytest.raises(HTTPException) as exc_info:
        DeepSeekProviderAdapter().resolve_current_authority(
            model_call_id=7,
            final_context_manifest_hash="1" * 64,
            framed_payload_hash="2" * 64,
            task_type="daily_report_regenerate",
            request_envelope_hash="3" * 64,
            prompt_contract_hash="4" * 64,
            sampling_parameters_hash="5" * 64,
        )

    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail["code"] == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"
