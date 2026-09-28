from __future__ import annotations

"""One-shot owner-authorized brownfield Atlas architecture spike.

No product candidate is persisted. No automatic retry is performed. Logs contain only safe
counts/statuses; raw PRD, source, provider body and credentials are never printed.
"""

import collections
import os
import hashlib
import json
import re
from pathlib import Path
import sys

if os.environ.get("ANXINBOARD_OWNER_LIVE_ATLAS_SPIKE") != "YES":
    raise SystemExit("LIVE_SPIKE_GUARD_REQUIRED")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from fastapi import HTTPException

from app import project_profile_generation as core
from app.brownfield_atlas_spike import (
    SpikeContractError,
    aggregate_module_status,
    build_orientation_messages,
    build_gap_catalog,
    build_gap_orientation_messages,
    build_requirement_verification_messages,
    choose_spike_module_ids,
    validate_orientation_result,
    validate_requirement_result,
)
from app.deepseek_live_profile_adapter import PROFILE_TASK
from app.brownfield_responses_adapter import build_brownfield_responses_adapter
from app.brownfield_spike_session import SpikeSession, estimate_spike_wire_bytes
from app.db import get_db_path
from app import project_profile_bootstrap_context as workspace
from app.git_client import GitClient
from app.git_workspace_locks import project_workspace_lock
from app.project_profiles import read_current_confirmed_project_profile
from app.repository_atlas import AtlasError, build_evidence_bundle, build_provider_catalog, build_repository_atlas
from app.repository_index import build_indexed_repository_map

PROJECT_ID = 3
EXPECTED_HEAD = "32ccc3737361a8fd9298b97c24f5ed9c9c8ed9d6"
EXPECTED_PROFILE_ID = 2
EXPECTED_MODEL = "deepseek-flash"  # Same-model synthetic schema probe: run 35943633894.
TRANSPORT_SCHEMA = "project-profile-build/2.0"
MAX_OUTPUT_TOKENS = 4_000
SAFETY_MARGIN_BYTES = 16_384
MAX_EXPANDED_PATHS = 20
MAX_ORIENTATION_WIRE_BYTES = 800_000
MIN_SAFE_PATHS = 1_000
MIN_METADATA_PATHS = 300
MIN_RELATION_EDGES = 180


def safe_code(exc: HTTPException) -> str:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code")
    if type(code) is str and code and all(ch.isalnum() or ch in "_:-" for ch in code):
        return code
    return "HTTP_ERROR"


def read_scope():
    project, prd, git_state = core._read_current_inputs(PROJECT_ID)
    profile = read_current_confirmed_project_profile(PROJECT_ID)
    if (
        git_state.get("remote_head") != EXPECTED_HEAD
        or git_state.get("local_head") != EXPECTED_HEAD
        or profile.get("id") != EXPECTED_PROFILE_ID
        or profile.get("source_prd_id") != prd.get("id")
    ):
        raise SpikeContractError("SPIKE_SCOPE_CHANGED")
    return project, git_state, profile


