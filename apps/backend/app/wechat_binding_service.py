"""Explicit QR pairing and owner-only context capture; never replies to messages."""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import hashlib
import json
import re
import struct
import threading
import time
from uuid import uuid4
import zlib

from app.approved_report_delivery import DeliveryError
from app import wechat_delivery_store as delivery, wechat_binding_store as store
from app.wechat_delivery_service import _positive,_payload,_text
from app.secret_store import SecretStoreError,SecretNotFoundError
from app.wechat_binding_secrets import put_secret,get_secret,delete_secret
from app.windows_credential_store import WindowsCredentialStore

_secret_store_factory=WindowsCredentialStore
_monotonic=time.monotonic
_TTL=300
_flows={}
_registry_lock=threading.RLock()
_project_locks=tuple(threading.Lock() for _ in range(64))
_ILINK_ERRORS={'ILINK_INPUT_INVALID','ILINK_AUTH_REJECTED','ILINK_SESSION_EXPIRED','ILINK_RATE_LIMITED',
               'ILINK_REQUEST_REJECTED','ILINK_RESPONSE_UNVERIFIED','ILINK_TIMEOUT','ILINK_NETWORK_ERROR',
               'ILINK_REDIRECT_REJECTED'}


def _client_factory():
    from app.wechat_ilink import IlinkClient
    return IlinkClient()


def clear_ephemeral_flows():
    with _registry_lock:_flows.clear()


@contextmanager
def _operation(project_id):
    _positive(project_id)
    lock=_project_locks[project_id%len(_project_locks)]
    if not lock.acquire(blocking=False):raise DeliveryError('WECHAT_BINDING_BUSY')
    try:yield
    finally:lock.release()


def _key(value):
    _text(value,128)
    if not re.fullmatch(r'[A-Za-z0-9._:-]+',value):raise DeliveryError('WECHAT_INPUT_INVALID')
    return value


def _provider(fn,*args,**kwargs):
    try:return fn(*args,**kwargs)
    except Exception as exc:
        code=getattr(exc,'code',None)
        raise DeliveryError(code if type(code) is str and code in _ILINK_ERRORS else 'WECHAT_BINDING_PROTOCOL_ERROR') from None


def _secrets():
    try:return _secret_store_factory()
    except SecretStoreError:raise DeliveryError('WECHAT_CREDENTIAL_UNAVAILABLE') from None


def _normalize_url(value):
    from app.wechat_ilink import normalize_base_url
    try:return normalize_base_url(value)
    except Exception:raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR') from None


def get_binding(project_id):
    config=delivery.get_config(_positive(project_id))
    if config is None:
        return dict(transport='direct',binding_state='unbound',version_no=0,account_id='',target='',
                    recipient_label='',context_ready=False)
    mode=config.get('transport','openclaw')
    binding=store.get_binding(project_id,config['version_no']) if mode=='direct' else None
    state=binding['binding_state'] if binding else ('ready' if mode=='openclaw' and config['secret_ref'] else 'unbound')
    return dict(transport=mode,binding_state=state,version_no=config['version_no'],
                account_id=config['account_id'],target=config['target'],recipient_label=config['recipient_label'],
                context_ready=bool(binding and state=='ready' and binding['context_ref'] and config['secret_ref']))


def _png_qr(content):
    """Render a local QR matrix as bounded grayscale PNG without external URLs."""
    _text(content,4096)
    try:
        import qrcode
        qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=4,border=4)
        qr.add_data(content);qr.make(fit=True)
        matrix=qr.get_matrix();width=len(matrix)*4
        if width>1024:raise ValueError
        raw=b''.join((b'\0'+bytes(0 if cell else 255 for bit in row for cell in [bit]*4))*4 for row in matrix)
        def chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,width,8,0,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
    except Exception:raise DeliveryError('WECHAT_QR_RENDER_FAILED') from None


def _flow(project_id,flow_id):
    _text(flow_id,128)
    with _registry_lock:flow=_flows.get(project_id)
    if flow is None or flow['flow_id']!=flow_id or flow['cancelled']:
        raise DeliveryError('WECHAT_LOGIN_STALE')
    if _monotonic()>=flow['deadline']:
        raise DeliveryError('WECHAT_LOGIN_EXPIRED')
    return flow


def _flow_response(flow):
    return dict(flow_id=flow['flow_id'],status=flow['status'],expires_at=flow['expires_at'],
                binding=get_binding(flow['project_id']) if flow['status'] in ('awaiting_message','ready') else None)


