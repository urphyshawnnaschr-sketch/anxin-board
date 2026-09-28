"""Synthetic SQLite authority tests; never open customer storage or external services."""
from copy import deepcopy
import importlib
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import anxin_board_reports as api, local_session_api
from app import report_approval as approvals
from app.anxin_board_report_v3 import build_anxin_board_report_v3
from test_approved_report_narrative import digest, fixture, stored_fixture
import test_anxin_board_report_v3 as v3


@pytest.fixture
def state(tmp_path, monkeypatch):
    conn, _ = stored_fixture()
    profile = v3._profile_v2()
    profile['content']['implementation_mappings'][2]['exact_head'] = '9' * 40
    profile['content_hash'] = digest(profile['content'])
    source = deepcopy(profile)
    source.update(id=22, project_id=2)
    source['content']['notes'] = 'Explicitly imported into another project.'
    source['content_hash'] = digest(source['content'])
    columns = ('id,project_id,version_no,source_prd_id,status,content_json,content_hash,'
               'edit_version,created_at,updated_at,confirmed_by,confirmed_at')
    conn.execute('CREATE TABLE project_profiles (' + ','.join(
        key + (' INTEGER' if key in {'id','project_id','version_no','source_prd_id','edit_version'} else ' TEXT')
        for key in columns.split(',')) + ')')
    for value in (profile, source):
        conn.execute('INSERT INTO project_profiles VALUES (' + ','.join('?' for _ in range(12)) + ')',
                     [value[k] for k in ('id','project_id','version_no','source_prd_id','status')]
                     + [json.dumps(value['content']),value['content_hash'],1,'2026-01-01','2026-01-01','Synthetic confirmer','2026-01-01'])
    row = dict(conn.execute('SELECT * FROM report_approval_snapshots').fetchone())
    row.update(profile_id=profile['id'], profile_version_no=profile['version_no'],
               profile_content_hash=profile['content_hash'], human_acknowledged=True)
    row['snapshot_json'] = approvals._canonical_json(approvals._snapshot_hash_payload(row))
    row['approval_snapshot_hash'] = approvals._stable_hash(approvals._snapshot_hash_payload(row))
    conn.execute('UPDATE report_approval_snapshots SET profile_id=?,profile_version_no=?,profile_content_hash=?,snapshot_json=?,approval_snapshot_hash=?',
                 [row[k] for k in ('profile_id','profile_version_no','profile_content_hash','snapshot_json','approval_snapshot_hash')])
    approval = approvals._close_row(row)
    report = build_anxin_board_report_v3(project_name='正式项目', profile=profile,
        ai_raw=fixture()['ai_raw'], daily_change=v3._daily_change(), manager_supplement='已人工核对', approval_snapshot=approval)
    conn.execute('CREATE TABLE projects (id INTEGER, name TEXT)')
    conn.executemany('INSERT INTO projects VALUES (?,?)', [(1,'正式项目'),(2,'来源项目')])
    conn.execute('''CREATE TABLE anxin_board_reports (id INTEGER,project_id INTEGER,schema_version TEXT,
        report_date TEXT,report_hash TEXT,report_json TEXT,created_at TEXT,
        approval_snapshot_id INTEGER,approval_snapshot_hash TEXT)''')
    conn.execute('INSERT INTO anxin_board_reports VALUES (1,1,?,?,?,?,?,?,?)',
                 [report['schema_version'],report['report_date'],report['anxin_board_report_hash'],json.dumps(report),
                  '2026-01-01',approval['approval_snapshot_id'],approval['approval_snapshot_hash']])
    identity = dict(project_id=2, exact_head='9'*40, git_url=approval['project_repository_url'], module_ids=['module-1','module-2','module-3'])
    output = dict(content=source['content'], coverage={}, requirements={
        'module-1':[dict(requirement_index=0,status='partial',rationale='Inputs exist; retry behavior remains unverified.',evidence_ids=['repo-code-module-1'])],
        'module-2':[dict(requirement_index=0,status='implemented',rationale='Saved records can be viewed.',evidence_ids=['repo-code-module-2'])],
        'module-3':[dict(requirement_index=0,status='unknown',rationale='No implementation evidence was established.',evidence_ids=[])]})
    conn.execute('CREATE TABLE brownfield_baseline_tasks (task_id TEXT,project_id INTEGER,profile_id INTEGER,status TEXT,identity_json TEXT,identity_hash TEXT)')
    conn.execute('INSERT INTO brownfield_baseline_tasks VALUES (?,?,?,?,?,?)',
                 ('baseline-synthetic',2,22,'succeeded',json.dumps(identity),digest(identity)))
    conn.execute('CREATE TABLE brownfield_baseline_outputs (task_id TEXT,output_json TEXT,output_hash TEXT)')
    conn.execute('INSERT INTO brownfield_baseline_outputs VALUES (?,?,?)', ('baseline-synthetic',json.dumps(output),digest(output)))
    conn.commit()
    path = tmp_path / 'module-narrative.sqlite3'
    with sqlite3.connect(path) as disk:
        conn.backup(disk)
    conn.close()
    def connect():
        value = sqlite3.connect(path, check_same_thread=False)
        value.row_factory = sqlite3.Row
        return value
    monkeypatch.setattr(api, 'get_connection', connect)
    # The product module may not exist during the first red route test.
    if importlib.util.find_spec('app.approved_module_narrative'):
        monkeypatch.setattr(importlib.import_module('app.approved_module_narrative'), 'get_connection', connect)
    payload = dict(anxin_board_report_hash=report['anxin_board_report_hash'], report_content_hash=report['report_content_hash'],
        approval_snapshot_id=approval['approval_snapshot_id'], approval_snapshot_hash=approval['approval_snapshot_hash'],
        profile_id=profile['id'],profile_content_hash=profile['content_hash'],
        source=dict(project_id=2,profile_id=22,profile_content_hash=source['content_hash'],task_id='baseline-synthetic',
                    task_identity_hash=digest(identity),output_hash=digest(output),exact_head='9'*40),
        confirmed_by='Codex',cross_project_reuse_ack=True,idempotency_key='module-confirm-1',
        modules=[dict(module_id='module-1',summary='输入检查已有实现。',remaining='重试行为仍待核实。',requirement_refs=[0]),
                 dict(module_id='module-2',summary='可以查看已保存记录。',remaining='静态证据不代表已完成验收。',requirement_refs=[0]),
                 dict(module_id='module-3',summary='现有证据尚不能确认具体实现。',remaining='需要补充实现证据。',requirement_refs=[0])])
    return dict(path=path,connect=connect,report=report,approval_snapshot=approval,profile=profile,payload=payload)


