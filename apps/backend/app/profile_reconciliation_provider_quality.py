"""Quality V1 provider execution path for Project Profile reconciliation.

This is additive to the immutable provider-v1 smoke path. It uses role-aware quality
batches, requirement-level messages, strict quality validation, and deterministic
completeness verification. It grants no model-call authority by itself: dispatch still
requires a separately persisted Human authorization bound to the exact execution plan.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import json
import sqlite3

from app.context_token_framing import COUNTING_POLICY_VERSION
from app.model_provider_contract import ProviderCapability, ProviderCredentialRequest
from app.profile_reconciliation_batches import BatchError, _validate_batch
from app.profile_reconciliation_provider import (
    MAX_OUTPUT_TOKENS,
    SAFETY_MARGIN_TOKENS,
    TRANSPORT_SCHEMA_VERSION,
    TRANSPORT_TASK_TYPE,
    _batch_identity,
    _classify,
    _digest,
    _identity_payload,
    _now,
    _positive,
    _public_requests,
    _request_hash,
    _scope_ok,
    _validate_execution,
    ensure_provider_schema,
    load_authorization,
    record_authorization,
)
from app.profile_reconciliation_quality import (
    build_quality_messages,
    validate_quality_result,
    verify_requirement_completeness,
)
from app.profile_reconciliation_quality_batches import plan_quality_reconciliation_batches

QUALITY_TASK_TYPE = "project_profile_reconcile_quality"
QUALITY_OUTPUT_SCHEMA_VERSION = "project-profile-reconciliation-quality/1.0"
QUALITY_RESULT_SCHEMA_VERSION = "profile_reconciliation_provider_quality_result_v1"

# Only product-owned validation codes may enter the durable error field. Never
# persist exception text or accept a prefix match from untrusted model output.
_QUALITY_VALIDATION_ERROR_CODES = frozenset({
    "QUALITY_RESULT_INVALID",
    "QUALITY_MODULE_COVERAGE_INVALID",
    "QUALITY_REQUIREMENT_COVERAGE_INVALID",
    "QUALITY_SUMMARY_INVALID",
    "QUALITY_STATE_INVALID",
    "QUALITY_CROSS_BATCH_EVIDENCE",
    "QUALITY_SUPPORT_WITHOUT_EVIDENCE",
    "QUALITY_IMPLEMENTATION_WITHOUT_SOURCE_CODE",
    "QUALITY_RATIONALE_INVALID",
    "QUALITY_GAP_INVALID",
    "QUALITY_SUPPORTED_WITH_GAP",
    "QUALITY_GAP_REQUIRED",
    "QUALITY_COMPLETENESS_INPUT_INVALID",
    "QUALITY_VALIDATED_HASH_MISMATCH",
    "QUALITY_COMPLETENESS_SCOPE_MISMATCH",
})


def _quality_validation_error_code(exc):
    if isinstance(exc, BatchError) and len(exc.args) == 1:
        code = exc.args[0]
        if type(code) is str and code in _QUALITY_VALIDATION_ERROR_CODES:
            return code
    return "PROVIDER_QUALITY_RESULT_INVALID"


class QualityReconciliationProviderAdapter:
    """Bridge the new local quality task to the already-reviewed live transport."""

    def __init__(self, live):
        self._live = live
        self._model = None

    @property
    def provider_id(self):
        return str(self._live.provider_id)

    def get_capability(self, *, task_type, output_schema_version):
        if (task_type, output_schema_version) != (QUALITY_TASK_TYPE, QUALITY_OUTPUT_SCHEMA_VERSION):
            raise BatchError("QUALITY_PROVIDER_IDENTITY_UNSUPPORTED")
        base = self._live.get_capability(
            task_type=TRANSPORT_TASK_TYPE,
            output_schema_version=TRANSPORT_SCHEMA_VERSION,
        )
        self._model = base.model_id
        return ProviderCapability(
            base.provider,
            base.model_id,
            base.model_version,
            QUALITY_TASK_TYPE,
            QUALITY_OUTPUT_SCHEMA_VERSION,
            base.context_window_tokens,
            base.max_output_tokens,
        )

    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens):
        from app.deepseek_profile_transport_policy import estimate_profile_request_utf8_bytes

        selected = self._live._selected_model()
        if self._model is None or selected != self._model:
            raise BatchError("MODEL_SELECTION_CHANGED_DURING_ADMISSION")
        return estimate_profile_request_utf8_bytes(
            messages=messages,
            max_tokens=max_output_tokens,
            model_id=selected,
        )

    def execute_with_credential(self, request, credential):
        if (request.task_type, request.output_schema_version) != (
            QUALITY_TASK_TYPE,
            QUALITY_OUTPUT_SCHEMA_VERSION,
        ):
            raise BatchError("QUALITY_PROVIDER_IDENTITY_UNSUPPORTED")
        bridged = ProviderCredentialRequest(
            request.local_task_id,
            request.provider,
            request.model_id,
            request.model_version,
            TRANSPORT_TASK_TYPE,
            TRANSPORT_SCHEMA_VERSION,
            request.messages,
            request.max_output_tokens,
        )
        return self._live.execute_with_credential(bridged, credential)


def build_quality_execution_plan(batches, *, adapter, selected_batch_indexes=None):
    identity = _batch_identity(batches)
    cap = adapter.get_capability(
        task_type=QUALITY_TASK_TYPE,
        output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
    )
    if any(type(value) is not int or value <= 0 for value in (cap.context_window_tokens, cap.max_output_tokens)):
        raise BatchError("PROVIDER_CAPABILITY_INVALID")
    max_output = min(MAX_OUTPUT_TOKENS, cap.max_output_tokens)
    max_input = cap.context_window_tokens - max_output - SAFETY_MARGIN_TOKENS
    if max_input <= 0:
        raise BatchError("PROVIDER_BUDGET_INVALID")
    ordered = sorted(batches, key=lambda batch: batch["batch_index"])
    selected = list(range(len(ordered))) if selected_batch_indexes is None else sorted(selected_batch_indexes)
    if (
        not selected
        or any(type(index) is not int or index < 0 or index >= len(ordered) for index in selected)
        or len(set(selected)) != len(selected)
    ):
        raise BatchError("INVALID_SELECTED_BATCHES")

    requests = []
    for index in selected:
        batch = ordered[index]
        messages = build_quality_messages(batch)
        wire = adapter.estimate_request_utf8_bytes(messages=messages, max_output_tokens=max_output)
        if type(wire) is not int or wire <= 0:
            raise BatchError("PROVIDER_WIRE_ESTIMATE_INVALID")
        if wire > max_input:
            raise BatchError("PROVIDER_REQUEST_OVER_BUDGET")
        requests.append(
            {
                "batch_index": index,
                "batch_id": batch["batch_id"],
                "input_hash": batch["input_hash"],
                "request_hash": _request_hash(cap, messages, max_output),
                "wire_bytes": wire,
                "module_ids": [module["client_id"] for module in batch["planned_modules"]],
                "evidence_count": len(batch["repo_evidence"]),
                "messages": messages,
            }
        )
    total = sum(request["wire_bytes"] for request in requests)
    frozen = identity | {
        "provider": cap.provider,
        "model_id": cap.model_id,
        "model_version": cap.model_version,
        "task_type": QUALITY_TASK_TYPE,
        "output_schema_version": QUALITY_OUTPUT_SCHEMA_VERSION,
        "transport_task_type": TRANSPORT_TASK_TYPE,
        "transport_schema_version": TRANSPORT_SCHEMA_VERSION,
        "max_output_tokens": max_output,
        "safety_margin_tokens": SAFETY_MARGIN_TOKENS,
        "max_input_bytes": max_input,
        "selected_batch_indexes": selected,
        "total_wire_bytes": total,
        "requests": _public_requests(requests),
    }
    return frozen | {"execution_identity_hash": _digest(frozen), "requests": requests}


def public_quality_preflight_summary(plan):
    frozen, _ = _validate_execution(plan)
    if frozen["task_type"] != QUALITY_TASK_TYPE or frozen["output_schema_version"] != QUALITY_OUTPUT_SCHEMA_VERSION:
        raise BatchError("QUALITY_EXECUTION_PLAN_REQUIRED")
    return frozen | {
        "execution_identity_hash": plan["execution_identity_hash"],
        "provider_calls": 0,
        "credential_read": False,
        "quality_v1": True,
    }


def record_quality_authorization(conn, **kwargs):
    plan = kwargs.get("execution_plan")
    if type(plan) is not dict:
        raise BatchError("QUALITY_EXECUTION_PLAN_REQUIRED")
    frozen, _ = _validate_execution(plan)
    if frozen["task_type"] != QUALITY_TASK_TYPE or frozen["output_schema_version"] != QUALITY_OUTPUT_SCHEMA_VERSION:
        raise BatchError("QUALITY_EXECUTION_PLAN_REQUIRED")
    return record_authorization(conn, **kwargs)


def _ensure_quality_result_schema(conn):
    ensure_provider_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS profile_reconciliation_provider_quality_results(
            result_record_hash TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL CHECK(schema_version='{QUALITY_RESULT_SCHEMA_VERSION}'),
            claim_hash TEXT NOT NULL UNIQUE,
            authorization_hash TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('succeeded','failed_pre_send','failed_after_send','unknown')),
            error_code TEXT,
            receipt_json TEXT,
            validated_quality_json TEXT,
            completeness_json TEXT,
            validated_quality_hash TEXT,
            completeness_hash TEXT,
            completed_at TEXT NOT NULL,
            UNIQUE(authorization_hash,batch_id));
        CREATE TRIGGER IF NOT EXISTS trg_prpqr_u BEFORE UPDATE ON profile_reconciliation_provider_quality_results
            BEGIN SELECT RAISE(ABORT,'provider quality result append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_prpqr_d BEFORE DELETE ON profile_reconciliation_provider_quality_results
            BEGIN SELECT RAISE(ABORT,'provider quality result append-only'); END;
        """
    )
    conn.commit()


