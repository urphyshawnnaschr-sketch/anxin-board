import hashlib
import json
import sqlite3
from fastapi import HTTPException
import pytest
from app.model_provider_contract import ProviderCapability, ProviderReceipt
from app.profile_reconciliation_batches import BatchError, plan_reconciliation_batches
from app.profile_reconciliation_provider import MAX_OUTPUT_TOKENS, OUTPUT_SCHEMA_VERSION, SAFETY_MARGIN_TOKENS, TASK_TYPE, _plan_wire_closed_batches, aggregate_authorized_candidate, build_batch_messages, build_execution_plan, dispatch_authorized_batch, ensure_provider_schema, public_preflight_summary, record_authorization
HEAD = 'a' * 40
PRD_HASH = 'b' * 64
PLAN_HASH = 'c' * 64
BUDGET = dict(context_window_tokens=40000, max_output_tokens=2000, reserved_output_tokens=2000, safety_margin_tokens=1000, counting_policy_version='utf8_byte_upper_bound_v1')
PLAN = {'schema_version': 'project_profile_v2', 'project_summary': 'safe', 'planned_modules': [{'client_id': 'one', 'name': 'invoice billing', 'description': '', 'requirements': ['invoice'], 'prd_refs': ['prd-a'], 'exclusions': []}, {'client_id': 'two', 'name': 'account login', 'description': '', 'requirements': ['login'], 'prd_refs': ['prd-a'], 'exclusions': []}], 'implementation_mappings': [], 'unplanned_code_features': [], 'domain_glossary': [], 'exclude_patterns': [], 'notes': ''}

def evidence(identity, text):
    return {'evidence_id': 'repo-code-' + identity, 'path': identity + '.py', 'content': text, 'content_hash': hashlib.sha256(text.encode()).hexdigest(), 'exact_head': HEAD}

def batches():
    return plan_reconciliation_batches(PLAN, [evidence('invoice', 'def invoice(): pass'), evidence('login', 'def login(): pass')], exact_head=HEAD, plan_profile_id=42, budget_record=BUDGET)

class FakeAdapter:
    def __init__(self, *, model='deepseek-test', context=1000000, wire=None, failure=None, interrupt=False):
        self.model = model; self.context = context; self.wire = wire; self.failure = failure; self.interrupt = interrupt; self.calls = 0; self.estimates = 0
    def get_capability(self, *, task_type, output_schema_version):
        assert (task_type, output_schema_version) == (TASK_TYPE, OUTPUT_SCHEMA_VERSION)
        return ProviderCapability(provider='deepseek', model_id=self.model, model_version=self.model, task_type=TASK_TYPE, output_schema_version=OUTPUT_SCHEMA_VERSION, context_window_tokens=self.context, max_output_tokens=24000)
    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens):
        self.estimates += 1
        if self.wire is not None: return self.wire
        return len(json.dumps({'messages': [dict(m) for m in messages], 'max_tokens': max_output_tokens}, ensure_ascii=False).encode())
    def execute_with_credential(self, request, credential):
        self.calls += 1; assert credential == 'secret-value'
        if self.interrupt: raise KeyboardInterrupt()
        if self.failure is not None: raise self.failure
        batch = json.loads(request.messages[1]['content'])['batch']; mappings=[]
        for module in batch['planned_modules']:
            refs=[item['evidence_id'] for item in batch['repo_evidence']]
            mappings.append({'planned_module_id': module['client_id'], 'status': 'implemented' if refs else 'unknown', 'evidence_ids': refs[:1], 'rationale': 'semantic match' if refs else 'no supporting evidence'})
        return ProviderReceipt(provider='deepseek', provider_response_id='resp-' + str(self.calls), actual_model=self.model, provider_runtime_fingerprint='fake-fingerprint', finish_reason='stop', prompt_tokens=100, completion_tokens=20, total_tokens=120, result={'mappings': mappings})

