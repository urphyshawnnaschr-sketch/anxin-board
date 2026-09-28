"""Exact frozen Git metric projection; fixtures never access live customer data."""
from copy import deepcopy
import importlib
import json

import pytest

from app import report_approval as approvals
from app.anxin_board_report_v3 import build_anxin_board_report_v3
from test_approved_module_narrative import state, client  # noqa: F401
from test_approved_report_narrative import digest, fixture
import test_anxin_board_report_v3 as v3


@pytest.fixture
def metrics_state(state,monkeypatch):
    approval=state['approval_snapshot']
    facts=dict(branch=approval['git_branch'],from_commit=approval['git_from_commit'],to_commit=approval['git_to_commit'],
               commits=[approval['git_to_commit']],commit_count=1,changed_file_count=3,added_lines=58,deleted_lines=2,diff_bytes=600)
    with state['connect']() as conn:
        conn.execute('''CREATE TABLE git_snapshots(id INTEGER,project_id INTEGER,analysis_lineage_id INTEGER,branch TEXT,
            from_commit TEXT,to_commit TEXT,commits_json TEXT,commit_count INTEGER,changed_file_count INTEGER,
            added_lines INTEGER,deleted_lines INTEGER,diff_bytes INTEGER,file_manifest_hash TEXT,frozen_at TEXT)''')
        conn.execute('INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (approval['git_snapshot_id'],1,8,facts['branch'],facts['from_commit'],facts['to_commit'],
                      json.dumps(facts['commits']),1,3,58,2,600,'f'*64,'2026-01-01'))
    result={**state,'facts':facts}
    bind_facts(result,facts)
    if importlib.util.find_spec('app.approved_report_git_metrics'):
        monkeypatch.setattr(importlib.import_module('app.approved_report_git_metrics'),'get_connection',state['connect'])
    return result


def bind_facts(state,facts):
    with state['connect']() as conn:
        row=dict(conn.execute('SELECT * FROM report_approval_snapshots').fetchone())
        row.update(git_facts_hash=digest(facts),human_acknowledged=True)
        row['snapshot_json']=approvals._canonical_json(approvals._snapshot_hash_payload(row))
        row['approval_snapshot_hash']=approvals._stable_hash(approvals._snapshot_hash_payload(row))
        conn.execute('UPDATE report_approval_snapshots SET git_facts_hash=?,snapshot_json=?,approval_snapshot_hash=?',
                     (row['git_facts_hash'],row['snapshot_json'],row['approval_snapshot_hash']))
        approval=approvals._close_row(row)
        report=build_anxin_board_report_v3(project_name='正式项目',profile=state['profile'],
            ai_raw=fixture()['ai_raw'],daily_change=v3._daily_change(),manager_supplement='已人工核对',approval_snapshot=approval)
        conn.execute('UPDATE anxin_board_reports SET report_hash=?,report_json=?,approval_snapshot_hash=?',
                     (report['anxin_board_report_hash'],json.dumps(report),approval['approval_snapshot_hash']))
    state.update(report=report,approval_snapshot=approval)


URL='/api/projects/1/anxin-board/reports/3/git-metrics'


def get(client,state):
    return client.get(URL,params={'report_hash':state['report']['anxin_board_report_hash']})


def test_exact_frozen_counts_are_hash_closed_readonly_and_not_daily_highlights(metrics_state,client):
    state=metrics_state;before=state['path'].read_bytes()
    response=get(client,state)
    assert response.status_code==200,response.text
    value=response.json()
    assert value['metrics']==dict(added_lines=58,deleted_lines=2,changed_file_count=3,commit_count=1,net_added_lines=56,line_change_total=60)
    assert value['source_git_facts']==state['facts']
    assert value['metrics_hash']==digest({k:v for k,v in value.items() if k!='metrics_hash'})
    assert value['git_facts_hash']==state['approval_snapshot']['git_facts_hash']
    assert response.headers['cache-control']=='no-store'
    assert state['path'].read_bytes()==before


