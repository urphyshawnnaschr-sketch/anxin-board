from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from fastapi import HTTPException

from app import (
    model_execution,
    model_execution_results,
    model_provider_gateway,
    model_provider_runtime,
)
from app.model_provider_contract import ProviderReceipt, ProviderRequest


def _preflight(*, model_call_id: int, provider: str = "synthetic-zero-network") -> dict[str, object]:
    return {
        "schema_version": "model_gateway_preflight_v1",
        "model_call_id": model_call_id,
        "call_identity_hash": "1" * 64,
        "final_context_manifest_hash": "2" * 64,
        "provider": provider,
        "model_id": "synthetic-model",
        "model_version": "synthetic-model-v1",
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "current_qualification_authority_state": "verified",
        "current_qualification_authority_ref": "qualification-ref",
        "current_qualification_evidence_hash": "3" * 64,
        "current_authorization_authority_state": "verified",
        "current_authorization_authority_ref": "authorization-ref",
        "current_authorization_evidence_hash": "4" * 64,
        "current_authorization_data_scope_hash": "5" * 64,
        "current_authorization_purpose_id": "synthetic-purpose",
        "framed_payload_hash": "6" * 64,
        "framed_payload_utf8_bytes": 5,
        "request_envelope_hash": "7" * 64,
        "final_request_fit_state": "fit_by_conservative_upper_bound",
        "provider_compatibility_state": "compatible",
        "local_gateway_state": "ready_for_provider_transport",
        "network_send_state": "not_attempted",
        "gateway_preflight_hash": "8" * 64,
    }


def _transient(*, model_call_id: int, provider: str = "synthetic-zero-network") -> dict[str, object]:
    return {
        "schema_version": "model_gateway_transient_request_v1",
        "model_call_id": model_call_id,
        "call_identity_hash": "1" * 64,
        "final_context_manifest_hash": "2" * 64,
        "provider": provider,
        "model_id": "synthetic-model",
        "model_version": "synthetic-model-v1",
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "framed_payload_hash": "6" * 64,
        "framed_payload_utf8_bytes": 5,
        "request_envelope_hash": "7" * 64,
        "conservative_local_request_upper_bound": 100,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "zero network"}],
    }


class _SyntheticAdapter:
    provider_id = "synthetic-zero-network"

    def __init__(self) -> None:
        self.calls: list[ProviderRequest] = []
        self._lock = Lock()

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        with self._lock:
            self.calls.append(request)
        return ProviderReceipt(
            provider=self.provider_id,
            provider_response_id="synthetic-response",
            actual_model=request.model_id,
            provider_runtime_fingerprint="synthetic-runtime",
            finish_reason="stop",
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
            result={"status": "synthetic-ok"},
        )


def _install(monkeypatch, *, model_call_id: int, adapter: _SyntheticAdapter, provider: str | None = None):
    provider_id = provider or adapter.provider_id
    monkeypatch.setattr(
        model_provider_gateway,
        "build_model_provider_gateway_preflight",
        lambda **_kwargs: _preflight(model_call_id=model_call_id, provider=provider_id),
    )
    monkeypatch.setattr(
        model_provider_gateway,
        "materialize_model_provider_gateway_request_transient",
        lambda **_kwargs: _transient(model_call_id=model_call_id, provider=provider_id),
    )
    monkeypatch.setattr(
        model_provider_runtime,
        "resolve_model_provider_adapter",
        lambda selected: adapter if selected == provider_id else (_ for _ in ()).throw(AssertionError("fallback")),
    )


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail.get("code"))


def test_production_registry_is_closed_world_deepseek_only() -> None:
    assert model_provider_runtime.production_provider_ids() == ("deepseek",)
    assert model_provider_runtime.resolve_model_provider_adapter("deepseek").provider_id == "deepseek"

    with pytest.raises(HTTPException) as exc_info:
        model_provider_runtime.resolve_model_provider_adapter("unknown-provider")
    assert _code(exc_info.value) == "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER"


def test_generic_execution_synthetic_path_is_zero_network_and_exact(monkeypatch) -> None:
    adapter = _SyntheticAdapter()
    model_call_id = 91001
    _install(monkeypatch, model_call_id=model_call_id, adapter=adapter)
    recorded: list[tuple[int, dict[str, object]]] = []

    def fake_record(*, model_call_id: int, receipt):
        recorded.append((model_call_id, dict(receipt)))
        return {"model_result_id": 1, "provider": receipt["provider"]}

    monkeypatch.setattr(model_execution_results, "record_model_execution_result", fake_record)

    result = model_execution.execute_model_call(
        model_call_id=model_call_id,
        budget_record={"reserved_output_tokens": 50, "safety_margin_tokens": 1},
    )

    assert result == {"model_result_id": 1, "provider": "synthetic-zero-network"}
    assert len(adapter.calls) == 1
    request = adapter.calls[0]
    assert request.provider == "synthetic-zero-network"
    assert request.model_id == "synthetic-model"
    assert request.messages == ({"role": "user", "content": "zero network"},)
    assert recorded == [
        (
            model_call_id,
            {
                "provider": "synthetic-zero-network",
                "provider_response_id": "synthetic-response",
                "actual_model": "synthetic-model",
                "provider_runtime_fingerprint": "synthetic-runtime",
                "finish_reason": "stop",
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "result": {"status": "synthetic-ok"},
            },
        )
    ]