def state(model='deepseek-test'):
    return {'exact_head': HEAD, 'prd_id': 7, 'prd_source_hash': PRD_HASH, 'plan_profile_id': 42, 'plan_content_hash': PLAN_HASH, 'provider': 'deepseek', 'model_id': model, 'model_version': model}

def authorize(conn, plan, *, nonce='auth-nonce-0001'):
    return record_authorization(conn, execution_plan=plan, project_id=9, prd_id=7, prd_source_hash=PRD_HASH, plan_content_hash=PLAN_HASH, authorization_nonce=nonce, authorized=True, authorized_at='2026-09-16T00:00:00+00:00')

def test_wire_admission_uses_adapter_estimator_and_preflight_has_no_credential_or_provider_call():
    adapter=FakeAdapter(); plan=build_execution_plan(batches(), adapter=adapter); summary=public_preflight_summary(plan)
    assert adapter.estimates == len(plan['requests']) and adapter.calls == 0
    assert summary['provider_calls'] == 0 and summary['credential_read'] is False
    assert all(request['request_hash'] and request['wire_bytes'] > 0 for request in summary['requests'])
    assert summary['max_output_tokens'] == MAX_OUTPUT_TOKENS

def test_actual_wire_budget_rejects_over_limit_before_authorization():
    max_input=30000-MAX_OUTPUT_TOKENS-SAFETY_MARGIN_TOKENS; assert max_input>0; adapter=FakeAdapter(context=30000,wire=max_input+1)
    with pytest.raises(BatchError,match='PROVIDER_REQUEST_OVER_BUDGET'): build_execution_plan(batches(),adapter=adapter)
    assert adapter.calls==0


def test_product_planning_recloses_against_final_wire_size_by_splitting_batches():
    class ExpandedWireAdapter(FakeAdapter):
        def estimate_request_utf8_bytes(self, *, messages, max_output_tokens):
            self.estimates += 1
            raw = len(json.dumps({'messages': [dict(m) for m in messages], 'max_tokens': max_output_tokens}, ensure_ascii=False).encode())
            return raw * 2
    adapter=ExpandedWireAdapter(context=60000)
    cap=adapter.get_capability(task_type=TASK_TYPE,output_schema_version=OUTPUT_SCHEMA_VERSION)
    budget=dict(context_window_tokens=cap.context_window_tokens,max_output_tokens=MAX_OUTPUT_TOKENS,reserved_output_tokens=MAX_OUTPUT_TOKENS,safety_margin_tokens=SAFETY_MARGIN_TOKENS,counting_policy_version='utf8_byte_upper_bound_v1')
    repo=[evidence('large', 'def existing_feature(): return True\n' * 1200)]
    naive=plan_reconciliation_batches(PLAN,repo,exact_head=HEAD,plan_profile_id=42,budget_record=budget)
    with pytest.raises(BatchError,match='PROVIDER_REQUEST_OVER_BUDGET'):
        build_execution_plan(naive,adapter=ExpandedWireAdapter(context=60000))
    closed=_plan_wire_closed_batches(PLAN,repo,exact_head=HEAD,plan_profile_id=42,budget_record=budget,repository_coverage={},adapter=adapter,capability=cap,max_output_tokens=MAX_OUTPUT_TOKENS)
    final=build_execution_plan(closed,adapter=adapter)
    assert len(closed)>len(naive)
    assert final['requests']
    assert all(request['wire_bytes']<=final['max_input_bytes'] for request in final['requests'])
    assert adapter.calls==0

