"""Product orchestration with synthetic code/model only; no credential store/network."""
import json
import hashlib
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_brownfield_atlas_spike import indexed, plan
from app import brownfield_baseline as core
from app import brownfield_baseline_store as store


class Model:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.cap = SimpleNamespace(provider='deepseek', model_id='deepseek-flash', model_version='deepseek-flash', context_window_tokens=1000000, max_output_tokens=8000)

    def get_capability(self, **kw):
        return self.cap

    def estimate_request_bytes(self, **kw):
        return len(json.dumps(kw, ensure_ascii=False).encode())

    def execute_with_credential(self, req, key):
        self.calls.append(req)
        if self.fail:
            raise self.fail
        p = json.loads(req.messages[-1]['content'])
        if 'repository_atlas' in p:
            result = {'modules': [{'planned_module_id': m['client_id'], 'seed_path_indexes': [0]} for m in p['planned_modules']]}
        else:
            result = {'requirements': [{'requirement_index': n, 'status': 'partial', 'evidence_indexes': [0], 'rationale': 'supported'} for n in range(len(p['planned_module']['requirements']))]}
        return SimpleNamespace(result=result, actual_model=self.cap.model_id, prompt_tokens=2, completion_tokens=3, total_tokens=5)


def setup(tmp_path, model=None):
    model = model or Model()
    scope = {'project_id': 1, 'plan_profile_id': 2, 'plan_content_hash': 'b'*64, 'prd_id': 3, 'prd_source_hash': 'c'*64, 'git_url': 'https://example.test/repo.git', 'git_branch': 'main', 'exact_head': 'a'*40}
    prepared = core.prepare_from_inputs(scope=scope, plan=plan(), indexed=indexed(), adapter=model)
    def connection():
        conn = sqlite3.connect(tmp_path/'ledger.db')
        conn.row_factory = sqlite3.Row
        return conn
    with connection() as conn:
        store.ensure_schema(conn)
        task = store.create_task(conn, project_id=1, authorization_nonce='synthetic-nonce', identity=prepared['identity'], max_calls=prepared['summary']['max_calls'])
    return model, prepared, task, connection


def test_local_preparation_and_all_modules_candidate(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    assert model.calls == []
    assert prepared['summary']['provider_calls'] == 0
    assert prepared['summary']['module_count'] == 4
    assert prepared['summary']['credential_read'] is False
    promoted = []
    core.run_baseline(prepared, task, connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda content: promoted.append(content) or {'id': 9})
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'succeeded'
    assert len(promoted[0]['implementation_mappings']) == 4
    assert all(m['status'] == 'partial' for m in promoted[0]['implementation_mappings'])
    assert len(model.calls) > 0


@pytest.mark.parametrize('failure', [HTTPException(502, detail={'code':'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN'}), RuntimeError('must not leak this body')])
def test_ambiguous_send_persisted_and_never_retried(tmp_path, failure):
    model, prepared, task, connect = setup(tmp_path)
    model.fail = failure
    args = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda _: pytest.fail('no promotion'))
    core.run_baseline(prepared, task, **args)
    core.run_baseline(prepared, task, **args)
    assert len(model.calls) == 1
    with connect() as conn:
        state = store.get_task(conn, task['task_id'])
        assert state['status'] == 'unknown'
        assert 'must not leak' not in json.dumps(state)


