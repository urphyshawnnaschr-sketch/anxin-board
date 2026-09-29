"""Local-session and secret-safe API contract, using only synthetic state."""
import pytest
from fastapi.testclient import TestClient
from app import local_session_api, main
from test_wechat_delivery import delivery, metrics_state, state  # noqa: F401


@pytest.mark.parametrize('method,path', [
    ('get', '/wechat-settings'), ('post', '/wechat-settings'),
    ('post', '/wechat-preview'), ('post', '/wechat-send'),
    ('get', '/wechat-history'), ('get', '/wechat-preview/synthetic/images/1'),
])
def test_every_wechat_route_requires_local_session_before_storage(method, path):
    local_session_api.invalidate_local_session_guard()
    # No lifespan: an unavailable guard must fail before any application storage opens.
    client = TestClient(main.app, base_url='http://127.0.0.1:5173')
    response = getattr(client, method)('/api/projects/1' + path)
    assert response.status_code == 503, response.text
    assert response.json()['detail']['code'] == 'LOCAL_SESSION_UNAVAILABLE'


@pytest.fixture
def wechat_client(delivery):
    local_session_api.configure_local_session_guard('synthetic-wechat-bootstrap')
    client=TestClient(main.app,base_url='http://127.0.0.1:5173')
    response=client.post('/api/local-session/exchange',json={'bootstrap_secret':'synthetic-wechat-bootstrap'},
                         headers={'Origin':'http://127.0.0.1:5173'})
    client.headers.update({'X-Anxin-Session':response.json()['session_token'],
                           'X-Request-Id':'wechat-tests','Origin':'http://127.0.0.1:5173',
                           'Local-Idempotency-Key':'wechat-api-test'})
    try:yield client
    finally:local_session_api.invalidate_local_session_guard();client.close()


def test_guarded_png_is_exact_and_history_is_safe(delivery,wechat_client):
    d=delivery;c=wechat_client;base='/api/projects/1'
    response=c.post(base+'/wechat-preview',json=d['preview_payload'])
    assert response.status_code==200,response.text
    preview=response.json()
    image=c.get(preview['pages'][0]['url'])
    assert image.status_code==200 and image.content==d['images'][0].png
    assert image.headers['cache-control']=='no-store' and image.headers['content-type']=='image/png'
    send=c.post(base+'/wechat-send',json={'preview_id':preview['preview_id'],'human_confirmed':True})
    assert send.status_code==200 and send.json()['state']=='accepted'
    history=c.get(base+'/wechat-history')
    assert history.json()==[send.json()] and history.headers['cache-control']=='no-store'
    assert 'synthetic-secret' not in history.text
    c.headers.pop('X-Anxin-Session')
    assert c.get(preview['pages'][0]['url']).status_code==403


@pytest.mark.parametrize('body',[
    '{"token":"synthetic-private-token",',
    '{"token":{"synthetic-private-token":true}}',
    '["synthetic-private-token"]',
    '{"token":"synthetic-private-token","unexpected":true}',
])
def test_bad_body_never_echoes_secret_or_produces_pydantic_422(wechat_client,body):
    result=wechat_client.post('/api/projects/1/wechat-settings',content=body)
    assert result.status_code==400
    assert 'synthetic-private-token' not in result.text and 'input' not in result.text


def test_request_body_is_not_read_before_guard(wechat_client):
    wechat_client.headers.pop('X-Anxin-Session')
    result=wechat_client.post('/api/projects/1/wechat-settings',content='{"token":"private"')
    assert result.status_code==403 and 'private' not in result.text
