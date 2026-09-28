import copy
import hashlib
import json
import sqlite3

import pytest

from app.profile_reconciliation_batches import (
    BatchError, BatchFailure, aggregate_batches, plan_reconciliation_batches, run_batch,
)

HEAD = "a" * 40
BUDGET = dict(context_window_tokens=12000, max_output_tokens=2000,
              reserved_output_tokens=2000, safety_margin_tokens=1000,
              counting_policy_version="utf8_byte_upper_bound_v1")
PLAN = {"schema_version": "project_profile_v2", "planned_modules": [
    {"client_id": "one", "name": "计划一", "requirements": ["safe 计划需求"], "prd_refs": ["prd-a"]},
    {"client_id": "two", "name": "计划二", "requirements": ["safe 另一个需求"], "prd_refs": ["prd-a"]},
]}


def evidence(content="safe source", identity="a"):
    return dict(evidence_id="repo-code-" + identity, path=identity + ".py", content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(), exact_head=HEAD)


def planned(items=None, plan=None, budget=None):
    return plan_reconciliation_batches(plan or PLAN, items if items is not None else [evidence()],
        exact_head=HEAD, plan_profile_id=42, budget_record=budget or BUDGET)


def success(batch):
    refs = [item["evidence_id"] for item in batch["repo_evidence"]]
    return [dict(planned_module_id=m["client_id"], status="implemented" if refs else "unknown",
                 evidence_ids=refs[:1] if refs else [], rationale="safe semantic evidence" if refs else "no supporting evidence in this batch") for m in batch["planned_modules"]]


def unknown(batch):
    return [dict(planned_module_id=m["client_id"], status="unknown", evidence_ids=[], rationale="no supporting evidence in this batch")
            for m in batch["planned_modules"]]


def run(conn, batch, dispatch=success, **kwargs):
    return run_batch(conn, batch, fake_dispatch=dispatch, current_head=lambda: HEAD, **kwargs)


def test_full_code_planner_never_drops_unrelated_safe_evidence_before_model():
    plan = {"schema_version": "project_profile_v2", "planned_modules": [
        {"client_id": "billing", "name": "账单管理", "requirements": ["生成账单"]},
        {"client_id": "login", "name": "用户登录", "requirements": ["账户认证"]},
        {"client_id": "camera", "name": "视频设备控制", "requirements": ["设备控制"]},
    ]}
    items = [evidence("def create_invoice(): pass", "billing"),
             evidence("def authenticate_account(): pass", "login"),
             evidence("def resize_bitmap(): pass", "image")]
    batches = planned(items, plan=plan)
    coverage = batches[0]["coverage"]
    assert coverage["retrieval_policy"] == "full_safe_evidence_coverage_v1"
    assert coverage["coverage_state"] == "full_safe_evidence_covered"
    assert coverage["repository_evidence_count"] == 3
    assert coverage["assigned_evidence_count"] == 3
    assert coverage["unassigned_evidence_count"] == 0
    visible = {item["evidence_id"] for batch in batches for item in batch["repo_evidence"]}
    assert visible == {"repo-code-billing", "repo-code-login", "repo-code-image"}
    # Chinese plan text does not need to lexically overlap English symbols for code to be sent.
    assert all({m["client_id"] for m in batch["planned_modules"]} == {"billing", "login", "camera"} for batch in batches)


def test_module_focus_caps_catalog_per_batch_without_dropping_code():
    plan = {"schema_version": "project_profile_v2", "planned_modules": [
        {"client_id": f"module-{i}", "name": f"模块{i}", "requirements": [f"requirement {i}"]}
        for i in range(10)
    ]}
    batches = planned([evidence("def generic_capability(): pass", "generic")], plan=plan, budget={**BUDGET, "context_window_tokens": 100000})
    assert len(batches) >= 4
    assert all(1 <= len({m["client_id"] for m in batch["planned_modules"]}) <= 3 for batch in batches)
    assert all(batch["repo_evidence"] for batch in batches)