def start_login(project_id,payload,key):
    _payload(payload,('expected_version_no',));expected=_positive(payload['expected_version_no'],zero=True);_key(key)
    with _operation(project_id):
        store.assert_version(project_id,expected)
        with _registry_lock:
            for pid,value in list(_flows.items()):
                if _monotonic()>=value['deadline']:_flows.pop(pid)
            previous=_flows.get(project_id)
            if previous:
                if (previous['start_key']==key and previous['start_expected']==expected
                        and not previous['cancelled'] and previous['status'] not in ('expired','verify_code_blocked')):
                    return dict(previous['start_response'])
                if not previous['cancelled'] and previous['status'] not in ('awaiting_message','ready','expired','verify_code_blocked'):raise DeliveryError('WECHAT_LOGIN_BUSY')
                if previous['start_key']==key:raise DeliveryError('WECHAT_LOGIN_STALE')
            if len(_flows)>=32 and project_id not in _flows:raise DeliveryError('WECHAT_LOGIN_BUSY')
        value=_provider(_client_factory().start_login)
        if type(value) is not dict:raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR')
        qrcode=_text(value.get('qrcode'),4096);png=_png_qr(value.get('qrcode_content'))
        store.assert_version(project_id,expected)
        flow_id=uuid4().hex
        expires=(datetime.now(timezone.utc)+timedelta(seconds=_TTL)).isoformat()
        response=dict(flow_id=flow_id,status='wait',expires_at=expires,
                      qr_url=f'/api/projects/{project_id}/wechat-login/{flow_id}/qr.png')
        flow=dict(project_id=project_id,flow_id=flow_id,start_key=key,start_expected=expected,
                  expected_version=expected,start_response=response,deadline=_monotonic()+_TTL,
                  expires_at=expires,status='wait',qrcode=qrcode,png=png,base_url='https://ilinkai.weixin.qq.com',
                  cancelled=False,replays={},redirects=0)
        with _registry_lock:
            if len(_flows)>=32 and project_id not in _flows:raise DeliveryError('WECHAT_LOGIN_BUSY')
            _flows[project_id]=flow
        return dict(response)


def get_qr(project_id,flow_id):
    _positive(project_id)
    return _flow(project_id,flow_id)['png']


def _write_secret(secrets,value,kind,new_refs):
    try:
        ref=put_secret(secrets,value,kind);new_refs.add(ref)
        return ref
    except SecretStoreError as exc:
        new_refs.update(getattr(exc,'cleanup_refs',()))
        raise DeliveryError('WECHAT_CREDENTIAL_UNAVAILABLE') from None


def _remove_refs(secrets,refs):
    failed=False
    for ref in refs:
        try:delete_secret(secrets,ref)
        except SecretNotFoundError:pass
        except SecretStoreError:failed=True
    if failed:raise DeliveryError('WECHAT_CREDENTIAL_REVOKE_FAILED')


def retire_direct_credentials(project_id):
    """Erase old local bot secrets after an atomic configuration replacement."""
    refs=store.direct_secret_refs(project_id)
    current=delivery.get_config(project_id)
    if current and current.get('transport')=='direct':
        bound=store.get_binding(project_id,current['version_no'])
        refs.discard(current['secret_ref'])
        if bound:refs-=set((bound['context_ref'],bound['cursor_ref']))
    if refs:_remove_refs(_secrets(),refs)


def _confirm(project_id,flow,value):
    owner=value.get('ilink_user_id');account=value.get('ilink_bot_id')
    if (type(owner) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}@im\.wechat',owner)
            or type(account) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}@im\.bot',account)):
        raise DeliveryError('WECHAT_BINDING_OWNER_INVALID')
    base=_normalize_url(value.get('baseurl'));refs=set();secrets=_secrets()
    try:
        token_ref=_write_secret(secrets,value.get('bot_token'),'bot',refs)
        # Empty cursor is represented by an absent ref until the first update.
        config=dict(gateway_url=base,account_id=delivery.canonical_account(account),target=owner,
                    recipient_label='扫码绑定的微信',session_key='',secret_ref=token_ref)
        with _registry_lock:
            _flow(project_id,flow['flow_id'])
            saved=store.save_binding(project_id,config,flow['expected_version'],state='awaiting_message')
    except Exception:
        _remove_refs(secrets,refs);raise
    flow['expected_version']=saved['version_no'];flow['status']='awaiting_message'
    retire_direct_credentials(project_id)


def _secret(secrets,ref,*,empty=False):
    if empty and not ref:return ''
    try:return get_secret(secrets,ref)
    except SecretStoreError:raise DeliveryError('WECHAT_CREDENTIAL_UNAVAILABLE') from None


