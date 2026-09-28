import copy
import sqlite3
import pytest
from app import project_progress_materialization as subject
from app.project_progress import compute_snapshot, read_latest, ensure_schema
from test_project_progress import inputs


def raw(*rows):
    return {'task_type':'daily_report_generate', 'content':{'feature_progress':list(rows)}}


def row(name='告警', stage='开发中', refs=None, scope='后端'):
    return {'feature':name, 'stage':stage, 'evidence_ids':refs or ['git-1'], 'implementation_scope':scope}


def test_activity_does_not_erase_completed_and_testing_is_not_whole_module_completion():
    scope, profile, report = inputs()
    previous = compute_snapshot(scope=scope, profile=profile, report=report, updates={})
    assert subject.compute_updates(profile=profile, ai_raw=raw(row('设备状态', '测试中')), previous=previous, allowed_git_refs={'git-1'}) == {}
    assert subject.compute_updates(profile=profile, ai_raw=raw(row(stage='已完成', scope='测试')), previous=previous, allowed_git_refs={'git-1'}) == {}


@pytest.mark.parametrize('rows', [ [row(refs=['foreign'])], [row('告警（服务）')], [row(),row()], [row(stage='暂时无法确认')]])
def test_unbound_duplicate_unknown_or_foreign_refs_cannot_update(rows):
    _, profile, _ = inputs()
    assert subject.compute_updates(profile=profile, ai_raw=raw(*rows), previous=None, allowed_git_refs={'git-1'}) == {}


def test_explicit_correction_requires_exact_refs_and_reason():
    scope, profile, report = inputs()
    previous = compute_snapshot(scope=scope, profile=profile, report=report, updates={})
    correction={'A':{'stage':'开发中','evidence_ids':['git-1'],'regression_reason':'人工核实本次接口退化'}}
    result = subject.compute_updates(profile=profile, ai_raw=raw(), previous=previous, allowed_git_refs={'git-1'}, corrections=correction)
    assert result['A']['regression_reason'] == correction['A']['regression_reason']
    for bad in [dict(correction['A'], evidence_ids=['foreign']), dict(correction['A'], regression_reason='')]:
        with pytest.raises(subject.ProgressMaterializationError):
            subject.compute_updates(profile=profile, ai_raw=raw(), previous=previous, allowed_git_refs={'git-1'}, corrections={'A':bad})


def test_three_approved_reports_preserve_untouched_and_unknown_modules():
    scope, profile, report=inputs()
    conn=sqlite3.connect(':memory:'); ensure_schema(conn); conn.execute('BEGIN')
    from app.project_progress import append_snapshot
    first=append_snapshot(conn,scope=scope,profile=profile,report=report,updates={})
    second_report=dict(report,report_version_id=2,approval_snapshot_id=2,parent_head_sha='d'*40,head_sha='e'*40)
    updates=subject.compute_updates(profile=profile,ai_raw=raw(row(stage='已完成')),previous=first,allowed_git_refs={'git-1'})
    second=append_snapshot(conn,scope=scope,profile=profile,report=second_report,updates=updates)
    third_report=dict(report,report_version_id=3,approval_snapshot_id=3,parent_head_sha='e'*40,head_sha='f'*40)
    updates=subject.compute_updates(profile=profile,ai_raw=raw(row('设备状态','测试中'),row(stage='暂时无法确认')),previous=second,allowed_git_refs={'git-1'})
    third=append_snapshot(conn,scope=scope,profile=profile,report=third_report,updates=updates)
    assert [m['stage'] for m in third['modules']]==['已完成','已完成']
    assert third['modules']==second['modules']
    conn.rollback(); assert read_latest(conn,scope=scope) is None


@pytest.fixture()
def authority():
    import json
    from app.evidence_snapshots import _git_facts_hash, _expected_snapshot_hash, _evidence_items_hash
    scope, profile, _ = inputs()
    conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
    ensure_schema(conn)
    git={'id':1,'project_id':4,'analysis_lineage_id':1,'branch':'main','from_commit':'a'*40,'to_commit':'d'*40,
         'commits_json':json.dumps(['b'*40,'c'*40,'d'*40]),'commit_count':3,'changed_file_count':1,
         'added_lines':1,'deleted_lines':0,'diff_bytes':10}
    evidence={'evidence_id':'git-1','type':'git_file_fact','source_ref':'git_file_evidence:1:1',
              'content_hash':'e'*64,'selected':1,'redaction_state':'not_applicable','snapshot_id':1}
    snapshot={'id':1,'schema_version':'evidence_snapshot_core_v3','project_id':4,'project_repository_url':scope['git_url'],
        'project_config_hash':'e'*64,'git_snapshot_id':1,'analysis_lineage_id':1,'branch':'main','from_commit':'a'*40,'to_commit':'d'*40,
        'git_facts_hash':_git_facts_hash(git),'prd_id':1,'prd_source_hash':'e'*64,'prd_parsed_hash':'e'*64,
        'prd_structured_hash':'e'*64,'prd_document_fingerprint':'e'*64,'profile_id':6,'profile_content_hash':profile['content_hash']}
    snapshot['snapshot_hash']=_expected_snapshot_hash(snapshot,_evidence_items_hash([evidence]))
    for table, record in [('git_snapshots',git),('evidence_snapshots',snapshot),('evidence_items',evidence)]:
        conn.execute('CREATE TABLE '+table+'('+','.join(k+(' INTEGER' if type(v) is int else ' TEXT') for k,v in record.items())+')')
        conn.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in record)+')',tuple(record.values()))
    conn.commit(); conn.execute('BEGIN')
    approval={'project_id':4,'report_version_id':1,'report_content_hash':'b'*64,'approval_snapshot_id':1,'approval_snapshot_hash':'c'*64,
        'evidence_snapshot_id':1,'evidence_snapshot_hash':snapshot['snapshot_hash'],'git_snapshot_id':1,'git_facts_hash':snapshot['git_facts_hash'],
        'git_branch':'main','git_from_commit':'a'*40,'git_to_commit':'d'*40,'project_repository_url':scope['git_url'],
        'profile_id':6,'profile_content_hash':profile['content_hash']}
    yield conn, profile, snapshot, approval
    conn.close()


