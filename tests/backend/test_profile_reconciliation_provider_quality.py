import hashlib
import json
import sqlite3

import pytest

from app.model_provider_contract import ProviderCapability, ProviderReceipt
from app.profile_reconciliation_batches import BatchError
from app.profile_reconciliation_provider import OUTPUT_SCHEMA_VERSION, TASK_TYPE
from app.profile_reconciliation_provider_quality import (
    QUALITY_OUTPUT_SCHEMA_VERSION,
    QUALITY_TASK_TYPE,
    build_quality_execution_plan,
    dispatch_authorized_quality_batch,
    public_quality_preflight_summary,
    record_quality_authorization,
)
from app.profile_reconciliation_quality import build_quality_contract
from app.profile_reconciliation_quality_batches import plan_quality_reconciliation_batches
from app.profile_response_errors import PROFILE_RESPONSE_ERROR_CODES

HEAD = "c" * 40
PRD_HASH = "d" * 64
PLAN_HASH = "e" * 64
BUDGET = {
    "context_window_tokens": 200000,
    "max_output_tokens": 8000,
    "reserved_output_tokens": 8000,
    "safety_margin_tokens": 16384,
    "counting_policy_version": "utf8_byte_upper_bound_v1",
}
PLAN = {
    "schema_version": "project_profile_v2",
    "planned_modules": [{
        "client_id": "FR-03-prd-preview",
        "name": "PRD preview",
        "description": "Parse preview and confirm PRD versions",
        "requirements": ["Parse preview", "Human confirm becomes current"],
    }],
}


def evidence(identity, path, text):
    return {
        "evidence_id": "repo-code-" + identity,
        "path": path,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "exact_head": HEAD,
    }


def quality_batch(items=None):
    return plan_quality_reconciliation_batches(
        PLAN,
        items or [
            evidence("source-a", "apps/backend/app/prd_parser.py", "parse preview"),
            evidence("source-b", "apps/backend/app/prd.py", "human confirm current"),
            evidence("test", "tests/backend/test_prd.py", "parse preview human confirm current"),
        ],
        exact_head=HEAD,
        plan_profile_id=1,
        budget_record=BUDGET,
    )[0]


def good_quality_result(batch):
    contract = build_quality_contract(batch)
    source_ids = [
        item["evidence_id"]
        for item in batch["repo_evidence"]
        if item.get("quality_evidence_role") == "source_code"
    ]
    assert source_ids
    return {
        "mappings": [{
            "planned_module_id": "FR-03-prd-preview",
            "module_summary": "Exact-HEAD source supports both requirements.",
            "requirement_results": [
                {
                    "requirement_id": requirement["requirement_id"],
                    "state": "supported",
                    "evidence_ids": [source_ids[min(index, len(source_ids) - 1)]],
                    "rationale": "The cited production source directly supports this requirement.",
                    "gap": "",
                }
                for index, requirement in enumerate(contract["modules"][0]["requirements"])
            ],
        }]
    }


class FakeQualityAdapter:
    def __init__(self, result):
        self.result = result
        self.execute_calls = 0

    @property
    def provider_id(self):
        return "deepseek"

    def get_capability(self, *, task_type, output_schema_version):
        if (task_type, output_schema_version) != (QUALITY_TASK_TYPE, QUALITY_OUTPUT_SCHEMA_VERSION):
            raise AssertionError("unexpected quality identity")
        return ProviderCapability(
            "deepseek",
            "deepseek-flash",
            "fixture-v1",
            QUALITY_TASK_TYPE,
            QUALITY_OUTPUT_SCHEMA_VERSION,
            200000,
            8000,
        )

    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens):
        assert max_output_tokens == 8000
        return len(json.dumps([dict(message) for message in messages], ensure_ascii=False).encode()) + 100

    def execute_with_credential(self, request, credential):
        self.execute_calls += 1
        assert credential == "secret"
        assert request.task_type == QUALITY_TASK_TYPE
        assert request.output_schema_version == QUALITY_OUTPUT_SCHEMA_VERSION
        return ProviderReceipt(
            provider="deepseek",
            provider_response_id="quality-response-1",
            actual_model="deepseek-flash",
            provider_runtime_fingerprint="fixture-runtime",
            finish_reason="stop",
            prompt_tokens=100,
            completion_tokens=30,
            total_tokens=130,
            result=self.result,
        )


