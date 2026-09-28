"""Real SQLite report/annotation/send ledgers, no network or customer storage."""
from dataclasses import replace

import pytest

from test_approved_module_narrative import state, client, URL
from test_approved_report_git_metrics import metrics_state
from app import db, mail_send_attempts as sends
from app.mail_gateway import MailSendResult, RecipientResult, RecipientOutcome
from app.mail_report_renderer import render_approved_report_for_mail
from app.mail_document_revision import current_document_predecessor
from app.approved_report_narrative import load_approved_report_narrative
from app.approved_report_git_metrics import load_approved_report_git_metrics


@pytest.fixture
def delivery(metrics_state, client, tmp_path, monkeypatch, request):
    state = metrics_state
    historical_version = getattr(request, 'param', 10)
    payload = {**state['payload'], 'anxin_board_report_hash': state['report']['anxin_board_report_hash'],
               'approval_snapshot_hash': state['approval_snapshot']['approval_snapshot_hash']}
    result = client.post(URL, json=payload)
    assert result.status_code == 200, result.text
    annotation = result.json()
    # Use the product's real send DDL with the fixture's real approved authorities.
    monkeypatch.setenv('ANXINBOARD_DB_PATH', str(tmp_path/'mail-schema.sqlite3'))
    db.init_db()
    with db.get_connection() as template:
        ddl = [r['sql'] for r in template.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND (tbl_name='mail_send_attempts' OR tbl_name='mail_send_recipient_results') ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END")]
    with state['connect']() as conn:
        for statement in ddl:
            conn.execute(statement)
    monkeypatch.setattr(sends, 'get_connection', state['connect'])
    report, approval = state['report'], state['approval_snapshot']
    old = sends.create_send_attempt(sends.SendAttemptBinding(
        send_attempt_id='old-document', project_id=1,
        approval_snapshot_id=approval['approval_snapshot_id'], approval_snapshot_hash=approval['approval_snapshot_hash'],
        report_version_id=report['report_version_id'], report_content_hash=report['report_content_hash'],
        render_identity=f'mail_report_render_v{historical_version}:'+report['anxin_board_report_hash']+':'+annotation['module_narrative_hash'],
        render_hash='a'*64, html_sha256='b'*64,
        message_id=f'<anxin-r{historical_version}-'+'a'*32+'@anxinboard.local>',
        to_recipients=('client@example.test',), subject='Saved document', from_identity='reports@example.test',
    ))
    with state['connect']() as conn:
        narrative = load_approved_report_narrative(project_id=1, report=report, approval_snapshot=approval, conn=conn)
    render = render_approved_report_for_mail(report, profile=state['profile'], approval_snapshot=approval, approved_narrative=narrative,
                                           approved_module_narrative=annotation,
                                           approved_git_metrics=load_approved_report_git_metrics(1, report, approval))
    revised = sends.SendAttemptBinding(
        send_attempt_id='revised-document', project_id=1,
        approval_snapshot_id=old.approval_snapshot_id, approval_snapshot_hash=old.approval_snapshot_hash,
        report_version_id=old.report_version_id, report_content_hash=old.report_content_hash,
        render_identity=render.render_identity, render_hash=render.render_hash, html_sha256=render.html_sha256,
        message_id='<anxin-r11-'+render.render_hash[:32]+'@anxinboard.local>',
        to_recipients=old.to_recipients, subject=render.subject, from_identity=old.from_identity,
        predecessor_send_attempt_id=old.send_attempt_id,
    )
    return state, old, revised, render


def _sent(old):
    sends.claim_prepared_attempt(old.send_attempt_id)
    return sends.record_send_result(old.send_attempt_id, MailSendResult(
        old.message_id, old.to_recipients,
        tuple(RecipientResult(r, RecipientOutcome.ACCEPTED, 'MAIL_GATEWAY_ACCEPTED', 'Accepted') for r in old.to_recipients),
    ))


def test_confirmed_revision_creates_new_document_and_preserves_delivered_record(delivery):
    state, old, revised, render = delivery
    terminal = _sent(old)
    assert current_document_predecessor(1, old.report_version_id, render) == old.send_attempt_id
    fresh = sends.create_send_attempt(revised)
    assert fresh.state == 'prepared' and fresh.message_id != terminal.message_id
    assert sends.get_send_attempt(old.send_attempt_id) == terminal
    assert fresh.predecessor_send_attempt_id == terminal.send_attempt_id
    # Same current document selects the same lineage; it cannot create another attempt.
    assert current_document_predecessor(1, old.report_version_id, render) == old.send_attempt_id
    with pytest.raises(sends.MailSendAttemptError):
        sends.create_send_attempt(replace(revised, send_attempt_id='duplicate-document'))
    _sent(fresh)
    assert current_document_predecessor(1, old.report_version_id, render) == old.send_attempt_id
    with pytest.raises(sends.MailSendAttemptError):
        sends.create_send_attempt(replace(revised, send_attempt_id='duplicate-after-sent'))
    assert len(sends.list_send_attempt_history(1)) == 2


def test_v11_revision_gate_accepts_sent_v10_with_same_confirmed_module(delivery):
    state, old, revised, render = delivery
    terminal = _sent(old)
    # The ledger accepts a foreign renderer's frozen binding; its version gate
    # must accept v11 while leaving the stored v10 identity untouched.
    revised = replace(revised, render_identity='mail_report_render_v11:' + render.render_identity.split(':', 1)[1])
    fresh = sends.create_send_attempt(revised)
    assert fresh.state == 'prepared'
    assert fresh.message_id.startswith('<anxin-r11-')
    assert sends.get_send_attempt(old.send_attempt_id) == terminal


@pytest.mark.parametrize('delivery', [9], indirect=True)
def test_frozen_v10_format_cannot_create_a_new_revision_from_sent_v9(delivery):
    _, old, revised, render = delivery
    terminal = _sent(old)
    historical_identity = 'mail_report_render_v10:' + render.render_identity.split(':', 1)[1]
    historical = replace(revised, render_identity=historical_identity,
                         message_id='<anxin-r10-' + render.render_hash[:32] + '@anxinboard.local>')
    with pytest.raises(sends.MailSendAttemptError):
        sends.create_send_attempt(historical)
    with pytest.raises(sends.MailSendAttemptError):
        current_document_predecessor(1, old.report_version_id, replace(render, render_identity=historical_identity))
    assert sends.get_send_attempt(old.send_attempt_id) == terminal
    assert len(sends.list_send_attempt_history(1)) == 1


@pytest.mark.parametrize('historical_version', [9, 10])
def test_schema_upgrade_keeps_frozen_sent_identity_and_results(delivery, monkeypatch, historical_version):
    _, old, _, _ = delivery
    monkeypatch.setattr(sends, 'get_connection', db.get_connection)
    binding = sends.SendAttemptBinding(
        send_attempt_id=old.send_attempt_id, project_id=old.project_id,
        approval_snapshot_id=old.approval_snapshot_id, approval_snapshot_hash=old.approval_snapshot_hash,
        report_version_id=old.report_version_id, report_content_hash=old.report_content_hash,
        render_identity=f'mail_report_render_v{historical_version}:' + old.render_identity.split(':', 1)[1],
        render_hash=old.render_hash, html_sha256=old.html_sha256,
        message_id=f'<anxin-r{historical_version}-' + old.render_hash[:32] + '@anxinboard.local>',
        to_recipients=old.to_recipients, subject=old.subject, from_identity=old.from_identity,
    )
    frozen = _sent(sends.create_send_attempt(binding))
    with db.get_connection() as conn:
        before = {t: [tuple(r) for r in conn.execute('SELECT * FROM ' + t)] for t in
                  ('mail_send_attempts', 'mail_send_recipient_results')}
    db.init_db()
    assert sends.get_send_attempt(old.send_attempt_id) == frozen
    with db.get_connection() as conn:
        for table, rows in before.items():
            assert [tuple(r) for r in conn.execute('SELECT * FROM ' + table)] == rows


@pytest.mark.parametrize('change', ['recipients','approval','hash','message','old-version','no-predecessor','corrupt-source'])
def test_revision_cannot_change_frozen_scope_or_bypass_its_own_confirmation(delivery, change):
    state, old, revised, render = delivery
    _sent(old)
    if change == 'recipients': revised=replace(revised,to_recipients=('different@example.test',))
    if change == 'approval': revised=replace(revised,approval_snapshot_hash='f'*64)
    if change == 'hash': revised=replace(revised,render_identity=render.render_identity[:-64]+'f'*64)
    if change == 'message': revised=replace(revised,message_id=old.message_id)
    if change == 'old-version': revised=replace(revised,render_identity=old.render_identity,message_id='<anxin-r10-'+revised.render_hash[:32]+'@anxinboard.local>')
    if change == 'no-predecessor': revised=replace(revised,predecessor_send_attempt_id=None)
    if change == 'corrupt-source':
        with state['connect']() as conn: conn.execute("UPDATE brownfield_baseline_outputs SET output_json='{}'")
    with pytest.raises(sends.MailSendAttemptError): sends.create_send_attempt(revised)
    assert len(sends.list_send_attempt_history(1)) == 1


@pytest.mark.parametrize('state_name', ['prepared','sending','unknown','failed'])
def test_uncertain_or_unfinished_prior_mail_cannot_be_retried_as_a_revision(delivery,state_name):
    state, old, revised, render = delivery
    if state_name != 'prepared': sends.claim_prepared_attempt(old.send_attempt_id)
    if state_name == 'unknown': sends.recover_stale_sending_as_unknown(old.send_attempt_id)
    if state_name == 'failed': sends.close_pre_send_failure(old.send_attempt_id,code='MAIL_GATEWAY_CALL_FAILED')
    with pytest.raises(sends.MailSendAttemptError): sends.create_send_attempt(revised)
    with pytest.raises(sends.MailSendAttemptError): current_document_predecessor(1,old.report_version_id,render)
