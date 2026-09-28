from __future__ import annotations

import hashlib
import inspect

import pytest
from fastapi import HTTPException

from app import model_provider_gateway as subject
from app.model_provider_contract import (
    ProviderCapability,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)


_PAYLOAD = b"hello"


def _admitted_target() -> dict[str, object]:
    return {
        "ordinal": 1,
        "target": "synthetic",
        "target_type": "file",
        "model_send_admission_hash": "9" * 64,
        "redaction_result_hash": "a" * 64,
        "frame_hash": "b" * 64,
        "frame_utf8_bytes": len(_PAYLOAD),
    }


def _manifest(*, provider: str = "synthetic-zero-network", model_id: str = "synthetic-model") -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "final_context_manifest_v1",
        "manifest_stage": "local_pre_gateway_final",
        "model_call_id": 7,
        "call_identity_hash": "1" * 64,
        "project_id": 3,
        "snapshot_id": 4,
        "snapshot_hash": "2" * 64,
        "candidate_set_hash": "3" * 64,
        "task_type": "daily_report_generate",
        "provider": provider,
        "model_id": model_id,
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
        "admitted_targets": [_admitted_target()],
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
    value["final_context_manifest_hash"] = subject._stable_hash(
        {field: value[field] for field in subject._FINAL_MANIFEST_HASH_KEYS}
    )
    return value


def _rehash(value: dict[str, object]) -> dict[str, object]:
    value["final_context_manifest_hash"] = subject._stable_hash(
        {field: value[field] for field in subject._FINAL_MANIFEST_HASH_KEYS}
    )
    return value


class _SyntheticAdapter:
    provider_id = "synthetic-zero-network"

    def __init__(self, manifest: dict[str, object]) -> None:
        self.manifest = manifest
        self.capability_calls = 0
        self.authority_calls = 0
        self.accounting_calls = 0
        self.execute_calls = 0
        self.authority_provider = str(manifest["provider"])
        self.authority_failure: HTTPException | None = None
        self.capability_context_window_tokens = 8192

    def get_capability(self, *, task_type: str, output_schema_version: str) -> ProviderCapability:
        self.capability_calls += 1
        return ProviderCapability(
            provider=str(self.manifest["provider"]),
            model_id=str(self.manifest["model_id"]),
            model_version=str(self.manifest["model_version"]),
            task_type=task_type,
            output_schema_version=output_schema_version,
            context_window_tokens=self.capability_context_window_tokens,
            max_output_tokens=4096,
        )

    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens: int) -> int:
        self.accounting_calls += 1
        assert len(messages) == 2
        assert max_output_tokens == 100
        return 256

    def resolve_current_authority(self, **kwargs) -> ProviderCurrentAuthority:
        self.authority_calls += 1
        if self.authority_failure is not None:
            raise self.authority_failure
        purpose = "synthetic-purpose"
        data_scope_hash = subject._expected_data_scope_hash(self.manifest, purpose_id=purpose)
        return ProviderCurrentAuthority(
            model_call_id=int(kwargs["model_call_id"]),
            call_identity_hash=str(self.manifest["call_identity_hash"]),
            provider=self.authority_provider,
            model_id=str(self.manifest["model_id"]),
            model_version=str(self.manifest["model_version"]),
            context_window_tokens=8192,
            max_output_tokens=4096,
            purpose_id=purpose,
            data_scope_hash=data_scope_hash,
            qualification_rule_version=str(self.manifest["rule_version"]),
            qualification_output_schema_version=str(self.manifest["output_schema_version"]),
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
        raise AssertionError("Gateway must never execute provider transport")


def _install(monkeypatch, manifest: dict[str, object], adapter: _SyntheticAdapter) -> None:
    monkeypatch.setattr(
        subject.final_context_manifest,
        "build_final_context_manifest",
        lambda **_kwargs: dict(manifest),
    )
    monkeypatch.setattr(
        subject.token_framing,
        "_materialize_context_payload_transient",
        lambda **_kwargs: {
            "payload": _PAYLOAD,
            "framed_payload_hash": manifest["framed_payload_hash"],
            "framed_payload_utf8_bytes": len(_PAYLOAD),
        },
    )
    monkeypatch.setattr(
        subject.model_provider_runtime,
        "resolve_model_provider_adapter",
        lambda provider: adapter if provider == manifest["provider"] else (_ for _ in ()).throw(AssertionError("fallback")),
    )


def _budget() -> dict[str, object]:
    return {"reserved_output_tokens": 100, "safety_margin_tokens": 10}


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail.get("code"))