def _existing_quality(conn, authorization_hash, batch_id):
    _ensure_quality_result_schema(conn)
    row = conn.execute(
        """SELECT c.claim_hash,r.status,r.error_code,r.receipt_json,
                  r.validated_quality_hash,r.completeness_hash
           FROM profile_reconciliation_provider_claims c
           LEFT JOIN profile_reconciliation_provider_quality_results r ON r.claim_hash=c.claim_hash
           WHERE c.authorization_hash=? AND c.batch_id=?""",
        (authorization_hash, batch_id),
    ).fetchone()
    if not row:
        return None
    if row[1] is None:
        return {"status": "unknown", "claim_hash": row[0], "batch_id": batch_id}
    receipt = json.loads(row[3]) if row[3] else {}
    return {
        "claim_hash": row[0],
        "status": row[1],
        "error_code": row[2],
        "provider_response_id": receipt.get("provider_response_id"),
        "actual_model": receipt.get("actual_model"),
        "validated_quality_hash": row[4],
        "completeness_hash": row[5],
        "batch_id": batch_id,
    }


def _persist_quality(
    conn,
    *,
    claim_hash,
    authorization_hash,
    batch_id,
    status,
    error_code=None,
    receipt=None,
    validated_quality=None,
    completeness=None,
):
    rec = None if receipt is None else {
        "provider": receipt.provider,
        "provider_response_id": receipt.provider_response_id,
        "actual_model": receipt.actual_model,
        "provider_runtime_fingerprint": receipt.provider_runtime_fingerprint,
        "finish_reason": receipt.finish_reason,
        "prompt_tokens": receipt.prompt_tokens,
        "completion_tokens": receipt.completion_tokens,
        "total_tokens": receipt.total_tokens,
    }
    validated_hash = validated_quality.get("validated_quality_hash") if validated_quality else None
    completeness_hash = completeness.get("completeness_hash") if completeness else None
    completed_at = _now()
    payload = {
        "schema_version": QUALITY_RESULT_SCHEMA_VERSION,
        "claim_hash": claim_hash,
        "authorization_hash": authorization_hash,
        "batch_id": batch_id,
        "status": status,
        "error_code": error_code,
        "receipt": rec,
        "validated_quality_hash": validated_hash,
        "completeness_hash": completeness_hash,
        "completed_at": completed_at,
    }
    result_record_hash = _digest(payload)
    conn.execute(
        "INSERT INTO profile_reconciliation_provider_quality_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            result_record_hash,
            QUALITY_RESULT_SCHEMA_VERSION,
            claim_hash,
            authorization_hash,
            batch_id,
            status,
            error_code,
            json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if rec else None,
            json.dumps(validated_quality, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if validated_quality else None,
            json.dumps(completeness, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if completeness else None,
            validated_hash,
            completeness_hash,
            completed_at,
        ),
    )
    conn.commit()
    return {
        "status": status,
        "batch_id": batch_id,
        "claim_hash": claim_hash,
        "error_code": error_code,
        "provider_response_id": rec.get("provider_response_id") if rec else None,
        "actual_model": rec.get("actual_model") if rec else None,
        "validated_quality_hash": validated_hash,
        "completeness_hash": completeness_hash,
    }


def _manifest_request_quality(batch, auth, cap, adapter):
    matches = [item for item in auth["request_manifest"] if item.get("batch_id") == batch.get("batch_id")]
    if len(matches) != 1:
        raise BatchError("BATCH_NOT_AUTHORIZED")
    messages = build_quality_messages(batch)
    wire = adapter.estimate_request_utf8_bytes(messages=messages, max_output_tokens=auth["max_output_tokens"])
    request_hash = _request_hash(cap, messages, auth["max_output_tokens"])
    manifest = matches[0]
    if (
        manifest.get("input_hash") != batch.get("input_hash")
        or manifest.get("request_hash") != request_hash
        or manifest.get("wire_bytes") != wire
    ):
        raise BatchError("AUTHORIZED_REQUEST_DRIFT")
    return manifest, messages


def dispatch_authorized_quality_batch(
    conn,
    batch,
    *,
    authorization_hash,
    adapter,
    credential_reader: Callable[[], str],
    current_state: Callable[[], Mapping[str, object]],
):
    """Execute exactly one separately authorized Quality V1 batch with at-most-once claim."""
    _validate_batch(batch)
    auth = load_authorization(conn, authorization_hash)
    if (auth.get("task_type"), auth.get("output_schema_version")) != (
        QUALITY_TASK_TYPE,
        QUALITY_OUTPUT_SCHEMA_VERSION,
    ):
        raise BatchError("QUALITY_AUTHORIZATION_REQUIRED")
    if batch.get("batch_set_hash") != auth["batch_set_hash"]:
        raise BatchError("BATCH_SET_NOT_AUTHORIZED")
    if batch.get("batch_index") not in auth["selected_batch_indexes"]:
        raise BatchError("BATCH_NOT_AUTHORIZED")
    old = _existing_quality(conn, authorization_hash, batch["batch_id"])
    if old:
        return old

    cap = adapter.get_capability(
        task_type=QUALITY_TASK_TYPE,
        output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
    )
    if not _scope_ok(auth, current_state(), cap):
        raise BatchError("AUTHORIZED_SCOPE_DRIFT")
    _, messages = _manifest_request_quality(batch, auth, cap, adapter)
    credential = credential_reader()
    if type(credential) is not str or not credential.strip():
        raise BatchError("PROVIDER_CREDENTIAL_INVALID")
    cap = adapter.get_capability(
        task_type=QUALITY_TASK_TYPE,
        output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
    )
    if not _scope_ok(auth, current_state(), cap):
        raise BatchError("AUTHORIZED_SCOPE_DRIFT")
    manifest, messages = _manifest_request_quality(batch, auth, cap, adapter)

    claimed_at = _now()
    claim_payload = {
        "schema_version": "profile_reconciliation_provider_claim_v1",
        "authorization_hash": authorization_hash,
        "batch_id": batch["batch_id"],
        "request_hash": manifest["request_hash"],
        "claimed_at": claimed_at,
    }
    claim_hash = _digest(claim_payload)
    try:
        conn.execute(
            "INSERT INTO profile_reconciliation_provider_claims VALUES(?,?,?,?,?,?)",
            (
                claim_hash,
                "profile_reconciliation_provider_claim_v1",
                authorization_hash,
                batch["batch_id"],
                manifest["request_hash"],
                claimed_at,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        old = _existing_quality(conn, authorization_hash, batch["batch_id"])
        if old:
            return old
        raise

    try:
        cap = adapter.get_capability(
            task_type=QUALITY_TASK_TYPE,
            output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
        )
        if not _scope_ok(auth, current_state(), cap):
            return _persist_quality(
                conn,
                claim_hash=claim_hash,
                authorization_hash=authorization_hash,
                batch_id=batch["batch_id"],
                status="failed_pre_send",
                error_code="AUTHORIZED_SCOPE_DRIFT_AFTER_CLAIM",
            )
        _manifest_request_quality(batch, auth, cap, adapter)
    except Exception:
        return _persist_quality(
            conn,
            claim_hash=claim_hash,
            authorization_hash=authorization_hash,
            batch_id=batch["batch_id"],
            status="failed_pre_send",
            error_code="PRE_DISPATCH_REVALIDATION_FAILED",
        )

    request = ProviderCredentialRequest(
        claim_hash[:32],
        auth["provider"],
        auth["model_id"],
        auth["model_version"],
        QUALITY_TASK_TYPE,
        QUALITY_OUTPUT_SCHEMA_VERSION,
        messages,
        auth["max_output_tokens"],
    )
    try:
        receipt = adapter.execute_with_credential(request, credential)
    except Exception as exc:
        status, code = _classify(exc)
        return _persist_quality(
            conn,
            claim_hash=claim_hash,
            authorization_hash=authorization_hash,
            batch_id=batch["batch_id"],
            status=status,
            error_code=code,
        )

    if (
        receipt.provider != auth["provider"]
        or receipt.actual_model != auth["model_id"]
        or type(receipt.provider_response_id) is not str
        or not receipt.provider_response_id.strip()
        or type(receipt.provider_runtime_fingerprint) is not str
        or not receipt.provider_runtime_fingerprint.strip()
        or receipt.finish_reason != "stop"
        or any(type(value) is not int or value < 0 for value in (receipt.prompt_tokens, receipt.completion_tokens, receipt.total_tokens))
        or receipt.total_tokens != receipt.prompt_tokens + receipt.completion_tokens
    ):
        return _persist_quality(
            conn,
            claim_hash=claim_hash,
            authorization_hash=authorization_hash,
            batch_id=batch["batch_id"],
            status="failed_after_send",
            error_code="PROVIDER_RECEIPT_IDENTITY_INVALID",
            receipt=receipt,
        )

    try:
        validated = validate_quality_result(batch, receipt.result)
        completeness = verify_requirement_completeness(batch, validated)
    except (BatchError, TypeError, ValueError) as exc:
        return _persist_quality(
            conn,
            claim_hash=claim_hash,
            authorization_hash=authorization_hash,
            batch_id=batch["batch_id"],
            status="failed_after_send",
            error_code=_quality_validation_error_code(exc),
            receipt=receipt,
        )

    cap = adapter.get_capability(
        task_type=QUALITY_TASK_TYPE,
        output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
    )
    if not _scope_ok(auth, current_state(), cap):
        return _persist_quality(
            conn,
            claim_hash=claim_hash,
            authorization_hash=authorization_hash,
            batch_id=batch["batch_id"],
            status="unknown",
            error_code="SOURCE_OR_MODEL_CHANGED_AFTER_PROVIDER_DISPATCH",
            receipt=receipt,
        )
    return _persist_quality(
        conn,
        claim_hash=claim_hash,
        authorization_hash=authorization_hash,
        batch_id=batch["batch_id"],
        status="succeeded",
        receipt=receipt,
        validated_quality=validated,
        completeness=completeness,
    )


def prepare_product_quality_execution(project_id, plan_profile_id, *, selected_batch_indexes=None, adapter=None,
                                      include_coverage_plan=False):
    """Prepare the real Product Quality V1 request set without reading credentials or calling a provider."""
    from app import project_profile_generation as core
    from app.deepseek_live_profile_adapter import build_live_deepseek_profile_adapter
    from app.project_profiles import read_current_confirmed_project_profile
    from app.repository_index import build_indexed_repository_map
    from app.profile_reconciliation_retrieval_coverage import retrieval_coverage

    _positive(project_id, "INVALID_PROJECT_ID")
    _positive(plan_profile_id, "INVALID_PLAN_ID")
    live = adapter or QualityReconciliationProviderAdapter(build_live_deepseek_profile_adapter())
    project, prd, git = core._read_current_inputs(project_id)
    with core.get_connection() as conn:
        plan = read_current_confirmed_project_profile(project_id, conn=conn)
    if plan["id"] != plan_profile_id or plan["source_prd_id"] != prd["id"]:
        raise BatchError("PLAN_NOT_CURRENT_CONFIRMED_AUTHORITY")
    if plan["content"].get("schema_version") != "project_profile_v2":
        raise BatchError("PLAN_SCHEMA_NOT_V2")

    cap = live.get_capability(
        task_type=QUALITY_TASK_TYPE,
        output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
    )
    max_output = min(MAX_OUTPUT_TOKENS, cap.max_output_tokens)
    budget = {
        "context_window_tokens": cap.context_window_tokens,
        "max_output_tokens": max_output,
        "reserved_output_tokens": max_output,
        "safety_margin_tokens": SAFETY_MARGIN_TOKENS,
        "counting_policy_version": COUNTING_POLICY_VERSION,
    }
    indexed = build_indexed_repository_map(project_id, project, git)
    if indexed["exact_head"] != git["remote_head"]:
        raise BatchError("HEAD_CHANGED")
    batches = plan_quality_reconciliation_batches(
        plan["content"],
        indexed["evidence"],
        exact_head=indexed["exact_head"],
        plan_profile_id=plan_profile_id,
        budget_record=budget,
        repository_coverage={
            key: indexed[key]
            for key in ("coverage", "complete_inventory", "complete_safe_analysis")
            if key in indexed
        },
    )
    execution = build_quality_execution_plan(
        batches,
        adapter=live,
        selected_batch_indexes=selected_batch_indexes,
    )

    coverage_plan = None
    coverage_summary = None
    if include_coverage_plan:
        from app.profile_reconciliation_coverage_plan import plan_coverage_batches, public_coverage_summary
        coverage_plan = plan_coverage_batches(
            plan["content"], indexed["evidence"], exact_head=indexed["exact_head"],
            plan_profile_id=plan_profile_id, budget_record=budget,
            selected_evidence_ids=sorted({item["evidence_id"] for batch in batches for item in batch["repo_evidence"]}),
        )
        coverage_summary = public_coverage_summary(coverage_plan)
        coverage_summary["selection_scope"] = "all_planned_module_batches_not_provider_receipts"
        coverage_summary["source_module_batch_set_hash"] = execution["batch_set_hash"]

    project2, prd2, git2 = core._read_current_inputs(project_id)
    with core.get_connection() as conn:
        plan2 = read_current_confirmed_project_profile(project_id, conn=conn)
    cap2 = live.get_capability(
        task_type=QUALITY_TASK_TYPE,
        output_schema_version=QUALITY_OUTPUT_SCHEMA_VERSION,
    )
    if (
        project2["id"] != project["id"]
        or prd2["id"] != prd["id"]
        or prd2["source_hash"] != prd["source_hash"]
        or git2["remote_head"] != git["remote_head"]
        or plan2["id"] != plan["id"]
        or plan2["content_hash"] != plan["content_hash"]
        or (cap2.provider, cap2.model_id, cap2.model_version)
        != (execution["provider"], execution["model_id"], execution["model_version"])
    ):
        raise BatchError("PREPARATION_SCOPE_CHANGED")
    return {
        "project_id": project_id,
        "prd_id": prd["id"],
        "prd_source_hash": prd["source_hash"],
        "plan_profile_id": plan["id"],
        "plan_content_hash": plan["content_hash"],
        "plan": deepcopy(plan["content"]),
        "exact_head": indexed["exact_head"],
        "batches": batches,
        "adapter": live,
        "execution_plan": execution,
        "coverage_plan": coverage_plan,
        "summary": public_quality_preflight_summary(execution)
        | {
            "tracked_files": indexed.get("tracked_files"),
            "safe_text_bytes": indexed.get("safe_text_bytes"),
            "coverage": deepcopy(indexed.get("coverage")),
            "retrieval_coverage": retrieval_coverage(indexed["evidence"], batches),
            "coverage_supplement": coverage_summary,
        },
    }
