"""Recoverable Atlas baseline execution; local inventory precedes every authorization.

Only validated stage results are durable. Exact-source requests are reconstructed from
the frozen repository and plan; the request hash binds each cached result to its bundle.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException

from app import brownfield_baseline_store as store
from app.brownfield_atlas_spike import (
    build_orientation_messages, build_gap_catalog,
    build_requirement_verification_messages, validate_orientation_result,
    validate_requirement_result,
)
from app.brownfield_baseline_aggregate import aggregate_baseline
from app.brownfield_strict_adapter import build_brownfield_strict_adapter, TRANSPORT_POLICY, normalization_metadata
from app.brownfield_strict_wire import WIRE_REASON_RULES, ROW_KEY_SHAPES
from app.brownfield_spike_session import estimate_spike_wire_bytes
from app.context_redaction import _redact_text
from app.context_token_framing import COUNTING_POLICY_VERSION
from app.deepseek_live_profile_adapter import PROFILE_TASK
from app.model_provider_contract import ProviderCredentialRequest
from app.project_profile_v2 import ProjectProfileV2Content
from app.repository_atlas import build_repository_atlas, build_provider_catalog, build_evidence_bundle
from app.repository_index import build_indexed_repository_map

VERSION = 'brownfield-baseline/1'
TRANSPORT_SCHEMA = 'project-profile-build/2.0'
SAFETY_MARGIN = 16384
MAX_OUTPUT = 32000
RATIONALE_LIMIT = 1200
OUTPUT_ROW_BUDGET = 4 * RATIONALE_LIMIT + 256
OUTPUT_FIXED_BUDGET = 256
MAX_PATHS = 20
MAX_CALLS = 512


class BaselineError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def cap_identity(cap):
    return {k: getattr(cap, k) for k in ('provider', 'model_id', 'model_version', 'context_window_tokens', 'max_output_tokens')}


def safe_code(exc):
    if isinstance(exc, HTTPException):
        code = exc.detail.get('code') if isinstance(exc.detail, dict) else None
    elif isinstance(exc, ValueError):
        code = str(exc)
    else:
        code = None
    if isinstance(code, str) and re.fullmatch(r'(?:BROWNFIELD_|BASELINE_|SPIKE_|ATLAS_|PROFILE_GENERATION_|PROFILE_RECONCILIATION_|ANALYSIS_EMPTY_|CANDIDATE_|CONTEXT_REDACTION_)[A-Z0-9_]{1,140}', code):
        return code
    return 'BROWNFIELD_RUNTIME_FAILED'


def safe_diagnostic(exc):
    detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
    source = detail.get('diagnostic')
    if not isinstance(source, dict):
        return {}
    from app.deepseek_live_profile_adapter import profile_http_diagnostic
    result = profile_http_diagnostic(source.get('provider_http_status'))
    for key in ('content_utf8_bytes', 'chunk_count', 'output_tokens', 'json_error_line', 'json_error_column', 'json_error_offset', 'tool_call_count', 'tool_call_ordinal', 'expected_row_count',
                'row_ordinal', 'slot_ordinal', 'missing_row_count', 'extra_row_count', 'missing_field_count', 'extra_field_count', 'accepted_row_count', 'numeric_row_key_count', 'wrapper_key_count', 'other_key_count'):
        value = source.get(key)
        if type(value) is int and 0 <= value <= 2**31-1:
            result[key] = value
    if type(source.get('output_limit_reached')) is bool:
        result['output_limit_reached'] = source['output_limit_reached']
    reason = source.get('wire_reason')
    if type(reason) is str and reason in WIRE_REASON_RULES:
        result['wire_reason'] = reason
        rule = source.get('wire_rule')
        if type(rule) is str and rule in WIRE_REASON_RULES[reason]:
            result['wire_rule'] = rule
    shape = source.get('row_key_shape')
    if (result.get('wire_reason') == 'ROWS_INVALID' and result.get('wire_rule') == 'EXACT_KEYS'
            and type(shape) is str and shape in ROW_KEY_SHAPES):
        result['row_key_shape'] = shape
    return result


def _safe(value):
    if isinstance(value, str):
        return _redact_text(value, include_assignments=True)[0]
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


def read_scope(project_id, plan_profile_id):
    from app import project_profile_generation as profile
    from app import project_profile_bootstrap_context as workspace
    from app.project_profiles import read_current_confirmed_project_profile
    from app.git_workspace_locks import project_workspace_lock
    from app.git_client import GitClient
    project, prd, git = profile._read_current_inputs(project_id)
    confirmed = read_current_confirmed_project_profile(project_id)
    if confirmed['id'] != plan_profile_id or confirmed['source_prd_id'] != prd['id'] or git.get('local_head') != git.get('remote_head'):
        raise BaselineError('BROWNFIELD_SCOPE_CHANGED')
    with project_workspace_lock(project_id, acquire_timeout=3.0):
        paths = workspace.resolve_workspace_paths(project_id, '0'*32, create=False)
        workspace.validate_workspace_paths(paths)
        access = workspace.open_workspace_access(paths, str(project['git_url']))
        try:
            client = GitClient()
            client.inspect_workspace(access)
            client.assert_clean(access)
            if client.get_local_head(access) != git['remote_head']:
                raise BaselineError('BROWNFIELD_SCOPE_CHANGED')
        finally:
            access.close()
    return dict(project_id=project_id, plan_profile_id=plan_profile_id,
                plan_content_hash=confirmed['content_hash'], prd_id=prd['id'], prd_source_hash=prd['source_hash'],
                git_url=project['git_url'], git_branch=project['branch'], exact_head=git['remote_head'])


def prepare_baseline(project_id, plan_profile_id):
    from app import project_profile_generation as profile
    from app.project_profiles import read_current_confirmed_project_profile
    scope = read_scope(project_id, plan_profile_id)
    project, _prd, git = profile._read_current_inputs(project_id)
    confirmed = read_current_confirmed_project_profile(project_id)
    indexed = build_indexed_repository_map(project_id, project, git)
    prepared = prepare_from_inputs(scope=scope, plan=confirmed['content'], indexed=indexed, adapter=build_brownfield_strict_adapter())
    if read_scope(project_id, plan_profile_id) != scope:
        raise BaselineError('BROWNFIELD_SCOPE_CHANGED')
    return prepared


def _bounded_messages(messages):
    """Bound explanation length so output reserve can account for every row."""
    messages = deepcopy(list(messages))
    payload = json.loads(messages[-1]['content'])
    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == 'rationale' and isinstance(v, dict) and v.get('type') == 'string':
                    v['maxLength'] = RATIONALE_LIMIT
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(payload.get('required_output_schema'))
    messages[-1]['content'] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return tuple(messages)


def _fits(messages, adapter, cap, output, rows):
    # Reuse the report lane's conservative UTF-8 byte upper-bound policy. Each
    # response row reserves four UTF-8 bytes per rationale character plus framing.
    return (OUTPUT_FIXED_BUDGET + rows*OUTPUT_ROW_BUDGET <= output and
            estimate_spike_wire_bytes(adapter, cap, messages, output) <= cap.context_window_tokens-output-SAFETY_MARGIN)


def orientation_requests(plan, catalog, module_ids, adapter, cap, output):
    if not catalog['paths'] or not module_ids:
        return []
    if len(module_ids) > 1:
        return [piece for module_id in module_ids for piece in orientation_requests(plan, catalog, [module_id], adapter, cap, output)]
    messages = _bounded_messages(build_orientation_messages(plan, catalog, module_ids))
    if _fits(messages, adapter, cap, output, len(module_ids)):
        return [{'catalog': catalog, 'module_ids': module_ids, 'messages': messages}]
    if len(catalog['paths']) > 1:
        # Partition the entire catalog, not top-k truncation. A path remains visible
        # in one shard; index resolution is against that exact hashed shard.
        mid = len(catalog['paths'])//2
        result = []
        for paths in (catalog['paths'][:mid], catalog['paths'][mid:]):
            subset = deepcopy(catalog)
            kept = {p['path_id'] for p in paths}
            subset['paths'] = deepcopy(paths)
            for path in subset['paths']:
                neighbors = path.get('neighbors', [])
                path['neighbors'] = [n for n in neighbors if n['path_id'] in kept]
                path['excluded_neighbor_count'] = path.get('excluded_neighbor_count', 0) + len(neighbors)-len(path['neighbors'])
            subset['parent_catalog_hash'] = catalog['catalog_hash']
            subset['safe_path_count'] = len(paths)
            subset['catalog_hash'] = digest({k:v for k,v in subset.items() if k != 'catalog_hash'})
            result.extend(orientation_requests(plan, subset, module_ids, adapter, cap, output))
        return result
    raise BaselineError('BROWNFIELD_ORIENTATION_ITEM_OVER_BUDGET')


def verification_requests(module, bundle, adapter, cap, output, indexes=None):
    indexes = list(range(len(module['requirements']))) if indexes is None else indexes
    if not indexes:
        return []
    if len(indexes) > 1:
        return [piece for index in indexes for piece in verification_requests(module, bundle, adapter, cap, output, [index])]
    part = deepcopy(module)
    part['requirements'] = [module['requirements'][i] for i in indexes]
    messages = _bounded_messages(build_requirement_verification_messages(part, bundle))
    if _fits(messages, adapter, cap, output, len(indexes)):
        return [{'module': part, 'indexes': indexes, 'bundle': bundle, 'messages': messages}]
    if len(bundle['evidence']) > 1:
        result = []
        mid = len(bundle['evidence'])//2
        for evidence in (bundle['evidence'][:mid], bundle['evidence'][mid:]):
            part_bundle = deepcopy(bundle)
            part_bundle.update(evidence=deepcopy(evidence), evidence_count=len(evidence), safe_text_bytes=sum(len(e['content'].encode()) for e in evidence),
                               selected_paths=sorted({e['path'] for e in evidence}), parent_bundle_hash=bundle['bundle_hash'],
                               source_partition=True, complete_for_selected_paths=False,
                               excluded_fragment_count=len(bundle['evidence'])-len(evidence))
            part_bundle['bundle_hash'] = digest({k:v for k,v in part_bundle.items() if k != 'bundle_hash'})
            result.extend(verification_requests(module, part_bundle, adapter, cap, output, indexes))
        return result
    raise BaselineError('BROWNFIELD_SOURCE_FRAGMENT_OVER_BUDGET')


def prepare_from_inputs(*, scope, plan, indexed, adapter):
    plan = ProjectProfileV2Content.model_validate(plan).model_dump()
    if not plan['planned_modules'] or indexed.get('complete_inventory') is not True or indexed['exact_head'] != scope['exact_head']:
        raise BaselineError('BROWNFIELD_PREPARATION_INVALID')
    # Original confirmed plan remains local; only a redacted copy enters requests.
    model_plan = _safe(plan)
    cap = adapter.get_capability(task_type=PROFILE_TASK, output_schema_version=TRANSPORT_SCHEMA)
    output = min(MAX_OUTPUT, cap.max_output_tokens)
    if output < OUTPUT_FIXED_BUDGET + OUTPUT_ROW_BUDGET or cap.context_window_tokens <= output+SAFETY_MARGIN:
        raise BaselineError('BROWNFIELD_MODEL_BUDGET_INVALID')
    atlas = build_repository_atlas(indexed)
    catalog = build_provider_catalog(atlas)
    if not catalog['paths']:
        raise BaselineError('BROWNFIELD_NO_SAFE_SOURCE')
    module_ids = [m['client_id'] for m in plan['planned_modules']]
    requests = orientation_requests(model_plan, catalog, module_ids, adapter, cap, output)
    # The bound is disclosed, frozen and enforced. Dynamic source splitting can
    # consume it, but cannot silently increase a user's authorized maximum.
    requirement_count = sum(len(m['requirements']) for m in plan['planned_modules'])
    max_calls = min(MAX_CALLS, 2*len(requests) + 4*max(requirement_count, len(module_ids)))
    if 2*len(requests) >= MAX_CALLS:
        raise BaselineError('BROWNFIELD_ORIENTATION_CALL_LIMIT')
    identity = dict(schema_version=VERSION, **scope, atlas_hash=atlas['atlas_hash'], catalog_hash=catalog['catalog_hash'],
                    transport_policy=deepcopy(TRANSPORT_POLICY),
                    capability=cap_identity(cap), module_ids=module_ids, max_calls=max_calls, max_output_tokens=output,
                    safety_margin=SAFETY_MARGIN, counting_policy=COUNTING_POLICY_VERSION, max_paths=MAX_PATHS, rounds=2,
                    rationale_limit=RATIONALE_LIMIT, output_row_budget=OUTPUT_ROW_BUDGET, output_fixed_budget=OUTPUT_FIXED_BUDGET,
                    orientation_requests=[digest(r['messages']) for r in requests])
    summary = dict(preflight_identity_hash=digest(identity), **scope, model_id=cap.model_id,
                   module_count=len(module_ids), max_calls=max_calls, tracked_files=indexed['tracked_files'],
                   safe_text_bytes=indexed['safe_text_bytes'], coverage=indexed['coverage'],
                   provider_calls=0, credential_read=False, orientation_request_count=len(requests),
                   complete_repository_semantic_proof=False)
    return dict(identity=identity, scope=scope, plan=plan, model_plan=model_plan, indexed=indexed, atlas=atlas,
                catalog=catalog, adapter=adapter, capability=cap, output=output, requests=requests, summary=summary)


def run_baseline(prepared, task, *, connection_factory, scope_reader, credential_reader, promote):
    """A single worker runs this; DB claims additionally exclude cross-process sends."""
    task_id = task['task_id']
    adapter, cap = prepared['adapter'], prepared['capability']
    key = None
    active_stage = None
    received = False

    def db(fn, *args, **kwargs):
        with closing(connection_factory()) as conn:
            return fn(conn, *args, **kwargs)

    def scope_check():
        actual = adapter.get_capability(task_type=PROFILE_TASK, output_schema_version=TRANSPORT_SCHEMA)
        if scope_reader() != prepared['scope'] or cap_identity(actual) != cap_identity(cap):
            raise BaselineError('BROWNFIELD_SCOPE_CHANGED')

    def send(stage, messages, validate):
        nonlocal key, active_stage, received
        scope_check()
        wire = estimate_spike_wire_bytes(adapter, cap, messages, prepared['output'])
        if wire > cap.context_window_tokens-prepared['output']-SAFETY_MARGIN:
            raise BaselineError('BROWNFIELD_REQUEST_OVER_BUDGET')
        input_hash = digest({'identity': task['identity_hash'], 'stage': stage, 'messages': messages})
        old = db(store.get_stage, task_id, stage)
        if old:
            if (old['input_hash'], old['wire_bytes']) != (input_hash, wire):
                raise BaselineError('BROWNFIELD_STAGE_INPUT_MISMATCH')
            if old['status'] != 'succeeded':
                raise BaselineError('BROWNFIELD_UNRESOLVED_CLAIM')
            return old['result']['value']
        reused = db(store.reuse_successful_stage, task_id=task_id, stage_key=stage, input_hash=input_hash, wire_bytes=wire)
        if reused is not None:
            return reused['result']['value']
        if key is None:
            key = credential_reader()
            if type(key) is not str or not key.strip():
                raise BaselineError('BROWNFIELD_CREDENTIAL_MISSING')
        scope_check()
        claim = db(store.claim_stage, task_id=task_id, stage_key=stage, input_hash=input_hash, wire_bytes=wire)
        if not claim['claimed_now']:
            return claim['result']['value']
        active_stage, received = stage, False
        request = ProviderCredentialRequest(local_task_id=task_id, provider=cap.provider, model_id=cap.model_id,
                    model_version=cap.model_version, task_type=PROFILE_TASK, output_schema_version=TRANSPORT_SCHEMA,
                    messages=messages, max_output_tokens=prepared['output'])
        receipt = adapter.execute_with_credential(request, key)
        received = True
        scope_check()
        def bound_rationale(value):
            if isinstance(value, dict):
                if isinstance(value.get('rationale'), str) and len(value['rationale'].strip()) > RATIONALE_LIMIT:
                    raise BaselineError('BROWNFIELD_RESPONSE_RATIONALE_LIMIT')
                for child in value.values():
                    bound_rationale(child)
            elif isinstance(value, list):
                for child in value:
                    bound_rationale(child)
        bound_rationale(receipt.result)
        value = _safe(validate(receipt.result))
        stage_result = {'value': value, 'prompt_tokens': receipt.prompt_tokens, 'completion_tokens': receipt.completion_tokens}
        normalization = normalization_metadata(receipt)
        if normalization:
            stage_result['collector_normalization'] = normalization
        db(store.finish_stage, task_id=task_id, stage_key=stage, status='succeeded', result=stage_result)
        active_stage = None
        return value

    current = db(store.get_task, task_id)
    if current['status'] not in {'queued', 'running'}:
        return
    try:
        if current['identity'] != prepared['identity']:
            raise BaselineError('BROWNFIELD_SCOPE_CHANGED')
        scope_check()
        # Restart with an unresolved claim cannot infer whether a provider received it.
        if any(s['status'] == 'claimed' for s in db(store.list_stages, task_id)):
            raise BaselineError('BROWNFIELD_UNRESOLVED_CLAIM')
        saved_record = db(store.get_output, task_id)
        saved = saved_record['output'] if saved_record else None
        if saved is None:
            modules = prepared['model_plan']['planned_modules']
            ids = [m['client_id'] for m in modules]
            rounds = {i: [] for i in ids}
            checked = {i: [] for i in ids}
            omitted = set()
            for round_index in range(2):
                catalog = prepared['catalog'] if round_index == 0 else build_gap_catalog(prepared['catalog'], ids, checked)
                requests = orientation_requests(prepared['model_plan'], catalog, ids, adapter, cap, prepared['output'])
                seeds = {i: [] for i in ids}
                for number, part in enumerate(requests):
                    found = send(f'orientation/{round_index}/{number}', part['messages'],
                                 lambda result, p=part: validate_orientation_result(result, catalog=p['catalog'], module_ids=p['module_ids']))
                    for identity, paths in found.items():
                        seeds[identity] = sorted(set(seeds[identity]) | set(paths))
                for module in modules:
                    identity = module['client_id']
                    if not module['requirements']:
                        continue
                    selected = seeds[identity]
                    # Several catalog shards may yield more than twelve leads. Every
                    # selected lead gets a bundle rather than truncating the union.
                    for seed_slot in range(0, len(selected), MAX_PATHS):
                        bundle = build_evidence_bundle(prepared['indexed'], prepared['atlas'], selected[seed_slot:seed_slot+MAX_PATHS], max_hops=1, max_paths=MAX_PATHS)
                        pieces = verification_requests(module, bundle, adapter, cap, prepared['output'])
                        for part_index, piece in enumerate(pieces):
                            rows = send(f'verify/{round_index}/{identity}/{seed_slot}/{part_index}', piece['messages'],
                                        lambda result, p=piece: validate_requirement_result(result, module=p['module'], bundle=p['bundle']))
                            global_rows = [dict(row, requirement_index=piece['indexes'][row['requirement_index']]) for row in rows]
                            rounds[identity].append({'requirements': global_rows, 'bundle_hash': piece['bundle']['bundle_hash'],
                                                     'allowed_evidence_ids': [e['evidence_id'] for e in piece['bundle']['evidence']]})
                        checked[identity] = sorted(set(checked[identity]) | set(bundle['selected_path_ids']))
                        omitted.update(bundle['omitted_neighbor_path_ids'])
            # No credible lead is an explicit unknown, never not_started. Keep all
            # requirements represented even if the model found no relevant path.
            for module in modules:
                identity = module['client_id']
                if module['requirements'] and not rounds[identity]:
                    rounds[identity].append({'requirements': [{'requirement_index': n, 'status': 'unknown', 'evidence_ids': [], 'rationale': '两轮检索没有足够代码证据。'} for n in range(len(module['requirements']))],
                                             'bundle_hash': digest({'empty': identity, 'identity': task['identity_hash']}), 'allowed_evidence_ids': []})
            by_id = {e['evidence_id']: e for e in prepared['indexed']['evidence']}
            saved = aggregate_baseline(prepared['plan'], exact_head=prepared['scope']['exact_head'], rounds=rounds, evidence=by_id)
            inspected = set().union(*(set(v) for v in checked.values()))
            saved['coverage'] = {'tracked_files': prepared['atlas']['tracked_files'], 'safe_paths': prepared['catalog']['safe_path_count'],
                                 'inspected_paths': len(inspected), 'unexplained_safe_paths': prepared['catalog']['safe_path_count']-len(inspected),
                                 'omitted_neighbor_paths': len(omitted-inspected), 'gap_check_completed': True,
                                 'complete_repository_semantic_proof': False, 'local_coverage': prepared['indexed']['coverage']}
            scope_check()
            db(store.save_output, task_id, output=saved)
        scope_check()
        try:
            candidate = promote(saved['content'])
            db(store.finish_task, task_id, status='succeeded', profile_id=candidate['id'])
        except Exception:
            # No new model work is needed. Keep validated output resumable if the
            # local candidate transaction cannot commit (including a process crash).
            try:
                db(store.set_local_error, task_id, 'BROWNFIELD_CANDIDATE_SAVE_FAILED')
            except Exception:
                # A unavailable DB must not transform recoverable local finalization
                # into a new model authorization. Durable output still identifies it.
                pass
            return
    except Exception as exc:
        code = safe_code(exc)
        if active_stage is None and (isinstance(exc, sqlite3.Error) or code == 'BROWNFIELD_STORE_DATABASE_CONFLICT'):
            # Successful stage records can be replayed locally after a transient
            # output-store failure; do not require a new paid authorization.
            try:
                db(store.set_local_error, task_id, 'BROWNFIELD_LOCAL_STORAGE_UNAVAILABLE')
            except Exception:
                pass
            return
        ambiguous = code in {'BROWNFIELD_UNRESOLVED_CLAIM', 'BROWNFIELD_STORE_STAGE_ALREADY_CLAIMED', 'BROWNFIELD_STORE_STAGE_IN_FLIGHT', 'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN'}
        if active_stage is not None:
            known_response = received or (isinstance(exc, HTTPException) and code not in {'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN', 'BROWNFIELD_RUNTIME_FAILED'})
            status = 'failed_after_send' if known_response else 'unknown'
            try:
                diagnostic = safe_diagnostic(exc)
                db(store.finish_stage, task_id=task_id, stage_key=active_stage, status=status,
                   result={'diagnostic': diagnostic} if diagnostic else None, error_code=code)
            except Exception:
                status = 'unknown'
        else:
            status = 'unknown' if ambiguous else 'failed_pre_send'
        db(store.finish_task, task_id, status=status, error_code=code)
