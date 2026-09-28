import hashlib
import json
import sqlite3

import pytest

from app.model_provider_contract import ProviderCapability, ProviderReceipt
from app.profile_reconciliation_batches import BatchError
from app.profile_reconciliation_provider_quality import (
    QUALITY_OUTPUT_SCHEMA_VERSION,
    QUALITY_TASK_TYPE,
    build_quality_execution_plan,
    dispatch_authorized_quality_batch,
    record_quality_authorization,
)
from app.profile_reconciliation_quality import build_quality_contract
from app.profile_reconciliation_quality_batches import plan_quality_reconciliation_batches
from app.profile_reconciliation_quality_candidate import aggregate_quality_candidate

HEAD = "7" * 40
PRD_HASH = "8" * 64
PLAN_CONTENT_HASH = "9" * 64
BUDGET = {
    "context_window_tokens": 200000,
    "max_output_tokens": 8000,
    "reserved_output_tokens": 8000,
    "safety_margin_tokens": 16384,
    "counting_policy_version": "utf8_byte_upper_bound_v1",
}
PLAN = {
    "schema_version": "project_profile_v2",
    "project_summary": "candidate fixture",
    "planned_modules": [{
        "client_id": "FR-03-prd-preview",
        "name": "PRD preview",
        "description": "Parse preview and confirm PRD versions",
        "prd_refs": [],
        "requirements": ["Parse preview", "Human confirm becomes current"],
        "exclusions": [],
    }],
    "implementation_mappings": [],
    "unplanned_code_features": [],
    "domain_glossary": [],
    "exclude_patterns": [],
    "notes": "",
}


def evidence(identity, path, text):
    return {
        "evidence_id": "repo-code-" + identity,
        "path": path,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "exact_head": HEAD,
    }


def batches():
    return plan_quality_reconciliation_batches(
        PLAN,
        [
            evidence("parser", "apps/backend/app/prd_parser.py", "parse preview"),
            evidence("confirm", "apps/backend/app/prd.py", "human confirm current"),
            evidence("test", "tests/backend/test_prd.py", "parse preview human confirm current"),
        ],
        exact_head=HEAD,
        plan_profile_id=11,
        budget_record=BUDGET,
    )


def result_for(batch, *, state="supported", gap=""):
    contract = build_quality_contract(batch)
    source_ids = [
        item["evidence_id"] for item in batch["repo_evidence"]
        if item.get("quality_evidence_role") == "source_code"
    ]
    return {
        "mappings": [{
            "planned_module_id": batch["planned_modules"][0]["client_id"],
            "module_summary": "Exact-HEAD source was reviewed requirement by requirement.",
            "requirement_results": [
                {
                    "requirement_id": requirement["requirement_id"],
                    "state": state,
                    "evidence_ids": [source_ids[min(index, len(source_ids) - 1)]],
                    "rationale": "Production source directly supports the bounded requirement.",
                    "gap": gap,
                }
                for index, requirement in enumerate(contract["modules"][0]["requirements"])
            ],
        }]
    }


class Adapter:
    provider_id = "deepseek"

    def __init__(self, results):
        self.results = results
        self.calls = 0

    def get_capability(self, *, task_type, output_schema_version):
        assert (task_type, output_schema_version) == (QUALITY_TASK_TYPE, QUALITY_OUTPUT_SCHEMA_VERSION)
        return ProviderCapability(
            "deepseek", "deepseek-flash", "fixture", QUALITY_TASK_TYPE,
            QUALITY_OUTPUT_SCHEMA_VERSION, 200000, 8000,
        )

    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens):
        return len(json.dumps([dict(message) for message in messages], ensure_ascii=False).encode()) + 100

    def execute_with_credential(self, request, credential):
        assert credential == "secret"
        self.calls += 1
        return ProviderReceipt(
            provider="deepseek",
            provider_response_id=f"candidate-response-{self.calls}",
            actual_model="deepseek-flash",
            provider_runtime_fingerprint="fixture-runtime",
            finish_reason="stop",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            result=self.results[request.local_task_id],
        )


def state():
    return {
        "exact_head": HEAD,
        "prd_id": 5,
        "prd_source_hash": PRD_HASH,
        "plan_profile_id": 11,
        "plan_content_hash": PLAN_CONTENT_HASH,
    }


