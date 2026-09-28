"""DeepSeek implementation of the generic model provider adapter contract.

This wrapper delegates to the existing reviewed DeepSeek current-authority and transport
modules. It normalizes provider-specific evidence into generic facts, but it does not own
qualification admission, Human authorization, one-shot claims, result persistence, retry,
or provider fallback. Generic Gateway remains the decision point for qualification and
provider-bound authorization closure.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app import deepseek_current_authority, deepseek_transport
from app.model_gateway_diagnostics import safe_authority_error
from app.model_provider_contract import (
    ProviderCapability,
    ProviderCurrentAuthority,
    ProviderReceipt,
    ProviderRequest,
)


_GENERATE_TASK = "daily_report_generate"
_REGENERATE_TASK = "daily_report_regenerate"
_CONTEXT_WINDOW_TOKENS = 1_000_000
_PURPOSE_BY_TASK = {
    _GENERATE_TASK: "anxin_board_daily_report_v1",
    _REGENERATE_TASK: "anxin_board_daily_report_regenerate_v1",
}
_CAPABILITY_KEYS = (
    "schema_version",
    "provider",
    "model_id",
    "model_version",
    "endpoint_origin",
    "endpoint_path",
    "task_type",
    "output_schema_version",
    "response_format",
    "stream",
    "thinking",
    "reasoning_effort",
    "max_output_tokens",
)
_AUTHORITY_KEYS = (
    "schema_version",
    "model_call_id",
    "call_identity_hash",
    "provider",
    "model_id",
    "model_version",
    "context_window_tokens",
    "max_output_tokens",
    "purpose_id",
    "data_scope_hash",
    "qualification",
    "authorization",
    "authority_hash",
)
_GENERATE_QUALIFICATION_KEYS = (
    "schema_version",
    "authority_source_id",
    "authority_source_version",
    "authority_ref",
    "provider",
    "model_id",
    "model_version",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_status",
    "context_window_tokens",
    "max_output_tokens",
    "checked_at",
    "models_source_hash",
    "metadata_source_hash",
    "evidence_hash",
)
_REGENERATE_QUALIFICATION_KEYS = (
    "schema_version",
    "authority_source_id",
    "authority_source_version",
    "authority_ref",
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "ai_contract_schema_version",
    "output_schema_version",
    "prompt_version",
    "prompt_contract_hash",
    "rule_version",
    "sample_pack_version",
    "sampling_parameters_hash",
    "qualification_status",
    "sample_manifest_hash",
    "qualification_harness_commit",
    "evidence_manifest_hash",
    "review_ref",
    "record_hash",
    "context_window_tokens",
    "max_output_tokens",
    "evidence_hash",
)
_AUTHORIZATION_KEYS = (
    "schema_version",
    "authority_source_id",
    "authority_source_version",
    "authority_ref",
    "provider",
    "authorized",
    "valid",
    "purpose_id",
    "data_scope_hash",
    "evidence_hash",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _adapter_inconsistent(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_ADAPTER_INCONSISTENT", message)


def _unsupported(message: str) -> HTTPException:
    return _error("MODEL_PROVIDER_ADAPTER_UNSUPPORTED_IDENTITY", message)


def _authority_unavailable() -> HTTPException:
    return _error(
        "MODEL_PROVIDER_AUTHORITY_UNAVAILABLE",
        "当前 provider authority 无法完成 exact-current 解析。",
    )


def _qualification_not_current() -> HTTPException:
    return _error(
        "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT",
        "当前 provider/model qualification 未形成 exact-current closure。",
    )


def _authorization_not_current() -> HTTPException:
    return _error(
        "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT",
        "当前 provider 数据发送授权未形成 exact-current closure。",
    )


def _non_empty(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _stable_hash(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _adapter_inconsistent("DeepSeek authority canonicalization 失败。") from exc
    return hashlib.sha256(raw).hexdigest()


def _evidence_hash_valid(value: Mapping[str, object], *, hash_field: str) -> bool:
    if not _is_sha256(value.get(hash_field)):
        return False
    unsigned = {key: deepcopy(value[key]) for key in value if key != hash_field}
    return value[hash_field] == _stable_hash(unsigned)


def _normalize_capability(raw: object) -> ProviderCapability:
    if not isinstance(raw, Mapping) or tuple(raw) != _CAPABILITY_KEYS:
        raise _adapter_inconsistent("DeepSeek capability exact shape 无法闭合。")
    if (
        raw["schema_version"] != deepseek_transport.CAPABILITY_SCHEMA_VERSION
        or raw["provider"] != deepseek_transport.PROVIDER
        or raw["model_id"] != deepseek_transport.MODEL_ID
        or raw["model_version"] != deepseek_transport.MODEL_VERSION
        or raw["endpoint_origin"] != deepseek_transport.ENDPOINT_ORIGIN
        or raw["endpoint_path"] != deepseek_transport.ENDPOINT_PATH
        or not _non_empty(raw["task_type"])
        or not _non_empty(raw["output_schema_version"])
        or raw["response_format"] != "json_object"
        or raw["stream"] is not False
        or raw["thinking"] != "enabled"
        or raw["reasoning_effort"] != "high"
        or type(raw["max_output_tokens"]) is not int
        or raw["max_output_tokens"] != deepseek_transport.MAX_OUTPUT_TOKENS
    ):
        raise _adapter_inconsistent("DeepSeek capability identity/constraint 无法闭合。")
    return ProviderCapability(
        provider=str(raw["provider"]),
        model_id=str(raw["model_id"]),
        model_version=str(raw["model_version"]),
        task_type=str(raw["task_type"]),
        output_schema_version=str(raw["output_schema_version"]),
        context_window_tokens=_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=int(raw["max_output_tokens"]),
    )


def _qualification_sample_pack(
    qualification: Mapping[str, object], *, task_type: str
) -> object:
    return qualification.get(
        "sample_pack_version"
        if task_type == _REGENERATE_TASK
        else "benchmark_sample_pack_version"
    )


def _generate_qualification_shape_valid(qualification: Mapping[str, object]) -> bool:
    return (
        tuple(qualification) == _GENERATE_QUALIFICATION_KEYS
        and qualification.get("schema_version")
        == deepseek_current_authority.QUALIFICATION_SCHEMA_VERSION
        and qualification.get("authority_source_id")
        == "deepseek_official_models_and_metadata"
        and qualification.get("authority_source_version") == "v1"
        and qualification.get("authority_ref")
        == {
            "model_list": "deepseek_api_models",
            "model_metadata": "deepseek_api_docs_models_pricing",
        }
        and _non_empty(qualification.get("checked_at"))
        and _is_sha256(qualification.get("models_source_hash"))
        and _is_sha256(qualification.get("metadata_source_hash"))
    )


def _regenerate_qualification_shape_valid(
    qualification: Mapping[str, object],
) -> bool:
    authority_ref = qualification.get("authority_ref")
    return (
        tuple(qualification) == _REGENERATE_QUALIFICATION_KEYS
        and qualification.get("schema_version")
        == deepseek_current_authority.REGISTRY_QUALIFICATION_SCHEMA_VERSION
        and qualification.get("authority_source_id")
        == "product_model_qualification_registry"
        and qualification.get("authority_source_version") == "v1"
        and isinstance(authority_ref, Mapping)
        and tuple(authority_ref) == ("record_hash", "review_ref")
        and authority_ref.get("record_hash") == qualification.get("record_hash")
        and authority_ref.get("review_ref") == qualification.get("review_ref")
        and qualification.get("task_type") == _REGENERATE_TASK
        and qualification.get("ai_contract_schema_version")
        == deepseek_current_authority.AI_CONTRACT_SCHEMA_VERSION
        and qualification.get("output_schema_version")
        == deepseek_current_authority.REGENERATE_OUTPUT_SCHEMA_VERSION
        and qualification.get("prompt_version")
        == deepseek_current_authority.REGENERATE_PROMPT_VERSION
        and _is_sha256(qualification.get("prompt_contract_hash"))
        and _is_sha256(qualification.get("sampling_parameters_hash"))
        and _is_sha256(qualification.get("sample_manifest_hash"))
        and _non_empty(qualification.get("qualification_harness_commit"))
        and _is_sha256(qualification.get("evidence_manifest_hash"))
        and _non_empty(qualification.get("review_ref"))
        and _is_sha256(qualification.get("record_hash"))
    )


def _authorization_shape_valid(
    authorization: Mapping[str, object], *, expected_purpose: str, data_scope_hash: object
) -> bool:
    return (
        tuple(authorization) == _AUTHORIZATION_KEYS
        and authorization.get("schema_version")
        == deepseek_current_authority.AUTHORIZATION_SCHEMA_VERSION
        and authorization.get("authority_source_id")
        == "windows_process_env_human_permit"
        and authorization.get("authority_source_version") == "v1"
        and authorization.get("authority_ref") == "process-env/exact-scope"
        and authorization.get("provider") == deepseek_transport.PROVIDER
        and authorization.get("authorized") is True
        and authorization.get("valid") is True
        and authorization.get("purpose_id") == expected_purpose
        and authorization.get("data_scope_hash") == data_scope_hash
    )


def _normalize_authority(
    raw: object,
    *,
    model_call_id: int,
    task_type: str,
) -> ProviderCurrentAuthority:
    if not isinstance(raw, Mapping) or tuple(raw) != _AUTHORITY_KEYS:
        raise _adapter_inconsistent("DeepSeek current authority exact shape 无法闭合。")
    qualification = raw.get("qualification")
    authorization = raw.get("authorization")
    expected_purpose = _PURPOSE_BY_TASK.get(task_type)
    if expected_purpose is None:
        raise _unsupported("DeepSeek adapter 不支持该 authority task identity。")
    if not isinstance(qualification, Mapping) or not isinstance(authorization, Mapping):
        raise _adapter_inconsistent("DeepSeek current authority evidence shape 无法闭合。")
    if task_type == _GENERATE_TASK:
        qualification_shape_valid = _generate_qualification_shape_valid(qualification)
    else:
        qualification_shape_valid = _regenerate_qualification_shape_valid(qualification)
    qualification_sample_pack = _qualification_sample_pack(
        qualification,
        task_type=task_type,
    )
    authority_hash = raw.get("authority_hash")
    if (
        not qualification_shape_valid
        or not _authorization_shape_valid(
            authorization,
            expected_purpose=expected_purpose,
            data_scope_hash=raw.get("data_scope_hash"),
        )
        or raw.get("schema_version") != deepseek_current_authority.SCHEMA_VERSION
        or raw.get("model_call_id") != model_call_id
        or raw.get("provider") != deepseek_transport.PROVIDER
        or raw.get("model_id") != deepseek_transport.MODEL_ID
        or raw.get("model_version") != deepseek_transport.MODEL_VERSION
        or not _is_sha256(raw.get("call_identity_hash"))
        or raw.get("context_window_tokens") != _CONTEXT_WINDOW_TOKENS
        or raw.get("max_output_tokens") != deepseek_transport.MAX_OUTPUT_TOKENS
        or raw.get("purpose_id") != expected_purpose
        or not _is_sha256(raw.get("data_scope_hash"))
        or qualification.get("provider") != deepseek_transport.PROVIDER
        or qualification.get("model_id") != deepseek_transport.MODEL_ID
        or qualification.get("model_version") != deepseek_transport.MODEL_VERSION
        or not _non_empty(qualification.get("rule_version"))
        or not _non_empty(qualification.get("output_schema_version"))
        or not _non_empty(qualification_sample_pack)
        or qualification.get("qualification_status") != "qualified"
        or qualification.get("context_window_tokens") != _CONTEXT_WINDOW_TOKENS
        or qualification.get("max_output_tokens") != deepseek_transport.MAX_OUTPUT_TOKENS
        or qualification.get("authority_ref") in (None, "", {}, [])
        or not _evidence_hash_valid(qualification, hash_field="evidence_hash")
        or not _evidence_hash_valid(authorization, hash_field="evidence_hash")
        or not _is_sha256(authority_hash)
        or authority_hash
        != _stable_hash({key: deepcopy(raw[key]) for key in raw if key != "authority_hash"})
    ):
        raise _adapter_inconsistent("DeepSeek current authority identity/evidence/hash 无法闭合。")
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
        qualification_benchmark_sample_pack_version=str(qualification_sample_pack),
        qualification_status=str(qualification["qualification_status"]),
        qualification_authority_ref=deepcopy(qualification["authority_ref"]),
        qualification_evidence_hash=str(qualification["evidence_hash"]),
        authorization_authorized=bool(authorization["authorized"]),
        authorization_valid=bool(authorization["valid"]),
        authorization_authority_ref=deepcopy(authorization["authority_ref"]),
        authorization_evidence_hash=str(authorization["evidence_hash"]),
    )


def _normalize_receipt(raw: object) -> ProviderReceipt:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("result"), Mapping):
        raise _adapter_inconsistent("DeepSeek normalized receipt shape 无法闭合。")
    string_fields = (
        "provider",
        "provider_response_id",
        "actual_model",
        "provider_runtime_fingerprint",
        "finish_reason",
    )
    if (
        raw.get("provider") != deepseek_transport.PROVIDER
        or any(not _non_empty(raw.get(field)) for field in string_fields)
        or any(
            type(raw.get(field)) is not int or raw[field] < 0
            for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        or raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]
    ):
        raise _adapter_inconsistent("DeepSeek normalized receipt identity/token closure 无法闭合。")
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


def _map_authority_failure(exc: HTTPException) -> HTTPException:
    detail = exc.detail
    code = detail.get("code") if isinstance(detail, Mapping) else None
    if code in {
        "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE",
        "DEEPSEEK_AUTHORITY_CREDENTIAL_READ_FAILED",
        "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE",
    }:
        return safe_authority_error({"code": "MODEL_PROVIDER_AUTHORITY_UNAVAILABLE", "cause_code": code, "credential_error_code": detail.get("credential_error_code")})
    if code in {
        "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED",
        "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED",
        "DEEPSEEK_AUTHORITY_PROVIDER_CONSTRAINT_CHANGED",
        "DEEPSEEK_AUTHORITY_QUALIFICATION_NOT_ADMITTED",
    }:
        return safe_authority_error({"code": "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT", "cause_code": code})
    if code == "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED":
        return safe_authority_error({"code": "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT", "cause_code": code})
    return _adapter_inconsistent("DeepSeek authority failure code 未在 adapter contract 中登记。")


class DeepSeekProviderAdapter:
    @property
    def provider_id(self) -> str:
        return deepseek_transport.PROVIDER

    def get_capability(
        self,
        *,
        task_type: str,
        output_schema_version: str,
    ) -> ProviderCapability:
        return _normalize_capability(
            deepseek_transport.get_deepseek_transport_capability(
                task_type=task_type,
                output_schema_version=output_schema_version,
            )
        )

    def estimate_request_utf8_bytes(
        self,
        *,
        messages: tuple[Mapping[str, object], ...],
        max_output_tokens: int,
    ) -> int:
        materialized_messages = [dict(message) for message in messages]
        validated_messages = deepseek_transport._validate_messages(materialized_messages)
        validated_max_tokens = deepseek_transport._validate_max_tokens(max_output_tokens)
        request_body = {
            "model": deepseek_transport.MODEL_ID,
            "messages": validated_messages,
            "response_format": {"type": "json_object"},
            "stream": False,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "max_tokens": validated_max_tokens,
        }
        try:
            return len(
                json.dumps(
                    request_body,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise _adapter_inconsistent("DeepSeek request byte accounting 失败。") from exc

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
        try:
            if task_type == _GENERATE_TASK:
                if any(
                    value is not None
                    for value in (
                        request_envelope_hash,
                        prompt_contract_hash,
                        sampling_parameters_hash,
                    )
                ):
                    raise _unsupported("Generate authority 不接受 regenerate-only identity。")
                raw = deepseek_current_authority.resolve_deepseek_current_authority(
                    model_call_id=model_call_id,
                    final_context_manifest_hash=final_context_manifest_hash,
                    framed_payload_hash=framed_payload_hash,
                )
            elif task_type == _REGENERATE_TASK:
                if not all(
                    _non_empty(value)
                    for value in (
                        request_envelope_hash,
                        prompt_contract_hash,
                        sampling_parameters_hash,
                    )
                ):
                    raise _unsupported("Regenerate authority 需要完整 request/prompt/sampling identity。")
                raw = deepseek_current_authority.resolve_deepseek_regenerate_current_authority(
                    model_call_id=model_call_id,
                    final_context_manifest_hash=final_context_manifest_hash,
                    framed_payload_hash=framed_payload_hash,
                    request_envelope_hash=str(request_envelope_hash),
                    prompt_contract_hash=str(prompt_contract_hash),
                    sampling_parameters_hash=str(sampling_parameters_hash),
                )
            else:
                raise _unsupported("DeepSeek adapter 不支持该 task identity。")
        except HTTPException as exc:
            detail = exc.detail
            code = detail.get("code") if isinstance(detail, Mapping) else None
            if code in {
                "MODEL_PROVIDER_ADAPTER_UNSUPPORTED_IDENTITY",
                "MODEL_PROVIDER_ADAPTER_INCONSISTENT",
            }:
                raise
            raise _map_authority_failure(exc) from exc
        return _normalize_authority(raw, model_call_id=model_call_id, task_type=task_type)

    def execute(self, request: ProviderRequest) -> ProviderReceipt:
        if (
            request.provider != self.provider_id
            or request.model_id != deepseek_transport.MODEL_ID
            or request.model_version != deepseek_transport.MODEL_VERSION
        ):
            raise _unsupported("Provider request identity 与 DeepSeek adapter 不一致。")
        capability = self.get_capability(
            task_type=request.task_type,
            output_schema_version=request.output_schema_version,
        )
        if request.max_output_tokens <= 0 or request.max_output_tokens > capability.max_output_tokens:
            raise _unsupported("Provider request max_output_tokens 超出 capability。")
        messages = [dict(message) for message in request.messages]
        return _normalize_receipt(
            deepseek_transport.send_deepseek_v4_flash(
                messages=messages,
                max_tokens=request.max_output_tokens,
            )
        )


def build_deepseek_provider_adapter() -> DeepSeekProviderAdapter:
    return DeepSeekProviderAdapter()
