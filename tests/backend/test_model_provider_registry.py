from __future__ import annotations

from app.model_provider_contract import (
    ProviderCapability,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)
from app.model_provider_registry import ModelProviderRegistry, ModelProviderRegistryError


class _SyntheticAdapter:
    provider_id = "synthetic-zero-network"

    def __init__(self) -> None:
        self.execute_calls = 0

    def get_capability(self, *, task_type: str, output_schema_version: str) -> ProviderCapability:
        return ProviderCapability(
            provider=self.provider_id,
            model_id="synthetic-model",
            model_version="synthetic-model-v1",
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
        assert max_output_tokens > 0
        return 128 + sum(len(repr(item).encode("utf-8")) for item in messages)

    def resolve_current_authority(
        self,
        *,
        model_call_id: int,
        final_context_manifest_hash: str,
        framed_payload_hash: str,
        task_type: str,
        request_envelope_hash: str | None = None,
        prompt_contract_hash: str | None = None,
        sampling_parameters_hash: str | None = None,
    ) -> ProviderCurrentAuthority:
        del final_context_manifest_hash, framed_payload_hash, task_type
        del request_envelope_hash, prompt_contract_hash, sampling_parameters_hash
        return ProviderCurrentAuthority(
            model_call_id=model_call_id,
            call_identity_hash="a" * 64,
            provider=self.provider_id,
            model_id="synthetic-model",
            model_version="synthetic-model-v1",
            context_window_tokens=8192,
            max_output_tokens=4096,
            purpose_id="synthetic-purpose",
            data_scope_hash="b" * 64,
            qualification_rule_version="rules/1.0",
            qualification_output_schema_version="daily-report/1.0",
            qualification_benchmark_sample_pack_version="samples/1.0",
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
        assert request.provider == self.provider_id
        return ProviderReceipt(
            provider=self.provider_id,
            provider_response_id="synthetic-response-1",
            actual_model=request.model_id,
            provider_runtime_fingerprint="synthetic-runtime-v1",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            result={"status": "ok"},
        )


class _SecondSyntheticAdapter(_SyntheticAdapter):
    provider_id = "synthetic-zero-network-b"


class _BrokenAdapter:
    provider_id = "broken"


def _request() -> ProviderRequest:
    return ProviderRequest(
        model_call_id=7,
        call_identity_hash="a" * 64,
        provider="synthetic-zero-network",
        model_id="synthetic-model",
        model_version="synthetic-model-v1",
        task_type="daily_report_generate",
        output_schema_version="daily-report/1.0",
        messages=({"role": "user", "content": "local synthetic only"},),
        max_output_tokens=100,
    )


def test_registry_resolves_exact_adapter_and_executes_without_network() -> None:
    adapter = _SyntheticAdapter()
    registry = ModelProviderRegistry([adapter])

    resolved = registry.resolve("synthetic-zero-network")
    receipt = resolved.execute(_request())

    assert resolved is adapter
    assert adapter.execute_calls == 1
    assert receipt.provider == "synthetic-zero-network"
    assert receipt.result == {"status": "ok"}
    assert registry.provider_ids() == ("synthetic-zero-network",)


def test_registry_request_accounting_stays_inside_adapter() -> None:
    adapter = _SyntheticAdapter()
    registry = ModelProviderRegistry([adapter])
    measured = registry.resolve(adapter.provider_id).estimate_request_utf8_bytes(
        messages=({"role": "user", "content": "synthetic"},),
        max_output_tokens=10,
    )
    assert type(measured) is int and measured > 0


def test_registry_rejects_duplicate_provider() -> None:
    registry = ModelProviderRegistry([_SyntheticAdapter()])

    try:
        registry.register(_SyntheticAdapter())
    except ModelProviderRegistryError as exc:
        assert exc.code == "MODEL_PROVIDER_REGISTRY_DUPLICATE_PROVIDER"
    else:
        raise AssertionError("duplicate provider registration must fail closed")


def test_registry_unknown_provider_has_no_fallback() -> None:
    adapter = _SyntheticAdapter()
    registry = ModelProviderRegistry([adapter])

    try:
        registry.resolve("another-provider")
    except ModelProviderRegistryError as exc:
        assert exc.code == "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER"
    else:
        raise AssertionError("unknown provider must fail closed")

    assert adapter.execute_calls == 0


def test_multiple_registered_adapters_still_require_exact_provider_identity() -> None:
    provider_a = _SyntheticAdapter()
    provider_b = _SecondSyntheticAdapter()
    registry = ModelProviderRegistry([provider_a, provider_b])

    assert registry.provider_ids() == (
        "synthetic-zero-network",
        "synthetic-zero-network-b",
    )
    assert registry.resolve(provider_a.provider_id) is provider_a
    assert registry.resolve(provider_b.provider_id) is provider_b

    try:
        registry.resolve("synthetic-zero-network-missing")
    except ModelProviderRegistryError as exc:
        assert exc.code == "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER"
    else:
        raise AssertionError("multi-adapter registry must never fallback")

    assert provider_a.execute_calls == 0
    assert provider_b.execute_calls == 0


def test_registry_rejects_incomplete_adapter_contract() -> None:
    registry = ModelProviderRegistry()

    try:
        registry.register(_BrokenAdapter())  # type: ignore[arg-type]
    except ModelProviderRegistryError as exc:
        assert exc.code == "MODEL_PROVIDER_REGISTRY_INVALID_ADAPTER"
    else:
        raise AssertionError("incomplete adapter must fail closed")


def test_registry_rejects_non_exact_provider_identity() -> None:
    registry = ModelProviderRegistry([_SyntheticAdapter()])

    for provider in (None, "", " synthetic-zero-network", "synthetic-zero-network "):
        try:
            registry.resolve(provider)
        except ModelProviderRegistryError as exc:
            assert exc.code == "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER"
        else:
            raise AssertionError("malformed provider identity must fail closed")