def scope_identity():
    project, git_state, profile = read_scope()
    _, prd, _ = core._read_current_inputs(PROJECT_ID)
    with project_workspace_lock(PROJECT_ID, acquire_timeout=3.0):
        paths = workspace.resolve_workspace_paths(PROJECT_ID, '0' * 32, create=False)
        workspace.validate_workspace_paths(paths)
        access = workspace.open_workspace_access(paths, str(project['git_url']))
        try:
            client = GitClient()
            client.inspect_workspace(access)
            client.assert_clean(access)
            if client.get_local_head(access) != EXPECTED_HEAD:
                raise SpikeContractError('SPIKE_SCOPE_CHANGED')
        finally:
            access.close()
    return {
        "head": git_state.get("local_head"), "remote_head": git_state.get("remote_head"),
        "git_url": project.get("git_url"), "branch": project.get("branch"),
        "profile_id": profile["id"], "profile_hash": profile.get("content_hash"),
        "prd_id": prd.get("id"), "prd_hash": prd.get("source_hash"),
        "plan_hash": hashlib.sha256(json.dumps(profile["content"], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def main() -> int:
    session = None
    try:
        authorization = os.environ.get("ANXINBOARD_ATLAS_AUTHORIZATION_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", authorization):
            raise SpikeContractError("SPIKE_AUTHORIZATION_ID_REQUIRED")
        initial_scope = scope_identity()
        project, git_state, profile = read_scope()
        indexed = build_indexed_repository_map(PROJECT_ID, project, git_state)
        if indexed.get("exact_head") != EXPECTED_HEAD or indexed.get("complete_inventory") is not True:
            raise SpikeContractError("SPIKE_INDEX_INVALID")
        atlas = build_repository_atlas(indexed)
        catalog = build_provider_catalog(atlas)
        module_ids = choose_spike_module_ids(profile["content"])
        if len(module_ids) != 3:
            raise SpikeContractError("SPIKE_THREE_MODULES_REQUIRED")
        messages = build_orientation_messages(profile["content"], catalog, module_ids)
        live = build_brownfield_responses_adapter()
        capability = live.get_capability(task_type=PROFILE_TASK, output_schema_version=TRANSPORT_SCHEMA)
        if capability.model_id != EXPECTED_MODEL:
            raise SpikeContractError("SPIKE_MODEL_CHANGED")
        max_input = capability.context_window_tokens - MAX_OUTPUT_TOKENS - SAFETY_MARGIN_BYTES
        wire = estimate_spike_wire_bytes(live, capability, messages, MAX_OUTPUT_TOKENS)
        safe_paths = int(catalog.get("safe_path_count") or 0)
        metadata_paths = sum(1 for item in atlas["paths"] if any(item.get("metadata", {}).values()))
        edges = len(atlas["edges"])
        for name, value in dict(TRACKED_FILES=atlas["tracked_files"], SAFE_PATHS=safe_paths,
                                METADATA_PATHS=metadata_paths, RELATION_EDGES=edges,
                                ORIENTATION_WIRE_BYTES=wire, MAX_INPUT_BYTES=max_input,
                                SELECTED_MODULES=len(module_ids)).items():
            print("LIVE_ATLAS_" + name + "=" + str(value), flush=True)
        if safe_paths < MIN_SAFE_PATHS or metadata_paths < MIN_METADATA_PATHS or edges < MIN_RELATION_EDGES:
            raise SpikeContractError("SPIKE_LOCAL_MAP_QUALITY_INSUFFICIENT")
        if wire <= 0 or wire > min(max_input, MAX_ORIENTATION_WIRE_BYTES):
            raise SpikeContractError("SPIKE_ORIENTATION_OVER_BUDGET")
        if scope_identity() != initial_scope:
            raise SpikeContractError("SPIKE_SCOPE_CHANGED")
        session = SpikeSession(live, capability, scope_identity, core._read_provider_credential,
                               get_db_path().parent / "atlas-spike-claims" / authorization,
                               max_output=MAX_OUTPUT_TOKENS, expected_scope=initial_scope)
        receipt = session.send("orientation", messages)
        seeds = validate_orientation_result(receipt.result, catalog=catalog, module_ids=module_ids)
        print("LIVE_ATLAS_ORIENTATION_VALID=YES", flush=True)
        module_by_id = {m["client_id"]: m for m in profile["content"]["planned_modules"]}
        checked = {identity: [] for identity in module_ids}
        verified_rounds = {identity: [] for identity in module_ids}
        nonempty = sum(bool(seeds[identity]) for identity in module_ids)
        verification_calls = 0
        for round_index in range(2):
            if round_index:
                globally_checked = sorted(set().union(*(set(values) for values in checked.values())))
                print("LIVE_ATLAS_GAP_REMAINING_SAFE_PATHS=" + str(safe_paths - len(globally_checked)), flush=True)
                if len(globally_checked) == safe_paths:
                    seeds = {identity: [] for identity in module_ids}
                    print("LIVE_ATLAS_GAP_ORIENTATION_VALID=LOCAL_EMPTY", flush=True)
                else:
                    gap_catalog = build_gap_catalog(catalog, module_ids, checked)
                    gap_messages = build_gap_orientation_messages(profile["content"], catalog, module_ids, checked)
                    gap_receipt = session.send("gap-orientation", gap_messages)
                    seeds = validate_orientation_result(gap_receipt.result, catalog=gap_catalog, module_ids=module_ids)
                    if any(set(values).intersection(globally_checked) for values in seeds.values()):
                        raise SpikeContractError("SPIKE_ORIENTATION_PATH_RESELECTED")
                    print("LIVE_ATLAS_GAP_ORIENTATION_VALID=YES", flush=True)
            for slot, identity in enumerate(module_ids):
                selected = seeds[identity]
                print(f"LIVE_ATLAS_ROUND_{round_index}_MODULE_{slot}_SEEDS={len(selected)}", flush=True)
                if not selected:
                    continue
                bundle = build_evidence_bundle(indexed, atlas, selected, max_hops=1, max_paths=MAX_EXPANDED_PATHS)
                if not bundle["evidence_count"]:
                    raise SpikeContractError("SPIKE_EVIDENCE_EMPTY")
                for name, value in dict(EXPANDED_PATHS=len(bundle["selected_path_ids"]),
                                       OMITTED_NEIGHBORS=len(bundle["omitted_neighbor_path_ids"]),
                                       EVIDENCE_COUNT=bundle["evidence_count"], SOURCE_BYTES=bundle["safe_text_bytes"]).items():
                    print(f"LIVE_ATLAS_ROUND_{round_index}_MODULE_{slot}_{name}={value}", flush=True)
                verify_messages = build_requirement_verification_messages(module_by_id[identity], bundle)
                print(f"LIVE_ATLAS_ROUND_{round_index}_MODULE_{slot}_EXPECTED_REQUIREMENTS={len(module_by_id[identity]['requirements'])}", flush=True)
                receipt = session.send(f"verify-{round_index}-{slot}", verify_messages)
                returned = receipt.result.get('requirements') if isinstance(receipt.result, dict) else None
                print(f"LIVE_ATLAS_ROUND_{round_index}_MODULE_{slot}_RETURNED_REQUIREMENTS={len(returned) if isinstance(returned, list) else -1}", flush=True)
                verified = validate_requirement_result(receipt.result, module=module_by_id[identity], bundle=bundle)
                verified_rounds[identity].append(verified)
                verification_calls += 1
                checked[identity] = sorted(set(checked[identity]) | set(bundle["selected_path_ids"]))
                counts = collections.Counter(item["status"] for item in verified)
                for status in ("implemented", "partial", "unknown"):
                    print(f"LIVE_ATLAS_ROUND_{round_index}_MODULE_{slot}_REQ_{status.upper()}={counts[status]}", flush=True)
                print(f"LIVE_ATLAS_ROUND_{round_index}_MODULE_{slot}_PROMPT_TOKENS={receipt.prompt_tokens}", flush=True)
        positive = sum(any(item["status"] in {"partial", "implemented"} for round_results in verified_rounds[identity]
                           for item in round_results if item["requirement_index"] == index)
                       for identity in module_ids for index in range(len(module_by_id[identity]["requirements"])))
        explained = set().union(*(set(value) for value in checked.values()))
        session.check_scope()
        print("LIVE_ATLAS_NONEMPTY_MODULES=" + str(nonempty))
        print("LIVE_ATLAS_VERIFICATION_CALLS=" + str(verification_calls))
        print("LIVE_ATLAS_POSITIVE_REQUIREMENTS=" + str(positive))
        print("LIVE_ATLAS_EXPLAINED_PATHS=" + str(len(explained)))
        print("LIVE_ATLAS_UNEXPLAINED_SAFE_PATHS=" + str(safe_paths - len(explained)))
        print("LIVE_ATLAS_GAP_CHECK_COMPLETED=YES")
        print("LIVE_ATLAS_COMPLETE_REPOSITORY_SEMANTIC_PROOF=NO")
        print("LIVE_ATLAS_AUTO_CONFIRM=NO")
        print("LIVE_ATLAS_PRODUCT_CANDIDATE_CREATED=NO")
        if verification_calls < 2 or nonempty < 2 or positive < 1:
            raise SpikeContractError("SPIKE_ARCHITECTURE_PROOF_INSUFFICIENT")
        print("LIVE_ATLAS_ARCHITECTURE_PROOF=PASS")
        return 0
    except HTTPException as exc:
        print("LIVE_ATLAS_PROVIDER_ERROR=" + safe_code(exc))
        print("LIVE_ATLAS_AUTORETRY=NO")
        return 91 if safe_code(exc) == "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN" else 90
    except (SpikeContractError, AtlasError) as exc:
        code = str(exc)
        print("LIVE_ATLAS_CONTRACT_ERROR=" + (code if re.fullmatch(r"[A-Z0-9_]+", code) else type(exc).__name__))
        return 92
    except Exception as exc:
        print("LIVE_ATLAS_UNEXPECTED_ERROR_CLASS=" + type(exc).__name__)
        return 93
    finally:
        print("LIVE_ATLAS_PROVIDER_DISPATCH_ATTEMPTS=" + str(session.attempts if session else 0), flush=True)
        print("LIVE_ATLAS_RAW_PRD_PRINTED=NO")
        print("LIVE_ATLAS_RAW_CODE_PRINTED=NO")
        print("LIVE_ATLAS_API_KEY_PRINTED=NO")


if __name__ == "__main__":
    raise SystemExit(main())
