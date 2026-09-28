"""Owner-only, zero-provider acceptance of the complete local Atlas preparation.

Prints only counts, fixed codes, the selected model identifier and identity digest.
Never authorizes a task, reads a credential, sends a model request or promotes a candidate.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

if os.environ.get("ANXINBOARD_OWNER_LOCAL_ATLAS_PREFLIGHT") != "YES":
    raise SystemExit("LOCAL_ATLAS_PREFLIGHT_GUARD_REQUIRED")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from app import brownfield_baseline as core
from app import brownfield_baseline_store as store
from app import project_profile_generation as profile
from app import project_state_baseline_api as baseline_api
from app.brownfield_responses_adapter import BrownfieldResponsesAdapter
from app.deepseek_live_profile_adapter import LiveDeepSeekProfileAdapter, PROFILE_TASK
from app.brownfield_spike_session import estimate_spike_wire_bytes

PROJECT_ID = 3
PROFILE_ID = 2
EXPECTED_HEAD = "32ccc3737361a8fd9298b97c24f5ed9c9c8ed9d6"
EXPECTED_MODULE_COUNT = 16


def main() -> int:
    blocked = {"credential": 0, "provider": 0, "mutation": 0}

    def forbid(kind):
        def reject(*args, **kwargs):
            blocked[kind] += 1
            raise AssertionError("LOCAL_ATLAS_PREFLIGHT_FORBIDDEN_OPERATION")
        return reject

    try:
        with patch.object(profile, "_read_provider_credential", forbid("credential")), \
             patch.object(BrownfieldResponsesAdapter, "execute_with_credential", forbid("provider")), \
             patch.object(LiveDeepSeekProfileAdapter, "execute_with_credential", forbid("provider")), \
             patch.object(LiveDeepSeekProfileAdapter, "execute", forbid("provider")), \
             patch.object(store, "create_task", forbid("mutation")), \
             patch.object(store, "claim_stage", forbid("mutation")), \
             patch.object(baseline_api, "_promote_generated_candidate", forbid("mutation")), \
             patch.object(core, "run_baseline", forbid("mutation")):
            initial = core.read_scope(PROJECT_ID, PROFILE_ID)
            if (initial.get("project_id"), initial.get("plan_profile_id"), initial.get("exact_head")) != (PROJECT_ID, PROFILE_ID, EXPECTED_HEAD):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_SCOPE_CHANGED")
            prepared = core.prepare_baseline(PROJECT_ID, PROFILE_ID)
            if prepared["scope"] != initial or core.read_scope(PROJECT_ID, PROFILE_ID) != initial:
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_SCOPE_CHANGED")
            summary = prepared["summary"]
            identity = prepared["identity"]
            indexed = prepared["indexed"]
            modules = prepared["plan"]["planned_modules"]
            cap = prepared["capability"]
            current_cap = prepared["adapter"].get_capability(task_type=PROFILE_TASK, output_schema_version=core.TRANSPORT_SCHEMA)
            if core.cap_identity(current_cap) != core.cap_identity(cap):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_MODEL_CHANGED")
            if len(modules) != EXPECTED_MODULE_COUNT or summary["module_count"] != EXPECTED_MODULE_COUNT:
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_MODULE_SCOPE_CHANGED")
            if indexed.get("complete_inventory") is not True or indexed["exact_head"] != EXPECTED_HEAD:
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_INVENTORY_INCOMPLETE")
            if summary["preflight_identity_hash"] != core.digest(identity):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_IDENTITY_INVALID")
            if summary.get("provider_calls") != 0 or summary.get("credential_read") is not False or any(blocked.values()):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_SIDE_EFFECT_BLOCKED")
            model = cap.model_id
            if type(model) is not str or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_MODEL_INVALID")
            requests = prepared["requests"]
            if not requests or len(requests) > identity["max_calls"]:
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_REQUEST_COUNT_INVALID")
            wires = [estimate_spike_wire_bytes(prepared["adapter"], cap, part["messages"], prepared["output"]) for part in requests]
            maximum = cap.context_window_tokens - prepared["output"] - core.SAFETY_MARGIN
            if any(size <= 0 or size > maximum for size in wires):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_REQUEST_OVER_BUDGET")
            stats = {
                "TRACKED_FILES": indexed["tracked_files"],
                "SAFE_PATHS": prepared["catalog"]["safe_path_count"],
                "SAFE_TEXT_BYTES": indexed["safe_text_bytes"],
                "EVIDENCE_COUNT": len(indexed["evidence"]),
                "RELATION_EDGES": len(prepared["atlas"]["edges"]),
                "METADATA_PATHS": sum(bool(any(item.get("metadata", {}).values())) for item in prepared["atlas"]["paths"]),
                "MODULE_COUNT": len(modules),
                "REQUIREMENT_COUNT": sum(len(module["requirements"]) for module in modules),
                "MAX_CALLS": identity["max_calls"],
                "ORIENTATION_REQUEST_COUNT": len(requests),
                "MAX_INPUT_BYTES": maximum,
            }
            coverage = indexed["coverage"]
            if (type(coverage) is not dict or any(type(name) is not str or not re.fullmatch(r"[a-z_]{1,64}", name) or type(value) is not int or value < 0 for name, value in coverage.items())
                    or sum(coverage.values()) != indexed["tracked_files"]):
                raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_COVERAGE_INVALID")
            for name, value in stats.items():
                if type(value) is not int or value < 0:
                    raise core.BaselineError("BROWNFIELD_LOCAL_PREFLIGHT_COUNT_INVALID")
            for name, value in stats.items():
                print(f"LOCAL_ATLAS_{name}={value}", flush=True)
            for name, value in sorted(coverage.items()):
                print(f"LOCAL_ATLAS_COVERAGE_{name.upper()}={value}", flush=True)
            print("LOCAL_ATLAS_MODEL=" + model, flush=True)
            print("LOCAL_ATLAS_PREFLIGHT_IDENTITY_HASH=" + summary["preflight_identity_hash"], flush=True)
            for number, wire in enumerate(wires):
                print(f"LOCAL_ATLAS_ORIENTATION_{number}_WIRE_BYTES={wire}", flush=True)
            print("LOCAL_ATLAS_COMPLETE_INVENTORY=YES", flush=True)
            print("LOCAL_ATLAS_COMPLETE_REPOSITORY_SEMANTIC_PROOF=NO", flush=True)
            print("LOCAL_ATLAS_PREFLIGHT=PASS", flush=True)
            return 0
    except Exception as exc:
        code = ("BROWNFIELD_LOCAL_PREFLIGHT_CREDENTIAL_ACCESS_BLOCKED" if blocked["credential"] else
                "BROWNFIELD_LOCAL_PREFLIGHT_PROVIDER_SEND_BLOCKED" if blocked["provider"] else
                "BROWNFIELD_LOCAL_PREFLIGHT_TASK_MUTATION_BLOCKED" if blocked["mutation"] else core.safe_code(exc))
        print("LOCAL_ATLAS_PREFLIGHT_ERROR_CODE=" + code, flush=True)
        print("LOCAL_ATLAS_PREFLIGHT=FAIL", flush=True)
        return 1
    finally:
        print("LOCAL_ATLAS_PROVIDER_CALLS=0", flush=True)
        print("LOCAL_ATLAS_API_KEY_READS=0", flush=True)
        print("LOCAL_ATLAS_TASKS_CREATED=0", flush=True)
        print("LOCAL_ATLAS_CANDIDATES_CREATED=0", flush=True)
        print("LOCAL_ATLAS_BLOCKED_PROVIDER_ATTEMPTS=" + str(blocked["provider"]), flush=True)
        print("LOCAL_ATLAS_BLOCKED_CREDENTIAL_ATTEMPTS=" + str(blocked["credential"]), flush=True)
        print("LOCAL_ATLAS_BLOCKED_MUTATION_ATTEMPTS=" + str(blocked["mutation"]), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
