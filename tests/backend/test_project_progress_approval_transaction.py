"""Approval transaction integration: real cumulative ledger, V3 and immutable stores.

External review/validation authority uses the existing synthetic approval fixture;
no provider, Git scan or customer database is involved.
"""
import copy
import json
import sqlite3

import pytest
from fastapi import HTTPException
from app import report_approval, project_progress, anxin_board_v3_materialization as materializer
from app import anxin_board_report_store as store
from app.evidence_snapshots import _git_facts_hash, _expected_snapshot_hash, _evidence_items_hash
from test_report_approval import approval_env
import test_anxin_board_report_v3 as v3_fixtures


def approve(corrections):
    return report_approval.create_report_approval_snapshot(project_id=1,report_version_id=3,
        expected_report_state_version=1,confirmed_by='张经理',confirmed_timezone='Asia/Shanghai',
        confirmed_utc_offset_minutes=480,human_confirmed=True,idempotency_key='cumulative-transaction',
        progress_corrections=corrections)


@pytest.fixture()
def integrated(approval_env, monkeypatch):
    path,bundle,closure,validation=approval_env
    profile=v3_fixtures._profile()
    git={'id':29,'project_id':1,'analysis_lineage_id':1,'branch':'main','from_commit':'a'*40,'to_commit':'b'*40,
         'commits_json':json.dumps(['b'*40]),'commit_count':1,'changed_file_count':1,'added_lines':1,'deleted_lines':0,'diff_bytes':10}
    evidence={'snapshot_id':11,'evidence_id':'git-approved-1','type':'git_file_fact','source_ref':'git_file_evidence:29:1',
              'content_hash':'e'*64,'selected':1,'redaction_state':'not_applicable'}
    snapshot={'id':11,'schema_version':'evidence_snapshot_core_v3','project_id':1,'project_repository_url':closure['snapshot']['project_repository_url'],
        'project_config_hash':'e'*64,'git_snapshot_id':29,'analysis_lineage_id':1,'branch':'main','from_commit':'a'*40,'to_commit':'b'*40,
        'git_facts_hash':_git_facts_hash(git),'prd_id':7,'prd_source_hash':closure['snapshot']['prd_source_hash'],
        'prd_parsed_hash':closure['snapshot']['prd_parsed_hash'],'prd_structured_hash':closure['snapshot']['prd_structured_hash'],
        'prd_document_fingerprint':closure['snapshot']['prd_document_fingerprint'],'profile_id':13,'profile_content_hash':profile['content_hash']}
    snapshot['snapshot_hash']=_expected_snapshot_hash(snapshot,_evidence_items_hash([evidence]))
    bundle['report_version']['evidence_snapshot_hash']=snapshot['snapshot_hash']
    bundle['ai_raw'].update(task_type='daily_report_generate',content={'feature_progress':[
        {'feature':'业务模块 1','stage':'开发中','implementation_scope':'后端','evidence_ids':['git-approved-1']}]})
    closure['snapshot'].update(snapshot)
    closure['current_authority']['current_profile']['content_hash']=profile['content_hash']
    closure['git_facts_hash']=snapshot['git_facts_hash']
    validation['git_facts_hash']=snapshot['git_facts_hash']
    validation['evidence_snapshot_hash']=snapshot['snapshot_hash']
    with sqlite3.connect(path) as conn:
        conn.row_factory=sqlite3.Row
        project_progress.ensure_schema(conn)
        store._ensure_store_schema(conn)
        conn.execute('CREATE TABLE projects (id INTEGER PRIMARY KEY,name TEXT)')
        conn.execute("INSERT INTO projects VALUES(1,'合成累计项目')")
        conn.execute('CREATE TABLE project_profiles(id INTEGER PRIMARY KEY,status TEXT,confirmed_by TEXT,confirmed_at TEXT)')
        conn.execute("INSERT INTO project_profiles VALUES(13,'confirmed','张经理','2026-09-27T00:00:00+00:00')")
        for table, record in [('git_snapshots',git),('evidence_snapshots',snapshot),('evidence_items',evidence)]:
            conn.execute('CREATE TABLE '+table+'('+','.join(k+(' INTEGER' if type(v) is int else ' TEXT') for k,v in record.items())+')')
            conn.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in record)+')',tuple(record.values()))
    monkeypatch.setattr(materializer,'read_bound_project_profile_for_report',lambda *_a,**_k:copy.deepcopy(profile))
    monkeypatch.setattr(store,'read_bound_project_profile_for_report',lambda *_a,**_k:copy.deepcopy(profile))
    monkeypatch.setattr(report_approval,'materialize_approved_anxin_board_v3_in_transaction',materializer.materialize_approved_anxin_board_v3_in_transaction)
    monkeypatch.setattr(report_approval,'build_plain_language_change_summary_from_snapshot',lambda **_k:v3_fixtures._daily_change())
    corrections=[{'module_id':'module-1','stage':'已完成','reason':'人工核对约定开发已完成','evidence_ids':['git-approved-1']}]
    return path,corrections


