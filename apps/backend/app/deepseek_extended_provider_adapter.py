"""DeepSeek provider adapter extension for the remaining PRD V0.5 model tasks.

The reviewed Lane-A adapter remains authoritative for daily generation/regeneration.
This extension adds the two remaining closed-world task identities and an explicit
in-memory credential seam needed by the pre-ModelCall Project Profile workflow.
No business authorization, qualification admission, retry, or fallback is granted here.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json

from fastapi import HTTPException

from app import deepseek_current_authority, deepseek_transport
from app.deepseek_provider_adapter import build_deepseek_provider_adapter
from app.model_provider_contract import (
    ProviderCapability,
    ProviderCredentialRequest,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)
from app import page07_contradiction_authority


_PROFILE_TASK = "project_profile_build"
_PROFILE_SCHEMA = "project-profile-build/1.0"
_CONTRADICTION_TASK = page07_contradiction_authority.TASK_TYPE
_CONTRADICTION_SCHEMA = page07_contradiction_authority.OUTPUT_SCHEMA_VERSION
_ADDITIONAL_IDENTITIES = {
    (_PROFILE_TASK, _PROFILE_SCHEMA),
    (_PROFILE_TASK, "project-profile-build/2.0"),
    (_CONTRADICTION_TASK, _CONTRADICTION_SCHEMA),
}
_CONTEXT_WINDOW_TOKENS = 1_000_000


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _unsupported(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_ADAPTER_UNSUPPORTED_IDENTITY", message)


def _inconsistent(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_ADAPTER_INCONSISTENT", message)


def _stable_hash(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_receipt(raw: object) -> ProviderReceipt:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("result"), Mapping):
        raise _inconsistent("DeepSeek extended receipt shape 无法闭合。")
    for field in (
        "provider", "provider_response_id", "actual_model",
        "provider_runtime_fingerprint", "finish_reason",
    ):
        if type(raw.get(field)) is not str or not str(raw[field]).strip():
            raise _inconsistent("DeepSeek extended receipt identity 不完整。")
    if raw.get("provider") != deepseek_transport.PROVIDER:
        raise _inconsistent("DeepSeek extended receipt provider mismatch。")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if type(raw.get(field)) is not int or raw[field] < 0:
            raise _inconsistent("DeepSeek extended receipt token fields 无效。")
    if raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]:
        raise _inconsistent("DeepSeek extended receipt token closure 失败。")
    return ProviderReceipt(
        provider=str(raw["provider"]),
        provider_response_id=str(raw["provider_response_id"]),
        actual_model=str(raw["actual_model"]),
        provider_runtime_fingerprint=str(raw["provider_runtime_fingerprint"]),
        finish_reason=str(raw["finish_reason"]),
        prompt_tokens=int(raw["prompt_tokens"]),
        completion_tokens=int(raw["completion_tokens"]),
        total_tokens=int(raw["total_tokens"]),
        result=deepcopy(raw["result"]),
    )


def _validate_request_identity(request: object, adapter: "DeepSeekExtendedProviderAdapter") -> None:
    if not isinstance(request, (ProviderRequest, ProviderCredentialRequest)):
        raise _unsupported("DeepSeek extended request type 不受支持。")
    capability = adapter.get_capability(
        task_type=request.task_type, output_schema_version=request.output_schema_version
    )
    if (
        request.provider != deepseek_transport.PROVIDER
        or request.model_id != deepseek_transport.MODEL_ID
        or request.model_version != deepseek_transport.MODEL_VERSION
        or request.provider != capability.provider
        or request.model_id != capability.model_id
        or request.model_version != capability.model_version
        or type(request.max_output_tokens) is not int
        or request.max_output_tokens <= 0
        or request.max_output_tokens > capability.max_output_tokens
    ):
        raise _unsupported("DeepSeek extended request identity/capability mismatch。")


def _normalize_contradiction_authority(raw: object, *, model_call_id: int) -> ProviderCurrentAuthority:
    if not isinstance(raw, Mapping):
        raise _inconsistent("Contradiction authority shape 无效。")
    qualification = raw.get("qualification")
    authorization = raw.get("authorization")
    if not isinstance(qualification, Mapping) or not isinstance(authorization, Mapping):
        raise _inconsistent("Contradiction authority evidence shape 无效。")
    unsigned = {key: deepcopy(raw[key]) for key in raw if key != "authority_hash"}
    q_unsigned = {key: deepcopy(qualification[key]) for key in qualification if key != "evidence_hash"}
    a_unsigned = {key: deepcopy(authorization[key]) for key in authorization if key != "evidence_hash"}
    if (
        raw.get("model_call_id") != model_call_id
        or raw.get("provider") != deepseek_transport.PROVIDER
        or raw.get("model_id") != deepseek_transport.MODEL_ID
        or raw.get("model_version") != deepseek_transport.MODEL_VERSION
        or raw.get("purpose_id") != page07_contradiction_authority.PURPOSE_ID
        or qualification.get("task_type") != _CONTRADICTION_TASK
        or qualification.get("output_schema_version") != _CONTRADICTION_SCHEMA
        or qualification.get("rule_version") != page07_contradiction_authority.RULE_VERSION
        or qualification.get("sample_pack_version") != page07_contradiction_authority.SAMPLE_PACK_VERSION
        or qualification.get("qualification_status") != "qualified"
        or authorization.get("provider") != deepseek_transport.PROVIDER
        or authorization.get("purpose_id") != page07_contradiction_authority.PURPOSE_ID
        or authorization.get("authorized") is not True
        or authorization.get("valid") is not True
        or authorization.get("data_scope_hash") != raw.get("data_scope_hash")
        or raw.get("authority_hash") != _stable_hash(unsigned)
        or qualification.get("evidence_hash") != _stable_hash(q_unsigned)
        or authorization.get("evidence_hash") != _stable_hash(a_unsigned)
    ):
        raise _inconsistent("Contradiction authority exact identity/hash closure 失败。")
    return ProviderCurrentAuthority(
        model_call_id=model_call_id,
        call_identity_hash=str(raw["call_identity_hash"]),
        provider=str(raw["provider"]),
        model_id=str(raw["model_id"]),
        model_version=str(raw["model_version"]),
        context_window_tokens=int(raw["context_window_tokens"]),
        max_output_tokens=int(raw["max_output_tokens"]),
        purpose_id=str(raw["purpose_id"]),
        data_scope_hash=str(raw["data_scope_hash"]),
        qualification_rule_version=str(qualification["rule_version"]),
        qualification_output_schema_version=str(qualification["output_schema_version"]),
        qualification_benchmark_sample_pack_version=str(qualification["sample_pack_version"]),
        qualification_status=str(qualification["qualification_status"]),
        qualification_authority_ref=deepcopy(qualification["authority_ref"]),
        qualification_evidence_hash=str(qualification["evidence_hash"]),
        authorization_authorized=True,
        authorization_valid=True,
        authorization_authority_ref=deepcopy(authorization["authority_ref"]),
        authorization_evidence_hash=str(authorization["evidence_hash"]),
    )


class DeepSeekExtendedProviderAdapter:
    def __init__(self) -> None:
        self._base = build_deepseek_provider_adapter()

    @property
    def provider_id(self) -> str:
        return deepseek_transport.PROVIDER

    def get_capability(self, *, task_type: str, output_schema_version: str) -> ProviderCapability:
        if (task_type, output_schema_version) not in _ADDITIONAL_IDENTITIES:
            return self._base.get_capability(
                task_type=task_type, output_schema_version=output_schema_version
            )
        return ProviderCapability(
            provider=deepseek_transport.PROVIDER,
            model_id=deepseek_transport.MODEL_ID,
            model_version=deepseek_transport.MODEL_VERSION,
            task_type=task_type,
            output_schema_version=output_schema_version,
            context_window_tokens=_CONTEXT_WINDOW_TOKENS,
            max_output_tokens=deepseek_transport.MAX_OUTPUT_TOKENS,
        )

    def estimate_request_utf8_bytes(
        self, *, messages: tuple[Mapping[str, object], ...], max_output_tokens: int
    ) -> int:
        return self._base.estimate_request_utf8_bytes(
            messages=messages, max_output_tokens=max_output_tokens
        )

    def resolve_current_authority(
        self, *, model_call_id: int, final_context_manifest_hash: str,
        framed_payload_hash: str, task_type: str,
        request_envelope_hash: str | None = None,
        prompt_contract_hash: str | None = None,
        sampling_parameters_hash: str | None = None,
    ) -> ProviderCurrentAuthority:
        if task_type != _CONTRADICTION_TASK:
            return self._base.resolve_current_authority(
                model_call_id=model_call_id,
                final_context_manifest_hash=final_context_manifest_hash,
                framed_payload_hash=framed_payload_hash,
                task_type=task_type,
                request_envelope_hash=request_envelope_hash,
                prompt_contract_hash=prompt_contract_hash,
                sampling_parameters_hash=sampling_parameters_hash,
            )
        if not all(
            type(value) is str and bool(value)
            for value in (request_envelope_hash, prompt_contract_hash, sampling_parameters_hash)
        ):
            raise _unsupported("Contradiction authority 需要完整 request/prompt/sampling identity。")
        raw = page07_contradiction_authority.resolve_deepseek_contradiction_current_authority(
            model_call_id=model_call_id,
            final_context_manifest_hash=final_context_manifest_hash,
            framed_payload_hash=framed_payload_hash,
            request_envelope_hash=str(request_envelope_hash),
            prompt_contract_hash=str(prompt_contract_hash),
            sampling_parameters_hash=str(sampling_parameters_hash),
        )
        return _normalize_contradiction_authority(raw, model_call_id=model_call_id)

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        _validate_request_identity(request, self)
        if request.task_type not in {_PROFILE_TASK, _CONTRADICTION_TASK}:
            return self._base.execute(request)
        return _normalize_receipt(
            deepseek_transport.send_deepseek_v4_flash(
                messages=[dict(message) for message in request.messages],
                max_tokens=request.max_output_tokens,
            )
        )

    def execute_with_credential(
        self, request: ProviderCredentialRequest, credential: str
    ) -> ProviderReceipt:
        _validate_request_identity(request, self)
        if type(credential) is not str or not credential.strip():
            raise _unsupported("Explicit provider credential 不能为空。")
        return _normalize_receipt(
            deepseek_transport.send_deepseek_v4_flash_with_api_key(
                messages=[dict(message) for message in request.messages],
                max_tokens=request.max_output_tokens,
                api_key=credential,
            )
        )


def build_deepseek_extended_provider_adapter() -> DeepSeekExtendedProviderAdapter:
    return DeepSeekExtendedProviderAdapter()