def test_dynamic_split_preserves_all_source_text_and_complete_module_coverage():
    source = "safe 一行安全代码\n" * 2000
    batches = planned([evidence(source)])
    assert len(batches) > 2
    fragments = [item for batch in batches for item in batch["repo_evidence"]]
    assert "".join(item["content"] for item in fragments) == source
    assert all(item["parent_evidence_id"] == "repo-code-a" for item in fragments)
    assert all(item["line_end"] >= item["line_start"] for item in fragments)
    assert planned([evidence(source)]) == batches
    conn = sqlite3.connect(":memory:")
    for batch in batches:
        assert run(conn, batch)["status"] == "succeeded"
    result = aggregate_batches(conn, batches, current_head=lambda: HEAD)
    assert {m["planned_module_id"] for m in result} == {"one", "two"}
    assert all(m["status"] == "partial" for m in result)
    with pytest.raises(BatchError, match="INCOMPLETE_BATCH_SET"):
        aggregate_batches(conn, batches[:-1], current_head=lambda: HEAD)


def test_positive_evidence_survives_unknown_from_other_code_fragments():
    batches = planned([evidence("safe line\n" * 5000)])
    assert len(batches) > 1
    conn = sqlite3.connect(":memory:")
    for index, batch in enumerate(batches):
        result = success(batch)
        if index == 0:
            result[0] = {"planned_module_id": "one", "status": "unknown", "evidence_ids": [], "rationale": "no evidence here"}
        run(conn, batch, lambda _batch, value=result: value)
    aggregated = aggregate_batches(conn, batches, current_head=lambda: HEAD)
    by_id = {item["planned_module_id"]: item for item in aggregated}
    assert by_id["one"]["status"] == "partial"
    assert by_id["one"]["evidence_ids"]


def test_no_repository_evidence_can_only_yield_unknown():
    conn = sqlite3.connect(":memory:")
    batches = planned([])
    for batch in batches:
        assert run(conn, batch, unknown)["status"] == "succeeded"
    assert all(item["status"] == "unknown" for item in aggregate_batches(conn, batches, current_head=lambda: HEAD))


def test_single_oversized_plan_module_is_fragmented_without_dropping_identity():
    plan = copy.deepcopy(PLAN)
    plan["planned_modules"][0]["requirements"] = ["复杂需求" * 4000]
    batches = planned(plan=plan)
    pieces = [module for batch in batches for module in batch["planned_modules"] if module["client_id"] == "one"]
    assert len(pieces) > 1
    assert all(piece["plan_module_hash"] == pieces[0]["plan_module_hash"] for piece in pieces)


@pytest.mark.parametrize("status", ["failed_pre_send", "failed_after_send"])
def test_explicit_failures_require_explicit_resume_and_keep_completed_batches(status):
    batches = planned([evidence("safe line\n" * 5000)])
    assert len(batches) > 1
    conn = sqlite3.connect(":memory:")
    run(conn, batches[0])
    def fail(_):
        raise BatchFailure(status)
    assert run(conn, batches[1], fail)["status"] == status
    assert run(conn, batches[1])["status"] == status
    assert run(conn, batches[1], retry_failed=True)["status"] == "succeeded"
    assert run(conn, batches[0], lambda _: pytest.fail("must not replay success"))["status"] == "succeeded"
    assert sorted(r[0] for r in conn.execute("SELECT attempts FROM profile_reconciliation_batches")) == [1, 2]


def test_timeout_unknown_is_never_retried_and_raw_error_is_not_persisted():
    conn = sqlite3.connect(":memory:")
    batch = planned()[0]
    def timeout(_):
        raise TimeoutError("PRIVATE FAKE ERROR BODY")
    assert run(conn, batch, timeout)["status"] == "unknown"
    assert run(conn, batch, lambda _: pytest.fail("blind retry"), retry_failed=True)["status"] == "unknown"
    assert "PRIVATE" not in str(conn.execute("SELECT * FROM profile_reconciliation_batches").fetchall())


