from copy import deepcopy
import hashlib,json
import pytest
from app import approved_report_narrative as n


def digest(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def fixture(regenerate=False):
    content=dict(plain_summary="今天完成输入检查；完整联调尚待验证。",feature_progress=[],code_change_summary=[],test_evidence=[],risks=[],unknown_items=[],source_warnings=[])
    result={'new_report':content,'correction_trace':[dict(reason='保留未知说明',handled=True,evidence_ids=['e1'])]} if regenerate else content
    approval=dict(project_id=1,approval_snapshot_id=2,approval_snapshot_hash='a'*64,report_version_id=3,report_content_hash=digest(result),model_execution_result_id=4,execution_result_hash='b'*64,human_acknowledged=True)
    report=dict(schema_version='anxin_board_report_v3',**{k:v for k,v in approval.items() if k not in ('project_id',)})
    report['anxin_board_report_hash']=digest(report)
    raw=dict(task_type='daily_report_regenerate' if regenerate else 'daily_report_generate',content=result,validated_result_hash=digest(result),model_execution_result_id=4,execution_result_hash='b'*64)
    return dict(project_id=1,report=report,approval_snapshot=approval,ai_raw=raw)


@pytest.mark.parametrize('regen',[False,True])
def test_original_content_empty_arrays_and_regenerate_roundtrip(regen):
    args=fixture(regen);before=deepcopy(args);value=n.build_approved_report_narrative(**args)
    assert value['content']['plain_summary']=='今天完成输入检查；完整联调尚待验证。'
    assert value['content']['risks']==[] and value['content']['unknown_items']==[]
    assert args==before
    assert n.validate_approved_report_narrative(value,report=args['report'],approval_snapshot=args['approval_snapshot'])==value


@pytest.mark.parametrize('field',['project_id','report_version_id','model_execution_result_id','execution_result_hash','report_content_hash'])
def test_cross_identity_rejected(field):
    args=fixture();args['approval_snapshot'][field]=99 if field.endswith('_id') else 'c'*64
    with pytest.raises(n.ApprovedReportNarrativeError):n.build_approved_report_narrative(**args)


def test_rehash_of_content_or_full_source_cannot_forge_approved_text():
    args=fixture();value=n.build_approved_report_narrative(**args)
    for target in ['content','source_result']:
        bad=deepcopy(value);bad[target]['plain_summary']='虚构今日全部完成';bad['narrative_hash']=digest({k:v for k,v in bad.items() if k!='narrative_hash'})
        with pytest.raises(n.ApprovedReportNarrativeError):n.validate_approved_report_narrative(bad,report=args['report'],approval_snapshot=args['approval_snapshot'])


@pytest.mark.parametrize('mutate',['secret','extra','baseline'])
def test_unsafe_or_wrong_source_never_becomes_empty_success(mutate):
    args=fixture()
    if mutate=='secret':args['ai_raw']['content']['plain_summary']='api_key=synthetic-secret-value'
    if mutate=='extra':args['ai_raw']['content']['unexpected']='x'
    if mutate=='baseline':args['ai_raw']['task_type']='project_state_baseline'
    args['ai_raw']['validated_result_hash']=digest(args['ai_raw']['content'])
    args['approval_snapshot']['report_content_hash']=args['ai_raw']['validated_result_hash']
    args['report']['report_content_hash']=args['ai_raw']['validated_result_hash']
    args['report']['anxin_board_report_hash']=digest({k:v for k,v in args['report'].items() if k!='anxin_board_report_hash'})
    with pytest.raises(n.ApprovedReportNarrativeError) as exc:n.build_approved_report_narrative(**args)
    assert 'synthetic-secret' not in str(exc.value)


def test_ordinary_credential_words_are_not_secrets():
    args=fixture();text='今天补充配置密码、API Key 和 Token 的帮助说明；不包含任何凭据值。'
    args['ai_raw']['content']['plain_summary']=text
    h=digest(args['ai_raw']['content']);args['ai_raw']['validated_result_hash']=h
    args['report']['report_content_hash']=h;args['approval_snapshot']['report_content_hash']=h
    args['report']['anxin_board_report_hash']=digest({k:v for k,v in args['report'].items() if k!='anxin_board_report_hash'})
    assert n.build_approved_report_narrative(**args)['content']['plain_summary']==text


def stored_fixture():
    import sqlite3
    import test_anxin_board_v3_approval_row_integrity as approval_fixture
    from app import model_call_ledger as calls, model_execution_results as results, report_approval as approvals
    args=fixture();source=args['ai_raw']['content']
    call=dict(id=19,schema_version=calls.SCHEMA_VERSION,project_id=1,snapshot_id=11,
      local_task_id='task-test',call_prepare_key='prepare-test',task_type='daily_report_generate',provider='deepseek',
      model_id='deepseek-flash',model_version='v1',rule_version='rules/1',output_schema_version='daily-report/1.0',
      benchmark_sample_pack_version='pack/1',qualification_status='qualified',authorization_provider='deepseek',
      authorization_authorized=1,authorization_valid=1,snapshot_hash='c'*64,candidate_set_hash='d'*64,created_at='2026-01-01')
    call['qualification_hash']=calls._stable_hash(calls._qualification_payload(call))
    call['authorization_hash']=calls._stable_hash(calls._authorization_payload(call))
    call['call_identity_hash']=calls._stable_hash(calls._call_identity_payload(call))
    formal=dict(result=source,status='succeeded',task_type='daily_report_generate')
    row=dict(id=4,schema_version=results.SCHEMA_VERSION,model_call_id=19,
      **{k:call[k] for k in ('project_id','snapshot_id','local_task_id','task_type','call_identity_hash','provider','model_id','model_version')},
      provider_response_id='response-synthetic',actual_model='deepseek-flash',provider_runtime_fingerprint='synthetic',finish_reason='stop',
      prompt_tokens=1,completion_tokens=2,total_tokens=3,formal_response_json=results._canonical_json(formal),
      formal_response_hash=digest(formal),validated_result_json=results._canonical_json(source),validated_result_hash=digest(source),created_at='2026-01-01')
    row['execution_result_hash']=results._stable_hash(results._execution_identity_payload(row))
    ap=approval_fixture._approval_snapshot();ap.update(report_content_hash=digest(source),model_execution_result_id=4,
      execution_result_hash=row['execution_result_hash'],call_identity_hash=call['call_identity_hash'])
    ap['snapshot_json']=approvals._canonical_json(approvals._snapshot_hash_payload(ap))
    ap.update(id=2,approval_snapshot_hash=approvals._stable_hash(approvals._snapshot_hash_payload(ap)),idempotency_key='approval-test',created_at='2026-01-01')
    ap['human_acknowledged']=1
    closed=approvals._close_row(ap)
    report=dict(schema_version='anxin_board_report_v3',**{k:closed[k] for k in n._BINDINGS},human_acknowledged=True)
    report['anxin_board_report_hash']=digest(report)
    version=dict(id=3,project_id=1,lifecycle='approved',state_version=2,version_no=1,report_content_hash=digest(source),
      model_execution_result_id=4,execution_result_hash=row['execution_result_hash'],validated_result_hash=digest(source),
      formal_response_hash=row['formal_response_hash'],model_call_id=19,call_identity_hash=call['call_identity_hash'],evidence_snapshot_id=11)
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    for table,values in [('report_approval_snapshots',ap),('report_versions',version),('model_calls',call),('model_execution_results',row)]:
      columns=', '.join('"'+k+'" '+('INTEGER' if type(v) is int else 'TEXT') for k,v in values.items())
      conn.execute('CREATE TABLE '+table+' ('+columns+')')
      conn.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
    conn.commit();return conn,dict(project_id=1,report=report,approval_snapshot=closed)


def test_loader_real_sqlite_identity_closure_readonly_and_same_transaction():
    conn,args=stored_fixture();conn.execute('BEGIN');queries=[];conn.set_trace_callback(queries.append)
    conn.execute('PRAGMA query_only=ON')
    got=n.load_approved_report_narrative(**args,conn=conn)
    assert got['content']['plain_summary'].startswith('今天') and conn.in_transaction
    assert all(q.startswith(('SELECT','PRAGMA')) for q in queries)
    conn.close()


@pytest.mark.parametrize('sql',[
 "UPDATE report_versions SET lifecycle='pending_review'",
 "UPDATE report_versions SET lifecycle='superseded',state_version=3",
 "UPDATE report_versions SET project_id=2",
 "UPDATE model_execution_results SET validated_result_json='{}'",
 "UPDATE model_execution_results SET execution_result_hash='bad'",
 "UPDATE model_calls SET authorization_valid=0",
 "UPDATE report_approval_snapshots SET snapshot_json='{}'",
])
def test_loader_rejects_frozen_source_tamper(sql):
    conn,args=stored_fixture();conn.execute(sql);conn.commit()
    with pytest.raises(n.ApprovedReportNarrativeError):n.load_approved_report_narrative(**args,conn=conn)
    conn.close()


def test_real_v3_builder_roundtrip_preserves_delta_narrative_without_baseline_rewrite():
    import test_anxin_board_report_v3 as v3fixture
    from app.anxin_board_report_v3 import build_anxin_board_report_v3,validate_anxin_board_report_v3
    profile=v3fixture._profile_v2();approval=v3fixture._approval(profile);approval['project_id']=profile['project_id']
    raw=fixture()['ai_raw'];approval['report_content_hash']=raw['validated_result_hash']
    raw.update(model_execution_result_id=approval['model_execution_result_id'],execution_result_hash=approval['execution_result_hash'])
    report=build_anxin_board_report_v3(project_name='正式项目',profile=profile,ai_raw=raw,
      daily_change=v3fixture._daily_change(),manager_supplement='已人工核对',approval_snapshot=approval)
    assert validate_anxin_board_report_v3(report,profile=profile,approval_snapshot=approval)==report
    before=deepcopy(report)
    narrative=n.build_approved_report_narrative(project_id=profile['project_id'],report=report,approval_snapshot=approval,ai_raw=raw)
    assert narrative['content']['plain_summary']==raw['content']['plain_summary']
    assert report==before
    assert narrative['content']['plain_summary']!=report['overall_message']

def test_owned_loader_starts_read_snapshot_without_schema_or_write(monkeypatch):
    conn, args = stored_fixture()
    conn.execute('PRAGMA query_only=ON')
    queries = []
    conn.set_trace_callback(queries.append)
    monkeypatch.setattr(n, 'get_connection', lambda: conn)
    got = n.load_approved_report_narrative(**args)
    assert got['report_version_id'] == 3
    assert queries[0] == 'BEGIN'
    assert all(q.startswith(('BEGIN', 'SELECT')) for q in queries)
