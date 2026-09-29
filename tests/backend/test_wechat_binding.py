"""Synthetic QR binding with real SQLite, opaque memory secrets and no network."""
import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
from contextlib import closing
import pytest
from test_wechat_delivery import state, metrics_state, delivery, preview, send  # noqa: F401


class FakeIlink:
    def __init__(self):
        self.calls=[]
        self.login={'status':'confirmed','bot_token':'synthetic-bot-token',
                    'ilink_bot_id':'abcdef@im.bot','baseurl':'https://ilinkai.weixin.qq.com',
                    'ilink_user_id':'owner@im.wechat'}
        self.updates={'cursor':'synthetic-cursor','messages':[
            {'from_user_id':'owner@im.wechat','message_type':1,'context_token':'synthetic-owner-context'}]}
    def start_login(self):
        self.calls.append('start')
        return {'qrcode':'synthetic-qr-nonce','qrcode_content':'https://example.invalid/synthetic-qr'}
    def poll_login(self,*a,**k):
        self.calls.append('poll')
        return self.login
    def get_updates(self,**k):
        self.calls.append(('updates',k.get('cursor')))
        return self.updates


@pytest.fixture
def binding(delivery,monkeypatch):
    from app import wechat_binding_service as s, wechat_binding_store as store
    d=delivery;fake=FakeIlink()
    store.ensure_wechat_binding_schema()
    monkeypatch.setattr(s,'_client_factory',lambda:fake)
    monkeypatch.setattr(s,'_secret_store_factory',lambda:d['secrets'])
    s.clear_ephemeral_flows()
    yield {**d,'binding':s,'binding_store':store,'ilink':fake}
    s.clear_ephemeral_flows()


def scan(d):
    flow=d['binding'].start_login(1,{'expected_version_no':d['service'].get_settings(1)['version_no']},'start-key')
    confirmed=d['binding'].poll_login(1,{'flow_id':flow['flow_id']},'poll-key')
    assert confirmed['status']=='awaiting_message'
    return flow


def ready(d):
    f=scan(d)
    result=d['binding'].poll_login(1,{'flow_id':f['flow_id']},'context-key')
    assert result['status']=='ready'
    return f,result['binding']


def test_qr_scan_then_owner_message_binds_without_exposing_private_material(binding):
    d=binding;flow=scan(d);s=d['binding']
    assert s.get_qr(1,flow['flow_id']).startswith(b'\x89PNG\r\n\x1a\n')
    assert d['service'].get_settings(1)['configured'] is False
    result=s.poll_login(1,{'flow_id':flow['flow_id']},'context-key')
    assert result['status']=='ready' and result['binding']['context_ready']
    settings=d['service'].get_settings(1)
    assert settings['configured'] and settings['transport']=='direct'
    assert settings['account_id']=='abcdef-im-bot' and settings['target']=='owner@im.wechat'
    with closing(d['connect']()) as conn:dump='\n'.join(conn.iterdump())
    public=json.dumps([flow,result,settings,s.get_binding(1)])
    for secret in ('synthetic-bot-token','synthetic-owner-context','synthetic-cursor','synthetic-qr-nonce','example.invalid/synthetic-qr'):
        assert secret not in dump and secret not in public
    assert not d['calls']


@pytest.mark.parametrize('message',[
    {'from_user_id':'stranger@im.wechat','message_type':1,'context_token':'stranger-context'},
    {'from_user_id':'owner@im.wechat','message_type':2,'context_token':'echo-context'},
    {'from_user_id':'owner@im.wechat','message_type':1,'context_token':'group-context','group_id':'group'},
])
def test_arbitrary_first_sender_group_or_bot_echo_cannot_bind_context(binding,message):
    d=binding;f=scan(d);d['ilink'].updates={'cursor':'cursor2','messages':[message]}
    result=d['binding'].poll_login(1,{'flow_id':f['flow_id']},'unrelated-message')
    assert result['status']=='awaiting_message' and not result['binding']['context_ready']
    assert not d['service'].get_settings(1)['configured']


def test_bound_configuration_survives_ephemeral_reset_without_network(binding):
    d=binding;_,expected=ready(d);calls=list(d['ilink'].calls)
    d['binding'].clear_ephemeral_flows()
    assert d['binding'].get_binding(1)==expected
    assert d['service'].get_settings(1)['configured']
    assert d['ilink'].calls==calls