def test_true_zero_is_not_unavailable(metrics_state,client):
    state=metrics_state;facts=deepcopy(state['facts'])
    # An empty commit is a real frozen comparison with no changed files or lines.
    facts.update(changed_file_count=0,added_lines=0,deleted_lines=0,diff_bytes=0)
    with state['connect']() as conn:
        conn.execute('UPDATE git_snapshots SET changed_file_count=0,added_lines=0,deleted_lines=0,diff_bytes=0')
    bind_facts(state,facts)
    response=get(client,state)
    assert response.status_code==200,response.text
    assert response.json()['metrics']==dict(added_lines=0,deleted_lines=0,changed_file_count=0,commit_count=1,net_added_lines=0,line_change_total=0)


@pytest.mark.parametrize('sql',[
    'DELETE FROM git_snapshots',
    'UPDATE git_snapshots SET added_lines=0',
    'UPDATE git_snapshots SET deleted_lines=NULL',
    'UPDATE git_snapshots SET project_id=2',
    "UPDATE git_snapshots SET commits_json='[]'",
    "UPDATE git_snapshots SET from_commit='bad'",
    "UPDATE report_approval_snapshots SET snapshot_json='{}'",
    "UPDATE report_versions SET lifecycle='superseded'",
])
def test_missing_corrupt_or_cross_project_authority_is_error_never_zero(metrics_state,client,sql):
    state=metrics_state
    with state['connect']() as conn:conn.execute(sql)
    before=state['path'].read_bytes();response=get(client,state)
    assert response.status_code==409,response.text
    assert set(response.json())=={'detail'} and 'metrics' not in response.json()
    assert state['path'].read_bytes()==before


def test_newer_snapshot_and_profile_cannot_replace_historical_bound_facts(metrics_state,client):
    state=metrics_state
    with state['connect']() as conn:
        conn.execute('''INSERT INTO git_snapshots SELECT 999,project_id,analysis_lineage_id,branch,
            from_commit,to_commit,commits_json,commit_count,changed_file_count,999,999,diff_bytes,file_manifest_hash,frozen_at FROM git_snapshots''')
        conn.execute("UPDATE project_profiles SET status='superseded'")
    response=get(client,state)
    assert response.status_code==200,response.text
    assert response.json()['metrics']['added_lines']==58
    assert response.json()['git_snapshot_id']==state['approval_snapshot']['git_snapshot_id']


def test_pure_validation_rejects_rehashed_counts_and_cross_target(metrics_state,client):
    state=metrics_state;response=get(client,state)
    assert response.status_code==200,response.text
    n=importlib.import_module('app.approved_report_git_metrics');value=response.json()
    assert n.validate_approved_report_git_metrics(value,state['report'],state['approval_snapshot'])==value
    for field in ('metrics','source_git_facts','approval_snapshot_id'):
        bad=deepcopy(value)
        if field=='approval_snapshot_id':bad[field]+=1
        else:bad[field]['added_lines']=999
        bad['metrics_hash']=digest({k:v for k,v in bad.items() if k!='metrics_hash'})
        with pytest.raises(n.ApprovedReportGitMetricsError):
            n.validate_approved_report_git_metrics(bad,state['report'],state['approval_snapshot'])


def test_owned_and_supplied_connections_are_read_only(metrics_state,client):
    state=metrics_state
    assert get(client,state).status_code==200
    n=importlib.import_module('app.approved_report_git_metrics')
    with state['connect']() as conn:
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN');queries=[];conn.set_trace_callback(queries.append)
        value=n.load_approved_report_git_metrics(1,state['report'],state['approval_snapshot'],conn=conn)
        assert value['metrics']['deleted_lines']==2 and conn.in_transaction
        assert all(q.lstrip().startswith(('SELECT','PRAGMA')) for q in queries)


def test_metrics_read_guard_runs_before_database(metrics_state,client,monkeypatch):
    from app import anxin_board_reports as api
    client.headers.pop('X-Anxin-Session')
    monkeypatch.setattr(api,'get_connection',lambda:pytest.fail('unauthorized database open'))
    assert get(client,metrics_state).status_code==403