def execution_and_auth(conn, batch, adapter):
    plan = build_quality_execution_plan([batch], adapter=adapter)
    summary = public_quality_preflight_summary(plan)
    assert summary["provider_calls"] == 0
    assert summary["credential_read"] is False
    assert summary["quality_v1"] is True
    auth = record_quality_authorization(
        conn,
        execution_plan=plan,
        project_id=2,
        prd_id=3,
        prd_source_hash=PRD_HASH,
        plan_content_hash=PLAN_HASH,
        authorization_nonce="quality-fixture-001",
        authorized=True,
    )
    return plan, auth


def current_state():
    return {
        "exact_head": HEAD,
        "prd_id": 3,
        "prd_source_hash": PRD_HASH,
        "plan_profile_id": 1,
        "plan_content_hash": PLAN_HASH,
    }


@pytest.mark.parametrize("code", sorted(PROFILE_RESPONSE_ERROR_CODES) + ["PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN"])
def test_response_failure_is_durable_without_retry_or_raw_response(code):
    from fastapi import HTTPException

    class FailedAdapter(FakeQualityAdapter):
        def execute_with_credential(self, request, credential):
            self.execute_calls += 1
            raise HTTPException(502, detail={"code": code, "message": "private-response-marker"})

    batch = quality_batch()
    adapter = FailedAdapter(None)
    conn = sqlite3.connect(":memory:")
    _, auth = execution_and_auth(conn, batch, adapter)
    results = [dispatch_authorized_quality_batch(
        conn, batch, authorization_hash=auth["authorization_hash"], adapter=adapter,
        credential_reader=lambda: "secret", current_state=current_state) for _ in range(2)]
    assert results[0] == results[1]
    assert results[0]["status"] == ("unknown" if code.endswith("NETWORK_UNKNOWN") else "failed_after_send")
    assert results[0]["error_code"] == code
    assert adapter.execute_calls == 1
    row = conn.execute("SELECT * FROM profile_reconciliation_provider_quality_results").fetchall()
    assert len(row) == 1
    assert "private-response-marker" not in repr(row)


def test_quality_execution_identity_is_versioned_separately_from_legacy_v1():
    batch = quality_batch()
    adapter = FakeQualityAdapter(good_quality_result(batch))
    plan = build_quality_execution_plan([batch], adapter=adapter)
    assert plan["task_type"] == QUALITY_TASK_TYPE
    assert plan["output_schema_version"] == QUALITY_OUTPUT_SCHEMA_VERSION
    assert (plan["task_type"], plan["output_schema_version"]) != (TASK_TYPE, OUTPUT_SCHEMA_VERSION)
    assert plan["requests"][0]["module_ids"] == ["FR-03-prd-preview"]
    assert plan["requests"][0]["evidence_count"] >= 2


def test_successful_quality_dispatch_persists_validated_and_completeness_hashes_at_most_once():
    batch = quality_batch()
    adapter = FakeQualityAdapter(good_quality_result(batch))
    conn = sqlite3.connect(":memory:")
    _, auth = execution_and_auth(conn, batch, adapter)
    credential_reads = 0

    def credential_reader():
        nonlocal credential_reads
        credential_reads += 1
        return "secret"

    first = dispatch_authorized_quality_batch(
        conn,
        batch,
        authorization_hash=auth["authorization_hash"],
        adapter=adapter,
        credential_reader=credential_reader,
        current_state=current_state,
    )
    second = dispatch_authorized_quality_batch(
        conn,
        batch,
        authorization_hash=auth["authorization_hash"],
        adapter=adapter,
        credential_reader=credential_reader,
        current_state=current_state,
    )
    assert first["status"] == "succeeded"
    assert first["validated_quality_hash"]
    assert first["completeness_hash"]
    assert second == first
    assert credential_reads == 1
    assert adapter.execute_calls == 1
    row = conn.execute(
        "SELECT validated_quality_json,completeness_json FROM profile_reconciliation_provider_quality_results"
    ).fetchone()
    assert json.loads(row[0])["mappings"][0]["support_state"] == "all_requirements_supported"
    assert json.loads(row[1])["mappings"][0]["status"] == "implemented"