def test_authorization_is_idempotent_for_same_nonce_and_refuses_scope_reuse():
    conn=sqlite3.connect(':memory:'); plan=build_execution_plan(batches(),adapter=FakeAdapter()); first=authorize(conn,plan); second=authorize(conn,plan)
    assert first['authorization_hash']==second['authorization_hash']
    changed=build_execution_plan(batches(),adapter=FakeAdapter(model='deepseek-other'))
    with pytest.raises(BatchError,match='AUTHORIZATION_NONCE_SCOPE_CONFLICT'): authorize(conn,changed)
    with pytest.raises(BatchError,match='AUTHORIZATION_NONCE_SCOPE_CONFLICT'):
        record_authorization(conn,execution_plan=plan,project_id=9,prd_id=7,prd_source_hash='d'*64,plan_content_hash=PLAN_HASH,authorization_nonce='auth-nonce-0001',authorized=True,authorized_at='2026-09-16T00:00:00+00:00')
    tampered=dict(plan); tampered['total_wire_bytes']+=1
    with pytest.raises(BatchError,match='EXECUTION_IDENTITY_MISMATCH'): authorize(sqlite3.connect(':memory:'),tampered,nonce='auth-nonce-tamper')

def test_scope_drift_stops_before_credential_read_and_claim():
    conn=sqlite3.connect(':memory:'); adapter=FakeAdapter(); bs=batches(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan); reads=[]; bad=state(); bad['exact_head']='d'*40
    with pytest.raises(BatchError,match='AUTHORIZED_SCOPE_DRIFT'):
        dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:reads.append(1) or 'secret-value',current_state=lambda:bad)
    assert reads==[] and conn.execute('SELECT count(*) FROM profile_reconciliation_provider_claims').fetchone()[0]==0

def test_scope_is_revalidated_after_credential_read_before_claim():
    conn=sqlite3.connect(':memory:'); adapter=FakeAdapter(); bs=batches(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan,nonce='auth-nonce-window'); calls={'state':0}
    def changing_state():
        calls['state']+=1; value=state()
        if calls['state']>=2:value['exact_head']='e'*40
        return value
    with pytest.raises(BatchError,match='AUTHORIZED_SCOPE_DRIFT'):
        dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=changing_state)
    assert conn.execute('SELECT count(*) FROM profile_reconciliation_provider_claims').fetchone()[0]==0 and adapter.calls==0

def test_success_is_at_most_once_and_secret_is_never_persisted():
    conn=sqlite3.connect(':memory:'); adapter=FakeAdapter(); bs=batches(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan)
    first=dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state)
    second=dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:pytest.fail('credential must not be reread'),current_state=state)
    assert first['status']==second['status']=='succeeded' and adapter.calls==1
    dump=' '.join(str(row) for table in ('profile_reconciliation_provider_authorizations','profile_reconciliation_provider_claims','profile_reconciliation_provider_results') for row in conn.execute(f'SELECT * FROM {table}').fetchall())
    assert 'secret-value' not in dump

def test_network_unknown_and_interrupted_claim_never_auto_resend():
    for adapter,interrupt in [(FakeAdapter(failure=HTTPException(status_code=502,detail={'code':'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN','message':'private transport details'})),False),(FakeAdapter(interrupt=True),True)]:
        conn=sqlite3.connect(':memory:'); bs=batches(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan,nonce='auth-nonce-'+('int' if interrupt else 'net'))
        if interrupt:
            with pytest.raises(KeyboardInterrupt): dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state)
        else:
            first=dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state); assert first['status']=='unknown'
            assert 'private transport details' not in str(conn.execute('SELECT * FROM profile_reconciliation_provider_results').fetchall())
        replay=dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:pytest.fail('must not reread'),current_state=state)
        assert replay['status']=='unknown' and adapter.calls==1