def test_start_replay_concurrent_tab_and_expiry_are_bounded(binding,monkeypatch):
    d=binding;s=d['binding'];now=[100.0]
    monkeypatch.setattr(s,'_monotonic',lambda:now[0])
    f=s.start_login(1,{'expected_version_no':1},'start-key')
    assert s.start_login(1,{'expected_version_no':1},'start-key')==f
    with pytest.raises(s.DeliveryError,match='WECHAT_LOGIN_BUSY'):
        s.start_login(1,{'expected_version_no':1},'other-tab')
    now[0]=401
    with pytest.raises(s.DeliveryError,match='WECHAT_LOGIN_EXPIRED'):
        s.poll_login(1,{'flow_id':f['flow_id']},'late-poll')
    assert d['ilink'].calls==['start']


def test_config_rotation_during_scan_rejects_late_result_without_persisting_bot_token(binding):
    d=binding;s=d['binding'];f=s.start_login(1,{'expected_version_no':1},'start-key')
    d['service'].save_settings(1,{**d['config'],'expected_version_no':1,'recipient_label':'new-config'})
    with pytest.raises(s.DeliveryError,match='WECHAT_CONFIG_STALE'):
        s.poll_login(1,{'flow_id':f['flow_id']},'stale-poll')
    assert 'synthetic-bot-token' not in d['secrets']._values.values()


def test_disconnect_invalidates_preview_revokes_secrets_and_preserves_history(binding):
    d=binding;_,bound=ready(d)
    d['preview_payload']['expected_config_version_no']=bound['version_no'];p=preview(d)
    result=d['binding'].disconnect(1,{'expected_version_no':bound['version_no'],'human_confirmed':True})
    assert result['binding_state']=='disconnected' and not d['service'].get_settings(1)['configured']
    with pytest.raises(d['service'].DeliveryError):send(d,p)
    assert not d['calls']
    assert 'synthetic-bot-token' not in d['secrets']._values.values()
    assert 'synthetic-owner-context' not in d['secrets']._values.values()


def test_cancel_after_scan_keeps_truthful_awaiting_binding(binding):
    d=binding;f=scan(d)
    result=d['binding'].cancel_login(1,{'flow_id':f['flow_id']})
    assert result['cancelled'] and result['binding']['binding_state']=='awaiting_message'
    with pytest.raises(d['binding'].DeliveryError):d['binding'].poll_login(1,{'flow_id':f['flow_id']},'after-cancel')
    expected=result['binding']['version_no']
    refreshed=d['binding'].refresh_binding(1,{'expected_version_no':expected})
    assert refreshed['binding_state']=='ready'


@pytest.mark.parametrize('outcome,code',[('accepted','GATEWAY_ACCEPTED'),('unknown','GATEWAY_TIMEOUT')])
def test_shipped_raw_bot_account_history_still_deduplicates_after_direct_binding(binding,monkeypatch,outcome,code):
    d=binding
    gateway={**d['config'],'expected_version_no':1,'account_id':'abcdef@im.bot','target':'owner@im.wechat'}
    d['service'].save_settings(1,gateway)
    d['preview_payload']['expected_config_version_no']=2
    old=preview(d)
    # Model the exact pre-direct shipped spelling and digest, without rewriting
    # production histories or changing the integrity triggers in product code.
    from app.approved_report_delivery import digest
    with d['connect']() as conn:
        raw=dict(conn.execute('SELECT * FROM wechat_previews WHERE preview_id=?',(old['preview_id'],)).fetchone())
        old_hash=digest([1,raw['document_hash'],'abcdef@im.bot','owner@im.wechat'])
        conn.execute('DROP TRIGGER wechat_configs_no_update')
        conn.execute('DROP TRIGGER wechat_previews_no_update')
        conn.execute("UPDATE wechat_configs SET account_id='abcdef@im.bot' WHERE version_no=2")
        conn.execute('UPDATE wechat_previews SET document_target_hash=? WHERE preview_id=?',(old_hash,old['preview_id']))
    d['store'].ensure_wechat_delivery_schema()
    calls=[]
    def gateway_send(*a,**k):
        calls.append(a);return SimpleNamespace(state=outcome,code=code)
    monkeypatch.setattr(d['service'],'send_image',gateway_send)
    original=send(d,old,'legacy-key');count=len(calls)
    _,bound=ready(d)
    d['preview_payload']['expected_config_version_no']=bound['version_no']
    direct=preview(d)
    monkeypatch.setattr(d['binding'],'send_direct_image',lambda *a,**k:pytest.fail('duplicate direct send'))
    assert send(d,direct,'direct-new-key')==original
    assert send(d,direct,'legacy-key')==original and len(calls)==count


