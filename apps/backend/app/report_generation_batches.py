"""Local, immutable report batch plans and durable per-call send claims.

Only redacted ranges and hashes are stored here. Provider bodies stay transient.
Each child still passes the normal gateway and result ledger.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from fastapi import HTTPException
from app.db import get_connection
from app.context_candidate_runtime import candidate_materialization_scope


def _error(message):
    return HTTPException(409, detail={"code": "REPORT_BATCH_STATE_INVALID", "message": message})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_batch_plans'").fetchone() is not None


def _init():
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS report_batch_plans (
          parent_call_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
          local_task_id TEXT NOT NULL, plan_hash TEXT NOT NULL UNIQUE,
          plan_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS report_generation_batches (
          parent_call_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
          model_call_id INTEGER NOT NULL UNIQUE, state TEXT NOT NULL
            CHECK(state IN ('pending','claimed','succeeded','unknown')),
          result_id INTEGER, PRIMARY KEY(parent_call_id,ordinal));
        """)


def split_text_ranges(text, *, max_bytes):
    if type(max_bytes) is not int or max_bytes < 1:
        raise _error("单批预算不足。")
    ranges = []
    start = 0
    while start < len(text):
        lo, hi = start + 1, len(text)
        end = start
        while lo <= hi:
            mid = (lo + hi) // 2
            if len(text[start:mid].encode("utf-8")) <= max_bytes:
                end, lo = mid, mid + 1
            else:
                hi = mid - 1
        if end == start:
            raise _error("单批预算不足以容纳一个字符；未截断内容。")
        ranges.append((start, end))
        start = end
    return ranges


def get_plan(parent_call_id):
    with get_connection() as conn:
        if not _exists(conn):
            return None
        row = conn.execute("SELECT * FROM report_batch_plans WHERE parent_call_id=?", (parent_call_id,)).fetchone()
        if row is None:
            return None
        plan = json.loads(row["plan_json"])
        if _hash(plan) != row["plan_hash"] or plan["parent_call_id"] != parent_call_id:
            raise _error("持久化分批计划校验失败。")
        rows = conn.execute("SELECT * FROM report_generation_batches WHERE parent_call_id=? ORDER BY ordinal", (parent_call_id,)).fetchall()
        if len(rows) != len(plan["batches"]):
            raise _error("持久化批次数不完整。")
        for expected, actual in zip(plan["batches"], rows):
            if expected["ordinal"] != actual["ordinal"] or expected["model_call_id"] != actual["model_call_id"]:
                raise _error("持久化批次身份漂移。")
        return {**plan, "plan_hash": row["plan_hash"], "states": [dict(r) for r in rows]}


def get_batch_call_selection(model_call_id):
    with get_connection() as conn:
        if not _exists(conn):
            return None
        row = conn.execute("SELECT parent_call_id,ordinal FROM report_generation_batches WHERE model_call_id=?", (model_call_id,)).fetchone()
    if row is None:
        return None
    plan = get_plan(row["parent_call_id"])
    selected = plan["batches"][row["ordinal"] - 1]
    from app.model_call_ledger import get_model_call
    call = get_model_call(model_call_id)
    if call["call_identity_hash"] != selected["call_identity_hash"]:
        raise _error("子调用身份与分批计划不一致。")
    return {"chunks": selected["chunks"], "evidence_ids": selected["evidence_ids"], "parent_context_sufficient": True}


def get_batch_parent_call_ids(parent_call_id):
    plan = get_plan(parent_call_id)
    if plan is None:
        raise _error("找不到父调用的分批计划。")
    return [b["model_call_id"] for b in plan["batches"]]


def summary(plan):
    states = {s["ordinal"]: s for s in plan["states"]}
    for state in states.values():
        if state["state"] == "claimed" and _existing_child_result(state["model_call_id"]) is not None:
            state = dict(state)
            state["state"] = "succeeded"
            states[state["ordinal"]] = state
    items = [{"ordinal": b["ordinal"], "model_call_id": b["model_call_id"],
              "state": states[b["ordinal"]]["state"], "estimated_input_tokens": b["estimated_input_tokens"]}
             for b in plan["batches"]]
    blocked = any(b["state"] in {"claimed", "unknown"} for b in items)
    completed = sum(b["state"] == "succeeded" for b in items)
    return {"plan_hash": plan["plan_hash"], "batch_count": len(items),
            "completed_batch_count": completed,
            "pending_batch_count": sum(b["state"] == "pending" for b in items),
            "state": "unknown" if blocked else "completed" if completed == len(items) else "pending",
            "can_resume": not blocked, "batches": items}