def test_missing_authorization_fails_before_credential_read_or_provider_call():
    batch = quality_batch()
    adapter = FakeQualityAdapter(good_quality_result(batch))
    conn = sqlite3.connect(":memory:")
    reads = 0

    def credential_reader():
        nonlocal reads
        reads += 1
        return "secret"

    with pytest.raises(BatchError, match="AUTHORIZATION_NOT_FOUND"):
        dispatch_authorized_quality_batch(
            conn,
            batch,
            authorization_hash="f" * 64,
            adapter=adapter,
            credential_reader=credential_reader,
            current_state=current_state,
        )
    assert reads == 0
    assert adapter.execute_calls == 0


def test_docs_only_model_supported_claim_is_rejected_after_fake_dispatch():
    batch = quality_batch([
        evidence("docs", "README.md", "parse preview human confirm current"),
    ])
    contract = build_quality_contract(batch)
    ref = batch["repo_evidence"][0]["evidence_id"]
    result = {
        "mappings": [{
            "planned_module_id": "FR-03-prd-preview",
            "module_summary": "Model overclaims implementation from docs.",
            "requirement_results": [
                {
                    "requirement_id": requirement["requirement_id"],
                    "state": "supported",
                    "evidence_ids": [ref],
                    "rationale": "README says it exists.",
                    "gap": "",
                }
                for requirement in contract["modules"][0]["requirements"]
            ],
        }]
    }
    adapter = FakeQualityAdapter(result)
    conn = sqlite3.connect(":memory:")
    _, auth = execution_and_auth(conn, batch, adapter)
    out = dispatch_authorized_quality_batch(
        conn,
        batch,
        authorization_hash=auth["authorization_hash"],
        adapter=adapter,
        credential_reader=lambda: "secret",
        current_state=current_state,
    )
    assert out["status"] == "failed_after_send"
    assert out["error_code"] == "QUALITY_IMPLEMENTATION_WITHOUT_SOURCE_CODE"
    assert out["validated_quality_hash"] is None
    assert out["completeness_hash"] is None


def test_scope_drift_fails_before_credential_read():
    batch = quality_batch()
    adapter = FakeQualityAdapter(good_quality_result(batch))
    conn = sqlite3.connect(":memory:")
    _, auth = execution_and_auth(conn, batch, adapter)
    reads = 0

    def credential_reader():
        nonlocal reads
        reads += 1
        return "secret"

    drifted = current_state() | {"exact_head": "9" * 40}
    with pytest.raises(BatchError, match="AUTHORIZED_SCOPE_DRIFT"):
        dispatch_authorized_quality_batch(
            conn,
            batch,
            authorization_hash=auth["authorization_hash"],
            adapter=adapter,
            credential_reader=credential_reader,
            current_state=lambda: drifted,
        )
    assert reads == 0
    assert adapter.execute_calls == 0


def assert_quality_failure_is_durable(batch, result, expected_code):
    adapter = FakeQualityAdapter(result)
    conn = sqlite3.connect(":memory:")
    _, auth = execution_and_auth(conn, batch, adapter)
    outputs = [dispatch_authorized_quality_batch(
        conn, batch, authorization_hash=auth["authorization_hash"], adapter=adapter,
        credential_reader=lambda: "secret", current_state=current_state,
    ) for _ in range(2)]
    assert outputs[0] == outputs[1]
    assert outputs[0]["status"] == "failed_after_send"
    assert outputs[0]["error_code"] == expected_code
    assert adapter.execute_calls == 1
    rows = conn.execute("SELECT error_code,receipt_json,validated_quality_json,completeness_json "
                        "FROM profile_reconciliation_provider_quality_results").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == expected_code
    assert json.loads(rows[0][1]) == {
        "provider": "deepseek", "provider_response_id": "quality-response-1",
        "actual_model": "deepseek-flash", "provider_runtime_fingerprint": "fixture-runtime",
        "finish_reason": "stop", "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
    }
    assert rows[0][2:] == (None, None)
    assert "private-response-marker" not in repr(rows)