def test_scope_drift_before_key_is_known_pre_send(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    core.run_baseline(prepared, task, connection_factory=connect, scope_reader=lambda: {}, credential_reader=lambda: pytest.fail('key read'), promote=lambda _: pytest.fail('promotion'))
    assert not model.calls
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'failed_pre_send'


def test_orphan_claim_stops_before_credentials(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    with connect() as conn:
        store.claim_stage(conn, task_id=task['task_id'], stage_key='orientation/0/0', input_hash='d'*64, wire_bytes=1)
    core.run_baseline(prepared, task, connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: pytest.fail('key read'), promote=lambda _: pytest.fail('promotion'))
    assert not model.calls
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'unknown'


def test_restart_between_successful_stages_reuses_result(tmp_path, monkeypatch):
    model, prepared, task, connect = setup(tmp_path)
    original = store.finish_stage
    stopped = []
    def saved_then_exit(conn, **kwargs):
        value = original(conn, **kwargs)
        if not stopped:
            stopped.append(True)
            raise SystemExit('synthetic process stop after result commit')
        return value
    monkeypatch.setattr(store, 'finish_stage', saved_then_exit)
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda _: {'id': 9})
    with pytest.raises(SystemExit):
        core.run_baseline(prepared, task, **common)
    first_messages = model.calls[0].messages
    core.run_baseline(prepared, task, **common)
    assert sum(req.messages == first_messages for req in model.calls) == 1
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'succeeded'


def test_returned_foreign_evidence_fails_known_without_retry(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    original = model.execute_with_credential
    def invalid(req, key):
        receipt = original(req, key)
        if 'requirements' in receipt.result:
            receipt.result['requirements'][0]['evidence_indexes'] = [99999]
        return receipt
    model.execute_with_credential = invalid
    core.run_baseline(prepared, task, connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda _: pytest.fail('promotion'))
    with connect() as conn:
        result = store.get_task(conn, task['task_id'])
        assert result['status'] == 'failed_after_send'
        assert result['error_code'] == 'SPIKE_VERIFICATION_EVIDENCE_INVALID_OUT_OF_RANGE'
    assert len(model.calls) == len(prepared['requests']) + 1


def test_cached_success_reused_after_local_promotion_failure(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic')
    def unavailable(_):
        raise sqlite3.OperationalError('busy')
    core.run_baseline(prepared, task, promote=unavailable, **common)
    calls = len(model.calls)
    core.run_baseline(prepared, task, promote=lambda _: {'id': 9}, **common)
    assert len(model.calls) == calls
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'succeeded'


def test_crash_between_candidate_and_final_status_recovers_without_send(tmp_path, monkeypatch):
    model, prepared, task, connect = setup(tmp_path)
    original = store.finish_task
    failures = []
    def once(conn, task_id, **kwargs):
        if kwargs['status'] == 'succeeded' and not failures:
            failures.append(True)
            raise sqlite3.OperationalError('transient')
        return original(conn, task_id, **kwargs)
    monkeypatch.setattr(store, 'finish_task', once)
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda _: {'id': 9})
    core.run_baseline(prepared, task, **common)
    calls = len(model.calls)
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'running'
    core.run_baseline(prepared, task, **common)
    assert len(model.calls) == calls
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'succeeded'


def test_verification_splits_before_dispatch_without_dropping_evidence():
    model = Model()
    model.cap.context_window_tokens = 34000
    source = indexed()
    source['evidence'][0]['content'] = 'x'*5000
    source['evidence'][1]['content'] = 'y'*5000
    for e in source['evidence']:
        e['content_hash'] = hashlib.sha256(e['content'].encode()).hexdigest()
    from app.repository_atlas import build_repository_atlas, build_evidence_bundle
    atlas = build_repository_atlas(source)
    bundle = build_evidence_bundle(source, atlas, [p['path_id'] for p in atlas['paths']])
    pieces = core.verification_requests(plan()['planned_modules'][0], bundle, model, model.cap, 8000)
    assert len(pieces) > 1
    assert {e['evidence_id'] for p in pieces for e in p['bundle']['evidence']} == {e['evidence_id'] for e in bundle['evidence']}
    assert model.calls == []


@pytest.mark.parametrize('count', [12, 25, 70])
def test_local_preflight_module_count_is_dynamic(count):
    sample = plan()
    sample['planned_modules'] = [dict(deepcopy(sample['planned_modules'][0]), client_id=f'module_{n}') for n in range(count)]
    model = Model()
    prepared = core.prepare_from_inputs(scope={'exact_head':'a'*40}, plan=sample, indexed=indexed(), adapter=model)
    assert prepared['summary']['module_count'] == count
    assert {m for r in prepared['requests'] for m in r['module_ids']} == {m['client_id'] for m in sample['planned_modules']}
    assert model.calls == []


def test_output_persistence_failure_resumes_without_new_requests(tmp_path, monkeypatch):
    model, prepared, task, connect = setup(tmp_path)
    original = store.save_output
    failed = []
    def once(conn, task_id, **kwargs):
        if not failed:
            failed.append(True)
            raise sqlite3.OperationalError('transient')
        return original(conn, task_id, **kwargs)
    monkeypatch.setattr(store, 'save_output', once)
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda _: {'id': 9})
    core.run_baseline(prepared, task, **common)
    calls = len(model.calls)
    core.run_baseline(prepared, task, **common)
    assert len(model.calls) == calls
    with connect() as conn:
        assert store.get_task(conn, task['task_id'])['status'] == 'succeeded'


def test_new_explicit_authorization_reuses_success_before_known_failure(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    original = model.execute_with_credential
    def fail_second(req, key):
        if len(model.calls) == 1:
            model.calls.append(req)
            raise HTTPException(502, detail={'code': 'PROFILE_GENERATION_RESPONSES_CONTENT_JSON_INVALID', 'diagnostic': {'content_utf8_bytes': 72, 'output_tokens': 30, 'raw_body': 'do-not-persist'}})
        return original(req, key)
    model.execute_with_credential = fail_second
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic', promote=lambda _: {'id': 9})
    core.run_baseline(prepared, task, **common)
    first_messages = model.calls[0].messages
    with connect() as conn:
        failed = store.get_stage(conn, task['task_id'], 'orientation/0/1')
        assert failed['result'] == {'diagnostic': {'content_utf8_bytes': 72, 'output_tokens': 30}}
        renewed = store.create_task(conn, project_id=1, authorization_nonce='new-explicit-owner-action', identity=prepared['identity'], max_calls=prepared['summary']['max_calls'])
    model.execute_with_credential = original
    core.run_baseline(prepared, renewed, **common)
    assert sum(req.messages == first_messages for req in model.calls) == 1
    with connect() as conn:
        assert store.get_task(conn, renewed['task_id'])['status'] == 'succeeded'
        stage = store.get_stage(conn, renewed['task_id'], 'orientation/0/0')
        assert stage['result']['reused_from_task_id'] == task['task_id']


def test_sampling_policy_is_frozen_in_authorization_identity(tmp_path, monkeypatch):
    model, prepared, task, connect = setup(tmp_path)
    assert prepared['identity']['transport_policy']['temperature'] == 0
    monkeypatch.setattr(core, 'TRANSPORT_POLICY', dict(core.TRANSPORT_POLICY, temperature=1))
    changed = core.prepare_from_inputs(scope=prepared['scope'], plan=plan(), indexed=indexed(), adapter=model)
    assert changed['summary']['preflight_identity_hash'] != prepared['summary']['preflight_identity_hash']
    assert model.calls == []


def test_collector_conflict_persisted_without_retry_or_content(tmp_path):
    model, prepared, task, connect = setup(tmp_path)
    model.fail = HTTPException(502, detail={'code': 'PROFILE_GENERATION_STRICT_ARGUMENT_CONFLICT',
        'diagnostic': {'tool_call_count': 4, 'tool_call_ordinal': 2, 'field': 'private-marker', 'raw_body': 'private-marker'}})
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'],
                  credential_reader=lambda: 'synthetic', promote=lambda _: pytest.fail('No candidate from conflict'))
    core.run_baseline(prepared, task, **common)
    core.run_baseline(prepared, task, **common)
    assert len(model.calls) == 1
    with connect() as conn:
        final = store.get_task(conn, task['task_id'])
        stage = store.get_stage(conn, task['task_id'], 'orientation/0/0')
    assert final['status'] == stage['status'] == 'failed_after_send'
    assert final['error_code'] == 'PROFILE_GENERATION_STRICT_ARGUMENT_CONFLICT'
    assert stage['result'] == {'diagnostic': {'tool_call_count': 4, 'tool_call_ordinal': 2}}
    assert 'private-marker' not in json.dumps(final) + json.dumps(stage)


@pytest.mark.parametrize('policy_key', ['collector_json_policy', 'collector_calls_policy', 'format'])
def test_collector_policy_is_frozen_in_authorization_identity(tmp_path, monkeypatch, policy_key):
    model, prepared, task, connect = setup(tmp_path)
    assert prepared['identity']['transport_policy']['collector_json_policy'] == 'identical-typed-duplicates/1'
    monkeypatch.setattr(core, 'TRANSPORT_POLICY', dict(core.TRANSPORT_POLICY, **{policy_key: 'future-policy'}))
    changed = core.prepare_from_inputs(scope=prepared['scope'], plan=plan(), indexed=indexed(), adapter=model)
    assert changed['summary']['preflight_identity_hash'] != prepared['summary']['preflight_identity_hash']
    assert model.calls == []


def test_collector_normalization_audit_survives_durable_reuse(tmp_path):
    from app.brownfield_strict_adapter import CollectorReceipt
    model, prepared, task, connect = setup(tmp_path)
    original = model.execute_with_credential
    def normalized_then_fail(req, key):
        if len(model.calls) == 1:
            model.calls.append(req)
            raise HTTPException(502, detail={'code': 'PROFILE_GENERATION_STRICT_ARGUMENT_CONFLICT'})
        receipt = original(req, key)
        row_count = len(receipt.result.get('modules', receipt.result.get('requirements', [])))
        return CollectorReceipt('deepseek', 'synthetic-response', 'deepseek-flash', 'synthetic',
                                'stop', 2, 3, 5, receipt.result, 2, 1, 3, 2,
                                row_occurrences_received=3 * row_count, duplicate_rows_removed=2 * row_count)
    model.execute_with_credential = normalized_then_fail
    common = dict(connection_factory=connect, scope_reader=lambda: prepared['scope'],
                  credential_reader=lambda: 'synthetic', promote=lambda _: {'id': 9})
    core.run_baseline(prepared, task, **common)
    with connect() as conn:
        saved = store.get_stage(conn, task['task_id'], 'orientation/0/0')['result']['collector_normalization']
        assert saved == {'policy': 'identical-typed-duplicates/1', 'duplicate_fields_removed': 2,
                         'tool_calls_normalized': 1, 'calls_received': 3, 'calls_collapsed': 2,
                         'row_occurrences_received': 3, 'duplicate_rows_removed': 2, 'unique_rows': 1,
                         'calls_policy': 'identical-complete-single-result/1'}
        renewed = store.create_task(conn, project_id=1, authorization_nonce='explicit-new-test-authorization',
                                    identity=prepared['identity'], max_calls=prepared['summary']['max_calls'])
    model.execute_with_credential = original
    core.run_baseline(prepared, renewed, **common)
    with connect() as conn:
        reused = store.get_stage(conn, renewed['task_id'], 'orientation/0/0')['result']
        assert reused['reused_from_task_id'] == task['task_id']
        assert reused['collector_normalization'] == saved
    assert sum(req.messages == model.calls[0].messages for req in model.calls) == 1


@pytest.mark.parametrize('length', [241, 800, 1200, 1201])
@pytest.mark.parametrize('stage_type', ['verification'])
def test_original_rationale_contract_is_preserved(tmp_path, length, stage_type):
    model, prepared, task, connect = setup(tmp_path)
    original = model.execute_with_credential
    def with_rationale(req, key):
        receipt = original(req, key)
        row_key = 'modules' if stage_type == 'orientation' else 'requirements'
        for row in receipt.result.get(row_key, []):
            row['rationale'] = '中' * length
        return receipt
    model.execute_with_credential = with_rationale
    promoted = []
    core.run_baseline(prepared, task, connection_factory=connect,
        scope_reader=lambda: prepared['scope'], credential_reader=lambda: 'synthetic',
        promote=lambda content: promoted.append(content) or {'id': 9})
    with connect() as conn:
        final = store.get_task(conn, task['task_id'])
    assert final['status'] == ('succeeded' if length <= 1200 else 'failed_after_send')
    assert bool(promoted) is (length <= 1200)
    if length > 1200:
        assert final['error_code'] == 'BROWNFIELD_RESPONSE_RATIONALE_LIMIT'
    for req in model.calls:
        payload = json.loads(req.messages[-1]['content'])
        if 'repository_atlas' in payload:
            assert 'rationale' not in payload['required_output_schema']['properties']['modules']['items']['properties']
            continue
        rows = payload['required_output_schema']['properties']
        schema = next(iter(rows.values()))['items']['properties']['rationale']
        assert schema['maxLength'] == 1200
        assert 'hard 240' not in req.messages[0]['content']
        assert '1200' in req.messages[0]['content']


def test_output_reserve_splits_rows_and_freezes_policy():
    model = Model()
    prepared = core.prepare_from_inputs(scope={'exact_head': 'a'*40}, plan=plan(), indexed=indexed(), adapter=model)
    assert prepared['output'] == 8000
    assert len(prepared['requests']) == 4
    assert all(len(r['module_ids']) == 1 for r in prepared['requests'])
    assert prepared['identity']['rationale_limit'] == 1200
    assert prepared['identity']['output_row_budget'] == 5056
    assert prepared['identity']['output_fixed_budget'] == 256
    messages = prepared['requests'][0]['messages']
    assert core._fits(messages, model, model.cap, 5312, 1)
    assert not core._fits(messages, model, model.cap, 5311, 1)
    assert not core._fits(messages, model, model.cap, 8000, 2)
    module = deepcopy(plan()['planned_modules'][0])
    module['requirements'] = ['one', 'two', 'three']
    bundle = core.build_evidence_bundle(prepared['indexed'], prepared['atlas'], [prepared['atlas']['paths'][0]['path_id']])
    pieces = core.verification_requests(module, bundle, model, model.cap, 8000)
    assert [p['indexes'] for p in pieces] == [[0], [1], [2]]
    model.cap.max_output_tokens = 64000
    larger = core.prepare_from_inputs(scope={'exact_head': 'a'*40}, plan=plan(), indexed=indexed(), adapter=model)
    assert larger['output'] == 32000
    assert len(larger['requests']) == 4
    assert all(len(r['module_ids']) == 1 for r in larger['requests'])
    assert not model.calls


def test_single_binding_call_limit_stops_without_partial_candidate(tmp_path):
    model, prepared, _, connect = setup(tmp_path)
    prepared['identity']['max_calls']=1
    with connect() as conn:
        task=store.create_task(conn,project_id=1,authorization_nonce='bounded-one-call',identity=prepared['identity'],max_calls=1)
    core.run_baseline(prepared,task,connection_factory=connect,scope_reader=lambda:prepared['scope'],credential_reader=lambda:'synthetic',promote=lambda _:pytest.fail('No partial candidate'))
    with connect() as conn:
        saved=store.get_task(conn,task['task_id'])
    assert saved['status']=='failed_pre_send'
    assert saved['error_code']=='BROWNFIELD_STORE_CALL_BUDGET_EXHAUSTED'
    assert len(model.calls)==1


@pytest.mark.parametrize("old_format,old_collector", [
    ("strict-chat-fixed-batch/5", "exact-key-complete-row-union/1"),
    ("strict-chat-single-binding/6", "identical-complete-single-result/1"),
])
def test_new_single_binding_identity_never_reuses_old_result(tmp_path, old_format, old_collector):
    model, prepared, initial, connect = setup(tmp_path)
    with connect() as conn:
        store.finish_task(conn,initial['task_id'],status='failed_pre_send',error_code='SYNTHETIC_PRE_SEND')
    old=deepcopy(prepared['identity'])
    old['transport_policy']['format']=old_format
    old['transport_policy']['collector_calls_policy']=old_collector
    with connect() as conn:
        prior=store.create_task(conn,project_id=1,authorization_nonce='old-policy',identity=old,max_calls=old['max_calls'])
        store.claim_stage(conn,task_id=prior['task_id'],stage_key='orientation/0/0',input_hash='a'*64,wire_bytes=1)
        store.finish_stage(conn,task_id=prior['task_id'],stage_key='orientation/0/0',status='succeeded',result={'value':{}})
        store.finish_task(conn,prior['task_id'],status='failed_after_send',error_code='PROFILE_GENERATION_STRICT_WIRE_INVALID')
        current=store.create_task(conn,project_id=1,authorization_nonce='new-v7',identity=prepared['identity'],max_calls=prepared['identity']['max_calls'])
        assert prior['identity_hash']!=current['identity_hash']
        assert store.reuse_successful_stage(conn,task_id=current['task_id'],stage_key='orientation/0/0',input_hash='a'*64,wire_bytes=1) is None