def _existing_child_result(call_id):
    from app.model_execution_results import get_model_execution_result_for_call
    try:
        return get_model_execution_result_for_call(call_id)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "MODEL_EXECUTION_RESULT_NOT_FOUND":
            return None
        raise


def _recover_completed_claims(plan):
    # A durable verified receipt is sufficient to finish the local bookkeeping.
    # Missing receipts never put a claimed call back into the pending queue.
    for state in plan["states"]:
        if state["state"] == "claimed":
            result = _existing_child_result(state["model_call_id"])
            if result is not None:
                _set_state(plan["parent_call_id"], state["ordinal"], "claimed", "succeeded", result["model_result_id"])
    return get_plan(plan["parent_call_id"])


def require_unsent_task(task, *, excluded_call_ids=()):
    """Fail closed if any durable receipt, batch claim or process claim exists."""
    from app import model_execution
    with get_connection() as conn:
        calls = conn.execute("SELECT id FROM model_calls WHERE project_id=? AND local_task_id=?",
                             (task["project_id"], task["local_task_id"])).fetchall()
        ids = {r["id"] for r in calls} - set(excluded_call_ids)
        with model_execution._SEND_CLAIM_LOCK:
            if ids & model_execution._SEND_CLAIMED_MODEL_CALL_IDS:
                raise _error("旧模型已有发送 claim，不能重新准备为新模型。")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for call_id in ids:
            if "model_execution_results" in tables and conn.execute(
                "SELECT 1 FROM model_execution_results WHERE model_call_id=?", (call_id,)).fetchone():
                raise _error("旧模型已有执行结果，不能重新准备为新模型。")
            if "report_generation_batches" in tables and conn.execute(
                "SELECT 1 FROM report_generation_batches WHERE model_call_id=? AND (state!='pending' OR result_id IS NOT NULL)",
                (call_id,)).fetchone():
                raise _error("旧批次已有发送状态，不能重新准备为新模型。")


def task_summary(task):
    with get_connection() as conn:
        if not _exists(conn):
            return None
        rows = conn.execute("SELECT parent_call_id FROM report_batch_plans WHERE project_id=? AND local_task_id=?",
                            (task["project_id"], task["local_task_id"])).fetchall()
    if not rows:
        return None
    from app.report_generation_preparation import _resolve_capability
    from app.model_call_ledger import get_model_call
    capability = _resolve_capability()
    if len(rows) == 1:
        parent = get_model_call(rows[0]["parent_call_id"])
        if tuple(parent[f] for f in ("provider", "model_id", "model_version")) != (capability.provider, capability.model_id, capability.model_version) and task["state"] == "queued":
            require_unsent_task(task)
            return None
    if len(rows) != 1:
        identity = (capability.provider, capability.model_id, capability.model_version)
        current = [r for r in rows if tuple(get_model_call(r["parent_call_id"])[f]
                   for f in ("provider", "model_id", "model_version")) == identity]
        if not current and task["state"] == "queued":
            require_unsent_task(task)
            return None
        if len(current) != 1:
            raise _error("任务存在多个分批计划，无法唯一确定当前模型。")
        selected = get_plan(current[0]["parent_call_id"])
        selected_ids = [selected["parent_call_id"]] + [b["model_call_id"] for b in selected["batches"]]
        require_unsent_task(task, excluded_call_ids=selected_ids)
        rows = current
    result = summary(get_plan(rows[0]["parent_call_id"]))
    result["can_resume"] = result["can_resume"] and task["state"] in {"queued", "running"}
    return result


def _prepare_child(parent, key):
    from app import model_call_ledger
    return model_call_ledger.prepare_model_call(
        local_task_id=parent["local_task_id"], call_prepare_key=key,
        snapshot_id=parent["snapshot_id"], task_type=parent["task_type"],
        provider=parent["provider"], model_id=parent["model_id"], model_version=parent["model_version"],
        rule_version=parent["rule_version"], output_schema_version=parent["output_schema_version"],
        benchmark_sample_pack_version=parent["benchmark_sample_pack_version"],
        qualification_record=model_call_ledger._qualification_payload(parent),
        data_sending_authorization=model_call_ledger._authorization_payload(parent))


