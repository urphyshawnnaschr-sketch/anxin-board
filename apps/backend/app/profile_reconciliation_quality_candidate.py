"""Deterministic Quality V1 candidate aggregation.

Consumes only durably successful, validated Quality V1 results for the full exact batch
set. The resulting ProjectProfileV2 remains an isolated candidate: no confirmation,
report generation, SMTP, Ready, merge, or release side effect is performed here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Mapping, Sequence

from app.profile_reconciliation_batches import BatchError, _hash, _validate_batch
from app.profile_reconciliation_provider import _batch_identity, load_authorization
from app.profile_reconciliation_provider_quality import (
    QUALITY_OUTPUT_SCHEMA_VERSION,
    QUALITY_TASK_TYPE,
)
from app.profile_reconciliation_quality import verify_requirement_completeness
from app.project_profile_v2 import ProjectProfileV2Content

QUALITY_CANDIDATE_SCHEMA_VERSION = "profile_reconciliation_provider_quality_candidate_v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _path_type(path: str) -> str:
    lowered = path.casefold()
    if "/test" in lowered or lowered.startswith(("test/", "tests/")) or "/__tests__/" in lowered:
        return "test"
    if any(part in lowered for part in ("/frontend/", "/src/views/", "/src/components/", "/web/")):
        return "frontend"
    if any(part in lowered for part in ("/backend/", "/server/", "/api/")):
        return "backend"
    if any(part in lowered for part in ("/data/", "/db/", "/database/", "/migrations/")):
        return "data"
    return "other"


def _load_successful_result(conn, *, authorization_hash: str, batch: Mapping[str, object]):
    row = conn.execute(
        """SELECT status,validated_quality_json,completeness_json,
                  validated_quality_hash,completeness_hash
           FROM profile_reconciliation_provider_quality_results
           WHERE authorization_hash=? AND batch_id=?""",
        (authorization_hash, batch["batch_id"]),
    ).fetchone()
    if not row or row[0] != "succeeded" or type(row[1]) is not str or type(row[2]) is not str:
        raise BatchError("QUALITY_BATCHES_NOT_COMPLETE")
    try:
        validated = json.loads(row[1])
        persisted_completeness = json.loads(row[2])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise BatchError("QUALITY_RESULT_RECORD_INVALID") from None
    if validated.get("validated_quality_hash") != row[3] or persisted_completeness.get("completeness_hash") != row[4]:
        raise BatchError("QUALITY_RESULT_RECORD_HASH_MISMATCH")
    recomputed = verify_requirement_completeness(batch, validated)
    if recomputed != persisted_completeness:
        raise BatchError("QUALITY_COMPLETENESS_REPLAY_MISMATCH")
    return validated, recomputed


def aggregate_quality_candidate(
    conn,
    batches: Sequence[Mapping[str, object]],
    plan: Mapping[str, object],
    *,
    authorization_hash: str,
    current_state,
):
    if not batches or any(batch.get("plan_hash") != _hash(plan) for batch in batches):
        raise BatchError("PLAN_CHANGED")
    for batch in batches:
        _validate_batch(batch)
        if len(batch.get("planned_modules", [])) != 1:
            raise BatchError("QUALITY_CANDIDATE_REQUIRES_ONE_MODULE_PER_BATCH")
    identity = _batch_identity(list(batches))
    auth = load_authorization(conn, authorization_hash)
    if (auth.get("task_type"), auth.get("output_schema_version")) != (
        QUALITY_TASK_TYPE,
        QUALITY_OUTPUT_SCHEMA_VERSION,
    ):
        raise BatchError("QUALITY_AUTHORIZATION_REQUIRED")
    if (
        auth["batch_set_hash"] != identity["batch_set_hash"]
        or auth["plan_profile_id"] != identity["plan_profile_id"]
        or len(auth["request_manifest"]) != len(batches)
        or auth["selected_batch_indexes"] != list(range(len(batches)))
    ):
        raise BatchError("FULL_QUALITY_BATCH_SET_NOT_AUTHORIZED")
    state = current_state()
    if (
        state.get("exact_head") != auth["exact_head"]
        or state.get("plan_profile_id") != auth["plan_profile_id"]
        or state.get("plan_content_hash") != auth["plan_content_hash"]
    ):
        raise BatchError("AUTHORIZED_SCOPE_DRIFT")

    evidence = {
        item["evidence_id"]: item
        for batch in batches
        for item in batch["repo_evidence"]
    }
    result = deepcopy(dict(plan))
    result["implementation_mappings"] = []
    result["unplanned_code_features"] = []
    seen_modules: set[str] = set()

    for batch in sorted(batches, key=lambda item: item["batch_index"]):
        validated, completeness = _load_successful_result(
            conn,
            authorization_hash=authorization_hash,
            batch=batch,
        )
        if len(validated.get("mappings", [])) != 1 or len(completeness.get("mappings", [])) != 1:
            raise BatchError("QUALITY_RESULT_RECORD_INVALID")
        quality_mapping = validated["mappings"][0]
        complete_mapping = completeness["mappings"][0]
        module_id = batch["planned_modules"][0]["client_id"]
        if (
            quality_mapping.get("planned_module_id") != module_id
            or complete_mapping.get("planned_module_id") != module_id
            or module_id in seen_modules
        ):
            raise BatchError("QUALITY_MODULE_COVERAGE_INVALID")
        seen_modules.add(module_id)
        refs = sorted(set(quality_mapping.get("evidence_ids", [])))
        if any(ref not in evidence for ref in refs):
            raise BatchError("QUALITY_CROSS_BATCH_EVIDENCE")
        paths = sorted({str(evidence[ref]["path"]) for ref in refs})
        if len(refs) > 100 or len(paths) > 100:
            raise BatchError("CANDIDATE_EVIDENCE_CAPACITY_EXCEEDED")
        status = complete_mapping.get("status")
        if status not in {"implemented", "partial", "unknown"}:
            raise BatchError("QUALITY_COMPLETENESS_STATUS_INVALID")
        rationale = (
            f"Quality V1 requirement audit: {quality_mapping.get('module_summary', '').strip()} "
            f"Deterministic completeness: {complete_mapping.get('reason', '')}."
        ).strip()
        if len(rationale) > 2000:
            rationale = rationale[:1997] + "..."
        mapping = {
            "planned_module_id": module_id,
            "status": status,
            "exact_head": auth["exact_head"],
            "evidence_ids": refs,
            "paths": [
                {
                    "type": _path_type(path),
                    "pattern": path,
                    "required": True,
                    "note": "Quality V1 exact-HEAD evidence",
                }
                for path in paths
            ],
            "rationale": rationale if status != "unknown" else rationale or "Quality V1 evidence remains insufficient.",
        }
        result["implementation_mappings"].append(mapping)

    expected_modules = [module["client_id"] for module in plan.get("planned_modules", [])]
    if seen_modules != set(expected_modules) or len(seen_modules) != len(expected_modules):
        raise BatchError("QUALITY_MODULE_COVERAGE_INVALID")
    result["implementation_mappings"].sort(key=lambda item: expected_modules.index(item["planned_module_id"]))
    content = ProjectProfileV2Content.model_validate(result).model_dump()
    if content["planned_modules"] != plan["planned_modules"]:
        raise BatchError("PLAN_CHANGED")
    if current_state().get("exact_head") != auth["exact_head"]:
        raise BatchError("AUTHORIZED_SCOPE_DRIFT")

    raw = _json(content)
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    frozen = {
        "schema_version": QUALITY_CANDIDATE_SCHEMA_VERSION,
        "authorization_hash": authorization_hash,
        "execution_identity_hash": auth["execution_identity_hash"],
        "batch_set_hash": auth["batch_set_hash"],
        "exact_head": auth["exact_head"],
        "plan_profile_id": auth["plan_profile_id"],
        "content_hash": content_hash,
        "status": "candidate",
    }
    candidate_record_hash = _digest(frozen)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS profile_reconciliation_provider_quality_candidates(
            candidate_record_hash TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL CHECK(schema_version='{QUALITY_CANDIDATE_SCHEMA_VERSION}'),
            authorization_hash TEXT NOT NULL UNIQUE,
            execution_identity_hash TEXT NOT NULL,
            batch_set_hash TEXT NOT NULL,
            exact_head TEXT NOT NULL,
            plan_profile_id INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            content_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status='candidate'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TRIGGER IF NOT EXISTS trg_prpqc_u BEFORE UPDATE ON profile_reconciliation_provider_quality_candidates
            BEGIN SELECT RAISE(ABORT,'provider quality candidate append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_prpqc_d BEFORE DELETE ON profile_reconciliation_provider_quality_candidates
            BEGIN SELECT RAISE(ABORT,'provider quality candidate append-only'); END;
        """
    )
    old = conn.execute(
        "SELECT candidate_record_hash,content_hash,content_json,status FROM profile_reconciliation_provider_quality_candidates WHERE authorization_hash=?",
        (authorization_hash,),
    ).fetchone()
    if old:
        if old != (candidate_record_hash, content_hash, raw, "candidate"):
            raise BatchError("QUALITY_CANDIDATE_IDENTITY_CONFLICT")
        return {
            "status": "candidate",
            "authorization_hash": authorization_hash,
            "content_hash": content_hash,
            "content": content,
            "candidate_record_hash": candidate_record_hash,
        }
    conn.execute(
        """INSERT INTO profile_reconciliation_provider_quality_candidates
           (candidate_record_hash,schema_version,authorization_hash,execution_identity_hash,
            batch_set_hash,exact_head,plan_profile_id,content_hash,content_json,status)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_record_hash,
            QUALITY_CANDIDATE_SCHEMA_VERSION,
            authorization_hash,
            auth["execution_identity_hash"],
            auth["batch_set_hash"],
            auth["exact_head"],
            auth["plan_profile_id"],
            content_hash,
            raw,
            "candidate",
        ),
    )
    conn.commit()
    return {
        "status": "candidate",
        "authorization_hash": authorization_hash,
        "content_hash": content_hash,
        "content": content,
        "candidate_record_hash": candidate_record_hash,
    }