def _sync_context(project_id,expected,*,flow=None):
    store.assert_version(project_id,expected)
    config=delivery.get_config(project_id)
    bound=store.get_binding(project_id,expected)
    if config.get('transport')!='direct' or not bound or bound['binding_state']=='disconnected':
        raise DeliveryError('WECHAT_BINDING_REQUIRED')
    secrets=_secrets()
    token=_secret(secrets,config['secret_ref']);cursor=_secret(secrets,bound['cursor_ref'],empty=True)
    updates=_provider(_client_factory().get_updates,base_url=config['gateway_url'],token=token,cursor=cursor)
    if flow:_flow(project_id,flow['flow_id'])
    if type(updates) is not dict or type(updates.get('messages')) is not list:
        raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR')
    context=None
    for message in updates['messages']:
        if (type(message) is dict and message.get('from_user_id')==config['target']
                and type(message.get('message_type')) is int and message['message_type']==1
                and not message.get('group_id') and type(message.get('context_token')) is str
                and message['context_token']):
            context=message['context_token']
    next_cursor=updates.get('cursor')
    if type(next_cursor) is not str:raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR')
    if context is None and next_cursor==cursor:
        with _registry_lock:
            if flow:_flow(project_id,flow['flow_id'])
            store.assert_version(project_id,expected)
            return get_binding(project_id)
    refs=set()
    try:
        context_ref=_write_secret(secrets,context,'context',refs) if context else bound['context_ref']
        cursor_ref=_write_secret(secrets,next_cursor,'cursor',refs) if next_cursor else ''
        fields={k:config[k] for k in ('gateway_url','account_id','target','recipient_label','session_key','secret_ref')}
        with _registry_lock:
            if flow:_flow(project_id,flow['flow_id'])
            saved=store.save_binding(project_id,fields,expected,state='ready' if context_ref else 'awaiting_message',
                                     context_ref=context_ref,cursor_ref=cursor_ref)
    except Exception:
        _remove_refs(secrets,refs);raise
    retire_direct_credentials(project_id)
    return get_binding(project_id)


def poll_login(project_id,payload,key):
    _payload(payload,('flow_id',),('verification_code',));_key(key)
    code=payload.get('verification_code')
    if code is not None and (type(code) is not str or not re.fullmatch('[0-9]{4,12}',code)):
        raise DeliveryError('WECHAT_INPUT_INVALID')
    request_hash=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    with _operation(project_id):
        flow=_flow(project_id,payload['flow_id'])
        if key in flow['replays']:
            prior_hash,result=flow['replays'][key]
            if prior_hash!=request_hash:raise DeliveryError('WECHAT_IDEMPOTENCY_CONFLICT')
            return result
        if len(flow['replays'])>=256:raise DeliveryError('WECHAT_LOGIN_EXPIRED')
        store.assert_version(project_id,flow['expected_version'])
        if flow['status'] not in ('awaiting_message','ready','expired','verify_code_blocked'):
            value=_provider(_client_factory().poll_login,flow['qrcode'],base_url=flow['base_url'],verification_code=code)
            _flow(project_id,flow['flow_id']);store.assert_version(project_id,flow['expected_version'])
            if type(value) is not dict:raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR')
            status=value.get('status')
            if status=='confirmed':_confirm(project_id,flow,value)
            elif status=='binded_redirect':
                flow['status']='expired'
                raise DeliveryError('WECHAT_LOGIN_ALREADY_BOUND')
            elif status=='scaned_but_redirect':
                if flow['redirects']>=2:raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR')
                flow['base_url']=_normalize_url('https://'+_text(value.get('redirect_host'),200))
                flow['redirects']+=1;flow['status']='scaned'
            elif status in ('wait','scaned','expired','need_verifycode','verify_code_blocked'):flow['status']=status
            else:raise DeliveryError('WECHAT_BINDING_PROTOCOL_ERROR')
        with _registry_lock:
            _flow(project_id,flow['flow_id'])
            result=_flow_response(flow);flow['replays'][key]=(request_hash,result)
        return result


def cancel_login(project_id,payload):
    _payload(payload,('flow_id',))
    _positive(project_id)
    # Cancellation must not wait for a long-poll network request. Commits check
    # this flag under the same short registry lock after the request returns.
    with _registry_lock:
        flow=_flow(project_id,payload['flow_id']);flow['cancelled']=True
        flow['png']=b'';flow['qrcode']=''
    return dict(cancelled=True,binding=get_binding(project_id))


def refresh_binding(project_id,payload):
    _payload(payload,('expected_version_no',));expected=_positive(payload['expected_version_no'])
    with _operation(project_id):return _sync_context(project_id,expected)


def disconnect(project_id,payload):
    _payload(payload,('expected_version_no','human_confirmed'))
    expected=_positive(payload['expected_version_no'],zero=True)
    if payload['human_confirmed'] is not True:raise DeliveryError('WECHAT_HUMAN_CONFIRMATION_REQUIRED')
    with _operation(project_id):
        config=dict(gateway_url='',account_id='',target='',recipient_label='',session_key='',secret_ref='')
        store.save_binding(project_id,config,expected,state='disconnected')
        with _registry_lock:
            flow=_flows.get(project_id)
            if flow:flow.update(cancelled=True,png=b'',qrcode='')
        retire_direct_credentials(project_id)
        return get_binding(project_id)


def direct_send_credentials(config):
    bound=store.get_binding(config['project_id'],config['version_no'])
    if (config.get('transport')!='direct' or not bound
            or bound['binding_state'] not in ('awaiting_message','ready') or not config['secret_ref']
            or type(config['target']) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}@im\.wechat',config['target'])):
        raise DeliveryError('WECHAT_BINDING_REQUIRED')
    secrets=_secrets()
    return (_secret(secrets,config['secret_ref']),
            _secret(secrets,bound['context_ref']) if bound['context_ref'] else None)


def send_direct_image(config,token,png,*,context_token,filename,caption):
    return _client_factory().send_image(config,token,png,context_token=context_token,filename=filename,caption=caption)