def test_synthetic_gateway_ready_and_transient_are_zero_network(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    _install(monkeypatch, manifest, adapter)

    preflight = subject.build_model_provider_gateway_preflight(
        model_call_id=7,
        budget_record=_budget(),
    )
    transient = subject.materialize_model_provider_gateway_request_transient(
        model_call_id=7,
        budget_record=_budget(),
    )

    assert preflight["provider"] == "synthetic-zero-network"
    assert preflight["local_gateway_state"] == "ready_for_provider_transport"
    assert preflight["current_qualification_authority_state"] == "verified"
    assert preflight["current_authorization_authority_state"] == "verified"
    assert transient["provider"] == preflight["provider"]
    assert transient["request_envelope_hash"] == preflight["request_envelope_hash"]
    assert adapter.execute_calls == 0
    assert adapter.capability_calls == 1
    assert adapter.authority_calls == 1
    assert adapter.accounting_calls == 2


def test_transient_is_single_consume(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    _install(monkeypatch, manifest, adapter)
    subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())
    subject.materialize_model_provider_gateway_request_transient(model_call_id=7, budget_record=_budget())

    with pytest.raises(HTTPException) as exc_info:
        subject.materialize_model_provider_gateway_request_transient(model_call_id=7, budget_record=_budget())
    assert _code(exc_info.value) == "MODEL_PROVIDER_GATEWAY_TRANSIENT_NOT_READY"
    assert adapter.execute_calls == 0


def test_capability_identity_mismatch_blocks_before_authority(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    _install(monkeypatch, manifest, adapter)

    original = adapter.get_capability

    def mismatched(**kwargs):
        capability = original(**kwargs)
        return ProviderCapability(
            provider=capability.provider,
            model_id="wrong-model",
            model_version=capability.model_version,
            task_type=capability.task_type,
            output_schema_version=capability.output_schema_version,
            context_window_tokens=capability.context_window_tokens,
            max_output_tokens=capability.max_output_tokens,
        )

    monkeypatch.setattr(adapter, "get_capability", mismatched)
    result = subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert result["local_gateway_state"] == "blocked_provider_compatibility"
    assert result["provider_compatibility_state"] == "incompatible"
    assert adapter.authority_calls == 0
    assert adapter.execute_calls == 0


def test_invalid_capability_runtime_type_fails_closed_before_authority(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    _install(monkeypatch, manifest, adapter)
    monkeypatch.setattr(adapter, "get_capability", lambda **_kwargs: {"provider": adapter.provider_id})

    result = subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert result["local_gateway_state"] == "blocked_provider_compatibility"
    assert result["provider_compatibility_state"] == "incompatible"
    assert adapter.authority_calls == 0
    assert adapter.execute_calls == 0


def test_capability_context_window_participates_in_request_fit(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.capability_context_window_tokens = 100
    _install(monkeypatch, manifest, adapter)

    result = subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert result["local_gateway_state"] == "blocked_request_budget"
    assert result["final_request_fit_state"] == "not_fit_by_conservative_upper_bound"
    assert adapter.execute_calls == 0


def test_old_provider_authority_cannot_be_reused_after_provider_switch(monkeypatch) -> None:
    manifest = _manifest(provider="provider-b")
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_provider = "provider-a"
    _install(monkeypatch, manifest, adapter)

    with pytest.raises(HTTPException) as exc_info:
        subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert _code(exc_info.value) == "MODEL_PROVIDER_GATEWAY_PROVIDER_INCONSISTENT"
    assert adapter.execute_calls == 0


def test_invalid_authority_runtime_type_fails_closed_before_transport(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    _install(monkeypatch, manifest, adapter)
    monkeypatch.setattr(adapter, "resolve_current_authority", lambda **_kwargs: {"provider": adapter.provider_id})

    with pytest.raises(HTTPException) as exc_info:
        subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert _code(exc_info.value) == "MODEL_PROVIDER_GATEWAY_PROVIDER_INCONSISTENT"
    assert adapter.execute_calls == 0


def test_provider_qualification_failure_is_visible_block_not_fallback(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_failure = HTTPException(
        status_code=409,
        detail={"code": "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT", "message": "synthetic"},
    )
    _install(monkeypatch, manifest, adapter)

    result = subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert result["local_gateway_state"] == "blocked_current_qualification"
    assert result["current_qualification_authority_state"] == "not_verified"
    assert result["current_authorization_authority_state"] == "not_evaluated"
    assert adapter.execute_calls == 0


def test_provider_authorization_failure_is_visible_block_not_fallback(monkeypatch) -> None:
    manifest = _manifest()
    adapter = _SyntheticAdapter(manifest)
    adapter.authority_failure = HTTPException(
        status_code=409,
        detail={"code": "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT", "message": "synthetic"},
    )
    _install(monkeypatch, manifest, adapter)

    result = subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert result["local_gateway_state"] == "blocked_current_authorization"
    assert result["current_authorization_authority_state"] == "not_authorized"
    assert adapter.execute_calls == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(admitted_target_count=2),
        lambda value: value["admitted_targets"][0].update(target_type=""),
        lambda value: value.update(context_admission_state="contains_denied_targets"),
        lambda value: value.update(local_request_readiness_state="blocked_context_budget"),
        lambda value: value.update(coverage_state="partial_fail_visible"),
    ],
)
def test_manifest_security_invariants_fail_closed_before_provider(monkeypatch, mutate) -> None:
    manifest = _manifest()
    mutate(manifest)
    _rehash(manifest)
    adapter = _SyntheticAdapter(manifest)
    _install(monkeypatch, manifest, adapter)

    with pytest.raises(HTTPException) as exc_info:
        subject.build_model_provider_gateway_preflight(model_call_id=7, budget_record=_budget())

    assert _code(exc_info.value) == "MODEL_PROVIDER_GATEWAY_UPSTREAM_INCONSISTENT"
    assert adapter.capability_calls == 0
    assert adapter.authority_calls == 0
    assert adapter.execute_calls == 0


def test_generic_gateway_source_has_no_deepseek_dispatch_or_credentials() -> None:
    source = inspect.getsource(subject)
    for forbidden in (
        "deepseek_transport",
        "deepseek_current_authority",
        "DEEPSEEK_API_KEY",
        "send_deepseek",
        "httpx",
        "os.environ",
    ):
        assert forbidden not in source
    assert "resolve_model_provider_adapter" in source
    assert "estimate_request_utf8_bytes" in source
