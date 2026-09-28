"""Exact one-shot owner-authorized Phase 3 live smoke for project 2 / profile 1 / batch 2.

This entrypoint is intentionally non-generic. It is hard-bound to the reviewed Phase 3
source/package and the zero-call preflight facts harvested on 2026-09-16. It may read the
current Windows user's DeepSeek API key only after durable authorization/scope checks and
may dispatch at most the single authorized batch. It never prints credentials or prompt
bodies and never aggregates/promotes a full candidate.
"""
from __future__ import annotations

import json
from pathlib import Path

from app import project_profile_generation as core
from app.profile_reconciliation_batches import BatchError
from app.profile_reconciliation_provider import (
    OUTPUT_SCHEMA_VERSION,
    TASK_TYPE,
    dispatch_authorized_batch,
    prepare_product_execution,
    record_authorization,
)
from app.project_profiles import read_current_confirmed_project_profile

PROJECT_ID = 2
PLAN_PROFILE_ID = 1
BATCH_INDEX = 2
EXPECTED_REPOSITORY_HEAD = "1164bff60119ecaecac01f13c7427ea37f9bd9a6"
EXPECTED_PROVIDER = "deepseek"
EXPECTED_MODEL = "deepseek-flash"
EXPECTED_WIRE_BYTES = 59736
EXPECTED_MODULE_IDS = ["FR-03-prd-preview"]
EXPECTED_EVIDENCE_COUNT = 2
AUTHORIZATION_NONCE = "chat-20260916-phase3-project2-profile1-batch2-smoke-v1"
SCHEMA_VERSION = "profile_reconciliation_provider_single_smoke_v1"


def _current_state(adapter) -> dict[str, object]:
    _project, prd, git_state = core._read_current_inputs(PROJECT_ID)
    with core.get_connection() as conn:
        plan = read_current_confirmed_project_profile(PROJECT_ID, conn=conn)
    cap = adapter.get_capability(task_type=TASK_TYPE, output_schema_version=OUTPUT_SCHEMA_VERSION)
    return {
        "exact_head": git_state["remote_head"],
        "prd_id": prd["id"],
        "prd_source_hash": prd["source_hash"],
        "plan_profile_id": plan["id"],
        "plan_content_hash": plan["content_hash"],
        "provider": cap.provider,
        "model_id": cap.model_id,
        "model_version": cap.model_version,
    }


def _assert_exact_scope(prepared: dict[str, object]) -> dict[str, object]:
    execution = prepared["execution_plan"]
    requests = execution["requests"]
    if prepared["project_id"] != PROJECT_ID or prepared["plan_profile_id"] != PLAN_PROFILE_ID:
        raise BatchError("SMOKE_PRODUCT_IDENTITY_DRIFT")
    if prepared["exact_head"] != EXPECTED_REPOSITORY_HEAD:
        raise BatchError("SMOKE_REPOSITORY_HEAD_DRIFT")
    if execution["provider"] != EXPECTED_PROVIDER or execution["model_id"] != EXPECTED_MODEL:
        raise BatchError("SMOKE_PROVIDER_MODEL_DRIFT")
    if execution["selected_batch_indexes"] != [BATCH_INDEX] or len(requests) != 1:
        raise BatchError("SMOKE_BATCH_SELECTION_DRIFT")
    request = requests[0]
    if (
        request["batch_index"] != BATCH_INDEX
        or request["wire_bytes"] != EXPECTED_WIRE_BYTES
        or request["module_ids"] != EXPECTED_MODULE_IDS
        or request["evidence_count"] != EXPECTED_EVIDENCE_COUNT
    ):
        raise BatchError("SMOKE_REQUEST_SCOPE_DRIFT")
    return request


def run(output: Path) -> int:
    prepared = prepare_product_execution(PROJECT_ID, PLAN_PROFILE_ID, selected_batch_indexes=[BATCH_INDEX])
    request = _assert_exact_scope(prepared)
    adapter = prepared["adapter"]
    batch = prepared["batches"][BATCH_INDEX]

    with core.get_connection() as conn:
        authorization = record_authorization(
            conn,
            execution_plan=prepared["execution_plan"],
            project_id=PROJECT_ID,
            prd_id=prepared["prd_id"],
            prd_source_hash=prepared["prd_source_hash"],
            plan_content_hash=prepared["plan_content_hash"],
            authorization_nonce=AUTHORIZATION_NONCE,
            authorized=True,
        )
        result = dispatch_authorized_batch(
            conn,
            batch,
            authorization_hash=authorization["authorization_hash"],
            adapter=adapter,
            credential_reader=core._read_provider_credential,
            current_state=lambda: _current_state(adapter),
        )

    safe = {
        "schema_version": SCHEMA_VERSION,
        "authorization_nonce": AUTHORIZATION_NONCE,
        "project_id": PROJECT_ID,
        "plan_profile_id": PLAN_PROFILE_ID,
        "repository_exact_head": EXPECTED_REPOSITORY_HEAD,
        "batch_index": BATCH_INDEX,
        "wire_bytes": request["wire_bytes"],
        "module_ids": request["module_ids"],
        "evidence_count": request["evidence_count"],
        "provider": EXPECTED_PROVIDER,
        "model_id": EXPECTED_MODEL,
        "status": result.get("status"),
        "error_code": result.get("error_code"),
        "provider_response_id": result.get("provider_response_id"),
        "actual_model": result.get("actual_model"),
        "validated_result_hash": result.get("validated_result_hash"),
        "credential_value_output": False,
        "prompt_or_source_body_output": False,
        "full_batch_execution_authorized": False,
        "candidate_aggregation_performed": False,
        "blind_retry_allowed": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(safe, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") == "succeeded" else 2


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.output))
