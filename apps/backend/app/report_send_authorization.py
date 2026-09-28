"""One-shot exact-scope Human send permit for prepared daily-report execution.

This module never reads provider credentials or performs provider I/O. It derives the
same exact data-scope hash used by DeepSeek Current Authority, records a short-lived
process-local permit only after explicit Human confirmation, and clears that permit
when one execution attempt finishes.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
from collections.abc import Mapping

from fastapi import HTTPException

from app import deepseek_current_authority, model_call_ledger, report_generation_preparation
from app.context_candidate_runtime import candidate_materialization_scope


SCHEMA_VERSION = "report_send_authorization_v1"
PERMIT_TTL_SECONDS = 300
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_AUTHORIZED = "ANXIN_DEEPSEEK_SEND_AUTHORIZED"
_ENV_PURPOSE = "ANXIN_DEEPSEEK_SEND_PURPOSE_ID"
_ENV_SCOPE = "ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH"
_lock = threading.Lock()
_active_scope_hash: str | None = None
_active_deadline: float | None = None
_in_flight = False
_active_parent_call_id: int | None = None
_active_child_scope_hash: str | None = None
_active_batch_plan_hash: str | None = None


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _invalid(message: str) -> HTTPException:
    return _error(409, "REPORT_SEND_AUTHORIZATION_INVALID", message)


def _not_ready(message: str) -> HTTPException:
    return _error(409, "REPORT_SEND_AUTHORIZATION_NOT_READY", message)


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _clear_env_if_scope(expected_scope_hash: str | None) -> None:
    if expected_scope_hash is None:
        return
    if (
        os.environ.get(_ENV_AUTHORIZED) == "true"
        and os.environ.get(_ENV_PURPOSE) == deepseek_current_authority.PURPOSE_ID
        and os.environ.get(_ENV_SCOPE) == expected_scope_hash
    ):
        os.environ.pop(_ENV_AUTHORIZED, None)
        os.environ.pop(_ENV_PURPOSE, None)
        os.environ.pop(_ENV_SCOPE, None)


def _foreign_or_unowned_env_permit_present_locked() -> bool:
    authorized = os.environ.get(_ENV_AUTHORIZED)
    purpose = os.environ.get(_ENV_PURPOSE)
    scope = os.environ.get(_ENV_SCOPE)
    if authorized is None and purpose is None and scope is None:
        return False
    return not (
        _active_scope_hash is not None
        and authorized == "true"
        and purpose == deepseek_current_authority.PURPOSE_ID
        and scope == _active_scope_hash
    )


def _reset_locked() -> None:
    global _active_scope_hash, _active_deadline, _in_flight, _active_parent_call_id, _active_child_scope_hash, _active_batch_plan_hash
    _clear_env_if_scope(_active_child_scope_hash)
    _clear_env_if_scope(_active_scope_hash)
    _active_scope_hash = None
    _active_deadline = None
    _in_flight = False
    _active_parent_call_id = None
    _active_child_scope_hash = None
    _active_batch_plan_hash = None


def revoke_report_send_authorization() -> None:
    """Clear only the permit owned by this module; never touches unrelated environment."""
    with _lock:
        _reset_locked()


def build_report_send_authorization_preview(
    *,
    project_id: int,
    local_task_id: str,
    expected_model_call_id: int,
) -> dict[str, object]:
    """Re-close local preparation and derive the exact Current-Authority data scope."""
    if type(expected_model_call_id) is not int or expected_model_call_id <= 0:
        raise _invalid("model_call_id 必须是正整数。")

    prepared = report_generation_preparation.prepare_report_generation_model_call(
        project_id=project_id,
        local_task_id=local_task_id,
        preparation_authorized=True,
    )
    if (
        prepared.get("preparation_state") != "prepared"
        or prepared.get("provider_send_state") != "not_attempted"
        or prepared.get("local_request_readiness_state") != "ready_for_gateway_evaluation"
        or prepared.get("next_gate") != "exact_human_send_authorization_required"
    ):
        raise _not_ready("本轮本地发送范围尚未通过，不能创建真实 AI 发送授权。")

    model_call_id = prepared.get("model_call_id")
    if model_call_id != expected_model_call_id:
        raise _invalid("页面持有的 Model Call 与当前正式准备结果不一致。")

    call = model_call_ledger.get_model_call(model_call_id)
    expected_binding = {
        "project_id": project_id,
        "local_task_id": local_task_id,
        "model_call_id": model_call_id,
        "call_identity_hash": prepared.get("call_identity_hash"),
        "provider": deepseek_current_authority.PROVIDER,
        "model_id": deepseek_current_authority.MODEL_ID,
        "model_version": deepseek_current_authority.MODEL_VERSION,
    }
    if not isinstance(call, Mapping) or any(call.get(k) != v for k, v in expected_binding.items()):
        raise _invalid("正式 Model Call 与当前项目/任务/模型身份无法闭合。")

    manifest_hash = prepared.get("final_context_manifest_hash")
    framed_payload_hash = prepared.get("framed_payload_hash")
    if not _is_hash(manifest_hash) or not _is_hash(framed_payload_hash):
        raise _invalid("本轮发送范围 hash 无法闭合。")

    # Reuse the exact frozen payload/hash implementation owned by Current Authority;
    # do not duplicate or reinterpret the authority contract here.
    scope_payload = deepseek_current_authority._data_scope_payload(
        call=dict(call),
        final_context_manifest_hash=manifest_hash,
        framed_payload_hash=framed_payload_hash,
    )
    data_scope_hash = deepseek_current_authority._stable_hash(scope_payload)
    if prepared.get("batch_plan") is not None:
        data_scope_hash = deepseek_current_authority._stable_hash({
            "single_scope": data_scope_hash, "batch_plan": prepared["batch_plan"],
            "purpose": "daily-report-batch-plan-v1"})
    if not _is_hash(data_scope_hash):
        raise _invalid("Current Authority data scope hash 无效。")

    result = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "local_task_id": local_task_id,
        "model_call_id": model_call_id,
        "provider": call["provider"],
        "model_id": call["model_id"],
        "model_version": call["model_version"],
        "purpose_id": deepseek_current_authority.PURPOSE_ID,
        "data_scope_hash": data_scope_hash,
        "final_context_manifest_hash": manifest_hash,
        "admitted_target_count": prepared.get("admitted_target_count", 0),
        "denied_target_count": prepared.get("denied_target_count", 0),
        "authorization_state": "awaiting_human_confirmation",
        "provider_send_state": "not_attempted",
    }
    if prepared.get("batch_plan") is not None:
        result["batch_plan"] = prepared["batch_plan"]
    return result


@contextmanager
def authorized_batch_scope(parent_call_id, child_call_id, budget_record):
    """Use a child scope only inside an explicitly authorized immutable parent plan."""
    from app import report_generation_batches, final_context_manifest
    global _active_child_scope_hash
    plan = report_generation_batches.get_plan(parent_call_id)
    if plan is None or child_call_id not in report_generation_batches.get_batch_parent_call_ids(parent_call_id):
        raise _invalid("子批不属于已授权计划。")
    parent = model_call_ledger.get_model_call(parent_call_id)
    # The parent preview was closed immediately before begin; its authorization binds
    # both immutable plan and the pending states. Never create a permit here.
    with _lock:
        if (not _in_flight or _active_scope_hash is None or _active_parent_call_id != parent_call_id
                or _active_batch_plan_hash != plan["plan_hash"]):
            raise _not_ready("分批执行没有用户确认的计划授权。")
        owner_scope = _active_scope_hash
        if os.environ.get(_ENV_SCOPE) != owner_scope:
            raise _invalid("分批发送授权被其他执行修改。")
    with candidate_materialization_scope():
        manifest = final_context_manifest.build_final_context_manifest(model_call_id=child_call_id, budget_record=budget_record)
    child = model_call_ledger.get_model_call(child_call_id)
    if any(child.get(k) != parent.get(k) for k in ("project_id", "snapshot_id", "snapshot_hash", "local_task_id", "provider", "model_id", "model_version")):
        raise _invalid("子批与父任务身份不一致。")
    scope = deepseek_current_authority._stable_hash(deepseek_current_authority._data_scope_payload(
        call=child, final_context_manifest_hash=manifest["final_context_manifest_hash"], framed_payload_hash=manifest["framed_payload_hash"]))
    with _lock:
        if not _in_flight or _active_scope_hash != owner_scope or os.environ.get(_ENV_SCOPE) != owner_scope:
            raise _not_ready("分批授权已撤销。")
        os.environ[_ENV_SCOPE] = scope
        _active_child_scope_hash = scope
    try:
        yield
    finally:
        with _lock:
            if _active_scope_hash == owner_scope and _in_flight and os.environ.get(_ENV_SCOPE) == scope:
                os.environ[_ENV_SCOPE] = owner_scope
            else:
                _clear_env_if_scope(scope)
            if _active_child_scope_hash == scope:
                _active_child_scope_hash = None


def authorize_report_send_scope(
    *,
    project_id: int,
    local_task_id: str,
    model_call_id: int,
    expected_data_scope_hash: str,
    human_confirmed: bool,
) -> dict[str, object]:
    """Record one short-lived process-local exact-scope permit after explicit confirmation."""
    global _active_scope_hash, _active_deadline, _in_flight, _active_parent_call_id, _active_batch_plan_hash
    if human_confirmed is not True:
        raise _invalid("必须由用户明确确认本次真实 AI 发送范围。")
    if not _is_hash(expected_data_scope_hash):
        raise _invalid("data_scope_hash 必须是 lowercase SHA-256。")

    preview = build_report_send_authorization_preview(
        project_id=project_id,
        local_task_id=local_task_id,
        expected_model_call_id=model_call_id,
    )
    if preview["data_scope_hash"] != expected_data_scope_hash:
        raise _invalid("确认的发送范围已经变化，请重新查看本次发送范围后再授权。")

    with _lock:
        if _in_flight:
            raise _invalid("已有一次真实 AI 执行正在占用发送授权，不能覆盖。")
        if _foreign_or_unowned_env_permit_present_locked():
            raise _invalid("当前进程已有另一个或无法证明归属的 exact send authorization；本页面不会覆盖它。")
        _reset_locked()
        os.environ[_ENV_AUTHORIZED] = "true"
        os.environ[_ENV_PURPOSE] = deepseek_current_authority.PURPOSE_ID
        os.environ[_ENV_SCOPE] = expected_data_scope_hash
        _active_scope_hash = expected_data_scope_hash
        _active_parent_call_id = model_call_id
        _active_batch_plan_hash = preview.get("batch_plan", {}).get("plan_hash")
        _active_deadline = time.monotonic() + PERMIT_TTL_SECONDS
        _in_flight = False

    return {
        **preview,
        "authorization_state": "authorized_once",
        "permit_ttl_seconds": PERMIT_TTL_SECONDS,
        "provider_send_state": "not_attempted",
    }


def begin_authorized_report_execution(
    *,
    project_id: int,
    local_task_id: str,
    model_call_id: int,
    expected_data_scope_hash: str,
) -> dict[str, object]:
    """Claim the active permit exactly once before entering the existing execution coordinator."""
    global _in_flight
    preview = build_report_send_authorization_preview(
        project_id=project_id,
        local_task_id=local_task_id,
        expected_model_call_id=model_call_id,
    )
    if preview["data_scope_hash"] != expected_data_scope_hash:
        raise _invalid("执行前发送范围已变化，原授权失效。")

    with _lock:
        if _in_flight:
            raise _not_ready("本次真实 AI 发送授权已被一个执行请求消费；不会并发重复发送。")
        expired = _active_deadline is None or time.monotonic() > _active_deadline
        env_matches = (
            os.environ.get(_ENV_AUTHORIZED) == "true"
            and os.environ.get(_ENV_PURPOSE) == deepseek_current_authority.PURPOSE_ID
            and os.environ.get(_ENV_SCOPE) == expected_data_scope_hash
        )
        if expired or _active_scope_hash != expected_data_scope_hash or not env_matches:
            _reset_locked()
            raise _not_ready("本次真实 AI 发送授权不存在、已过期或已被消费，请重新确认发送范围。")
        _in_flight = True

    return preview


def finish_authorized_report_execution(expected_data_scope_hash: str) -> None:
    """Always revoke the exact permit after the single execution attempt returns or raises."""
    global _in_flight
    with _lock:
        if _active_scope_hash == expected_data_scope_hash:
            _reset_locked()
        else:
            _in_flight = False