@pytest.fixture
def client(state):
    app = FastAPI(); app.include_router(api.router); app.include_router(local_session_api.router)
    assert local_session_api.configure_local_session_guard('synthetic-module-bootstrap')
    try:
        with TestClient(app,base_url='http://127.0.0.1:5173') as client:
            result=client.post('/api/local-session/exchange',json={'bootstrap_secret':'synthetic-module-bootstrap'},headers={'Origin':'http://127.0.0.1:5173'})
            client.headers.update({'X-Anxin-Session':result.json()['session_token'],'X-Request-Id':'module-test','Origin':'http://127.0.0.1:5173'})
            yield client
    finally:
        local_session_api.invalidate_local_session_guard()


URL='/api/projects/1/anxin-board/reports/3/module-narrative'


def test_absent_annotation_is_readonly_and_does_not_require_baseline(state,client):
    with state['connect']() as conn:
        conn.execute('DROP TABLE brownfield_baseline_tasks');conn.execute('DROP TABLE brownfield_baseline_outputs')
    before=state['path'].read_bytes()
    result=client.get(URL,params={'report_hash':state['report']['anxin_board_report_hash']})
    assert result.status_code==200, result.text
    assert result.json() is None and state['path'].read_bytes()==before


def test_confirm_exact_source_once_and_preserve_all_original_authorities(state,client):
    with state['connect']() as conn:
        originals={t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in
                   ('project_profiles','report_versions','report_approval_snapshots','anxin_board_reports','model_execution_results','brownfield_baseline_outputs')}
    response=client.post(URL,json=state['payload'])
    assert response.status_code==200,response.text
    value=response.json()
    assert value['attribution']=='已确认的模块说明' and value['historical_baseline'] is True
    assert [m['source_requirements'][0]['status'] for m in value['modules']]==['partial','implemented','unknown']
    assert value['modules'][0]['summary']=='输入检查已有实现。'
    assert value['source']['profile_content_hash']!=value['profile_content_hash']
    assert client.post(URL,json=state['payload']).json()==value
    reordered=deepcopy(state['payload']);reordered['modules'].reverse()
    assert client.post(URL,json=reordered).json()==value
    assert client.get(URL,params={'report_hash':state['report']['anxin_board_report_hash']}).json()==value
    assert value['module_narrative_hash']==digest({k:v for k,v in value.items() if k!='module_narrative_hash'})
    with state['connect']() as conn:
        for table,rows in originals.items():
            assert [tuple(r) for r in conn.execute('SELECT * FROM '+table)]==rows
        with pytest.raises(sqlite3.IntegrityError): conn.execute("UPDATE approved_module_narratives SET narrative_json='{}'")
        with pytest.raises(sqlite3.IntegrityError): conn.execute('DELETE FROM approved_module_narratives')