@pytest.mark.parametrize("case,expected", [
    ("shape", "QUALITY_RESULT_INVALID"),
    ("module", "QUALITY_MODULE_COVERAGE_INVALID"),
    ("requirement", "QUALITY_REQUIREMENT_COVERAGE_INVALID"),
    ("cross_batch", "QUALITY_CROSS_BATCH_EVIDENCE"),
    ("no_evidence", "QUALITY_SUPPORT_WITHOUT_EVIDENCE"),
    ("state", "QUALITY_STATE_INVALID"),
    ("rationale", "QUALITY_RATIONALE_INVALID"),
    ("summary", "QUALITY_SUMMARY_INVALID"),
    ("gap_type", "QUALITY_GAP_INVALID"),
    ("supported_gap", "QUALITY_SUPPORTED_WITH_GAP"),
    ("missing_gap", "QUALITY_GAP_REQUIRED"),
])
def test_quality_validation_preserves_safe_specific_failure(case, expected):
    batch = quality_batch()
    result = good_quality_result(batch)
    mapping = result["mappings"][0]
    requirement = mapping["requirement_results"][0]
    if case == "shape":
        result = {"private-response-marker": True}
    elif case == "module":
        result["mappings"] = []
    elif case == "requirement":
        mapping["requirement_results"].pop()
    elif case == "cross_batch":
        requirement["evidence_ids"] = ["private-response-marker"]
    elif case == "no_evidence":
        requirement["evidence_ids"] = []
    elif case == "state":
        requirement["state"] = "private-response-marker"
    elif case == "rationale":
        requirement["rationale"] = "private-response-marker" * 100
    elif case == "summary":
        mapping["module_summary"] = "private-response-marker" * 100
    elif case == "gap_type":
        requirement["gap"] = {"private-response-marker": True}
    elif case == "supported_gap":
        requirement["gap"] = "private-response-marker"
    elif case == "missing_gap":
        requirement["state"] = "unknown"
    assert_quality_failure_is_durable(batch, result, expected)


@pytest.mark.parametrize("code", [
    "QUALITY_COMPLETENESS_INPUT_INVALID", "QUALITY_VALIDATED_HASH_MISMATCH",
    "QUALITY_COMPLETENESS_SCOPE_MISMATCH",
])
def test_completeness_failure_preserves_safe_code(monkeypatch, code):
    def fail(*args):
        raise BatchError(code)
    monkeypatch.setattr("app.profile_reconciliation_provider_quality.verify_requirement_completeness", fail)
    batch = quality_batch()
    assert_quality_failure_is_durable(batch, good_quality_result(batch), code)


@pytest.mark.parametrize("error", [
    BatchError("QUALITY_private-response-marker"),
    BatchError("QUALITY_GAP_REQUIRED private-response-marker"),
    BatchError("QUALITY_GAP_REQUIRED", "private-response-marker"),
    BatchError(["QUALITY_GAP_REQUIRED"]),
    TypeError("QUALITY_GAP_REQUIRED"), ValueError("QUALITY_GAP_REQUIRED"),
])
@pytest.mark.parametrize("step", ["validate_quality_result", "verify_requirement_completeness"])
def test_untrusted_validation_exception_is_sanitized(monkeypatch, error, step):
    def fail(*args):
        raise error
    monkeypatch.setattr("app.profile_reconciliation_provider_quality." + step, fail)
    batch = quality_batch()
    assert_quality_failure_is_durable(batch, good_quality_result(batch), "PROVIDER_QUALITY_RESULT_INVALID")