def request_budget_exceeded(manifest, budget_record):
    from app import context_token_framing, model_provider_gateway, model_provider_runtime
    if manifest["local_request_readiness_state"] == "blocked_context_denied":
        return False
    if manifest["local_request_readiness_state"] == "blocked_context_budget":
        return True
    material = context_token_framing._materialize_context_payload_transient(
        model_call_id=manifest["model_call_id"], budget_record=budget_record)
    request = model_provider_gateway._build_gateway_request_plan(
        manifest=manifest, payload=material["payload"], budget_record=budget_record,
        adapter=model_provider_runtime.resolve_model_provider_adapter(manifest["provider"]))
    return request["conservative_local_request_upper_bound"] > budget_record["context_window_tokens"]


def prepare_plan(*, parent, manifest, budget_record):
    """Compute every safe range locally; no credentials, authority or provider calls."""
    from app import context_redaction_runtime, context_token_framing, model_provider_gateway, model_provider_runtime
    existing = get_plan(parent["model_call_id"])
    if existing is not None:
        if existing["parent_manifest_hash"] != manifest["final_context_manifest_hash"] or existing["budget_hash"] != _hash(budget_record):
            raise _error("冻结范围或模型预算已经变化，原分批计划不可复用。")
        validate_plan(existing, budget_record)
        return _recover_completed_claims(existing)
    if manifest["local_request_readiness_state"] == "blocked_context_denied":
        raise _error("剩余证据不足，不能以分批绕过上下文准入。")
    adapter = model_provider_runtime.resolve_model_provider_adapter(parent["provider"])
    ceiling = budget_record["context_window_tokens"]
    selected = []
    current = []
    current_frames = []

    def estimate(frames):
        payload = b"\n".join(_json(f).encode("utf-8") for f in frames)
        value = model_provider_gateway._build_gateway_request_plan(
            manifest=manifest, payload=payload, budget_record=budget_record, adapter=adapter)
        # Child ids / partial-task instructions have bounded additional envelope space.
        return value["conservative_local_request_upper_bound"] + 2048

    for target in manifest["admitted_targets"]:
        redaction = context_redaction_runtime.build_context_redaction_result(
            model_call_id=parent["model_call_id"], budget_record=budget_record, target=target["target"])
        body = _json(redaction["redacted_body"])
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        start = 0
        while start < len(body):
            def frame(end):
                return {"frame_schema_version": context_token_framing.FRAME_SCHEMA_VERSION, "ordinal": len(current) + 1,
                        "target": target["target"], "target_type": target["target_type"],
                        "redaction_result_hash": redaction["redaction_result_hash"],
                        "body": {"encoding": "canonical_json_fragment_v1", "start": start, "end": end,
                                 "source_body_hash": body_hash, "text": body[start:end]}}
            lo, hi, best = start + 1, len(body), start
            while lo <= hi:
                mid = (lo + hi) // 2
                if estimate(current_frames + [frame(mid)]) <= ceiling:
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            if best == start:
                if current:
                    selected.append((current, estimate(current_frames)))
                    current, current_frames = [], []
                    continue
                raise _error("模型预算无法容纳最小安全证据片段；未截断内容。")
            current_frames.append(frame(best))
            current.append({"target": target["target"], "start": start, "end": best, "body_hash": body_hash})
            start = best
            if start < len(body):
                selected.append((current, estimate(current_frames)))
                current, current_frames = [], []
    if current:
        selected.append((current, estimate(current_frames)))
    if not selected:
        raise _error("没有可安全分批的证据。")
    identity = {"parent_call_id": parent["model_call_id"], "parent_call_hash": parent["call_identity_hash"],
                "parent_manifest_hash": manifest["final_context_manifest_hash"], "budget_hash": _hash(budget_record),
                "denied_target_count": manifest["denied_target_count"],
                "unsupported_context_sources": manifest["unsupported_context_sources"]}
    batches = []
    for ordinal, (chunks, estimate_total) in enumerate(selected, 1):
        key = "report-batch:" + _hash({**identity, "ordinal": ordinal, "chunks": chunks})
        child = _prepare_child(parent, key)
        batches.append({"ordinal": ordinal, "model_call_id": child["model_call_id"],
                        "call_identity_hash": child["call_identity_hash"], "chunks": chunks,
                        "evidence_ids": sorted({c["target"] for c in chunks if c["target"] != "profile"}),
                        "estimated_input_tokens": estimate_total - budget_record["reserved_output_tokens"] - budget_record["safety_margin_tokens"]})
    plan = {**identity, "batches": batches}
    _init()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT plan_hash FROM report_batch_plans WHERE parent_call_id=?", (parent["model_call_id"],)).fetchone()
        if row is None:
            conn.execute("INSERT INTO report_batch_plans VALUES (?,?,?,?,?)",
                         (parent["model_call_id"], parent["project_id"], parent["local_task_id"], _hash(plan), _json(plan)))
            conn.executemany("INSERT INTO report_generation_batches VALUES (?,?,?,'pending',NULL)",
                             [(parent["model_call_id"], b["ordinal"], b["model_call_id"]) for b in batches])
        elif row["plan_hash"] != _hash(plan):
            raise _error("并发准备产生不同分批计划。")
    persisted = get_plan(parent["model_call_id"])
    validate_plan(persisted, budget_record)
    return persisted


