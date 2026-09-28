"""Exact-scope Human send permits for Page07 regenerate and contradiction execution.

All preview work is local-only: it re-closes durable identities, materializes the exact
Gateway request plan, and derives the same data-scope hash that Current Authority will
verify at send time. This module never reads provider credentials or performs provider
I/O. A permit is process-local, short-lived, single-consumption, and cannot overwrite a
permit owned by another flow.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import os
import re
import threading
import time

from fastapi import HTTPException

from app.context_candidate_runtime import candidate_materialization_scope
from app.page07_qualification_preflight import require_regenerate_qualification

from app import final_context_manifest, model_gateway, model_provider_runtime, report_review, report_task_subjects
from app.model_call_ledger import get_model_call
from app.model_provider_contract import ProviderCapability
from app.page07_contradiction_authority import (
    OUTPUT_SCHEMA_VERSION as CONTRADICTION_OUTPUT_SCHEMA_VERSION,
    PURPOSE_ID as CONTRADICTION_PURPOSE_ID,
    RULE_VERSION as CONTRADICTION_RULE_VERSION,
    SAMPLE_PACK_VERSION as CONTRADICTION_SAMPLE_PACK_VERSION,
    TASK_TYPE as CONTRADICTION_TASK_TYPE,
)
from app.page07_contradiction_preparation import get_report_contradiction_budget_record
from app.page07_model_preparation import get_daily_report_regenerate_budget_record


SCHEMA_VERSION = "page07_send_authorization_v1"
PERMIT_TTL_SECONDS = 300
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_AUTHORIZED = "ANXIN_DEEPSEEK_SEND_AUTHORIZED"
_ENV_PURPOSE = "ANXIN_DEEPSEEK_SEND_PURPOSE_ID"
_ENV_SCOPE = "ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH"
_REGENERATE_TASK = "daily_report_regenerate"
_REGENERATE_OUTPUT_SCHEMA = "daily-report-regenerate/1.0"
_REGENERATE_RULE = "page07-regenerate-rules/2.0"
_REGENERATE_SAMPLE_PACK = "page07-regenerate-qualification-pack/2.0"
_REGENERATE_PURPOSE = "anxin_board_daily_report_regenerate_v1"

_lock = threading.Lock()
_active_scope_hash: str | None = None
_active_purpose_id: str | None = None
_active_deadline: float | None = None
_in_flight = False
_active_binding: dict[str, object] | None = None


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _require_preview_inputs(project_id: object, report_version_id: object, model_call_id: object) -> tuple[int, int, int]:
    if not all(_positive(value) for value in (project_id, report_version_id, model_call_id)):
        raise _error("PAGE07_SEND_AUTHORIZATION_INPUT_INVALID", "project/report/model_call selector 必须是正整数。")
    return int(project_id), int(report_version_id), int(model_call_id)


def _manifest_for(model_call_id: int, budget: Mapping[str, object]) -> Mapping[str, object]:
    return model_gateway._validate_manifest(
        final_context_manifest.build_final_context_manifest(
            model_call_id=model_call_id,
            budget_record=budget,
        ),
        model_call_id=model_call_id,
    )


def _fit_or_block(*, reserved_output: object, upper: object, capability: object) -> None:
    if (
        type(reserved_output) is not int
        or reserved_output <= 0
        or type(upper) is not int
        or upper <= 0
        or not isinstance(capability, ProviderCapability)
        or type(capability.max_output_tokens) is not int
        or capability.max_output_tokens <= 0
        or type(capability.context_window_tokens) is not int
        or capability.context_window_tokens <= 0
        or reserved_output > capability.max_output_tokens
        or upper > capability.context_window_tokens
    ):
        raise _error("PAGE07_SEND_AUTHORIZATION_REQUEST_NOT_FIT", "当前 exact request 超过 provider capability，不能请求真实发送授权。")


@candidate_materialization_scope()
def build_reanalysis_send_authorization_preview(*, project_id: int, report_version_id: int, model_call_id: int) -> dict[str, object]:
    project_id, report_version_id, model_call_id = _require_preview_inputs(project_id, report_version_id, model_call_id)
    bundle = report_review.get_reanalysis_request(project_id=project_id, report_version_id=report_version_id)
    if bundle.get("cancelled") is True:
        raise _error("REPORT_REANALYSIS_CANCELLED", "这次未发送的重分析已撤销；请返回原报告继续审阅。")
    replacement = bundle.get("replacement_task")
    call = get_model_call(model_call_id)
    if (
        not isinstance(replacement, Mapping)
        or call.get("project_id") != project_id
        or call.get("local_task_id") != replacement.get("local_task_id")
        or call.get("task_type") != _REGENERATE_TASK
        or call.get("output_schema_version") != _REGENERATE_OUTPUT_SCHEMA
        or call.get("rule_version") != _REGENERATE_RULE
        or call.get("benchmark_sample_pack_version") != _REGENERATE_SAMPLE_PACK
        or call.get("preparation_state") != "prepared"
    ):
        raise _error("PAGE07_SEND_AUTHORIZATION_BINDING_INVALID", "ReanalysisRequest / replacement task / ModelCall 无法形成 exact binding。")

    early_adapter = model_provider_runtime.resolve_model_provider_adapter(str(call["provider"]))
    early_capability = early_adapter.get_capability(task_type=_REGENERATE_TASK, output_schema_version=_REGENERATE_OUTPUT_SCHEMA)
    require_regenerate_qualification(capability=early_capability, rule_version=call["rule_version"],
        sample_pack_version=call["benchmark_sample_pack_version"], expected_identity=call)
    budget = get_daily_report_regenerate_budget_record(provider=str(call["provider"]))
    manifest = _manifest_for(model_call_id, budget)
    expected_manifest = {
        "project_id": project_id,
        "task_type": _REGENERATE_TASK,
        "output_schema_version": _REGENERATE_OUTPUT_SCHEMA,
        "rule_version": _REGENERATE_RULE,
        "benchmark_sample_pack_version": _REGENERATE_SAMPLE_PACK,
        "call_identity_hash": call.get("call_identity_hash"),
    }
    if any(manifest.get(field) != expected for field, expected in expected_manifest.items()):
        raise _error("PAGE07_SEND_AUTHORIZATION_BINDING_INVALID", "Regenerate Final Context Manifest 与 exact call 漂移。")
    if manifest.get("local_request_readiness_state") != "ready_for_gateway_evaluation":
        raise _error("PAGE07_SEND_AUTHORIZATION_NOT_READY", "Regenerate 本地上下文尚未通过，不能请求真实发送授权。")

    payload = model_gateway._materialize_payload_rebound(
        manifest=manifest,
        model_call_id=model_call_id,
        budget_record=budget,
    )
    rebound_call = model_gateway._read_regenerate_model_call(manifest=manifest, model_call_id=model_call_id)
    subject = model_gateway._read_regenerate_subject(
        call=rebound_call,
        manifest=manifest,
        report_version_id=report_version_id,
    )
    transport_capability = model_gateway._load_regenerate_capability()
    if transport_capability is None:
        raise _error("PAGE07_SEND_AUTHORIZATION_PROVIDER_INCOMPATIBLE", "当前 provider 不支持 exact regenerate request。")
    adapter = model_provider_runtime.resolve_model_provider_adapter(str(manifest["provider"]))
    capability = adapter.get_capability(
        task_type=_REGENERATE_TASK, output_schema_version=_REGENERATE_OUTPUT_SCHEMA,
    )
    identity_fields = ("provider", "model_id", "model_version", "task_type", "output_schema_version")
    if (not isinstance(capability, ProviderCapability)
            or any(getattr(capability, key) != manifest.get(key)
                   or getattr(capability, key) != transport_capability.get(key) for key in identity_fields)):
        raise _error("PAGE07_SEND_AUTHORIZATION_PROVIDER_INCOMPATIBLE", "当前 provider 与 exact regenerate transport 身份不一致。")
    plan = model_gateway._build_regenerate_gateway_request_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget,
        task_subject=subject,
    )
    _fit_or_block(
        reserved_output=plan.get("reserved_output_tokens"),
        upper=plan.get("conservative_local_request_upper_bound"),
        capability=capability,
    )
    transport_output = transport_capability.get("max_output_tokens")
    if type(transport_output) is not int or transport_output <= 0 or plan["reserved_output_tokens"] > transport_output:
        raise _error("PAGE07_SEND_AUTHORIZATION_REQUEST_NOT_FIT", "当前 exact request 超过 transport 输出能力，不能请求真实发送授权。")
    data_scope_hash = model_gateway._expected_regenerate_data_scope_hash(manifest=manifest, plan=plan)
    if not _is_hash(data_scope_hash):
        raise _error("PAGE07_SEND_AUTHORIZATION_BINDING_INVALID", "Regenerate exact data scope hash 无效。")
    return {
        "schema_version": SCHEMA_VERSION,
        "flow": "report_reanalysis",
        "project_id": project_id,
        "report_version_id": report_version_id,
        "local_task_id": call["local_task_id"],
        "model_call_id": model_call_id,
        "call_identity_hash": call["call_identity_hash"],
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "purpose_id": _REGENERATE_PURPOSE,
        "data_scope_hash": data_scope_hash,
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "request_envelope_hash": plan["request_envelope_hash"],
        "authorization_state": "awaiting_human_confirmation",
        "provider_send_state": "not_attempted",
    }


def build_contradiction_send_authorization_preview(*, project_id: int, report_version_id: int, model_call_id: int) -> dict[str, object]:
    project_id, report_version_id, model_call_id = _require_preview_inputs(project_id, report_version_id, model_call_id)
    call = get_model_call(model_call_id)
    if (
        call.get("project_id") != project_id
        or call.get("task_type") != CONTRADICTION_TASK_TYPE
        or call.get("output_schema_version") != CONTRADICTION_OUTPUT_SCHEMA_VERSION
        or call.get("rule_version") != CONTRADICTION_RULE_VERSION
        or call.get("benchmark_sample_pack_version") != CONTRADICTION_SAMPLE_PACK_VERSION
        or call.get("preparation_state") != "prepared"
    ):
        raise _error("PAGE07_SEND_AUTHORIZATION_BINDING_INVALID", "Contradiction ModelCall 与 route selector 无法形成 exact binding。")

    subject = report_task_subjects.build_report_contradiction_subject(
        project_id=project_id,
        report_version_id=report_version_id,
    )
    if subject.get("task_type") != CONTRADICTION_TASK_TYPE or subject.get("subject", {}).get("local_task_id") not in {None, call.get("local_task_id")}:
        raise _error("PAGE07_SEND_AUTHORIZATION_BINDING_INVALID", "Contradiction subject 与 exact call 漂移。")

    budget = get_report_contradiction_budget_record(provider=str(call["provider"]))
    manifest = _manifest_for(model_call_id, budget)
    expected_manifest = {
        "project_id": project_id,
        "task_type": CONTRADICTION_TASK_TYPE,
        "output_schema_version": CONTRADICTION_OUTPUT_SCHEMA_VERSION,
        "rule_version": CONTRADICTION_RULE_VERSION,
        "benchmark_sample_pack_version": CONTRADICTION_SAMPLE_PACK_VERSION,
        "call_identity_hash": call.get("call_identity_hash"),
    }
    if any(manifest.get(field) != expected for field, expected in expected_manifest.items()):
        raise _error("PAGE07_SEND_AUTHORIZATION_BINDING_INVALID", "Contradiction Final Context Manifest 与 exact call 漂移。")
    if manifest.get("local_request_readiness_state") != "ready_for_gateway_evaluation":
        raise _error("PAGE07_SEND_AUTHORIZATION_NOT_READY", "Contradiction 本地上下文尚未通过，不能请求真实发送授权。")

    payload = model_gateway._materialize_payload_rebound(
        manifest=manifest,
        model_call_id=model_call_id,
        budget_record=budget,
    )
    plan = model_gateway._build_contradiction_plan(
        manifest=manifest,
        payload=payload,
        budget_record=budget,
        task_subject=subject,
    )
    adapter = model_provider_runtime.resolve_model_provider_adapter(str(manifest["provider"]))
    capability = adapter.get_capability(
        task_type=CONTRADICTION_TASK_TYPE,
        output_schema_version=CONTRADICTION_OUTPUT_SCHEMA_VERSION,
    )
    if (
        capability.provider != manifest["provider"]
        or capability.model_id != manifest["model_id"]
        or capability.model_version != manifest["model_version"]
    ):
        raise _error("PAGE07_SEND_AUTHORIZATION_PROVIDER_INCOMPATIBLE", "当前 provider 不支持 exact contradiction request。")
    _fit_or_block(
        reserved_output=plan.get("reserved_output_tokens"),
        upper=plan.get("conservative_local_request_upper_bound"),
        capability=capability,
    )
    data_scope_hash = model_gateway._stable_hash(
        {
            "provider": manifest["provider"],
            "model_id": manifest["model_id"],
            "model_version": manifest["model_version"],
            "model_call_id": model_call_id,
            "call_identity_hash": manifest["call_identity_hash"],
            "final_context_manifest_hash": manifest["final_context_manifest_hash"],
            "framed_payload_hash": manifest["framed_payload_hash"],
            "request_envelope_hash": plan["request_envelope_hash"],
            "prompt_contract_hash": plan["prompt_contract_hash"],
            "sampling_parameters_hash": plan["sampling_parameters_hash"],
            "task_type": CONTRADICTION_TASK_TYPE,
            "output_schema_version": CONTRADICTION_OUTPUT_SCHEMA_VERSION,
            "purpose_id": CONTRADICTION_PURPOSE_ID,
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "flow": "report_contradiction_check",
        "project_id": project_id,
        "report_version_id": report_version_id,
        "local_task_id": call["local_task_id"],
        "model_call_id": model_call_id,
        "call_identity_hash": call["call_identity_hash"],
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "purpose_id": CONTRADICTION_PURPOSE_ID,
        "data_scope_hash": data_scope_hash,
        "final_context_manifest_hash": manifest["final_context_manifest_hash"],
        "request_envelope_hash": plan["request_envelope_hash"],
        "authorization_state": "awaiting_human_confirmation",
        "provider_send_state": "not_attempted",
    }


def _owned_env_locked() -> bool:
    return (
        _active_scope_hash is not None
        and _active_purpose_id is not None
        and os.environ.get(_ENV_AUTHORIZED) == "true"
        and os.environ.get(_ENV_PURPOSE) == _active_purpose_id
        and os.environ.get(_ENV_SCOPE) == _active_scope_hash
    )


def _foreign_env_present_locked() -> bool:
    present = any(os.environ.get(name) is not None for name in (_ENV_AUTHORIZED, _ENV_PURPOSE, _ENV_SCOPE))
    return present and not _owned_env_locked()


def _clear_locked() -> None:
    global _active_scope_hash, _active_purpose_id, _active_deadline, _in_flight, _active_binding
    if _owned_env_locked():
        os.environ.pop(_ENV_AUTHORIZED, None)
        os.environ.pop(_ENV_PURPOSE, None)
        os.environ.pop(_ENV_SCOPE, None)
    _active_scope_hash = None
    _active_purpose_id = None
    _active_deadline = None
    _in_flight = False
    _active_binding = None


@contextmanager
def _chain_guard_locked(preview):
    """Caller holds permit lock; reserve SQLite before consuming a stale preview."""
    if preview.get("flow") != "report_reanalysis":
        yield
        return
    from app.db import get_connection
    from app.report_reanalysis_cancellation import assert_active_chain
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert_active_chain(conn, project_id=preview.get("project_id"),
            report_version_id=preview.get("report_version_id"), local_task_id=preview.get("local_task_id"),
            model_call_id=preview.get("model_call_id"), allowed_states=("queued",))
        yield


def _cancellation_permit_locked(*, project_id, report_version_id, local_task_id):
    """Inspect, never revoke, until cancellation's DB commit succeeds."""
    if _foreign_env_present_locked():
        raise _error("PAGE07_SEND_AUTHORIZATION_FOREIGN_PERMIT", "存在无法证明归属的发送授权；未撤销任何状态。")
    same = _active_binding is not None and all(_active_binding.get(k) == v for k, v in {
        "flow": "report_reanalysis", "project_id": project_id,
        "report_version_id": report_version_id, "local_task_id": local_task_id}.items())
    if _in_flight and (same or _active_binding is None):
        raise _error("PAGE07_SEND_AUTHORIZATION_BUSY", "这次重分析正在消费发送授权，不能按未发送撤销。")
    return _active_scope_hash if same else None


