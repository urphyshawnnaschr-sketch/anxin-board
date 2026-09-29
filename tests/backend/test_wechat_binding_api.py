"""Guard precedence and no-secret QR/API contracts."""
import pytest
from fastapi.testclient import TestClient
from app import main,local_session_api
from test_wechat_binding import binding,delivery,metrics_state,state  # noqa: F401
from test_wechat_delivery_api import wechat_client  # noqa: F401


@pytest.mark.parametrize('method,path',[
    ('get','/wechat-binding'),('post','/wechat-login/start'),('post','/wechat-login/poll'),
    ('get','/wechat-login/private-flow/qr.png'),('post','/wechat-login/cancel'),
    ('post','/wechat-binding/refresh'),('post','/wechat-binding/disconnect')])
def test_binding_guard_before_any_storage_or_protocol(method,path):
    local_session_api.invalidate_local_session_guard()
    client=TestClient(main.app,base_url='http://127.0.0.1:5173')
    try:
        response=getattr(client,method)('/api/projects/1'+path)
        assert response.status_code==503 and response.json()['detail']['code']=='LOCAL_SESSION_UNAVAILABLE'
    finally:client.close()


def test_guarded_qr_and_binding_http_flow(binding,wechat_client):
    d=binding;c=wechat_client;url='/api/projects/1'
    started=c.post(url+'/wechat-login/start',json={'expected_version_no':1})
    assert started.status_code==200,started.text
    flow=started.json();qr=c.get(flow['qr_url'])
    assert qr.status_code==200 and qr.content.startswith(b'\x89PNG\r\n\x1a\n')
    assert qr.headers['content-type']=='image/png' and qr.headers['cache-control']=='no-store'
    c.headers['Local-Idempotency-Key']='scan-key'
    scanned=c.post(url+'/wechat-login/poll',json={'flow_id':flow['flow_id']})
    assert scanned.status_code==200 and scanned.json()['status']=='awaiting_message'
    c.headers['Local-Idempotency-Key']='owner-message-key'
    received=c.post(url+'/wechat-login/poll',json={'flow_id':flow['flow_id']})
    assert received.status_code==200 and received.json()['status']=='ready'
    assert c.get(url+'/wechat-settings').json()['configured']
    for secret in ('synthetic-bot-token','synthetic-owner-context','synthetic-cursor','synthetic-qr-nonce'):
        assert secret not in started.text+scanned.text+received.text
    c.headers.pop('X-Anxin-Session')
    assert c.get(flow['qr_url']).status_code==403
    assert not d['calls']


@pytest.mark.parametrize('path,body',[
    ('/wechat-login/start','{"expected_version_no":{"secret":"private-value"}}'),
    ('/wechat-login/poll','{"flow_id":"private-value","verification_code":"bad secret code"}'),
    ('/wechat-binding/disconnect','{"expected_version_no":1,"human_confirmed":"private-value"}'),
])
def test_binding_input_never_echoes_private_payload(binding,wechat_client,path,body):
    result=wechat_client.post('/api/projects/1'+path,content=body)
    assert result.status_code in (400,409) and 'private-value' not in result.text
    assert not binding['ilink'].calls


def test_missing_write_key_rejects_before_provider(binding,wechat_client):
    wechat_client.headers.pop('Local-Idempotency-Key')
    result=wechat_client.post('/api/projects/1/wechat-login/start',json={'expected_version_no':1})
    assert result.status_code==403 and not binding['ilink'].calls