def authorize_and_dispatch_all(conn, bs, result_state="supported", gap=""):
    # Build once with a provisional adapter, then bind each claim-local result after authorization.
    class DynamicAdapter(Adapter):
        def __init__(self):
            super().__init__({})

        def execute_with_credential(self, request, credential):
            assert credential == "secret"
            self.calls += 1
            # The test has one module per quality batch; bind by dispatched request order.
            batch = bs[self.calls - 1]
            return ProviderReceipt(
                provider="deepseek",
                provider_response_id=f"candidate-response-{self.calls}",
                actual_model="deepseek-flash",
                provider_runtime_fingerprint="fixture-runtime",
                finish_reason="stop",
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                result=result_for(batch, state=result_state, gap=gap),
            )

    adapter = DynamicAdapter()
    execution = build_quality_execution_plan(bs, adapter=adapter)
    auth = record_quality_authorization(
        conn,
        execution_plan=execution,
        project_id=2,
        prd_id=5,
        prd_source_hash=PRD_HASH,
        plan_content_hash=PLAN_CONTENT_HASH,
        authorization_nonce="quality-candidate-001",
        authorized=True,
    )
    for batch in bs:
        out = dispatch_authorized_quality_batch(
            conn,
            batch,
            authorization_hash=auth["authorization_hash"],
            adapter=adapter,
            credential_reader=lambda: "secret",
            current_state=state,
        )
        assert out["status"] == "succeeded"
    return auth, adapter


def test_full_successful_quality_set_aggregates_to_isolated_implemented_candidate():
    bs = batches()
    conn = sqlite3.connect(":memory:")
    auth, adapter = authorize_and_dispatch_all(conn, bs)
    candidate = aggregate_quality_candidate(
        conn,
        bs,
        PLAN,
        authorization_hash=auth["authorization_hash"],
        current_state=state,
    )
    assert candidate["status"] == "candidate"
    mapping = candidate["content"]["implementation_mappings"][0]
    assert mapping["status"] == "implemented"
    assert mapping["exact_head"] == HEAD
    assert any(path["type"] == "backend" for path in mapping["paths"])
    assert adapter.calls == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM profile_reconciliation_provider_quality_candidates"
    ).fetchone()[0] == 1
    # Aggregation must not create/promote the Product authority table in this isolated fixture.
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='project_profiles'"
    ).fetchone()[0] == 0


def test_candidate_replay_is_idempotent_and_append_only():
    bs = batches()
    conn = sqlite3.connect(":memory:")
    auth, _ = authorize_and_dispatch_all(conn, bs)
    first = aggregate_quality_candidate(
        conn, bs, PLAN, authorization_hash=auth["authorization_hash"], current_state=state,
    )
    second = aggregate_quality_candidate(
        conn, bs, PLAN, authorization_hash=auth["authorization_hash"], current_state=state,
    )
    assert second == first
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "UPDATE profile_reconciliation_provider_quality_candidates SET status='candidate' WHERE authorization_hash=?",
            (auth["authorization_hash"],),
        )


def test_partial_quality_stays_partial_in_candidate():
    bs = batches()
    conn = sqlite3.connect(":memory:")
    auth, _ = authorize_and_dispatch_all(
        conn, bs, result_state="partial", gap="Some required behavior remains unproven.",
    )
    candidate = aggregate_quality_candidate(
        conn, bs, PLAN, authorization_hash=auth["authorization_hash"], current_state=state,
    )
    assert candidate["content"]["implementation_mappings"][0]["status"] == "partial"


def test_full_batch_authorization_is_required_for_candidate_aggregation():
    plan = json.loads(json.dumps(PLAN))
    plan["planned_modules"].append({
        "client_id": "FR-04-report",
        "name": "Report",
        "description": "Generate final report HTML",
        "prd_refs": [],
        "requirements": ["Generate final report HTML"],
        "exclusions": [],
    })
    bs = plan_quality_reconciliation_batches(
        plan,
        [
            evidence("prd", "apps/backend/app/prd.py", "parse preview human confirm current"),
            evidence("report", "apps/backend/app/report.py", "generate final report html"),
        ],
        exact_head=HEAD,
        plan_profile_id=11,
        budget_record=BUDGET,
    )
    adapter = Adapter({})
    execution = build_quality_execution_plan(bs, adapter=adapter, selected_batch_indexes=[0])
    conn = sqlite3.connect(":memory:")
    auth = record_quality_authorization(
        conn,
        execution_plan=execution,
        project_id=2,
        prd_id=5,
        prd_source_hash=PRD_HASH,
        plan_content_hash=PLAN_CONTENT_HASH,
        authorization_nonce="quality-candidate-partial-set",
        authorized=True,
    )
    with pytest.raises(BatchError, match="FULL_QUALITY_BATCH_SET_NOT_AUTHORIZED"):
        aggregate_quality_candidate(
            conn, bs, plan, authorization_hash=auth["authorization_hash"], current_state=state,
        )