def test_mode_switch_needs_gateway_token_and_invalidates_direct_preview(binding):
    d=binding;_,bound=ready(d)
    d['preview_payload']['expected_config_version_no']=bound['version_no'];p=preview(d)
    payload={**d['config'],'expected_version_no':bound['version_no']};payload.pop('token')
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_TOKEN_REQUIRED'):
        d['service'].save_settings(1,payload)
    payload['token']='new-gateway-secret'
    assert d['service'].save_settings(1,payload)['transport']=='openclaw'
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_CONFIG_STALE'):send(d,p)
    assert 'synthetic-bot-token' not in d['secrets']._values.values() and not d['calls']


def test_poll_replay_does_not_repeat_network_or_accept_changed_verification_code(binding):
    d=binding;f=scan(d);s=d['binding']
    first=s.poll_login(1,{'flow_id':f['flow_id']},'context-key');calls=list(d['ilink'].calls)
    assert s.poll_login(1,{'flow_id':f['flow_id']},'context-key')==first
    with pytest.raises(s.DeliveryError,match='WECHAT_IDEMPOTENCY_CONFLICT'):
        s.poll_login(1,{'flow_id':f['flow_id'],'verification_code':'123456'},'context-key')
    assert d['ilink'].calls==calls


def test_protocol_already_bound_response_does_not_guess_owner_or_redirect(binding):
    d=binding;s=d['binding'];f=s.start_login(1,{'expected_version_no':1},'start-key')
    d['ilink'].login={'status':'binded_redirect'}
    with pytest.raises(s.DeliveryError,match='WECHAT_LOGIN_ALREADY_BOUND'):
        s.poll_login(1,{'flow_id':f['flow_id']},'already-bound')
    assert d['service'].get_settings(1)['version_no']==1
    assert s.start_login(1,{'expected_version_no':1},'new-start')['flow_id']!=f['flow_id']


def test_concurrent_poll_and_external_config_change_never_commit_late_context(binding,monkeypatch):
    d=binding;f=scan(d);entered=threading.Event();release=threading.Event()
    def updates(**kwargs):
        with closing(d['connect']()) as conn:conn.execute('BEGIN IMMEDIATE');conn.rollback()
        entered.set();assert release.wait(10)
        return d['ilink'].updates
    monkeypatch.setattr(d['ilink'],'get_updates',updates)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(d['binding'].poll_login,1,{'flow_id':f['flow_id']},'pending')
        assert entered.wait(10)
        with pytest.raises(d['binding'].DeliveryError,match='WECHAT_BINDING_BUSY'):
            d['binding'].poll_login(1,{'flow_id':f['flow_id']},'concurrent')
        d['service'].save_settings(1,{**d['config'],'expected_version_no':2,'token':'other-gateway'})
        release.set()
        with pytest.raises(d['binding'].DeliveryError,match='WECHAT_CONFIG_STALE'):pending.result(timeout=10)
    assert d['service'].get_settings(1)['transport']=='openclaw'
    assert 'synthetic-owner-context' not in d['secrets']._values.values()


def test_nonowner_updates_cannot_make_unready_report_preview(binding):
    d=binding;scan(d);d['preview_payload']['expected_config_version_no']=2
    with pytest.raises(d['service'].DeliveryError,match='WECHAT_BINDING_CONTEXT_REQUIRED'):preview(d)
    assert not d['calls']