@pytest.mark.parametrize('change',['ack','approval','profile','output','task','module','refs','secret'])
def test_invalid_identity_source_or_unsafe_text_never_persists(state,client,change):
    payload=deepcopy(state['payload'])
    if change=='ack':payload['cross_project_reuse_ack']=False
    if change=='approval':payload['approval_snapshot_hash']='f'*64
    if change=='profile':payload['profile_content_hash']='f'*64
    if change=='output':payload['source']['output_hash']='f'*64
    if change=='task':payload['source']['task_id']='other-task'
    if change=='module':payload['modules'][0]['module_id']='same-name-other-id'
    if change=='refs':payload['modules'][0]['requirement_refs']=[1]
    if change=='secret':payload['modules'][0]['summary']='api_key=synthetic-secret-value'
    response=client.post(URL,json=payload)
    assert response.status_code in (400,409),response.text
    assert 'synthetic-secret' not in response.text
    assert client.get(URL,params={'report_hash':state['report']['anxin_board_report_hash']}).json() is None


def test_changed_replay_and_second_confirmation_are_rejected(state,client):
    assert client.post(URL,json=state['payload']).status_code==200
    changed=deepcopy(state['payload']);changed['modules'][0]['summary']='Different manual text'
    assert client.post(URL,json=changed).status_code==409
    changed['idempotency_key']='different-key'
    assert client.post(URL,json=changed).status_code==409


@pytest.mark.parametrize('sql',[
    "UPDATE brownfield_baseline_tasks SET profile_id=21",
    "UPDATE brownfield_baseline_tasks SET identity_json='{}'",
    "UPDATE brownfield_baseline_outputs SET output_json='{}'",
    "UPDATE project_profiles SET content_hash='invalid' WHERE id=22",
    "UPDATE report_versions SET lifecycle='superseded'",
])
def test_stored_binding_never_falls_back_after_authority_corruption(state,client,sql):
    assert client.post(URL,json=state['payload']).status_code==200
    with state['connect']() as conn:conn.execute(sql)
    result=client.get(URL,params={'report_hash':state['report']['anxin_board_report_hash']})
    assert result.status_code==409 and set(result.json())=={'detail'}


def test_write_guard_rejects_before_opening_database(state,client,monkeypatch):
    client.headers.pop('Origin')
    monkeypatch.setattr(api,'get_connection',lambda:pytest.fail('unauthorized DB access'))
    result=client.post(URL,json=state['payload'])
    assert result.status_code==403


