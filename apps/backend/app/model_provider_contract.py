"""Provider-agnostic model adapter contract.

The contract carries provider-specific capability/current-authority facts, provider
request byte accounting, and exactly one normalized transport request/receipt boundary.
It intentionally owns no Evidence, Human authorization, qualification admission,
one-shot claim, Result Ledger, retry, or business-state authority.

Adapters provide normalized current facts. The generic Gateway remains responsible for
deciding whether qualification and provider-bound Human authorization close against the
durable Model Call / Final Context Manifest identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider: str
    model_id: str
    model_version: str
    task_type: str
    output_schema_version: str
    context_window_tokens: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderCurrentAuthority:
    model_call_id: int
    call_identity_hash: str
    provider: str
    model_id: str
    model_version: str
    context_window_tokens: int
    max_output_tokens: int
    purpose_id: str
    data_scope_hash: str
    qualification_rule_version: str
    qualification_output_schema_version: str
    qualification_benchmark_sample_pack_version: str
    qualification_status: str
    qualification_authority_ref: object
    qualification_evidence_hash: str
    authorization_authorized: bool
    authorization_valid: bool
    authorization_authority_ref: object
    authorization_evidence_hash: str


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    model_call_id: int
    call_identity_hash: str
    provider: str
    model_id: str
    model_version: str
    task_type: str
    output_schema_version: str
    messages: tuple[Mapping[str, object], ...]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderCredentialRequest:
    """Provider-agnostic transient request with an explicit in-memory credential.

    This seam exists for legacy/upstream product tasks that already own a durable local
    task/authorization ledger but predate the formal ModelCall ledger. It grants no
    qualification or Human authorization by itself.
    """

    local_task_id: str
    provider: str
    model_id: str
    model_version: str
    task_type: str
    output_schema_version: str
    messages: tuple[Mapping[str, object], ...]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    provider: str
    provider_response_id: str
    actual_model: str
    provider_runtime_fingerprint: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    result: Mapping[str, object]


class ModelProviderAdapter(Protocol):
    """Narrow provider seam consumed by generic model orchestration."""

    @property
    def provider_id(self) -> str: ...

    def get_capability(
        self,
        *,
        task_type: str,
        output_schema_version: str,
    ) -> ProviderCapability: ...

    def estimate_request_utf8_bytes(
        self,
        *,
        messages: tuple[Mapping[str, object], ...],
        max_output_tokens: int,
    ) -> int: ...

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
    ) -> ProviderCurrentAuthority: ...

    def execute(self, request: ProviderRequest) -> ProviderReceipt: ...

    def execute_with_credential(
        self, request: ProviderCredentialRequest, credential: str
    ) -> ProviderReceipt: ...