@candidate_materialization_scope()
def validate_plan(plan, budget_record):
    from app import final_context_manifest, context_token_framing, model_provider_gateway, model_provider_runtime
    from app.model_call_ledger import get_model_call
    for batch in plan["batches"]:
        call = get_model_call(batch["model_call_id"])
        manifest = final_context_manifest.build_final_context_manifest(model_call_id=call["model_call_id"], budget_record=budget_record)
        payload = context_token_framing._materialize_context_payload_transient(model_call_id=call["model_call_id"], budget_record=budget_record)
        request = model_provider_gateway._build_gateway_request_plan(manifest=manifest, payload=payload["payload"],
                    budget_record=budget_record, adapter=model_provider_runtime.resolve_model_provider_adapter(call["provider"]))
        if manifest["local_request_readiness_state"] != "ready_for_gateway_evaluation" or request["conservative_local_request_upper_bound"] > budget_record["context_window_tokens"]:
            raise _error("分批后的实际请求仍超预算或准入失败；未发送。")


def _set_state(parent_id, ordinal, expected, state, result_id=None):
    with get_connection() as conn:
        changed = conn.execute("UPDATE report_generation_batches SET state=?,result_id=? WHERE parent_call_id=? AND ordinal=? AND state=?",
                               (state, result_id, parent_id, ordinal, expected)).rowcount
        if changed != 1:
            raise _error("批次已经由另一个执行占用；不会重复发送。")


def execute_plan(*, task, parent, budget_record):
    from app import model_execution, model_execution_results, report_send_authorization, report_generation_execution
    from app.report_generation_tasks import transition_report_generation_task
    plan = get_plan(parent["model_call_id"])
    if plan is not None:
        plan = _recover_completed_claims(plan)
    if plan is None or not summary(plan)["can_resume"]:
        raise _error("批次存在未确定的发送结果，禁止自动重试。")
    validate_plan(plan, budget_record)
    if task["state"] == "queued":
        task = transition_report_generation_task(project_id=task["project_id"], local_task_id=task["local_task_id"], expected_state="queued", new_state="running")
    elif task["state"] != "running":
        raise _error("当前任务不能继续分批。")
    for batch, state in zip(plan["batches"], plan["states"]):
        if state["state"] == "succeeded":
            continue
        with report_send_authorization.authorized_batch_scope(parent["model_call_id"], batch["model_call_id"], budget_record):
            model_execution.build_ready_model_execution_preflight(model_call_id=batch["model_call_id"], budget_record=budget_record)
            _set_state(parent["model_call_id"], batch["ordinal"], "pending", "claimed")
            try:
                result = model_execution.execute_model_call(model_call_id=batch["model_call_id"], budget_record=budget_record)
            except Exception:
                try:
                    result = model_execution_results.get_model_execution_result_for_call(batch["model_call_id"])
                except Exception:
                    _set_state(parent["model_call_id"], batch["ordinal"], "claimed", "unknown")
                    transition_report_generation_task(project_id=task["project_id"], local_task_id=task["local_task_id"], expected_state="running", new_state="unknown")
                    raise
            _set_state(parent["model_call_id"], batch["ordinal"], "claimed", "succeeded", result["model_result_id"])
    plan = get_plan(parent["model_call_id"])
    result = model_execution_results.record_aggregate_model_execution_result(
        parent_call_id=parent["model_call_id"], child_result_ids=[s["result_id"] for s in plan["states"]])
    return report_generation_execution._finish_from_result(task=task, call=parent, result=result, execution_source="deterministic_batch_aggregation")