def test_explicit_new_authorization_can_retry_after_known_pre_send_failure():
    conn=sqlite3.connect(':memory:'); bs=batches(); bad=FakeAdapter(failure=HTTPException(status_code=409,detail={'code':'PROFILE_GENERATION_MODEL_PREFLIGHT_FAILED','message':'safe'})); plan=build_execution_plan(bs,adapter=bad); auth1=authorize(conn,plan,nonce='auth-nonce-fail')
    assert dispatch_authorized_batch(conn,bs[0],authorization_hash=auth1['authorization_hash'],adapter=bad,credential_reader=lambda:'secret-value',current_state=state)['status']=='failed_pre_send'
    good=FakeAdapter(); plan2=build_execution_plan(bs,adapter=good); auth2=authorize(conn,plan2,nonce='auth-nonce-retry')
    assert dispatch_authorized_batch(conn,bs[0],authorization_hash=auth2['authorization_hash'],adapter=good,credential_reader=lambda:'secret-value',current_state=state)['status']=='succeeded'

def test_provider_result_wrapper_cross_batch_and_model_identity_fail_closed():
    class BadResult(FakeAdapter):
        def execute_with_credential(self,request,credential):
            self.calls+=1; return ProviderReceipt(provider='deepseek',provider_response_id='r',actual_model=self.model,provider_runtime_fingerprint='fp',finish_reason='stop',prompt_tokens=1,completion_tokens=1,total_tokens=2,result={'mappings':[{'planned_module_id':'invented','status':'partial','evidence_ids':['repo-code-outside'],'rationale':'invalid outside evidence'}]})
    conn=sqlite3.connect(':memory:'); bs=batches(); adapter=BadResult(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan,nonce='auth-nonce-badmap')
    out=dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state)
    assert out['status']=='failed_after_send' and out['error_code']=='PROVIDER_RESULT_MAPPING_INVALID'
    conn2=sqlite3.connect(':memory:'); adapter2=FakeAdapter(); plan2=build_execution_plan(bs,adapter=adapter2); auth2=authorize(conn2,plan2,nonce='auth-nonce-model'); adapter2.model='deepseek-drift'
    with pytest.raises(BatchError,match='AUTHORIZED_SCOPE_DRIFT'):
        dispatch_authorized_batch(conn2,bs[0],authorization_hash=auth2['authorization_hash'],adapter=adapter2,credential_reader=lambda:pytest.fail('no credential'),current_state=state)

def test_full_success_uses_frozen_aggregator_and_candidate_stays_isolated():
    conn=sqlite3.connect(':memory:'); conn.execute('CREATE TABLE project_profiles(id INTEGER PRIMARY KEY, marker TEXT)'); conn.execute("INSERT INTO project_profiles VALUES(1,'untouched')"); conn.commit(); bs=batches(); adapter=FakeAdapter(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan,nonce='auth-nonce-full')
    for batch in bs:
        out=dispatch_authorized_batch(conn,batch,authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state); assert out['status']=='succeeded'
    candidate=aggregate_authorized_candidate(conn,bs,PLAN,authorization_hash=auth['authorization_hash'],current_state=state)
    assert candidate['status']=='candidate' and {m['planned_module_id'] for m in candidate['content']['implementation_mappings']}=={'one','two'}
    assert all(m['status']!='implemented' for m in candidate['content']['implementation_mappings']) and conn.execute('SELECT marker FROM project_profiles WHERE id=1').fetchone()[0]=='untouched'


def test_durable_aggregated_candidate_replay_is_idempotent_with_sqlite_row_factory():
    conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
    bs=batches(); adapter=FakeAdapter(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan,nonce='auth-nonce-candidate-replay')
    for batch in bs:
        assert dispatch_authorized_batch(conn,batch,authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state)['status']=='succeeded'
    first=aggregate_authorized_candidate(conn,bs,PLAN,authorization_hash=auth['authorization_hash'],current_state=state)
    provider_calls=adapter.calls
    second=aggregate_authorized_candidate(conn,bs,PLAN,authorization_hash=auth['authorization_hash'],current_state=state)
    assert second==first
    assert adapter.calls==provider_calls
    assert conn.execute('SELECT count(*) FROM profile_reconciliation_provider_candidates').fetchone()[0]==1

