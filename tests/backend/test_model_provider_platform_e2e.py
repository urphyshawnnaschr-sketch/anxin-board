from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from app import (
    model_execution,
    model_execution_results,
    model_provider_gateway,
    model_provider_runtime,
)
from app.model_provider_contract import (
    ProviderCapability,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)


_PAYLOAD = b"synthetic provider platform e2e"
_MODEL_CALL_ID = 92001


def _manifest() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "final_context_manifest_v1",
        "manifest_stage": "local_pre_gateway_final",
        "model_call_id": _MODEL_CALL_ID,
        "call_identity_hash": "1" * 64,
        "project_id": 3,
        "snapshot_id": 4,
        "snapshot_hash": "2" * 64,
        "candidate_set_hash": "3" * 64,
        "task_type": "daily_report_generate",
        "provider": "synthetic-zero-network",
        "model_id": "synthetic-model",
        "model_version": "synthetic-model-v1",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "samples/1.0",
        "qualification_hash": "4" * 64,
        "authorization_hash": "5" * 64,
        "manifest_core_hash": "6" * 64,
        "budget_profile_hash": "7" * 64,
        "token_framing_accounting_hash": "8" * 64,
        "counting_policy_version": "v1",
        "framing_policy_version": "v1",
        "framing_scope": "context_payload_only",
        "context_admission_state": "all_targets_admitted",
        "coverage_state": "supported_context_complete",
        "admitted_target_count": 1,
        "denied_target_count": 0,
        "admitted_targets": [
            {
                "ordinal": 1,
                "target": "synthetic",
                "target_type": "file",
                "model_send_admission_hash": "9" * 64,
                "redaction_result_hash": "a" * 64,
                "frame_hash": "b" * 64,
                "frame_utf8_bytes": len(_PAYLOAD),
            }
        ],
        "denied_targets": [],
        "framed_payload_hash": hashlib.sha256(_PAYLOAD).hexdigest(),
        "framed_payload_utf8_bytes": len(_PAYLOAD),
        "conservative_input_token_upper_bound": len(_PAYLOAD),
        "exact_tokens": None,
        "budget_fit_state": "fit_by_conservative_upper_bound",
        "final_request_fit_state": "not_evaluated",
        "unsupported_context_sources": [],
        "local_request_readiness_state": "ready_for_gateway_evaluation",
        "final_manifest_state": "finalized_local_metadata",
        "gateway_send_state": "not_evaluated",
    }
    value["final_context_manifest_hash"] = model_provider_gateway._stable_hash(
        {
            field: value[field]
            for field in model_provider_gateway._FINAL_MANIFEST_HASH_KEYS
        }
    )
    return value


class _SyntheticAdapter:
    provider_id = "synthetic-zero-network"

    def __init__(self, manifest: dict[str, object]) -> None:
        self.manifest = manifest
        self.capability_calls = 0
        self.accounting_calls = 0
        self.authority_calls = 0
        self.execute_calls = 0
        self.fail_qualification = False

    def get_capability(
        self, *, task_type: str, output_schema_version: str
    ) -> ProviderCapability:
        self.capability_calls += 1
        return ProviderCapability(
            provider=self.provider_id,
            model_id=str(self.manifest["model_id"]),
            model_version=str(self.manifest["model_version"]),
            task_type=task_type,
            output_schema_version=output_schema_version,
            context_window_tokens=8192,
            max_output_tokens=4096,
        )

    def estimate_request_utf8_bytes(
        self,
        *,
        messages: tuple[object, ...],
        max_output_tokens: int,
    ) -> int:
        self.accounting_calls += 1
        assert len(messages) == 2
        assert max_output_tokens == 100
        return 300

    def resolve_current_authority(self, **kwargs) -> ProviderCurrentAuthority:
        self.authority_calls += 1
        if self.fail_qualification:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT",
                    "message": "synthetic qualification blocked",
                },
            )
        purpose = "synthetic-purpose"
        data_scope_hash = model_provider_gateway._expected_data_scope_hash(
            self.manifest,
            purpose_id=purpose,
        )
        return ProviderCurrentAuthority(
            model_call_id=int(kwargs["model_call_id"]),
            call_identity_hash=str(self.manifest["call_identity_hash"]),
            provider=self.provider_id,
            model_id=str(self.manifest["model_id"]),
            model_version=str(self.manifest["model_version"]),
            context_window_tokens=8192,
            max_output_tokens=4096,
            purpose_id=purpose,
            data_scope_hash=data_scope_hash,
            qualification_rule_version=str(self.manifest["rule_version"]),
            qualification_output_schema_version=str(
                self.manifest["output_schema_version"]
            ),
            qualification_benchmark_sample_pack_version=str(
                self.manifest["benchmark_sample_pack_version"]
            ),
            qualification_status="qualified",
            qualification_authority_ref="synthetic-qualification",
            qualification_evidence_hash="c" * 64,
            authorization_authorized=True,
            authorization_valid=True,
            authorization_authority_ref="synthetic-authorization",
            authorization_evidence_hash="d" * 64,
        )

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        self.execute_calls += 1
        assert request.model_call_id == _MODEL_CALL_ID
        assert request.provider == self.provider_id
        assert request.model_id == self.manifest["model_id"]
        assert request.model_version == self.manifest["model_version"]
        assert request.task_type == self.manifest["task_type"]
        assert request.output_schema_version == self.manifest["output_schema_version"]
        assert request.max_output_tokens == 100
        return ProviderReceipt(
            provider=self.provider_id,
            provider_response_id="synthetic-response-e2e",
            actual_model=str(self.manifest["model_id"]),
            provider_runtime_fingerprint="synthetic-runtime-e2e",
            finish_reason="stop",
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            result={"status": "synthetic-ok"},
        )


