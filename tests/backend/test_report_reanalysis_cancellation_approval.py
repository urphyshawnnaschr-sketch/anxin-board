"""Restore the saved result, approve it, and render it without any provider call.

All writes target the synthetic review_state database; socket access is denied by
the suite's global fixture. No approval, validation, or materialization is mocked.
"""
import sqlite3

from app import report_review, report_approval, anxin_board_report_store
from app import approved_report_narrative, mail_report_renderer, mail_brand_asset
from app.approved_report_git_metrics import load_approved_report_git_metrics
from app.project_profiles import read_bound_project_profile_for_report
from test_report_review import review_state, _model_row, _report_identity_row
from test_report_reanalysis_cancellation import chain, cancel


def test_restored_result_can_be_formally_approved_and_rendered(review_state):
    state = review_state
    report, replacement, payload = chain(state)
    report_id = report['report_version_id']
    original = _model_row(state), _report_identity_row(state, report_id)
    cancel(state, report, payload)
    statement = '本次为独立测试分支模拟验收，5 项本地检查通过，尚未进行真实设备联调。'
    supplement = report_review.append_supplement_version(
        project_id=state['project_id'], report_version_id=report_id,
        content=statement, source_type='pm_external_fact',
        provided_by='Codex（用户授权验收）', provided_timezone='Asia/Shanghai',
        idempotency_key='restored-report-supplement', expected_latest_supplement_version=0)
    result = report_approval.create_report_approval_snapshot(
        project_id=state['project_id'], report_version_id=report_id,
        expected_report_state_version=3, confirmed_by='Codex（用户授权验收）',
        confirmed_timezone='Asia/Shanghai', confirmed_utc_offset_minutes=480,
        human_confirmed=True, idempotency_key='restored-report-approval')
    approval = result['approval_snapshot']
    assert result['created'] is True
    assert approval['supplement_version_id'] == supplement['supplement_version']['supplement_version_id']
    formal = anxin_board_report_store.load_latest_anxin_board_report(project_id=state['project_id'])
    assert formal is not None
    assert len(formal['modules']) == len(state['profile_content']['modules'])
    narrative = approved_report_narrative.load_approved_report_narrative(
        project_id=state['project_id'], report=formal, approval_snapshot=approval)
    rendered = mail_report_renderer.render_approved_report_for_mail(
        formal, profile=read_bound_project_profile_for_report(formal['profile_id']), approval_snapshot=approval,
        approved_narrative=narrative,
        approved_git_metrics=load_approved_report_git_metrics(
            project_id=state['project_id'], report=formal, approval_snapshot=approval))
    standalone = mail_brand_asset.standalone_customer_html(rendered.html_body)
    assert statement in standalone
    assert '项目经理说明' in standalone
    assert 'data:image/png;base64,' in standalone
    assert 'cid:' not in standalone
    assert original == (_model_row(state), _report_identity_row(state, report_id))
    with sqlite3.connect(state['db_path']) as conn:
        assert conn.execute('SELECT lifecycle,state_version FROM report_versions WHERE id=?', (report_id,)).fetchone() == ('approved', 4)
        assert conn.execute('SELECT COUNT(*) FROM report_approval_snapshots').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM project_progress_snapshots').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM anxin_board_reports').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM model_execution_results').fetchone()[0] == 1
        assert conn.execute('SELECT state FROM report_generation_tasks WHERE id=?', (replacement['replacement_task']['id'],)).fetchone()[0] == 'voided'