def revoke_page07_send_authorization() -> None:
    with _lock:
        _clear_locked()


def authorize_page07_send_scope(*, preview: Mapping[str, object], expected_data_scope_hash: str, human_confirmed: bool) -> dict[str, object]:
    global _active_scope_hash, _active_purpose_id, _active_deadline, _in_flight, _active_binding
    if human_confirmed is not True or not _is_hash(expected_data_scope_hash):
        raise _error("PAGE07_SEND_AUTHORIZATION_INVALID", "必须明确确认一个合法的 exact data scope。")
    if preview.get("data_scope_hash") != expected_data_scope_hash or preview.get("authorization_state") != "awaiting_human_confirmation":
        raise _error("PAGE07_SEND_AUTHORIZATION_INVALID", "确认的发送范围已经变化，请重新查看后再授权。")
    purpose_id = preview.get("purpose_id")
    if type(purpose_id) is not str or not purpose_id:
        raise _error("PAGE07_SEND_AUTHORIZATION_INVALID", "exact purpose identity 无效。")
    with _lock:
        if _in_flight:
            raise _error("PAGE07_SEND_AUTHORIZATION_BUSY", "已有一次 Page07 模型执行正在消费授权。")
        if _foreign_env_present_locked():
            raise _error("PAGE07_SEND_AUTHORIZATION_FOREIGN_PERMIT", "当前进程存在另一个或无法证明归属的真实发送授权；不会覆盖。")
        with _chain_guard_locked(preview):
            _clear_locked()
            os.environ[_ENV_AUTHORIZED] = "true"
            os.environ[_ENV_PURPOSE] = purpose_id
            os.environ[_ENV_SCOPE] = expected_data_scope_hash
            _active_scope_hash = expected_data_scope_hash
            _active_purpose_id = purpose_id
            _active_deadline = time.monotonic() + PERMIT_TTL_SECONDS
            _in_flight = False
            _active_binding = {k: preview.get(k) for k in ("flow", "project_id", "report_version_id", "local_task_id", "model_call_id")}
    return {
        **dict(preview),
        "authorization_state": "authorized_once",
        "permit_ttl_seconds": PERMIT_TTL_SECONDS,
        "provider_send_state": "not_attempted",
    }