def _install(monkeypatch, adapter: _SyntheticAdapter):
    manifest = adapter.manifest
    monkeypatch.setattr(
        model_provider_gateway.final_context_manifest,
        "build_final_context_manifest",
        lambda **_kwargs: dict(manifest),
    )
    monkeypatch.setattr(
        model_provider_gateway.token_framing,
        "_materialize_context_payload_transient",
        lambda **_kwargs: {
            "payload": _PAYLOAD,
            "framed_payload_hash": manifest["framed_payload_hash"],
            "framed_payload_utf8_bytes": len(_PAYLOAD),
        },
    )
    monkeypatch.setattr(
        model_provider_runtime,
        "get_model_call",
        lambda model_call_id: {
            "model_call_id": model_call_id,
            "call_identity_hash": manifest["call_identity_hash"],
            "provider": manifest["provider"],
            "model_id": manifest["model_id"],
            "model_version": manifest["model_version"],
            "task_type": manifest["task_type"],
            "rule_version": manifest["rule_version"],
            "output_schema_version": manifest["output_schema_version"],
            "benchmark_sample_pack_version": manifest[
                "benchmark_sample_pack_version"
            ],
        },
    )

    closing_adapter = model_provider_runtime._AuthorityClosingAdapter(adapter)

    def resolve(provider):
        if provider != adapter.provider_id:
            raise AssertionError("implicit provider fallback attempted")
        return closing_adapter

    monkeypatch.setattr(
        model_provider_runtime,
        "resolve_model_provider_adapter",
        resolve,
    )
    recorded: list[tuple[int, dict[str, object]]] = []

    def record(*, model_call_id: int, receipt):
        recorded.append((model_call_id, dict(receipt)))
        return {
            "model_result_id": 93001,
            "model_call_id": model_call_id,
            "provider": receipt["provider"],
        }

    monkeypatch.setattr(
        model_execution_results,
        "record_model_execution_result",
        record,
    )
    model_execution._SEND_CLAIMED_MODEL_CALL_IDS.discard(_MODEL_CALL_ID)
    return recorded


def _budget() -> dict[str, object]:
    return {
        "provider": "synthetic-zero-network",
        "model_id": "synthetic-model",
        "model_version": "synthetic-model-v1",
        "reserved_output_tokens": 100,
        "safety_margin_tokens": 10,
    }


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail.get("code"))


def test_synthetic_provider_runs_same_gateway_execution_ledger_seam_with_zero_network(
    monkeypatch,
) -> None:
    adapter = _SyntheticAdapter(_manifest())
    recorded = _install(monkeypatch, adapter)

    result = model_execution.execute_model_call(
        model_call_id=_MODEL_CALL_ID,
        budget_record=_budget(),
    )

    assert result == {
        "model_result_id": 93001,
        "model_call_id": _MODEL_CALL_ID,
        "provider": "synthetic-zero-network",
    }
    assert adapter.capability_calls == 1
    assert adapter.authority_calls == 1
    assert adapter.accounting_calls == 2
    assert adapter.execute_calls == 1
    assert len(recorded) == 1
    assert recorded[0][0] == _MODEL_CALL_ID
    assert recorded[0][1]["provider"] == "synthetic-zero-network"
    assert recorded[0][1]["result"] == {"status": "synthetic-ok"}


def test_missing_current_qualification_fails_before_transient_send_claim_or_ledger(
    monkeypatch,
) -> None:
    adapter = _SyntheticAdapter(_manifest())
    adapter.fail_qualification = True
    recorded = _install(monkeypatch, adapter)

    with pytest.raises(HTTPException) as exc_info:
        model_execution.execute_model_call(
            model_call_id=_MODEL_CALL_ID,
            budget_record=_budget(),
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT"
    assert adapter.capability_calls == 1
    assert adapter.authority_calls == 1
    assert adapter.execute_calls == 0
    assert recorded == []
    assert _MODEL_CALL_ID not in model_execution._SEND_CLAIMED_MODEL_CALL_IDS
