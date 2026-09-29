"""Official iLink wire contracts using synthetic HTTP transport only."""

import base64
import importlib
import json
import logging
import struct
import zlib

import httpx
import pytest


TOKEN = 'synthetic-bot-token-do-not-display'
CONTEXT = 'synthetic-owner-context-do-not-display'
OWNER = 'synthetic-owner@im.wechat'
BOT = 'synthetic-bot@im.bot'
BASE = 'https://ilinkai.weixin.qq.com'
CDN = 'https://novac2c.cdn.weixin.qq.com/c2c'


def module():
    return importlib.import_module('app.wechat_ilink')


def chunk(kind, value):
    return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))


PNG = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(b'\0\xff\xff\xff')) + chunk(b'IEND', b''))


def client_for(response):
    calls = []
    def handle(request):
        calls.append(request)
        return response(request) if callable(response) else response
    return module().IlinkClient(transport=httpx.MockTransport(handle)), calls


def test_login_start_uses_official_post_and_no_credentials():
    client, calls = client_for(httpx.Response(200, json={'qrcode': 'synthetic-qr', 'qrcode_img_content': 'https://weixin.qq.com/synthetic'}))
    assert client.start_login() == {'qrcode': 'synthetic-qr', 'qrcode_content': 'https://weixin.qq.com/synthetic'}
    request = calls[0]
    assert str(request.url) == BASE + '/ilink/bot/get_bot_qrcode?bot_type=3'
    assert request.method == 'POST'
    assert json.loads(request.content) == {'local_token_list': []}
    assert request.headers['authorizationtype'] == 'ilink_bot_token'
    assert request.headers['ilink-app-id'] == 'bot'
    assert request.headers['ilink-app-clientversion'] == '132105'
    assert 0 <= int(base64.b64decode(request.headers['x-wechat-uin'])) < 2**32
    assert 'authorization' not in request.headers


@pytest.mark.parametrize('status', ['wait', 'scaned', 'expired', 'need_verifycode', 'verify_code_blocked', 'binded_redirect'])
def test_poll_returns_safe_known_state_and_encodes_verification(status):
    client, calls = client_for(httpx.Response(200, json={'status': status, 'errmsg': TOKEN}))
    assert client.poll_login('synthetic&qr', verification_code='123456') == {'status': status}
    request = calls[0]
    assert request.url.params['qrcode'] == 'synthetic&qr'
    assert request.url.params['verify_code'] == '123456'
    assert not {'authorization', 'authorizationtype', 'x-wechat-uin'} & set(request.headers)


def test_confirmed_owner_fields_are_validated_without_exposing_provider_text():
    payload = {'status': 'confirmed', 'bot_token': TOKEN, 'ilink_bot_id': BOT,
               'baseurl': BASE, 'ilink_user_id': OWNER, 'errmsg': 'private-provider-text'}
    client, _ = client_for(httpx.Response(200, json=payload))
    assert client.poll_login('synthetic-qr') == {key: value for key, value in payload.items() if key != 'errmsg'}


@pytest.mark.parametrize('field,value', [('bot_token', ''), ('ilink_bot_id', 'bad/id'), ('baseurl', 'https://evil.invalid'), ('ilink_user_id', 'nickname')])
def test_invalid_confirmation_fails_closed(field, value):
    payload = {'status': 'confirmed', 'bot_token': TOKEN, 'ilink_bot_id': BOT, 'baseurl': BASE, 'ilink_user_id': OWNER}
    payload[field] = value
    client, _ = client_for(httpx.Response(200, json=payload))
    with pytest.raises(module().IlinkError) as caught:
        client.poll_login('synthetic-qr')
    assert caught.value.code == 'ILINK_RESPONSE_UNVERIFIED'
    assert TOKEN not in str(caught.value)


def test_updates_return_context_and_cursor_but_never_message_bodies():
    payload = {'ret': 0, 'get_updates_buf': 'cursor-2', 'msgs': [
        {'from_user_id': OWNER, 'message_type': 1, 'context_token': CONTEXT,
         'item_list': [{'type': 1, 'text_item': {'text': 'private customer text'}}]},
        {'from_user_id': 'other@im.wechat', 'message_type': 1, 'context_token': 'other-context', 'group_id': 'group-1'},
    ]}
    client, calls = client_for(httpx.Response(200, json=payload))
    result = client.get_updates(base_url=BASE, token=TOKEN, cursor='cursor-1')
    assert result == {'cursor': 'cursor-2', 'messages': [
        {'from_user_id': OWNER, 'message_type': 1, 'context_token': CONTEXT},
        {'from_user_id': 'other@im.wechat', 'message_type': 1, 'context_token': 'other-context', 'group_id': 'group-1'},
    ]}
    assert 'private customer text' not in repr(result)
    assert calls[0].headers['authorization'] == 'Bearer ' + TOKEN
    assert json.loads(calls[0].content)['get_updates_buf'] == 'cursor-1'