def begin_page07_authorized_execution(*, preview: Mapping[str, object], expected_data_scope_hash: str) -> None:
    global _in_flight
    if preview.get("data_scope_hash") != expected_data_scope_hash or not _is_hash(expected_data_scope_hash):
        raise _error("PAGE07_SEND_AUTHORIZATION_INVALID", "执行前发送范围已变化，原授权失效。")
    purpose_id = preview.get("purpose_id")
    with _lock:
        expired = _active_deadline is None or time.monotonic() > _active_deadline
        same_scope = (
            _active_scope_hash == expected_data_scope_hash
            and _active_purpose_id == purpose_id
        )
        exact = same_scope and _owned_env_locked()
        if _in_flight:
            raise _error("PAGE07_SEND_AUTHORIZATION_BUSY", "已有一次 Page07 模型执行正在消费授权。")
        if expired:
            _clear_locked()
            raise _error("PAGE07_SEND_AUTHORIZATION_NOT_READY", "本次真实发送授权不存在、已过期、已消费或范围发生变化。")
        if not same_scope:
            raise _error("PAGE07_SEND_AUTHORIZATION_NOT_READY", "本次真实发送授权不存在、已过期、已消费或范围发生变化。")
        if not exact:
            _clear_locked()
            raise _error("PAGE07_SEND_AUTHORIZATION_NOT_READY", "本次真实发送授权不存在、已过期、已消费或范围发生变化。")
        with _chain_guard_locked(preview):
            _in_flight = True


def finish_page07_authorized_execution(*, expected_data_scope_hash: str) -> None:
    with _lock:
        if _active_scope_hash == expected_data_scope_hash:
            _clear_locked()
        # A stale finalizer must never mutate a newer permit or execution in flight.
