"""Synthetic Tencent HTTP + real formal renderer + guarded API, never a real login."""
import base64
import hashlib
import json
import os
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from test_wechat_binding import binding  # noqa: F401
from test_wechat_delivery import delivery, metrics_state, state  # noqa: F401
from test_wechat_delivery_api import wechat_client  # noqa: F401


def test_qr_owner_context_and_real_formal_pngs_are_sent_once(binding, wechat_client, monkeypatch):
    from app import report_screenshot, wechat_ilink
    d = binding
    c = wechat_client
    base = '/api/projects/1'
    uploaded, submitted, issued = [], [], []
    uploads = []

    def transport(request):
        path = request.url.path
        body = json.loads(request.content) if request.headers.get('content-type','').startswith('application/json') else None
        if path == '/ilink/bot/get_bot_qrcode':
            issued.append('qr')
            return httpx.Response(200, json={'qrcode':'synthetic-direct-nonce',
                'qrcode_img_content':'https://example.invalid/synthetic-direct-qr'})
        if path == '/ilink/bot/get_qrcode_status':
            return httpx.Response(200, json={'status':'confirmed','bot_token':'synthetic-direct-token',
                'ilink_bot_id':'abcdef@im.bot','baseurl':'https://ilinkai.weixin.qq.com',
                'ilink_user_id':'owner@im.wechat'})
        if path == '/ilink/bot/getupdates':
            # Successful protobuf JSON may omit its zero-valued ret field.
            return httpx.Response(200, json={'get_updates_buf':'synthetic-direct-cursor','msgs':[
                {'from_user_id':'other@im.wechat','message_type':1,'context_token':'synthetic-wrong-context'},
                {'from_user_id':'owner@im.wechat','message_type':1,'context_token':'synthetic-direct-context'},
            ]})
        if path == '/ilink/bot/getuploadurl':
            assert body['to_user_id'] == 'owner@im.wechat'
            uploads.append(body)
            return httpx.Response(200, json={'ret':0,'upload_param':'synthetic-upload'})
        if path == '/c2c/upload':
            assert 'authorization' not in request.headers
            value = uploads[-1]
            decryptor = Cipher(algorithms.AES(bytes.fromhex(value['aeskey'])), modes.ECB()).decryptor()
            padded = decryptor.update(request.content) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            png = unpadder.update(padded) + unpadder.finalize()
            assert len(png) == value['rawsize']
            assert hashlib.md5(png).hexdigest() == value['rawfilemd5']
            assert len(request.content) == value['filesize']
            uploaded.append(png)
            return httpx.Response(200, headers={'x-encrypted-param':'synthetic-download'})
        if path == '/ilink/bot/sendmessage':
            msg = body['msg']
            assert msg['to_user_id'] == 'owner@im.wechat'
            assert msg['context_token'] == 'synthetic-direct-context'
            assert msg['message_type'] == 2 and msg['message_state'] == 2
            assert len(msg['item_list']) == 1 and msg['item_list'][0]['type'] == 2
            media = msg['item_list'][0]['image_item']['media']
            assert base64.b64decode(media['aes_key']).decode('ascii') == uploads[-1]['aeskey']
            submitted.append(msg)
            return httpx.Response(200, json={'message_id':str(len(submitted))})
        raise AssertionError('unexpected synthetic API path: ' + path)

    ilink = wechat_ilink.IlinkClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(d['binding'], '_client_factory', lambda: ilink)
    monkeypatch.setattr(d['service'], 'render_report_images', report_screenshot.render_report_images)
    response = c.post(base + '/wechat-login/start', json={'expected_version_no':1},
                      headers={'Local-Idempotency-Key':'direct-start'})
    assert response.status_code == 200, response.text
    flow = response.json()
    qr = c.get(flow['qr_url'])
    assert qr.status_code == 200 and qr.content.startswith(b'\x89PNG\r\n\x1a\n')
    assert qr.headers['cache-control'] == 'no-store'
    first = c.post(base + '/wechat-login/poll', json={'flow_id':flow['flow_id']},
                   headers={'Local-Idempotency-Key':'direct-login'})
    assert first.status_code == 200 and first.json()['status'] == 'awaiting_message', first.text
    second = c.post(base + '/wechat-login/poll', json={'flow_id':flow['flow_id']},
                    headers={'Local-Idempotency-Key':'direct-context'})
    assert second.status_code == 200 and second.json()['status'] == 'ready', second.text
    assert submitted == [] and uploaded == []
    config_version = second.json()['binding']['version_no']
    response = c.post(base + '/wechat-preview', json={**d['preview_payload'],
                      'expected_config_version_no':config_version})
    assert response.status_code == 200, response.text
    preview = response.json()
    pngs = []
    for page in preview['pages']:
        image = c.get(page['url'])
        assert image.status_code == 200
        assert hashlib.sha256(image.content).hexdigest() == page['sha256']
        pngs.append(image.content)
    assert submitted == []
    payload = {'preview_id':preview['preview_id'],'human_confirmed':True}
    result = c.post(base + '/wechat-send', json=payload,
                    headers={'Local-Idempotency-Key':'direct-send'} )
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'accepted', result.text
    assert uploaded == pngs and len(submitted) == len(pngs)
    duplicate = c.post(base + '/wechat-send', json=payload,
                       headers={'Local-Idempotency-Key':'direct-send'})
    assert duplicate.json() == result.json()
    assert len(submitted) == len(pngs) and issued == ['qr']
    assert c.get(base + '/wechat-history').json() == [result.json()]
    public = json.dumps([flow,first.json(),second.json(),result.json()])
    assert not any(secret in public for secret in ('synthetic-direct-token','synthetic-direct-context',
                                                   'synthetic-direct-cursor','synthetic-direct-nonce'))
    destination = os.environ.get('WECHAT_DIRECT_SYNTHETIC_EVIDENCE_DIR')
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / 'result.json').write_text(json.dumps({
            'status':'PASS','scope':'synthetic QR/iLink HTTP; real renderer and guarded application API',
            'pages':len(pngs),'submitted_images':len(submitted),'duplicate_submissions':0,
            'owner_only':True,'encrypted_upload_matches_preview':True,
            'real_wechat_login':False,'real_phone_receipt':False,
            'image_sha256':[hashlib.sha256(png).hexdigest() for png in pngs],
        },ensure_ascii=False,indent=2),encoding='utf-8')