def test_interrupted_claim_blocks_resume():
    conn = sqlite3.connect(":memory:")
    batch = planned()[0]
    def interrupted(_):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(conn, batch, interrupted)
    assert run(conn, batch, lambda _: pytest.fail("blind retry"), retry_failed=True)["status"] == "unknown"


@pytest.mark.parametrize("result", [
    [{"planned_module_id": "one", "status": "not_started", "evidence_ids": []}],
    [{"planned_module_id": "one", "status": "implemented", "evidence_ids": []}],
    [{"planned_module_id": "one", "status": "partial", "evidence_ids": ["repo-code-outside"]}],
    [{"planned_module_id": "missing", "status": "unknown", "evidence_ids": []}],
])
def test_adversarial_results_are_known_failed_after_send(result):
    assert run(sqlite3.connect(":memory:"), planned()[0], lambda _: result)["status"] == "failed_after_send"


def test_head_drift_before_dispatch_after_dispatch_and_aggregate():
    conn = sqlite3.connect(":memory:")
    batches = planned()
    with pytest.raises(BatchError, match="HEAD_CHANGED"):
        run_batch(conn, batches[0], fake_dispatch=lambda _: pytest.fail("dispatch"), current_head=lambda: "b" * 40)
    heads = iter([HEAD, HEAD, "b" * 40])
    assert run_batch(conn, batches[0], fake_dispatch=success, current_head=lambda: next(heads))["status"] == "unknown"
    with pytest.raises(BatchError, match="HEAD_CHANGED"):
        aggregate_batches(conn, batches, current_head=lambda: "b" * 40)


def test_batch_identity_tamper_and_input_hash_drift_fail_closed():
    batch = planned()[0]
    batch["repo_evidence"][0]["content"] = "tampered"
    with pytest.raises(BatchError, match="BATCH_IDENTITY_MISMATCH"):
        run(sqlite3.connect(":memory:"), batch)
    changed = evidence(); changed["content_hash"] = "b" * 64
    with pytest.raises(BatchError, match="EVIDENCE_HASH_MISMATCH"):
        planned([changed])


def test_final_request_budget_counts_instructions_schema_and_large_plan_catalog():
    plan = {"schema_version": "project_profile_v2", "planned_modules": [
        {"client_id": f"module-{i:02d}-" + "x" * 45, "name": "safe", "requirements": ["safe function"]}
        for i in range(38)
    ]}
    batches = planned([evidence("safe value\n" * 1800)], plan=plan)
    assert len(batches) > 1
    for batch in batches:
        assert batch["system_instruction"]
        assert batch["response_schema"]["items"]["additionalProperties"] is False
        assert batch["structure_summary"]["file_count"] == 1
        actual = len(json.dumps(batch, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        assert actual <= BUDGET["context_window_tokens"] - BUDGET["reserved_output_tokens"] - BUDGET["safety_margin_tokens"]


def test_even_unanimous_implemented_fragments_remain_partial_until_requirement_coverage_exists():
    batches = planned([evidence("safe relevant code\n" * 1000)])
    conn = sqlite3.connect(':memory:')
    for batch in batches:
        run(conn, batch)
    result = aggregate_batches(conn, batches, current_head=lambda: HEAD)
    assert all(item['status'] == 'partial' for item in result)


def test_optional_framed_byte_limit_is_conservative_and_never_exceeds_model_budget():
    normal=planned([evidence("safe line\n" * 1200)])
    capped=plan_reconciliation_batches(PLAN,[evidence("safe line\n" * 1200)],exact_head=HEAD,plan_profile_id=42,budget_record=BUDGET,framed_byte_limit=5000)
    assert len(capped)>=len(normal)
    assert all(len(json.dumps(batch,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode())<=5000 for batch in capped)
    with pytest.raises(BatchError,match="INVALID_FRAMED_BYTE_LIMIT"):
        plan_reconciliation_batches(PLAN,[evidence()],exact_head=HEAD,plan_profile_id=42,budget_record=BUDGET,framed_byte_limit=10000)