def test_historical_profiles_remain_readable_and_getter_does_not_write(state,client):
    assert client.post(URL,json=state['payload']).status_code==200
    with state['connect']() as conn:conn.execute("UPDATE project_profiles SET status='superseded'")
    n=importlib.import_module('app.approved_module_narrative')
    with state['connect']() as conn:
        from app.project_profiles import read_bound_project_profile_for_report
        profile=read_bound_project_profile_for_report(21,conn=conn)
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN');queries=[];conn.set_trace_callback(queries.append)
        value=n.get_approved_module_narrative(1,state['report'],state['approval_snapshot'],profile,conn=conn)
        assert value['source']['profile_id']==22 and conn.in_transaction
        assert all(q.lstrip().startswith(('SELECT','PRAGMA')) for q in queries)


@pytest.mark.parametrize('change',['repository','candidate','different_module','requirement_gap','status','raw_hash'])
def test_copied_names_or_rehashed_source_cannot_replace_explicit_equal_profile(state,client,change):
    payload=deepcopy(state['payload'])
    with state['connect']() as conn:
        if change=='candidate':
            conn.execute("UPDATE project_profiles SET status='candidate' WHERE id=22")
        elif change=='repository':
            row=conn.execute('SELECT identity_json FROM brownfield_baseline_tasks').fetchone()
            identity=json.loads(row[0]);identity['git_url']='https://example.com/different/repository'
            payload['source']['task_identity_hash']=digest(identity)
            conn.execute('UPDATE brownfield_baseline_tasks SET identity_json=?,identity_hash=?',(json.dumps(identity),digest(identity)))
        else:
            output=json.loads(conn.execute('SELECT output_json FROM brownfield_baseline_outputs').fetchone()[0])
            if change=='different_module':
                output['content']['planned_modules'][0]['description']='Different requirements with the same module name'
                payload['source']['profile_content_hash']=digest(output['content'])
                conn.execute('UPDATE project_profiles SET content_json=?,content_hash=? WHERE id=22',(json.dumps(output['content']),digest(output['content'])))
            if change=='requirement_gap':output['requirements']['module-1'][0]['requirement_index']=1
            if change=='status':output['requirements']['module-1'][0]['status']='implemented'
            if change=='raw_hash':output['requirements']['module-1'][0]['rationale']='Different statement'
            if change!='raw_hash':payload['source']['output_hash']=digest(output)
            conn.execute('UPDATE brownfield_baseline_outputs SET output_json=?,output_hash=?',(json.dumps(output),digest(output)))
    result=client.post(URL,json=payload)
    assert result.status_code==409,result.text


def test_two_sqlite_connections_cannot_confirm_twice(state):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app import approved_module_narrative as n
    gate=Barrier(2)
    def confirm(index):
        payload=deepcopy(state['payload']);payload['idempotency_key']=f'concurrent-{index}'
        gate.wait(timeout=10)
        try:
            return n.confirm_approved_module_narrative(project_id=1,report_version_id=3,payload=payload)
        except n.ApprovedModuleNarrativeError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(confirm,[1,2]))
    assert sum(isinstance(v,dict) for v in values)==1
    assert 'APPROVED_MODULE_NARRATIVE_ALREADY_CONFIRMED' in values
    with state['connect']() as conn:
        assert conn.execute('SELECT count(*) FROM approved_module_narratives').fetchone()[0]==1


def test_rehashing_dto_cannot_forge_raw_source_requirements(state,client):
    assert client.post(URL,json=state['payload']).status_code==200
    with state['connect']() as conn:
        conn.execute('DROP TRIGGER trg_approved_module_narratives_no_update')
        value=json.loads(conn.execute('SELECT narrative_json FROM approved_module_narratives').fetchone()[0])
        value['modules'][0]['source_requirements'][0]['rationale']='Forged source statement'
        value['module_narrative_hash']=digest({k:v for k,v in value.items() if k!='module_narrative_hash'})
        conn.execute('UPDATE approved_module_narratives SET narrative_json=?,narrative_hash=?',
                     (json.dumps(value),value['module_narrative_hash']))
    result=client.get(URL,params={'report_hash':state['report']['anxin_board_report_hash']})
    assert result.status_code==409 and 'Forged source' not in result.text