def test_unknown_provider_fails_before_transient_or_transport(monkeypatch) -> None:
    model_call_id = 91002
    adapter = _SyntheticAdapter()
    transient_calls = 0

    monkeypatch.setattr(
        model_provider_gateway,
        "build_model_provider_gateway_preflight",
        lambda **_kwargs: _preflight(model_call_id=model_call_id, provider="unknown-provider"),
    )

    def unexpected_transient(**_kwargs):
        nonlocal transient_calls
        transient_calls += 1
        return _transient(model_call_id=model_call_id, provider="unknown-provider")

    monkeypatch.setattr(
        model_provider_gateway,
        "materialize_model_provider_gateway_request_transient",
        unexpected_transient,
    )
    monkeypatch.setattr(
        model_provider_runtime,
        "resolve_model_provider_adapter",
        lambda _provider: (_ for _ in ()).throw(
            HTTPException(
                status_code=409,
                detail={"code": "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER", "message": "blocked"},
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})

    assert _code(exc_info.value) == "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER"
    assert transient_calls == 0
    assert adapter.calls == []


def test_second_execution_is_blocked_without_second_adapter_or_ledger_call(monkeypatch) -> None:
    adapter = _SyntheticAdapter()
    model_call_id = 91003
    _install(monkeypatch, model_call_id=model_call_id, adapter=adapter)
    ledger_calls = 0

    def fake_record(**_kwargs):
        nonlocal ledger_calls
        ledger_calls += 1
        return {"model_result_id": 1}

    monkeypatch.setattr(model_execution_results, "record_model_execution_result", fake_record)

    first = model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})
    assert first == {"model_result_id": 1}

    with pytest.raises(HTTPException) as exc_info:
        model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})

    assert _code(exc_info.value) == "MODEL_EXECUTION_REPLAY_BLOCKED"
    assert len(adapter.calls) == 1
    assert ledger_calls == 1


def test_concurrent_same_call_reaches_adapter_at_most_once(monkeypatch) -> None:
    adapter = _SyntheticAdapter()
    model_call_id = 91004
    barrier = Barrier(2)
    monkeypatch.setattr(
        model_provider_gateway,
        "build_model_provider_gateway_preflight",
        lambda **_kwargs: _preflight(model_call_id=model_call_id),
    )

    def concurrent_transient(**_kwargs):
        barrier.wait(timeout=5)
        return _transient(model_call_id=model_call_id)

    monkeypatch.setattr(
        model_provider_gateway,
        "materialize_model_provider_gateway_request_transient",
        concurrent_transient,
    )
    monkeypatch.setattr(
        model_provider_runtime,
        "resolve_model_provider_adapter",
        lambda _provider: adapter,
    )
    monkeypatch.setattr(
        model_execution_results,
        "record_model_execution_result",
        lambda **_kwargs: {"model_result_id": 1},
    )

    def run_once():
        try:
            return model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})
        except HTTPException as exc:
            return _code(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: run_once(), range(2)))

    assert len(adapter.calls) == 1
    assert outcomes.count({"model_result_id": 1}) == 1
    assert outcomes.count("MODEL_EXECUTION_REPLAY_BLOCKED") == 1


def test_transient_provider_switch_after_ready_fails_before_claim_and_transport(monkeypatch) -> None:
    adapter = _SyntheticAdapter()
    model_call_id = 91005
    monkeypatch.setattr(
        model_provider_gateway,
        "build_model_provider_gateway_preflight",
        lambda **_kwargs: _preflight(model_call_id=model_call_id, provider=adapter.provider_id),
    )
    monkeypatch.setattr(
        model_provider_gateway,
        "materialize_model_provider_gateway_request_transient",
        lambda **_kwargs: _transient(model_call_id=model_call_id, provider="other-provider"),
    )
    monkeypatch.setattr(
        model_provider_runtime,
        "resolve_model_provider_adapter",
        lambda _provider: adapter,
    )

    with pytest.raises(HTTPException) as exc_info:
        model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})

    assert _code(exc_info.value) == "MODEL_EXECUTION_TRANSIENT_MISMATCH"
    assert adapter.calls == []


def test_invalid_adapter_receipt_blocks_result_ledger_and_consumes_send_claim(monkeypatch) -> None:
    adapter = _SyntheticAdapter()
    model_call_id = 91006
    _install(monkeypatch, model_call_id=model_call_id, adapter=adapter)
    ledger_calls = 0

    def malformed_execute(request: ProviderRequest):
        with adapter._lock:
            adapter.calls.append(request)
        return {"provider": adapter.provider_id, "result": {"status": "not-normalized"}}

    def unexpected_ledger(**_kwargs):
        nonlocal ledger_calls
        ledger_calls += 1
        raise AssertionError("invalid receipt must not reach Result Ledger")

    monkeypatch.setattr(adapter, "execute", malformed_execute)
    monkeypatch.setattr(model_execution_results, "record_model_execution_result", unexpected_ledger)
    model_execution._SEND_CLAIMED_MODEL_CALL_IDS.discard(model_call_id)

    with pytest.raises(HTTPException) as exc_info:
        model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})

    assert _code(exc_info.value) == "MODEL_EXECUTION_RECEIPT_INVALID"
    assert len(adapter.calls) == 1
    assert ledger_calls == 0
    assert model_call_id in model_execution._SEND_CLAIMED_MODEL_CALL_IDS

    with pytest.raises(HTTPException) as replay_exc:
        model_execution.execute_model_call(model_call_id=model_call_id, budget_record={})
    assert _code(replay_exc.value) == "MODEL_EXECUTION_REPLAY_BLOCKED"
    assert len(adapter.calls) == 1