def test_smoke_only_authorization_cannot_be_promoted_to_full_candidate():
    large = 'def full_coverage_probe(): pass\n' * 8000
    bs = plan_reconciliation_batches(PLAN, [evidence('large', large)], exact_head=HEAD, plan_profile_id=42, budget_record=BUDGET)
    assert len(bs) > 1
    conn=sqlite3.connect(':memory:'); adapter=FakeAdapter(); plan=build_execution_plan(bs,adapter=adapter,selected_batch_indexes=[0]); auth=authorize(conn,plan,nonce='auth-nonce-smoke')
    dispatch_authorized_batch(conn,bs[0],authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state)
    with pytest.raises(BatchError,match='FULL_BATCH_SET_NOT_AUTHORIZED'): aggregate_authorized_candidate(conn,bs,PLAN,authorization_hash=auth['authorization_hash'],current_state=state)

def test_all_unknown_full_code_result_is_rejected_as_empty_analysis():
    class UnknownAdapter(FakeAdapter):
        def execute_with_credential(self, request, credential):
            self.calls += 1
            batch = json.loads(request.messages[1]['content'])['batch']
            mappings = [{'planned_module_id': m['client_id'], 'status': 'unknown', 'evidence_ids': [], 'rationale': 'reviewed supplied evidence but found no support'} for m in batch['planned_modules']]
            return ProviderReceipt(provider='deepseek', provider_response_id='resp-empty', actual_model=self.model, provider_runtime_fingerprint='fp', finish_reason='stop', prompt_tokens=100, completion_tokens=20, total_tokens=120, result={'mappings': mappings})
    conn=sqlite3.connect(':memory:'); adapter=UnknownAdapter(); bs=batches(); plan=build_execution_plan(bs,adapter=adapter); auth=authorize(conn,plan,nonce='auth-nonce-empty-analysis')
    for batch in bs:
        assert dispatch_authorized_batch(conn,batch,authorization_hash=auth['authorization_hash'],adapter=adapter,credential_reader=lambda:'secret-value',current_state=state)['status']=='succeeded'
    with pytest.raises(BatchError, match='ANALYSIS_EMPTY_NO_CODE_EVIDENCE'):
        aggregate_authorized_candidate(conn,bs,PLAN,authorization_hash=auth['authorization_hash'],current_state=state)


def test_schema_is_append_only():
    conn=sqlite3.connect(':memory:'); ensure_provider_schema(conn); plan=build_execution_plan(batches(),adapter=FakeAdapter()); auth=authorize(conn,plan,nonce='auth-nonce-immut')
    with pytest.raises(sqlite3.IntegrityError): conn.execute("UPDATE profile_reconciliation_provider_authorizations SET payload_json='{}' WHERE authorization_hash=?",(auth['authorization_hash'],))
def test_model_visible_batch_hides_project_global_module_ids():
    expanded=json.loads(json.dumps(PLAN))
    expanded['planned_modules'].extend([
        {'client_id':'three','name':'audit log','description':'','requirements':['audit'],'prd_refs':['prd-a'],'exclusions':[]},
        {'client_id':'four','name':'device groups','description':'','requirements':['device'],'prd_refs':['prd-a'],'exclusions':[]},
    ])
    bs=plan_reconciliation_batches(expanded,[evidence('invoice','def invoice(): pass'),evidence('login','def login(): pass')],exact_head=HEAD,plan_profile_id=42,budget_record=BUDGET)
    assert len(bs)>1
    first=bs[0]
    assert set(first['all_module_ids'])=={'one','two','three','four'}
    visible=json.loads(build_batch_messages(first)[1]['content'])['batch']
    assert 'all_module_ids' not in visible
    current={m['client_id'] for m in first['planned_modules']}
    assert current and len(current)<=3
    assert current < set(first['all_module_ids'])
    serialized=json.dumps(visible,ensure_ascii=False)
    for hidden in set(first['all_module_ids'])-current:
        assert hidden not in serialized