def test_empty_long_poll_may_omit_messages_and_preserves_cursor():
    client, calls = client_for(httpx.Response(200, json={'ret': 0}))
    assert client.get_updates(base_url=BASE, token=TOKEN, cursor='cursor-1') == {
        'cursor': 'cursor-1', 'messages': [],
    }
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', ['qr', 'updates'])
def test_idle_long_poll_timeout_keeps_session_and_cursor_without_internal_retry(endpoint):
    def timeout(request):
        raise httpx.ReadTimeout(TOKEN + CONTEXT, request=request)
    client, calls = client_for(timeout)
    if endpoint == 'qr':
        assert client.poll_login('same-synthetic-qr') == {'status': 'wait'}
        assert calls[0].url.params['qrcode'] == 'same-synthetic-qr'
    else:
        assert client.get_updates(base_url=BASE, token=TOKEN, cursor='same-cursor') == {
            'cursor': 'same-cursor', 'messages': [],
        }
        assert json.loads(calls[0].content)['get_updates_buf'] == 'same-cursor'
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', ['qr', 'updates'])
def test_non_timeout_poll_network_errors_remain_visible(endpoint):
    def fail(request):
        raise httpx.ConnectError(TOKEN, request=request)
    client, calls = client_for(fail)
    with pytest.raises(module().IlinkError) as caught:
        if endpoint == 'qr':
            client.poll_login('same-synthetic-qr')
        else:
            client.get_updates(base_url=BASE, token=TOKEN, cursor='same-cursor')
    assert caught.value.code == 'ILINK_NETWORK_ERROR'
    assert len(calls) == 1 and TOKEN not in str(caught.value)


@pytest.mark.parametrize('messages', [None, {}, ''])
def test_present_malformed_messages_cannot_masquerade_as_an_empty_poll(messages):
    client, _ = client_for(httpx.Response(200, json={'ret': 0, 'msgs': messages}))
    with pytest.raises(module().IlinkError) as caught:
        client.get_updates(base_url=BASE, token=TOKEN)
    assert caught.value.code == 'ILINK_RESPONSE_UNVERIFIED'


@pytest.mark.parametrize('url', ['http://ilinkai.weixin.qq.com', 'https://ilinkai.weixin.qq.com.evil.invalid', 'https://u:p@ilinkai.weixin.qq.com', 'https://127.0.0.1', 'https://ilinkai.weixin.qq.com:8443', BASE + '/private', BASE + '?token=bad'])
def test_untrusted_api_bases_are_rejected_before_request(url):
    client, calls = client_for(httpx.Response(200, json={'ret': 0, 'msgs': []}))
    with pytest.raises(module().IlinkError) as caught:
        client.get_updates(base_url=url, token=TOKEN)
    assert caught.value.code == 'ILINK_INPUT_INVALID'
    assert not calls


def send(client):
    return client.send_image({'base_url': BASE, 'target': OWNER}, TOKEN, PNG,
                             context_token=CONTEXT, filename='report-01.png', caption='第 1 页')


def test_image_is_aes_encrypted_and_sent_once_as_native_image(caplog):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={'upload_param': 'synthetic upload&param'})
        if len(requests) == 2:
            return httpx.Response(200, headers={'x-encrypted-param': 'synthetic-download-param'})
        return httpx.Response(200, json={'ret': 0, 'message_id': '18446744073709551614'})
    client = module().IlinkClient(transport=httpx.MockTransport(handle))
    result = send(client)
    assert (result.state, result.code, result.message_id) == ('accepted', 'ILINK_ACCEPTED', '18446744073709551614')
    assert len(requests) == 3
    prepare = json.loads(requests[0].content)
    assert requests[0].url.path == '/ilink/bot/getuploadurl'
    assert prepare['media_type'] == 1 and prepare['to_user_id'] == OWNER
    assert prepare['rawsize'] == len(PNG) and prepare['no_need_thumb'] is True
    assert len(prepare['aeskey']) == len(prepare['filekey']) == 32
    key = bytes.fromhex(prepare['aeskey'])
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(requests[1].content) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    assert unpadder.update(padded) + unpadder.finalize() == PNG
    assert prepare['filesize'] == len(requests[1].content)
    assert requests[1].url.host == 'novac2c.cdn.weixin.qq.com'
    assert requests[1].url.params['encrypted_query_param'] == 'synthetic upload&param'
    assert requests[1].headers['content-type'] == 'application/octet-stream'
    assert 'authorization' not in requests[1].headers
    payload = json.loads(requests[2].content)
    msg = payload['msg']
    assert requests[2].url.path == '/ilink/bot/sendmessage'
    assert (msg['to_user_id'], msg['message_type'], msg['message_state'], msg['context_token']) == (OWNER, 2, 2, CONTEXT)
    assert msg['from_user_id'] == '' and msg['client_id']
    assert msg['item_list'] == [{'type': 2, 'image_item': {'media': {
        'encrypt_query_param': 'synthetic-download-param',
        'aes_key': base64.b64encode(prepare['aeskey'].encode()).decode(), 'encrypt_type': 1,
    }, 'mid_size': len(requests[1].content)}}]
    assert TOKEN not in repr(result) + caplog.text and CONTEXT not in repr(result) + caplog.text


