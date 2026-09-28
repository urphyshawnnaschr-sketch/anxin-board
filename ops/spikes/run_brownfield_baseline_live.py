"""Explicit owner authorization for one full product Atlas baseline execution.

Uses the same preparation, durable stage core and candidate promotion as the product.
No Human confirmation, automatic retry, SMTP or raw source/provider output is permitted.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import os
from pathlib import Path
import re
import sys
import threading

if os.environ.get("ANXINBOARD_OWNER_LIVE_ATLAS_BASELINE") != "YES":
    raise SystemExit("LIVE_BASELINE_GUARD_REQUIRED")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from app import brownfield_baseline as core
from app import brownfield_baseline_store as store
from app import project_profile_generation as profile
from app.db import get_connection
from app.project_profiles import get_profile
from app.project_state_baseline_api import _promote_generated_candidate, _require_exact_head_reconciliation_coverage

PROJECT_ID = 3
PROFILE_ID = 2
EXPECTED_HEAD = "32ccc3737361a8fd9298b97c24f5ed9c9c8ed9d6"
EXPECTED_MODEL = "deepseek-flash"
EXPECTED_MODULE_COUNT = 16
_AUTH_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,100}\Z")
_STAGE_PATTERN = re.compile(r"[A-Za-z0-9_/-]{1,180}\Z")
_STATUS_VALUES = {"queued", "running", "succeeded", "failed_pre_send", "failed_after_send", "unknown"}


def _check_scope(scope):
    if (scope.get("project_id"), scope.get("plan_profile_id"), scope.get("exact_head")) != (PROJECT_ID, PROFILE_ID, EXPECTED_HEAD):
        raise core.BaselineError("BROWNFIELD_LIVE_SCOPE_CHANGED")


def _progress(task_id, stop):
    """Observe committed stages only. Never admit work or read credentials."""
    last = None
    while not stop.is_set():
        try:
            with closing(get_connection()) as conn:
                current = store.task_status(conn, task_id)
            stages = current["stages"]
            stage_key = stages[-1]["stage_key"] if stages else "none"
            if not _STAGE_PATTERN.fullmatch(stage_key):
                stage_key = "redacted"
            state = current["status"] if current["status"] in _STATUS_VALUES else "unknown"
            snapshot = (state, len(stages), sum(s["status"] == "succeeded" for s in stages), stage_key)
            if snapshot != last:
                print("LIVE_BASELINE_TASK_STATUS=" + state, flush=True)
                print("LIVE_BASELINE_CLAIMED_CALLS=" + str(snapshot[1]), flush=True)
                print("LIVE_BASELINE_COMPLETED_STAGES=" + str(snapshot[2]), flush=True)
                print("LIVE_BASELINE_CURRENT_STAGE=" + stage_key, flush=True)
                last = snapshot
            if state not in {"queued", "running"}:
                return
        except Exception:
            # Observation errors never grant a retry or print an exception body.
            print("LIVE_BASELINE_PROGRESS_READ=UNAVAILABLE", flush=True)
            return
        stop.wait(2)


def main() -> int:
    observer = None
    stop = threading.Event()
    task = None
    try:
        nonce = os.environ.get("ANXINBOARD_ATLAS_BASELINE_AUTHORIZATION_ID", "")
        if not _AUTH_PATTERN.fullmatch(nonce):
            raise core.BaselineError("BROWNFIELD_LIVE_AUTHORIZATION_REQUIRED")
        initial = core.read_scope(PROJECT_ID, PROFILE_ID)
        _check_scope(initial)
        prepared = core.prepare_baseline(PROJECT_ID, PROFILE_ID)
        if prepared["scope"] != initial or core.read_scope(PROJECT_ID, PROFILE_ID) != initial:
            raise core.BaselineError("BROWNFIELD_LIVE_SCOPE_CHANGED")
        capability = prepared["capability"]
        current_cap = prepared["adapter"].get_capability(task_type=core.PROFILE_TASK, output_schema_version=core.TRANSPORT_SCHEMA)
        if capability.model_id != EXPECTED_MODEL or core.cap_identity(current_cap) != core.cap_identity(capability):
            raise core.BaselineError("BROWNFIELD_LIVE_MODEL_CHANGED")
        module_ids = [module["client_id"] for module in prepared["plan"]["planned_modules"]]
        if len(module_ids) != EXPECTED_MODULE_COUNT or len(set(module_ids)) != EXPECTED_MODULE_COUNT:
            raise core.BaselineError("BROWNFIELD_LIVE_MODULE_SCOPE_CHANGED")
        if prepared["summary"]["preflight_identity_hash"] != core.digest(prepared["identity"]):
            raise core.BaselineError("BROWNFIELD_LIVE_IDENTITY_INVALID")
        with closing(get_connection()) as conn:
            store.ensure_schema(conn)
            task = store.create_task(conn, project_id=PROJECT_ID, authorization_nonce=nonce,
                                     identity=prepared["identity"], max_calls=prepared["identity"]["max_calls"])
        print("LIVE_BASELINE_MODEL=" + EXPECTED_MODEL, flush=True)
        print("LIVE_BASELINE_MODULE_COUNT=" + str(len(module_ids)), flush=True)
        print("LIVE_BASELINE_MAX_CALLS=" + str(task["max_calls"]), flush=True)
        print("LIVE_BASELINE_PREFLIGHT_IDENTITY_HASH=" + task["identity_hash"], flush=True)
        observer = threading.Thread(target=_progress, args=(task["task_id"], stop), name="atlas-owner-observer", daemon=True)
        observer.start()
        core.run_baseline(
            prepared, task, connection_factory=get_connection,
            scope_reader=lambda: core.read_scope(PROJECT_ID, PROFILE_ID),
            credential_reader=profile._read_provider_credential,
            promote=lambda content: _promote_generated_candidate(
                project_id=PROJECT_ID, prepared=prepared["scope"],
                authorization_hash=core.digest({"task_id": task["task_id"], "identity_hash": task["identity_hash"]}),
                content=content),
        )
        stop.set()
        observer.join(timeout=5)
        with closing(get_connection()) as conn:
            final = store.task_status(conn, task["task_id"])
            saved = store.get_output(conn, task["task_id"])
        status = final["status"]
        if status not in _STATUS_VALUES:
            raise core.BaselineError("BROWNFIELD_LIVE_TASK_STATUS_INVALID")
        print("LIVE_BASELINE_FINAL_STATUS=" + status, flush=True)
        print("LIVE_BASELINE_FINAL_CLAIMED_CALLS=" + str(final["claimed_calls"]), flush=True)
        reused = sum(bool((stage.get('result') or {}).get('reused_from_task_id')) for stage in final['stages'])
        print("LIVE_BASELINE_REUSED_STAGES=" + str(reused), flush=True)
        print("LIVE_BASELINE_NON_REUSED_DISPATCH_CLAIMS=" + str(final['claimed_calls']-reused), flush=True)
        if status != "succeeded":
            for stage in final['stages']:
                diagnostic = (stage.get('result') or {}).get('diagnostic') or {}
                for name in ('content_utf8_bytes', 'chunk_count', 'output_tokens', 'output_limit_reached', 'json_error_line', 'json_error_column', 'json_error_offset', 'tool_call_count'):
                    value = diagnostic.get(name)
                    if type(value) in (int, bool):
                        print('LIVE_BASELINE_DIAGNOSTIC_' + name.upper() + '=' + str(value), flush=True)
            error = final.get("error_code")
            raise core.BaselineError(error if isinstance(error, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,150}", error) else "BROWNFIELD_LIVE_TASK_NOT_SUCCEEDED")
        if saved is None or type(final.get("profile_id")) is not int or final["profile_id"] <= 0:
            raise core.BaselineError("BROWNFIELD_LIVE_CANDIDATE_MISSING")
        candidate = get_profile(final["profile_id"])
        if candidate.get("status") != "candidate" or candidate.get("project_id") != PROJECT_ID or candidate.get("source_prd_id") != prepared["scope"]["prd_id"]:
            raise core.BaselineError("BROWNFIELD_LIVE_CANDIDATE_INVALID")
        content = candidate.get("content")
        _require_exact_head_reconciliation_coverage(content, exact_head=EXPECTED_HEAD)
        if (content != saved["output"]["content"] or content.get("schema_version") != "project_profile_v2"
                or len(content["planned_modules"]) != EXPECTED_MODULE_COUNT
                or len(content["implementation_mappings"]) != EXPECTED_MODULE_COUNT
                or {m["planned_module_id"] for m in content["implementation_mappings"]} != set(module_ids)):
            raise core.BaselineError("BROWNFIELD_LIVE_CANDIDATE_COVERAGE_INVALID")
        if core.read_scope(PROJECT_ID, PROFILE_ID) != initial:
            raise core.BaselineError("BROWNFIELD_LIVE_SCOPE_CHANGED")
        mapping_counts = Counter(mapping["status"] for mapping in content["implementation_mappings"])
        print("LIVE_BASELINE_MAPPING_COUNT=" + str(len(content["implementation_mappings"])), flush=True)
        for label in ("implemented", "partial", "unknown"):
            print(f"LIVE_BASELINE_MODULES_{label.upper()}={mapping_counts[label]}", flush=True)
        requirements = saved["output"]["requirements"]
        if type(requirements) is not dict or set(requirements) != set(module_ids):
            raise core.BaselineError("BROWNFIELD_LIVE_REQUIREMENT_COVERAGE_INVALID")
        for slot, module_id in enumerate(module_ids):
            rows = requirements[module_id]
            if len(rows) != len(prepared["plan"]["planned_modules"][slot]["requirements"]) or any(row["status"] not in {"implemented", "partial", "unknown"} for row in rows):
                raise core.BaselineError("BROWNFIELD_LIVE_REQUIREMENT_COVERAGE_INVALID")
            counts = Counter(row["status"] for row in rows)
            print(f"LIVE_BASELINE_MODULE_{slot}_REQUIREMENT_COUNT={len(rows)}", flush=True)
            for label in ("implemented", "partial", "unknown"):
                print(f"LIVE_BASELINE_MODULE_{slot}_REQ_{label.upper()}={counts[label]}", flush=True)
        coverage = saved["output"]["coverage"]
        if coverage.get("gap_check_completed") is not True or coverage.get("complete_repository_semantic_proof") is not False:
            raise core.BaselineError("BROWNFIELD_LIVE_COVERAGE_INVALID")
        for key in ("tracked_files", "safe_paths", "inspected_paths", "unexplained_safe_paths", "omitted_neighbor_paths"):
            value = coverage.get(key)
            if type(value) is not int or value < 0:
                raise core.BaselineError("BROWNFIELD_LIVE_COVERAGE_INVALID")
            print("LIVE_BASELINE_" + key.upper() + "=" + str(value), flush=True)
        print("LIVE_BASELINE_GAP_CHECK_COMPLETED=" + ("YES" if coverage.get("gap_check_completed") is True else "NO"), flush=True)
        print("LIVE_BASELINE_COMPLETE_REPOSITORY_SEMANTIC_PROOF=NO", flush=True)
        print("LIVE_BASELINE_CANDIDATE_ID=" + str(candidate["id"]), flush=True)
        print("LIVE_BASELINE_CANDIDATE_STATUS=candidate", flush=True)
        print("LIVE_BASELINE_ACCEPTANCE=PASS", flush=True)
        return 0
    except Exception as exc:
        print("LIVE_BASELINE_ERROR_CODE=" + core.safe_code(exc), flush=True)
        print("LIVE_BASELINE_ACCEPTANCE=FAIL", flush=True)
        return 1
    finally:
        stop.set()
        if observer is not None and observer.ident is not None:
            observer.join(timeout=5)
        print("LIVE_BASELINE_AUTO_RETRY=NO", flush=True)
        print("LIVE_BASELINE_AUTO_CONFIRM=NO", flush=True)
        print("LIVE_BASELINE_RAW_PRD_PRINTED=NO", flush=True)
        print("LIVE_BASELINE_RAW_CODE_PRINTED=NO", flush=True)
        print("LIVE_BASELINE_API_KEY_PRINTED=NO", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