def counts(path):
    with sqlite3.connect(path) as conn:
        result={}
        for table in ('report_approval_snapshots','report_progress_corrections','project_progress_snapshots','anxin_board_reports'):
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            result[table]=conn.execute('SELECT count(*) FROM '+table).fetchone()[0] if exists else 0
        result['report_state']=conn.execute('SELECT lifecycle,state_version FROM report_versions WHERE id=3').fetchone()
        return result


def test_actual_approval_commits_all_four_stores_and_replays_exactly(integrated):
    path,corrections=integrated
    first=approve(corrections)
    assert first['created'] is True
    before=counts(path)
    assert before=={'report_approval_snapshots':1,'report_progress_corrections':1,'project_progress_snapshots':1,'anxin_board_reports':1,'report_state':('approved',2)}
    with sqlite3.connect(path) as conn:
        progress=json.loads(conn.execute('SELECT snapshot_json FROM project_progress_snapshots').fetchone()[0])
        report=json.loads(conn.execute('SELECT report_json FROM anxin_board_reports').fetchone()[0])
        correction=json.loads(conn.execute('SELECT corrections_json FROM report_progress_corrections').fetchone()[0])
    assert progress['modules'][0]['stage']=='已完成'
    assert report['modules'][0]['stage']=='已完成'
    assert len(report['modules'])==3 and report['unknown_module_count']==2
    assert correction==corrections
    assert progress['report']['approval_snapshot_hash']==first['approval_snapshot']['approval_snapshot_hash']
    assert approve(corrections)['created'] is False
    assert counts(path)==before
    changed=copy.deepcopy(corrections); changed[0]['reason']='另一条原因'
    with pytest.raises(HTTPException) as error:
        approve(changed)
    assert error.value.status_code==409
    assert counts(path)==before


def test_failure_after_real_v3_insert_rolls_back_approval_correction_progress_and_report(integrated,monkeypatch):
    path,corrections=integrated
    actual=materializer.persist_anxin_board_report_v3_in_transaction
    reached_after_insert=[]
    def fail_after_insert(**kwargs):
        actual(**kwargs)
        assert kwargs['conn'].execute('SELECT count(*) FROM project_progress_snapshots').fetchone()[0]==1
        assert kwargs['conn'].execute('SELECT count(*) FROM anxin_board_reports').fetchone()[0]==1
        reached_after_insert.append(True)
        raise RuntimeError('synthetic downstream failure')
    monkeypatch.setattr(materializer,'persist_anxin_board_report_v3_in_transaction',fail_after_insert)
    with pytest.raises(HTTPException):
        approve(corrections)
    assert reached_after_insert==[True]
    assert counts(path)=={'report_approval_snapshots':0,'report_progress_corrections':0,'project_progress_snapshots':0,'anxin_board_reports':0,'report_state':('pending_review',1)}