@pytest.mark.parametrize('raw', [b'{"qrcode":"a","qrcode":"b","qrcode_img_content":"x"}', b'{"status":NaN}', b'[]', b'{}', b'x' * 65537], ids=['duplicate', 'nan', 'array', 'empty', 'oversize'])
def test_malformed_or_unbounded_json_is_not_success(raw):
    client, _ = client_for(httpx.Response(200, content=raw))
    with pytest.raises(module().IlinkError) as caught:
        client.start_login()
    assert caught.value.code == 'ILINK_RESPONSE_UNVERIFIED'


def test_send_timeout_is_unknown_and_never_retried():
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={'upload_param': 'synthetic-param'})
        if len(requests) == 2:
            return httpx.Response(200, headers={'x-encrypted-param': 'synthetic-download'})
        raise httpx.ReadTimeout(TOKEN + CONTEXT, request=request)
    result = send(module().IlinkClient(transport=httpx.MockTransport(handle)))
    assert (result.state, result.code) == ('unknown', 'ILINK_SEND_UNCERTAIN')
    assert len(requests) == 3 and TOKEN not in repr(result) and CONTEXT not in repr(result)


@pytest.mark.parametrize('body', [{'ret': 0, 'errcode': -14}, {'ret': '0'}, {'ret': True}, {}, {'ret': 0, 'message_id': {'leak': TOKEN}}])
def test_send_contradictory_or_unverified_receipts_are_not_accepted(body):
    count = 0
    def handle(_):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={'upload_param': 'synthetic-param'})
        if count == 2:
            return httpx.Response(200, headers={'x-encrypted-param': 'synthetic-download'})
        return httpx.Response(200, json=body)
    result = send(module().IlinkClient(transport=httpx.MockTransport(handle)))
    assert result.state != 'accepted' and count == 3
    assert TOKEN not in repr(result)


@pytest.mark.parametrize('url', ['https://evil.invalid/upload', 'http://novac2c.cdn.weixin.qq.com/c2c/upload', 'https://novac2c.cdn.weixin.qq.com.evil.invalid/x'])
def test_untrusted_cdn_location_never_receives_png(url):
    client, calls = client_for(httpx.Response(200, json={'upload_full_url': url}))
    result = send(client)
    assert result.state == 'rejected' and len(calls) == 1


def test_redirect_is_not_followed_or_retried():
    client, calls = client_for(httpx.Response(302, headers={'Location': 'https://evil.invalid/collect'}))
    with pytest.raises(module().IlinkError) as caught:
        client.start_login()
    assert caught.value.code == 'ILINK_REDIRECT_REJECTED' and len(calls) == 1


def test_qr_and_verification_queries_do_not_enter_http_diagnostics(caplog):
    caplog.set_level(logging.DEBUG)
    client, calls = client_for(httpx.Response(200, json={'status': 'wait'}))
    client.poll_login('synthetic-private-qr', verification_code='791327')
    assert len(calls) == 1
    assert 'synthetic-private-qr' not in caplog.text and '791327' not in caplog.text
    # A different caller's diagnostics remain usable after this call ends.
    logging.getLogger('httpx').info('unrelated-http-marker')
    assert 'unrelated-http-marker' in caplog.text


@pytest.mark.parametrize('host,allowed', [('ilinkai.weixin.qq.com', True), ('evil.invalid', False),
                                         ('ilinkai.weixin.qq.com.evil.invalid', False), ('127.0.0.1', False)])
def test_protocol_redirect_only_accepts_the_fixed_verified_host(host, allowed):
    client, calls = client_for(httpx.Response(200, json={'status': 'scaned_but_redirect', 'redirect_host': host}))
    if allowed:
        assert client.poll_login('synthetic-qr') == {'status': 'scaned_but_redirect', 'redirect_host': host}
    else:
        with pytest.raises(module().IlinkError) as caught:
            client.poll_login('synthetic-qr')
        assert caught.value.code == 'ILINK_RESPONSE_UNVERIFIED'
    assert len(calls) == 1


