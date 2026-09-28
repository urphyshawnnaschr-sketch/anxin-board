from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

from app import model_provider_runtime as subject
from app.model_provider_contract import (
    ProviderCapability,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)


CALL = {
    "model_call_id": 7,
    "call_identity_hash": "1" * 64,
    "provider": "synthetic-zero-network",
    "model_id": "synthetic-model",
    "model_version": "synthetic-model-v1",
    "task_type": "daily_report_generate",
    "rule_version": "rules/1.0",
    "output_schema_version": "daily-report/1.0",
    "benchmark_sample_pack_version": "samples/1.0",
}


def _authority() -> ProviderCurrentAuthority:
    return ProviderCurrentAuthority(
        model_call_id=7,
        call_identity_hash="1" * 64,
        provider="synthetic-zero-network",
        model_id="synthetic-model",
        model_version="synthetic-model-v1",
        context_window_tokens=8192,
        max_output_tokens=4096,
        purpose_id="synthetic-purpose",
        data_scope_hash="2" * 64,
        qualification_rule_version="rules/1.0",
        qualification_output_schema_version="daily-report/1.0",
        qualification_benchmark_sample_pack_version="samples/1.0",
        qualification_status="qualified",
        qualification_authority_ref="synthetic-qualification",
        qualification_evidence_hash="3" * 64,
        authorization_authorized=True,
        authorization_valid=True,
        authorization_authority_ref="synthetic-authorization",
        authorization_evidence_hash="4" * 64,
    )


class _RawAdapter:
    provider_id = "synthetic-zero-network"

    def __init__(self, authority: object | None = None) -> None:
        self.authority = _authority() if authority is None else authority
        self.authority_calls = 0

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

    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens: int) -> int:
        return 128

    def resolve_current_authority(self, **_kwargs):
        self.authority_calls += 1
        return self.authority

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        raise AssertionError("authority closure tests never execute transport")


def _install_call(monkeypatch, call=None) -> None:
    value = dict(CALL if call is None else call)
    monkeypatch.setattr(subject, "get_model_call", lambda model_call_id: dict(value))


def _code(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail.get("code"))


def _resolve(proxy: subject._AuthorityClosingAdapter):
    return proxy.resolve_current_authority(
        model_call_id=7,
        final_context_manifest_hash="5" * 64,
        framed_payload_hash="6" * 64,
        task_type="daily_report_generate",
    )


def _request(*, provider: str = "synthetic-zero-network") -> ProviderRequest:
    return ProviderRequest(
        model_call_id=7,
        call_identity_hash="1" * 64,
        provider=provider,
        model_id="synthetic-model",
        model_version="synthetic-model-v1",
        task_type="daily_report_generate",
        output_schema_version="daily-report/1.0",
        messages=({"role": "user", "content": "synthetic"},),
        max_output_tokens=10,
    )


def test_generic_runtime_recloses_capability_to_selected_adapter() -> None:
    raw = _RawAdapter()
    proxy = subject._AuthorityClosingAdapter(raw)

    capability = proxy.get_capability(
        task_type="daily_report_generate",
        output_schema_version="daily-report/1.0",
    )

    assert capability.provider == raw.provider_id
    assert capability.task_type == "daily_report_generate"
    assert capability.output_schema_version == "daily-report/1.0"


def test_capability_provider_drift_fails_closed_before_preparation(monkeypatch) -> None:
    raw = _RawAdapter()
    proxy = subject._AuthorityClosingAdapter(raw)
    monkeypatch.setattr(
        raw,
        "get_capability",
        lambda **_kwargs: ProviderCapability(
            provider="other-provider",
            model_id="synthetic-model",
            model_version="synthetic-model-v1",
            task_type="daily_report_generate",
            output_schema_version="daily-report/1.0",
            context_window_tokens=8192,
            max_output_tokens=4096,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        proxy.get_capability(
            task_type="daily_report_generate",
            output_schema_version="daily-report/1.0",
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"


def test_invalid_capability_runtime_type_fails_closed(monkeypatch) -> None:
    raw = _RawAdapter()
    proxy = subject._AuthorityClosingAdapter(raw)
    monkeypatch.setattr(raw, "get_capability", lambda **_kwargs: {"provider": raw.provider_id})

    with pytest.raises(HTTPException) as exc_info:
        proxy.get_capability(
            task_type="daily_report_generate",
            output_schema_version="daily-report/1.0",
        )

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"


def test_execute_provider_drift_fails_before_raw_adapter_transport() -> None:
    raw = _RawAdapter()
    proxy = subject._AuthorityClosingAdapter(raw)

    with pytest.raises(HTTPException) as exc_info:
        proxy.execute(_request(provider="other-provider"))

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"


def test_generic_runtime_recloses_normalized_authority_against_durable_call(monkeypatch) -> None:
    _install_call(monkeypatch)
    raw = _RawAdapter()
    proxy = subject._AuthorityClosingAdapter(raw)

    authority = _resolve(proxy)

    assert authority == _authority()
    assert raw.authority_calls == 1


def test_durable_provider_or_task_drift_blocks_before_adapter_authority(monkeypatch) -> None:
    for call in (
        {**CALL, "provider": "other-provider"},
        {**CALL, "task_type": "daily_report_regenerate"},
    ):
        _install_call(monkeypatch, call)
        raw = _RawAdapter()
        proxy = subject._AuthorityClosingAdapter(raw)

        with pytest.raises(HTTPException) as exc_info:
            _resolve(proxy)

        assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"
        assert raw.authority_calls == 0


@pytest.mark.parametrize(
    "authority",
    [
        replace(_authority(), qualification_rule_version="other-rules"),
        replace(_authority(), qualification_output_schema_version="other-schema"),
        replace(_authority(), qualification_benchmark_sample_pack_version="other-pack"),
        replace(_authority(), qualification_status="not-qualified"),
    ],
)
def test_adapter_cannot_self_admit_mismatched_qualification(monkeypatch, authority) -> None:
    _install_call(monkeypatch)
    raw = _RawAdapter(authority)
    proxy = subject._AuthorityClosingAdapter(raw)

    with pytest.raises(HTTPException) as exc_info:
        _resolve(proxy)

    assert _code(exc_info.value) == "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT"
    assert raw.authority_calls == 1


@pytest.mark.parametrize(
    "authority",
    [
        replace(_authority(), authorization_authorized=False),
        replace(_authority(), authorization_valid=False),
    ],
)
def test_adapter_cannot_self_grant_human_authorization(monkeypatch, authority) -> None:
    _install_call(monkeypatch)
    raw = _RawAdapter(authority)
    proxy = subject._AuthorityClosingAdapter(raw)

    with pytest.raises(HTTPException) as exc_info:
        _resolve(proxy)

    assert _code(exc_info.value) == "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT"
    assert raw.authority_calls == 1


def test_invalid_authority_runtime_type_fails_closed(monkeypatch) -> None:
    _install_call(monkeypatch)
    raw = _RawAdapter({"provider": "synthetic-zero-network"})
    proxy = subject._AuthorityClosingAdapter(raw)

    with pytest.raises(HTTPException) as exc_info:
        _resolve(proxy)

    assert _code(exc_info.value) == "MODEL_PROVIDER_ADAPTER_INCONSISTENT"
    assert raw.authority_calls == 1