def test_exact_snapshot_materialization_idempotent_and_rollback(authority):
    conn,profile,snapshot,approval=authority
    kwargs=dict(conn=conn,project_id=4,profile=profile,ai_raw=raw(row()),approval_snapshot=approval)
    first=subject.materialize_progress(**kwargs)
    assert first['modules'][1]['stage']=='开发中'
    assert subject.materialize_progress(**kwargs)==first
    conn.rollback()
    assert read_latest(conn,scope=subject.progress_scope(snapshot)) is None


def test_first_real_approval_creates_progress_storage_in_its_transaction(authority):
    conn, profile, snapshot, approval = authority
    conn.execute('DROP TABLE project_progress_snapshots')
    result = subject.materialize_progress(conn=conn, project_id=4, profile=profile,
        ai_raw=raw(row()), approval_snapshot=approval)
    assert result['modules'][0]['stage'] == '已完成'
    assert read_latest(conn, scope=subject.progress_scope(snapshot)) == result


def test_stale_unbound_legacy_snapshot_cannot_overwrite_existing(authority):
    conn,profile,snapshot,approval=authority
    subject.materialize_progress(conn=conn,project_id=4,profile=profile,ai_raw=raw(row()),approval_snapshot=approval)
    with pytest.raises(subject.ProgressMaterializationError,match='PROJECT_PROGRESS_STALE'):
        subject.read_progress_inputs(conn=conn,project_id=4,profile=profile,evidence_snapshot=snapshot)


@pytest.mark.parametrize('target', ['approval','evidence','git','lineage'])
def test_exact_authority_rejects_mismatch_before_append(authority,target):
    conn,profile,snapshot,approval=authority
    if target=='approval': approval['git_branch']='other'
    if target=='evidence': conn.execute("UPDATE evidence_items SET evidence_id='foreign'")
    if target=='git': conn.execute("UPDATE git_snapshots SET added_lines=999")
    if target=='lineage':
        profile=copy.deepcopy(profile)
        profile['content']['implementation_mappings'][0]['exact_head']='f'*40
    with pytest.raises(subject.ProgressMaterializationError):
        subject.materialize_progress(conn=conn,project_id=4,profile=profile,ai_raw=raw(row()),approval_snapshot=approval)
    assert conn.execute('SELECT count(*) FROM project_progress_snapshots').fetchone()[0]==0


def test_legitimate_mixed_refs_need_at_least_one_selected_git_ref():
    _,profile,_=inputs()
    result=subject.compute_updates(profile=profile,ai_raw=raw(row(refs=['git-1','prd-1'])),previous=None,
        allowed_git_refs={'git-1'},allowed_evidence_refs={'git-1','prd-1'})
    assert result['B']['evidence_ids']==['git-1','prd-1']
    assert subject.compute_updates(profile=profile,ai_raw=raw(row(refs=['prd-1'])),previous=None,
        allowed_git_refs={'git-1'},allowed_evidence_refs={'git-1','prd-1'})=={}


def test_explicit_human_uncertainty_can_retract_known_status():
    scope,profile,report=inputs()
    updates=subject.compute_updates(profile=profile,ai_raw=raw(),previous=None,allowed_git_refs={'git-1'},
        corrections={'A':{'stage':'暂时无法确认','regression_reason':'人工发现原功能范围与实际代码不一致','evidence_ids':['git-1']}})
    result=compute_snapshot(scope=scope,profile=profile,report=report,updates=updates)
    assert result['modules'][0]['stage']=='暂时无法确认'
    assert result['modules'][0]['regression_reason']


def test_unchanged_git_range_is_valid_without_invented_progress(authority):
    import json
    from app.evidence_snapshots import _git_facts_hash, _expected_snapshot_hash, _evidence_items_hash
    conn,profile,snapshot,approval=authority
    conn.execute("UPDATE git_snapshots SET to_commit=from_commit, commits_json='[]',commit_count=0")
    git=dict(conn.execute('SELECT * FROM git_snapshots').fetchone())
    snapshot['to_commit']=snapshot['from_commit']; snapshot['git_facts_hash']=_git_facts_hash(git)
    rows=[dict(x) for x in conn.execute('SELECT * FROM evidence_items')]
    snapshot['snapshot_hash']=_expected_snapshot_hash(snapshot,_evidence_items_hash(rows))
    conn.execute('UPDATE evidence_snapshots SET to_commit=?,git_facts_hash=?,snapshot_hash=?',
        (snapshot['to_commit'],snapshot['git_facts_hash'],snapshot['snapshot_hash']))
    values=subject.read_progress_inputs(conn=conn,project_id=4,profile=profile,evidence_snapshot=snapshot)
    assert values['head_sha']==values['parent_head_sha']=='a'*40
