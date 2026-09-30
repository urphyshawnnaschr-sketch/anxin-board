"""Frozen native-image delivery with real isolated SQLite and no external services."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
import json
import sqlite3
import threading

import pytest
from app.secret_store import InMemorySecretStore
from test_approved_module_narrative import state  # noqa: F401
from test_approved_report_git_metrics import metrics_state  # noqa: F401


@pytest.fixture
def delivery(metrics_state, monkeypatch):
    from app import wechat_delivery_service as service, wechat_delivery_store as store
    monkeypatch.setenv('ANXINBOARD_DB_PATH', str(metrics_state['path']))
    secrets = InMemorySecretStore()
    monkeypatch.setattr(service, '_secret_store_factory', lambda: secrets)
    store.ensure_wechat_delivery_schema()
    images = (SimpleNamespace(png=b'\x89PNG\r\n\x1a\nsynthetic-page-1', width=720, height=900),
              SimpleNamespace(png=b'\x89PNG\r\n\x1a\nsynthetic-page-2', width=720, height=700))
    monkeypatch.setattr(service, 'render_report_images', lambda html: images)
    calls = []
    def send(config, token, png, **kwargs):
        calls.append((config, token, png, kwargs))
        return SimpleNamespace(state='accepted', code='WECHAT_GATEWAY_ACCEPTED', message_id=None)
    monkeypatch.setattr(service, 'send_image', send)
    config = dict(expected_version_no=0, gateway_url='http://127.0.0.1:18789', account_id='synthetic-account',
                  target='synthetic-user@im.wechat', recipient_label='合成接收人', session_key='synthetic-session', token='synthetic-secret')
    service.save_settings(1, config)
    report = metrics_state['report']
    payload = dict(expected_report_version_id=report['report_version_id'],
        expected_report_hash=report['anxin_board_report_hash'], expected_module_narrative_hash=None,
        expected_config_version_no=1)
    return dict(**metrics_state, service=service, store=store, secrets=secrets,
                calls=calls, config=config, preview_payload=payload, images=images)


def preview(d):
    return d['service'].create_preview(1, d['preview_payload'])


def send(d, p, key='synthetic-send-1'):
    return d['service'].send_preview(1, p['preview_id'], key, human_confirmed=True)


def test_settings_token_is_opaque_and_endpoint_rotation_requires_new_token(delivery):
    d=delivery; s=d['service']
    public=s.get_settings(1)
    assert public['token_configured'] and public['version_no']==1
    assert 'synthetic-secret' not in json.dumps(public) and 'secret_ref' not in public
    with closing(d['connect']()) as conn:
        assert 'synthetic-secret' not in '\n'.join(conn.iterdump())
    changed={**d['config'], 'expected_version_no':1, 'gateway_url':'https://example.invalid'}
    changed.pop('token')
    with pytest.raises(s.DeliveryError, match='WECHAT_TOKEN_REQUIRED'): s.save_settings(1,changed)
    assert s.get_settings(1)==public
    changed.update(gateway_url=public['gateway_url'], recipient_label='更新名称')
    assert s.save_settings(1,changed)['version_no']==2


def test_preview_freezes_every_page_and_send_uses_exact_bytes_once(delivery):
    d=delivery;p=preview(d)
    assert len(p['pages'])==2 and p['recipient_label']=='合成接收人'
    for index,page in enumerate(p['pages'],1):
        assert page['index']==index
        assert d['service'].get_image(1,p['preview_id'],index)==d['images'][index-1].png
    result=send(d,p)
    assert result['state']=='accepted' and [x['state'] for x in result['pages']]==['accepted','accepted']
    assert [c[2] for c in d['calls']]==[i.png for i in d['images']]
    assert send(d,p)==result
    p2=preview(d)
    assert send(d,p2,'another-idempotency-key')==result
    assert len(d['calls'])==2


@pytest.mark.parametrize('change', ['draft','report','module','config'])
def test_drift_fails_closed_before_any_send(delivery,change):
    d=delivery;p=preview(d)
    if change=='config':
        d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'recipient_label':'新名称'})
    elif change=='module':
        d['preview_payload']['expected_module_narrative_hash']='a'*64
        with pytest.raises(d['service'].DeliveryError): preview(d)
        return
    else:
        with d['connect']() as conn:
            conn.execute("UPDATE report_versions SET lifecycle='draft'" if change=='draft' else
                         "UPDATE anxin_board_reports SET report_hash='changed'")
    with pytest.raises(d['service'].DeliveryError): send(d,p)
    assert not d['calls']


def test_real_formal_renderer_rejects_draft_and_never_reads_smtp(delivery,monkeypatch):
    d=delivery
    from app import mail_send_service
    # The real renderer is used by every fixture; SMTP admission is not part of preview.
    monkeypatch.setattr(mail_send_service,'read_smtp_gateway_binding',lambda **k:pytest.fail('SMTP read'),raising=False)
    assert preview(d)['report_version_id']==3
    with d['connect']() as conn:conn.execute("UPDATE report_versions SET lifecycle='reviewable'")
    with pytest.raises(d['service'].DeliveryError):preview(d)


@pytest.mark.parametrize('outcome,expected', [('rejected','partial'),('unknown','unknown'),('exception','unknown')])
def test_second_page_failure_stops_and_never_retries(delivery,monkeypatch,outcome,expected):
    d=delivery; calls=[]
    def transport(*args,**kwargs):
        calls.append(args[2])
        if len(calls)==1:return SimpleNamespace(state='accepted',code='WECHAT_GATEWAY_ACCEPTED')
        if outcome=='exception':raise TimeoutError('must-not-echo-private-provider-text')
        return SimpleNamespace(state=outcome,code='WECHAT_GATEWAY_UNKNOWN' if outcome=='unknown' else 'WECHAT_GATEWAY_REJECTED')
    monkeypatch.setattr(d['service'],'send_image',transport)
    p=preview(d);result=send(d,p)
    assert result['state']==expected
    assert 'must-not-echo' not in json.dumps(result)
    assert send(d,preview(d),'different-key')==result and len(calls)==2


def test_network_has_no_writer_lock_and_concurrent_previews_claim_once(delivery,monkeypatch):
    d=delivery;p1=preview(d);p2=preview(d);entered=threading.Event();release=threading.Event();calls=[]
    def transport(*args,**kwargs):
        with closing(d['connect']()) as conn:
            conn.execute('BEGIN IMMEDIATE');conn.rollback()
        calls.append(args[2]);entered.set();assert release.wait(10)
        return SimpleNamespace(state='accepted',code='WECHAT_GATEWAY_ACCEPTED')
    monkeypatch.setattr(d['service'],'send_image',transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(send,d,p1)
        assert entered.wait(10)
        try:
            second=pool.submit(send,d,p2,'concurrent-key').result(timeout=10)
            assert second['state']=='sending' and len(calls)==1
        finally:release.set()
        result=first.result(timeout=10)
    assert result['state']=='accepted' and len(calls)==2


def test_restart_sending_becomes_unknown_and_is_not_resumed(delivery,monkeypatch):
    d=delivery;p=preview(d)
    def crash(*a,**k):raise SystemExit('synthetic process exit')
    monkeypatch.setattr(d['service'],'send_image',crash)
    with pytest.raises(SystemExit):send(d,p)
    d['store'].recover_interrupted_sends()
    result=send(d,p)
    assert result['state']=='unknown' and result['pages'][0]['state']=='unknown'
    assert result['pages'][1]['state']=='not_sent'
    assert not d['calls']


def test_preview_and_attempt_identity_are_immutable(delivery):
    d=delivery;p=preview(d);send(d,p)
    with closing(d['connect']()) as conn:
        for sql in ("UPDATE wechat_previews SET recipient_label='other'",
                    "UPDATE wechat_preview_images SET png=X'00'",
                    "UPDATE wechat_attempts SET document_target_hash='other'",
                    'DELETE FROM wechat_attempts'):
            with pytest.raises(sqlite3.IntegrityError):conn.execute(sql)


def test_missing_confirmation_and_cross_project_image_never_send(delivery):
    d=delivery;p=preview(d)
    with pytest.raises(d['service'].DeliveryError):
        d['service'].send_preview(1,p['preview_id'],'synthetic-key',human_confirmed=False)
    with pytest.raises(d['service'].DeliveryError):d['service'].get_image(2,p['preview_id'],1)
    assert not d['calls']


def test_idempotency_key_on_existing_attempt_cannot_be_reused_for_new_target(delivery):
    d=delivery;p=preview(d);result=send(d,p)
    assert send(d,p,'second-key')==result
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'target':'another-user@im.wechat'})
    d['preview_payload']['expected_config_version_no']=2
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_IDEMPOTENCY_CONFLICT'):
        send(d,preview(d),'second-key')
    assert len(d['calls'])==2


@pytest.mark.parametrize('target',['invalid-target','a@im.wechat\n','https://example.invalid'])
def test_invalid_target_is_rejected_before_credential_storage(delivery,target):
    d=delivery
    with pytest.raises(d['service'].DeliveryError):
        d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'target':target})
    assert d['service'].get_settings(1)['version_no']==1


def test_browser_work_has_no_writer_lock_and_config_drift_cannot_freeze_preview(delivery,monkeypatch):
    d=delivery
    def render(html):
        d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'recipient_label':'变化'})
        return d['images']
    monkeypatch.setattr(d['service'],'render_report_images',render)
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_CONFIG_STALE'):preview(d)
    with closing(d['connect']()) as conn:
        assert conn.execute('SELECT count(*) FROM wechat_previews').fetchone()[0]==0


def test_credential_failure_and_screenshot_failure_do_not_consume_attempt(delivery,monkeypatch):
    d=delivery;p=preview(d)
    monkeypatch.setattr(d['service'],'_secret_store_factory',lambda:InMemorySecretStore())
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_CREDENTIAL_UNAVAILABLE'):send(d,p)
    assert d['service'].get_history(1)==[]
    def render(html):raise RuntimeError('private-runtime-output')
    monkeypatch.setattr(d['service'],'render_report_images',render)
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_SCREENSHOT_FAILED'):preview(d)
    assert d['service'].get_history(1)==[]


def test_first_page_rejection_is_terminal_failed_and_stops_remaining_pages(delivery,monkeypatch):
    d=delivery;calls=[]
    def reject(*args,**kwargs):
        calls.append(args[2]);return SimpleNamespace(state='rejected',code='provider-private-message')
    monkeypatch.setattr(d['service'],'send_image',reject)
    p=preview(d);result=send(d,p)
    assert result['state']=='failed'
    assert [x['state'] for x in result['pages']]==['rejected','not_sent']
    assert 'provider-private-message' not in json.dumps(result)
    assert send(d,preview(d),'another-key')==result and len(calls)==1


def test_configuration_drift_between_pages_stops_remaining_images(delivery,monkeypatch):
    d=delivery;calls=[]
    def accepted(*args,**kwargs):
        calls.append(args[2])
        d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'recipient_label':'变化'})
        return SimpleNamespace(state='accepted',code='WECHAT_GATEWAY_ACCEPTED')
    monkeypatch.setattr(d['service'],'send_image',accepted)
    result=send(d,preview(d))
    assert result['state']=='partial' and len(calls)==1
    assert result['pages'][1]['state']=='not_sent'


@pytest.mark.parametrize('safe_code',['GATEWAY_AUTH_REJECTED','GATEWAY_UPLOADS_DISABLED','GATEWAY_POLICY_REJECTED'])
def test_fixed_gateway_failure_reason_is_preserved_without_raw_provider_text(delivery,monkeypatch,safe_code):
    d=delivery
    monkeypatch.setattr(d['service'],'send_image',lambda *a,**k:SimpleNamespace(state='rejected',code=safe_code))
    result=send(d,preview(d))
    assert result['pages'][0]['code']==safe_code


def test_gateway_migration_does_not_repeat_document_to_same_account_and_target(delivery):
    d=delivery;result=send(d,preview(d))
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'gateway_url':'https://example.invalid',
                                 'session_key':'migrated-session'})
    d['preview_payload']['expected_config_version_no']=2
    assert send(d,preview(d),'migrated-key')==result
    assert len(d['calls'])==2


@pytest.mark.parametrize('code',['REPORT_SCREENSHOT_BLOCK_TOO_TALL','REPORT_SCREENSHOT_RUNTIME_PATH_TOO_LONG',
                                 'REPORT_SCREENSHOT_IMAGE_TOO_LARGE','REPORT_SCREENSHOT_BUSY'])
def test_screenshot_fixed_failure_reason_is_preserved(delivery,monkeypatch,code):
    d=delivery
    from app.report_screenshot import ReportScreenshotError
    def render(html):raise ReportScreenshotError(code)
    monkeypatch.setattr(d['service'],'render_report_images',render)
    with pytest.raises(d['service'].DeliveryError,match=code):preview(d)
    assert not d['service'].get_history(1)


def test_new_confirmed_module_annotation_invalidates_old_preview(delivery):
    from app.approved_module_narrative import confirm_approved_module_narrative
    d=delivery;p=preview(d);payload=deepcopy(d['payload'])
    payload.update(anxin_board_report_hash=d['report']['anxin_board_report_hash'],
                   approval_snapshot_hash=d['approval_snapshot']['approval_snapshot_hash'])
    annotation=confirm_approved_module_narrative(project_id=1,report_version_id=3,payload=payload)
    with pytest.raises(d['service'].DeliveryError):send(d,p)
    assert not d['calls']
    d['preview_payload']['expected_module_narrative_hash']=annotation['module_narrative_hash']
    assert len(preview(d)['pages'])==2


def test_schema_initialization_preserves_existing_immutable_records(delivery):
    d=delivery;p=preview(d);result=send(d,p)
    d['store'].ensure_wechat_delivery_schema()
    d['store'].ensure_wechat_delivery_schema()
    assert d['service'].get_image(1,p['preview_id'],1)==d['images'][0].png
    assert d['service'].get_history(1)==[result]


def test_reads_and_preview_do_not_open_credentials(delivery,monkeypatch):
    d=delivery
    monkeypatch.setattr(d['service'],'_secret_store_factory',lambda:pytest.fail('credential store opened'))
    assert d['service'].get_settings(1)['token_configured']
    p=preview(d)
    assert d['service'].get_image(1,p['preview_id'],1)==d['images'][0].png
    assert not d['service'].get_history(1)


@pytest.mark.parametrize('code',['GATEWAY_AUTH_REJECTED','GATEWAY_POLICY_REJECTED','GATEWAY_UPLOADS_DISABLED',
                                 'GATEWAY_TOOL_UNAVAILABLE','GATEWAY_INPUT_INVALID'])
def test_confirmed_retry_requires_new_config_new_preview_new_key_after_safe_rejection(delivery,monkeypatch,code):
    d=delivery;calls=[]
    def transport(*args,**kwargs):
        calls.append(args[2])
        return SimpleNamespace(state='rejected',code=code) if len(calls)==1 else SimpleNamespace(state='accepted',code='GATEWAY_ACCEPTED')
    monkeypatch.setattr(d['service'],'send_image',transport)
    p=preview(d);failed=send(d,p)
    assert failed['state']=='failed' and len(calls)==1
    unchanged=preview(d)
    assert send(d,unchanged,'unchanged-config')==failed and len(calls)==1
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'token':'corrected-synthetic-token'})
    d['preview_payload']['expected_config_version_no']=2
    fresh=preview(d)
    assert send(d,p,'old-preview-new-key')==failed
    assert send(d,fresh,'synthetic-send-1')==failed
    assert send(d,fresh,'new-key-on-resolved-preview')==failed
    with pytest.raises(d['service'].DeliveryError):
        d['service'].send_preview(1,fresh['preview_id'],'new-key',human_confirmed=False)
    retry_preview=preview(d)
    result=send(d,retry_preview,'new-key')
    assert result['state']=='accepted' and result['attempt_id']!=failed['attempt_id'] and len(calls)==3
    assert send(d,retry_preview,'another-key')==result and len(calls)==3
    assert send(d,unchanged,'old-alias-preview-key')==failed
    assert failed['retry_requires_config_change'] is True and result['retry_requires_config_change'] is False
    assert d['service'].get_history(1)==[result,failed]


@pytest.mark.parametrize('outcome,code',[('accepted','GATEWAY_ACCEPTED'),('unknown','GATEWAY_TIMEOUT'),
                                         ('rejected','GATEWAY_TOOL_REJECTED'),('rejected','GATEWAY_REQUEST_REJECTED')])
def test_config_rotation_never_retries_delivery_or_nonguaranteed_rejection(delivery,monkeypatch,outcome,code):
    d=delivery;calls=[]
    def transport(*args,**kwargs):
        calls.append(args[2]);return SimpleNamespace(state=outcome,code=code)
    monkeypatch.setattr(d['service'],'send_image',transport)
    result=send(d,preview(d));count=len(calls)
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'token':'corrected-synthetic-token'})
    d['preview_payload']['expected_config_version_no']=2
    assert send(d,preview(d),'new-key')==result and len(calls)==count


def test_concurrent_explicit_retries_after_config_fix_claim_only_once(delivery,monkeypatch):
    d=delivery
    monkeypatch.setattr(d['service'],'send_image',lambda *a,**k:SimpleNamespace(state='rejected',code='GATEWAY_AUTH_REJECTED'))
    failed=send(d,preview(d))
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'token':'corrected-synthetic-token'})
    d['preview_payload']['expected_config_version_no']=2
    p1=preview(d);p2=preview(d);entered=threading.Event();release=threading.Event();calls=[]
    def transport(*args,**kwargs):
        calls.append(args[2]);entered.set();assert release.wait(10)
        return SimpleNamespace(state='accepted',code='GATEWAY_ACCEPTED')
    monkeypatch.setattr(d['service'],'send_image',transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(send,d,p1,'retry-key1')
        assert entered.wait(10)
        try:
            second=pool.submit(send,d,p2,'retry-key2').result(timeout=10)
            assert second['state']=='sending' and len(calls)==1
        finally:release.set()
        result=first.result(timeout=10)
    assert result['state']=='accepted' and len(calls)==2
    assert d['service'].get_history(1)==[result,failed]


@pytest.mark.parametrize('entrypoint', ['service', 'claim'])
@pytest.mark.parametrize('cross_pair', ['old_preview_new_key', 'new_preview_old_key'])
def test_bound_preview_and_key_for_different_attempts_conflict_without_mutation(delivery,monkeypatch,entrypoint,cross_pair):
    d=delivery;calls=[]
    def transport(*args,**kwargs):
        calls.append(args[2])
        return (SimpleNamespace(state='rejected',code='GATEWAY_AUTH_REJECTED') if len(calls)==1
                else SimpleNamespace(state='accepted',code='GATEWAY_ACCEPTED'))
    monkeypatch.setattr(d['service'],'send_image',transport)
    original_preview=preview(d);failed=send(d,original_preview,'original-key')
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'token':'corrected-synthetic-token'})
    d['preview_payload']['expected_config_version_no']=2
    retry_preview=preview(d);accepted=send(d,retry_preview,'retry-key')
    assert accepted['state']=='accepted' and len(calls)==3
    candidate,key=((original_preview,'retry-key') if cross_pair=='old_preview_new_key'
                   else (retry_preview,'original-key'))
    with closing(d['connect']()) as conn:
        before='\n'.join(conn.iterdump())
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_IDEMPOTENCY_CONFLICT'):
        if entrypoint=='service':
            send(d,candidate,key)
        else:
            frozen,images=d['store'].get_preview(1,candidate['preview_id'])
            d['store'].claim_attempt(frozen,key,len(images))
    with closing(d['connect']()) as conn:
        assert '\n'.join(conn.iterdump())==before
    assert len(calls)==3
    assert send(d,original_preview,'original-key')==failed
    assert send(d,retry_preview,'retry-key')==accepted


def test_config_drift_after_claim_before_first_page_allows_explicit_unsent_recovery(delivery,monkeypatch):
    d=delivery;p=preview(d)
    original_claim=d['store'].claim_attempt
    def claim_then_rotate(*args):
        result=original_claim(*args)
        d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'recipient_label':'更新配置'})
        return result
    monkeypatch.setattr(d['store'],'claim_attempt',claim_then_rotate)
    unsent=send(d,p)
    monkeypatch.setattr(d['store'],'claim_attempt',original_claim)
    assert unsent['state']=='failed' and all(page['state']=='not_sent' for page in unsent['pages'])
    assert unsent['retry_requires_config_change'] is True and not d['calls']
    assert send(d,p,'fresh-key-on-old-preview')==unsent and not d['calls']
    d['preview_payload']['expected_config_version_no']=2
    resolved=preview(d)
    assert send(d,resolved,'synthetic-send-1')==unsent and not d['calls']
    retry=preview(d)
    with pytest.raises(d['service'].DeliveryError):
        d['service'].send_preview(1,retry['preview_id'],'explicit-retry',human_confirmed=False)
    assert not d['calls']
    accepted=send(d,retry,'explicit-retry')
    assert accepted['state']=='accepted' and accepted['attempt_id']!=unsent['attempt_id']
    assert len(d['calls'])==2 and send(d,retry,'explicit-retry')==accepted
    assert send(d,p,'new-key-for-original')==unsent and len(d['calls'])==2