@pytest.mark.parametrize('body', [{'status': 'unknown'}, {'status': []}, {'status': 'confirmed'}, {'status': 'scaned_but_redirect'}])
def test_poll_unknown_or_incomplete_states_fail_closed(body):
    client, _ = client_for(httpx.Response(200, json=body))
    with pytest.raises(module().IlinkError) as caught:
        client.poll_login('synthetic-qr')
    assert caught.value.code == 'ILINK_RESPONSE_UNVERIFIED'


@pytest.mark.parametrize('code', ['x1234', '', '123\n', '1' * 13])
def test_bad_verification_code_does_not_reach_network(code):
    client, calls = client_for(httpx.Response(200, json={'status': 'wait'}))
    with pytest.raises(module().IlinkError) as caught:
        client.poll_login('synthetic-qr', verification_code=code)
    assert caught.value.code == 'ILINK_INPUT_INVALID' and not calls


@pytest.mark.parametrize('payload,code', [
    ({'ret': -14}, 'ILINK_SESSION_EXPIRED'),
    ({'ret': 0, 'errcode': -14, 'msgs': []}, 'ILINK_SESSION_EXPIRED'),
    ({'ret': 1}, 'ILINK_REQUEST_REJECTED'),
    ({'ret': True, 'msgs': []}, 'ILINK_RESPONSE_UNVERIFIED'),
    ({'ret': 0, 'msgs': [{}], 'get_updates_buf': []}, 'ILINK_RESPONSE_UNVERIFIED'),
    ({'ret': 0, 'msgs': [{'message_type': True}]}, 'ILINK_RESPONSE_UNVERIFIED'),
    ({'ret': 0, 'msgs': [{'context_token': []}]}, 'ILINK_RESPONSE_UNVERIFIED'),
])
def test_updates_reject_business_errors_and_malformed_metadata(payload, code):
    client, calls = client_for(httpx.Response(200, json=payload))
    with pytest.raises(module().IlinkError) as caught:
        client.get_updates(base_url=BASE, token=TOKEN)
    assert caught.value.code == code and len(calls) == 1


@pytest.mark.parametrize('stage,status,state,code', [
    (1, 401, 'rejected', 'ILINK_AUTH_REJECTED'),
    (1, 500, 'rejected', 'ILINK_UPLOAD_FAILED'),
    (2, 500, 'rejected', 'ILINK_UPLOAD_FAILED'),
    (3, 401, 'rejected', 'ILINK_AUTH_REJECTED'),
    (3, 429, 'rejected', 'ILINK_RATE_LIMITED'),
    (3, 500, 'unknown', 'ILINK_SEND_UNCERTAIN'),
    (3, 408, 'unknown', 'ILINK_SEND_UNCERTAIN'),
    (3, 302, 'unknown', 'ILINK_SEND_UNCERTAIN'),
])
def test_each_stage_preserves_sending_certainty_without_retries(stage, status, state, code):
    calls = []
    def handle(request):
        calls.append(request)
        if len(calls) == stage:
            return httpx.Response(status, json={'errmsg': TOKEN + CONTEXT})
        if len(calls) == 1:
            return httpx.Response(200, json={'upload_param': 'synthetic-param'})
        return httpx.Response(200, headers={'x-encrypted-param': 'synthetic-download'})
    result = send(module().IlinkClient(transport=httpx.MockTransport(handle)))
    assert (result.state, result.code) == (state, code)
    assert len(calls) == stage and TOKEN not in repr(result) and CONTEXT not in repr(result)


def test_ret_zero_without_server_id_uses_safe_client_id_once():
    count = 0
    def handle(_):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={'upload_param': 'synthetic-param'})
        if count == 2:
            return httpx.Response(200, headers={'x-encrypted-param': 'synthetic-download'})
        return httpx.Response(200, json={'ret': 0})
    result = send(module().IlinkClient(transport=httpx.MockTransport(handle)))
    assert result.state == 'accepted' and result.message_id.startswith('anxin-') and count == 3


def test_input_rejection_occurs_before_upload():
    client, calls = client_for(httpx.Response(200, json={}))
    result = client.send_image({'base_url': BASE, 'target': 'nickname'}, TOKEN, PNG,
                              context_token=CONTEXT, filename='report.png', caption='')
    assert result.code == 'ILINK_INPUT_INVALID' and not calls


def test_compressed_response_is_not_decompressed_or_accepted():
    import gzip
    client, calls = client_for(httpx.Response(200, content=gzip.compress(b'{"status":"wait"}'),
                                              headers={'Content-Encoding': 'gzip'}))
    with pytest.raises(module().IlinkError) as caught:
        client.poll_login('synthetic-qr')
    assert caught.value.code == 'ILINK_RESPONSE_UNVERIFIED' and len(calls) == 1
