import copy
import hashlib
import json
import sqlite3

import pytest
from app import project_progress as progress


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def inputs():
    content = {'schema_version': 'project_profile_v2', 'planned_modules': [
        {'client_id': 'A', 'name': '设备状态'}, {'client_id': 'B', 'name': '告警'}],
        'implementation_mappings': [{'planned_module_id': 'A', 'status': 'implemented', 'exact_head': 'a'*40,
            'evidence_ids': ['repo-code-a']} ]}
    profile = {'id': 6, 'status': 'confirmed', 'content_hash': digest(content), 'content': content}
    scope = {'project_id': 4, 'git_url': 'https://example.test/repo.git', 'branch': 'main',
             'profile_id': 6, 'profile_content_hash': digest(content), 'analysis_lineage_id': 1}
    report = {'report_version_id': 1, 'report_content_hash': 'b'*64, 'approval_snapshot_id': 1,
              'approval_snapshot_hash': 'c'*64, 'head_sha': 'd'*40, 'parent_head_sha': 'a'*40}
    return scope, profile, report


def test_dynamic_baseline_unknown_carry_and_development_only_summary():
    scope, profile, report = inputs()
    result = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={
        'A': {'stage': '暂时无法确认', 'evidence_ids': []}})
    assert [m['module_id'] for m in result['modules']] == ['A', 'B']
    assert [m['stage'] for m in result['modules']] == ['已完成', '暂时无法确认']
    summary = result['modules'][0]['client_stage_summary']
    assert '开发' in summary['summary'] and '通过' not in summary['summary'] and '检查' not in summary['summary']
    assert result['modules'][0]['baseline_status'] == 'implemented'


def test_evidenced_regression_and_untouched_inheritance():
    scope, profile, report = inputs()
    previous = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={})
    report = dict(report, report_version_id=2, approval_snapshot_id=2, head_sha='e'*40, parent_head_sha='d'*40)
    update = {'A': {'stage': '开发中', 'evidence_ids': ['git-diff-2']}}
    with pytest.raises(progress.ProjectProgressError):
        progress.compute_snapshot(scope=scope, profile=profile, report=report, updates=update, previous=previous)
    update['A']['regression_reason'] = '已核实接口回退，需要重新开发'
    result = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates=update, previous=previous)
    assert result['modules'][0]['stage'] == '开发中'
    assert result['modules'][1] == previous['modules'][1]
    assert previous['modules'][0]['stage'] == '已完成'


@pytest.mark.parametrize('change', ['branch', 'profile_content_hash', 'analysis_lineage_id'])
def test_scope_never_mixes(change):
    scope, profile, report = inputs()
    previous = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={})
    changed = dict(scope, **{change: 'other'})
    with pytest.raises(progress.ProjectProgressError):
        progress.compute_snapshot(scope=changed, profile=profile, report=report, updates={}, previous=previous)


@pytest.mark.parametrize('mutation', ['candidate', 'hash', 'module', 'refs', 'parent'])
def test_invalid_authority_and_updates_rejected(mutation):
    scope, profile, report = inputs()
    updates = {}
    if mutation == 'candidate': profile['status'] = 'candidate'
    if mutation == 'hash': profile['content_hash'] = 'f'*64
    if mutation == 'module': updates['FOREIGN'] = {'stage': '开发中', 'evidence_ids': ['x']}
    if mutation == 'refs': updates['B'] = {'stage': '开发中', 'evidence_ids': []}
    if mutation == 'parent': report['parent_head_sha'] = 'f'*40
    with pytest.raises(progress.ProjectProgressError):
        progress.compute_snapshot(scope=scope, profile=profile, report=report, updates=updates)


def test_external_transaction_idempotency_immutable_and_rollback():
    conn = sqlite3.connect(':memory:')
    progress.ensure_schema(conn)
    scope, profile, report = inputs()
    with pytest.raises(progress.ProjectProgressError):
        progress.append_snapshot(conn, scope=scope, profile=profile, report=report, updates={})
    conn.execute('BEGIN')
    first = progress.append_snapshot(conn, scope=scope, profile=profile, report=report, updates={})
    assert progress.append_snapshot(conn, scope=scope, profile=profile, report=report, updates={}) == first
    with pytest.raises(progress.ProjectProgressError):
        progress.append_snapshot(conn, scope=scope, profile=profile, report=dict(report, head_sha='e'*40), updates={})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('UPDATE project_progress_snapshots SET snapshot_json = ?', ('{}',))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('DELETE FROM project_progress_snapshots')
    conn.rollback()
    assert progress.read_latest(conn, scope=scope) is None
    conn.execute('BEGIN')
    progress.append_snapshot(conn, scope=scope, profile=profile, report=report, updates={})
    conn.commit()
    assert progress.read_latest(conn, scope=scope) == first
    assert progress.read_latest(conn, scope=dict(scope, analysis_lineage_id=2)) is None


def test_summary_projection_has_existing_shape_and_rehashed_development_copy():
    scope, profile, report = inputs()
    snapshot = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={})
    summaries = progress.to_module_summaries(snapshot)
    assert summaries == [m['client_stage_summary'] for m in snapshot['modules']]
    for summary in summaries:
        assert summary['client_stage_summary_hash'] == digest({k: v for k, v in summary.items() if k != 'client_stage_summary_hash'})
        assert set(summary) == {'schema_version', 'module_name', 'stage', 'display_stage', 'tone', 'summary', 'next_step', 'client_stage_summary_hash'}


def test_followup_unknown_preserves_prior_provenance_and_head_drift_rejects():
    scope, profile, report = inputs()
    first = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={
        'B': {'stage': '测试中', 'evidence_ids': ['test-run-1']}})
    second_report = dict(report, report_version_id=2, approval_snapshot_id=2, parent_head_sha='d'*40, head_sha='e'*40)
    second = progress.compute_snapshot(scope=scope, profile=profile, report=second_report, updates={
        'B': {'stage': '暂时无法确认', 'evidence_ids': []}}, previous=first)
    assert second['modules'] == first['modules']
    with pytest.raises(progress.ProjectProgressError):
        progress.compute_snapshot(scope=scope, profile=profile, report=dict(second_report, parent_head_sha='a'*40), updates={}, previous=first)


def test_dynamic_eleven_modules_and_no_cross_profile_inheritance():
    scope, profile, report = inputs()
    profile['content']['planned_modules'] += [{'client_id': f'M{i}', 'name': f'功能{i}'} for i in range(9)]
    profile['content_hash'] = scope['profile_content_hash'] = digest(profile['content'])
    first = progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={})
    assert len(first['modules']) == 11
    profile['id'] = scope['profile_id'] = 7
    with pytest.raises(progress.ProjectProgressError):
        progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={}, previous=first)


def test_same_report_cannot_be_rebound_to_another_project():
    conn = sqlite3.connect(':memory:')
    progress.ensure_schema(conn)
    conn.execute('BEGIN')
    scope, profile, report = inputs()
    progress.append_snapshot(conn, scope=scope, profile=profile, report=report, updates={})
    with pytest.raises(progress.ProjectProgressError):
        progress.append_snapshot(conn, scope=dict(scope, project_id=5), profile=profile, report=report, updates={})