@pytest.mark.parametrize('phase',['scan','context'])
def test_cancel_during_network_poll_prevents_late_binding_or_context(binding,monkeypatch,phase):
    d=binding;s=d['binding'];f=(scan(d) if phase=='context' else s.start_login(1,{'expected_version_no':1},'start-key'))
    entered=threading.Event();release=threading.Event()
    def delayed(*args,**kwargs):
        entered.set();assert release.wait(10)
        return d['ilink'].login if phase=='scan' else d['ilink'].updates
    monkeypatch.setattr(d['ilink'],'poll_login' if phase=='scan' else 'get_updates',delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(s.poll_login,1,{'flow_id':f['flow_id']},'delayed-poll')
        assert entered.wait(10)
        try:
            result=s.cancel_login(1,{'flow_id':f['flow_id']})
            assert result['cancelled']
        finally:release.set()
        with pytest.raises(s.DeliveryError,match='WECHAT_LOGIN_STALE'):pending.result(timeout=10)
    assert s.get_binding(1)['version_no']==(2 if phase=='context' else 1)
    assert 'synthetic-owner-context' not in d['secrets']._values.values()
    if phase=='scan':assert 'synthetic-bot-token' not in d['secrets']._values.values()


@pytest.mark.parametrize('terminal',['expired','verify_code_blocked'])
def test_provider_early_terminal_allows_immediate_new_flow_but_never_old_key(binding,terminal):
    d=binding;s=d['binding'];f=s.start_login(1,{'expected_version_no':1},'start-key')
    d['ilink'].login={'status':terminal}
    assert s.poll_login(1,{'flow_id':f['flow_id']},'terminal-poll')['status']==terminal
    with pytest.raises(s.DeliveryError):s.start_login(1,{'expected_version_no':1},'start-key')
    assert s.start_login(1,{'expected_version_no':1},'fresh-start')['flow_id']!=f['flow_id']


def test_empty_long_poll_preserves_configuration_and_credentials(binding):
    d=binding;s=d['binding'];f=scan(d)
    d['ilink'].updates={'cursor':'','messages':[]}
    with closing(d['connect']()) as conn:before='\n'.join(conn.iterdump())
    secrets=dict(d['secrets']._values)
    assert s.poll_login(1,{'flow_id':f['flow_id']},'empty-poll')['status']=='awaiting_message'
    with closing(d['connect']()) as conn:assert '\n'.join(conn.iterdump())==before
    assert d['secrets']._values==secrets


def test_long_opaque_protocol_values_survive_restart_and_revoke_as_bounded_secret_entries(binding):
    d=binding;s=d['binding'];d['ilink'].login['bot_token']='b'*8192
    d['ilink'].updates={'cursor':'c'*16384,'messages':[
        {'from_user_id':'owner@im.wechat','message_type':1,'context_token':'x'*8192}]}
    _,bound=ready(d)
    s.clear_ephemeral_flows()
    config=d['store'].get_config(1)
    assert s.direct_send_credentials(config)==('b'*8192,'x'*8192)
    assert all(len(v.encode('utf-16-le'))<=2048 for v in d['secrets']._values.values())
    with closing(d['connect']()) as conn:dump='\n'.join(conn.iterdump())
    assert 'b'*1024 not in dump and 'c'*1024 not in dump and 'x'*1024 not in dump
    s.disconnect(1,{'expected_version_no':bound['version_no'],'human_confirmed':True})
    assert not any(key.startswith('wcb1-') for key in d['secrets']._values)


def test_binding_context_write_failure_rolls_back_only_new_secrets(binding,monkeypatch):
    from app.secret_store import SecretStoreOSError
    d=binding;s=d['binding'];f=scan(d);before=dict(d['secrets']._values)
    original_put=d['secrets'].put
    def fail_context(ref,value):
        if value=='synthetic-owner-context':raise SecretStoreOSError()
        original_put(ref,value)
    monkeypatch.setattr(d['secrets'],'put',fail_context)
    with pytest.raises(s.DeliveryError,match='WECHAT_CREDENTIAL_UNAVAILABLE'):
        s.poll_login(1,{'flow_id':f['flow_id']},'failed-context')
    assert d['secrets']._values==before and s.get_binding(1)['version_no']==2


def test_context_poll_expiring_in_flight_cannot_commit(binding,monkeypatch):
    d=binding;s=d['binding'];now=[100.0];monkeypatch.setattr(s,'_monotonic',lambda:now[0]);f=scan(d)
    def expire(**kwargs):now[0]=401.0;return d['ilink'].updates
    monkeypatch.setattr(d['ilink'],'get_updates',expire)
    with pytest.raises(s.DeliveryError,match='WECHAT_LOGIN_EXPIRED'):
        s.poll_login(1,{'flow_id':f['flow_id']},'expired-context')
    assert s.get_binding(1)['version_no']==2 and not s.get_binding(1)['context_ready']


@pytest.mark.parametrize('raw,canonical',[
    ('ABCDEF@im.bot','abcdef-im-bot'),
    ('bot..scanner@im.bot','bot-scanner-im-bot'),
    ('--scanner..bot@im.bot','scanner-bot-im-bot'),
    ('A'*80+'@im.bot','a'*64),
    ('MixedCASE-gateway','mixedcase-gateway'),
])
def test_bot_account_identity_matches_pinned_official_normalization(raw,canonical):
    from app.wechat_delivery_store import canonical_account
    assert canonical_account(raw)==canonical
    assert canonical_account(canonical)==canonical
