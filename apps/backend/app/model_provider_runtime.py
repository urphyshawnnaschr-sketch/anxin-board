"""Production registry binding and generic authority closure for model providers.

Importing this module performs no provider network or credential access. The production
registry is closed-world: only explicitly reviewed adapters are registered, and unknown
providers fail closed without fallback. Product default selection is explicit and lives
here rather than in report business state machines.

Adapters provide provider-specific current facts. This generic layer rebinds those facts
to the selected adapter and durable Model Call before they can be treated as capability,
qualification, or Human-authorization authority, so an adapter cannot self-admit a
different provider, task, qualification identity, or authorization state.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from app.deepseek_extended_provider_adapter import build_deepseek_extended_provider_adapter
from app.model_call_ledger import get_model_call
from app.model_provider_contract import (
    ModelProviderAdapter,
    ProviderCapability,
    ProviderCredentialRequest,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)
from app.model_provider_registry import ModelProviderRegistry, ModelProviderRegistryError


DEFAULT_PROVIDER_ID = "deepseek"
_PRODUCTION_REGISTRY = ModelProviderRegistry([build_deepseek_extended_provider_adapter()])


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _adapter_inconsistent(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_ADAPTER_INCONSISTENT", message)


def _qualification_not_current(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT", message)


def _authorization_not_current(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT", message)


def _registry_error(exc: ModelProviderRegistryError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": exc.code, "message": str(exc)},
    )


def _close_capability_to_adapter(
    capability: object,
    *,
    provider_id: str,
    task_type: str,
    output_schema_version: str,
) -> ProviderCapability:
    if not isinstance(capability, ProviderCapability):
        raise _adapter_inconsistent("Provider adapter 未返回规范化 ProviderCapability。")
    if (
        capability.provider != provider_id
        or capability.task_type != task_type
        or capability.output_schema_version != output_schema_version
    ):
        raise _adapter_inconsistent(
            "Provider capability 与 selected adapter/task/schema identity 不一致。"
        )
    return capability


def _require_durable_call_for_authority(
    *,
    model_call_id: int,
    provider_id: str,
    task_type: str,
) -> Mapping[str, object]:
    call = get_model_call(model_call_id)
    if not isinstance(call, Mapping):
        raise _adapter_inconsistent("Model Call Ledger 未返回可闭合的 durable call。")
    if (
        call.get("model_call_id") != model_call_id
        or call.get("provider") != provider_id
        or call.get("task_type") != task_type
    ):
        raise _adapter_inconsistent(
            "Provider authority 请求与 durable Model Call provider/task identity 不一致。"
        )
    return call


def _close_current_authority_against_call(
    authority: object,
    *,
    call: Mapping[str, object],
    provider_id: str,
) -> ProviderCurrentAuthority:
    if not isinstance(authority, ProviderCurrentAuthority):
        raise _adapter_inconsistent("Provider adapter 未返回规范化 ProviderCurrentAuthority。")
    if (
        authority.model_call_id != call.get("model_call_id")
        or authority.call_identity_hash != call.get("call_identity_hash")
        or authority.provider != provider_id
        or authority.provider != call.get("provider")
        or authority.model_id != call.get("model_id")
        or authority.model_version != call.get("model_version")
    ):
        raise _adapter_inconsistent(
            "Provider current authority 与 durable Model Call identity 不一致。"
        )
    if (
        authority.qualification_rule_version != call.get("rule_version")
        or authority.qualification_output_schema_version
        != call.get("output_schema_version")
        or authority.qualification_benchmark_sample_pack_version
        != call.get("benchmark_sample_pack_version")
        or authority.qualification_status != "qualified"
    ):
        raise _qualification_not_current(
            "Provider current qualification facts 与 durable Model Call qualification identity 不一致。"
        )
    if (
        authority.authorization_authorized is not True
        or authority.authorization_valid is not True
    ):
        raise _authorization_not_current(
            "Provider-bound Human authorization 当前未形成 strict True closure。"
        )
    return authority


class _AuthorityClosingAdapter:
    """Generic proxy: adapter supplies facts; core decides exact authority closure."""

    def __init__(self, adapter: ModelProviderAdapter) -> None:
        self._adapter = adapter

    @property
    def provider_id(self) -> str:
        return self._adapter.provider_id

    def get_capability(
        self,
        *,
        task_type: str,
        output_schema_version: str,
    ) -> ProviderCapability:
        capability = self._adapter.get_capability(
            task_type=task_type,
            output_schema_version=output_schema_version,
        )
        return _close_capability_to_adapter(
            capability,
            provider_id=self.provider_id,
            task_type=task_type,
            output_schema_version=output_schema_version,
        )

    def estimate_request_utf8_bytes(
        self,
        *,
        messages: tuple[Mapping[str, object], ...],
        max_output_tokens: int,
    ) -> int:
        return self._adapter.estimate_request_utf8_bytes(
            messages=messages,
            max_output_tokens=max_output_tokens,
        )

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
        call = _require_durable_call_for_authority(
            model_call_id=model_call_id,
            provider_id=self.provider_id,
            task_type=task_type,
        )
        authority = self._adapter.resolve_current_authority(
            model_call_id=model_call_id,
            final_context_manifest_hash=final_context_manifest_hash,
            framed_payload_hash=framed_payload_hash,
            task_type=task_type,
            request_envelope_hash=request_envelope_hash,
            prompt_contract_hash=prompt_contract_hash,
            sampling_parameters_hash=sampling_parameters_hash,
        )
        return _close_current_authority_against_call(
            authority,
            call=call,
            provider_id=self.provider_id,
        )

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        if not isinstance(request, ProviderRequest) or request.provider != self.provider_id:
            raise _adapter_inconsistent(
                "Provider request 与 selected adapter provider identity 不一致。"
            )
        return self._adapter.execute(request)

    def execute_with_credential(
        self, request: ProviderCredentialRequest, credential: str
    ) -> ProviderReceipt:
        if (
            not isinstance(request, ProviderCredentialRequest)
            or request.provider != self.provider_id
            or type(credential) is not str
            or not credential.strip()
        ):
            raise _adapter_inconsistent(
                "Provider credential request 与 selected adapter identity 不一致。"
            )
        execute = getattr(self._adapter, "execute_with_credential", None)
        if not callable(execute):
            raise _adapter_inconsistent(
                "Selected provider adapter 未实现 explicit-credential execution seam。"
            )
        receipt = execute(request, credential)
        if not isinstance(receipt, ProviderReceipt) or receipt.provider != self.provider_id:
            raise _adapter_inconsistent(
                "Provider credential execution 未返回规范化 receipt。"
            )
        return receipt


def resolve_model_provider_adapter(provider: object) -> ModelProviderAdapter:
    """Resolve one exact reviewed production adapter; never selects a fallback."""
    try:
        adapter = _PRODUCTION_REGISTRY.resolve(provider)
    except ModelProviderRegistryError as exc:
        raise _registry_error(exc) from exc
    return _AuthorityClosingAdapter(adapter)


def resolve_default_model_provider_adapter() -> ModelProviderAdapter:
    """Resolve the explicit Product default; adding adapters does not change this choice."""
    return resolve_model_provider_adapter(DEFAULT_PROVIDER_ID)


def production_provider_ids() -> tuple[str, ...]:
    """Expose non-secret provider identities only; performs zero I/O."""
    return _PRODUCTION_REGISTRY.provider_ids()
