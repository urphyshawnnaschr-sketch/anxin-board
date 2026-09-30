"""Real formal HTML -> bundled Chromium -> API PNG -> real loopback HTTP.

Only SQLite fixtures and synthetic credentials are used. The loopback receiver
implements the documented OpenClaw envelope; it is not a real WeChat receipt.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

from app import report_screenshot, wechat_gateway
from test_wechat_delivery import delivery, metrics_state, state  # noqa: F401
from test_wechat_delivery_api import wechat_client  # noqa: F401
from test_wechat_gateway import local_gateway, ACCOUNT, TARGET


def test_real_formal_images_are_previewed_then_delivered_exactly_once(delivery, wechat_client, monkeypatch):
    d = delivery
    service = d['service']
    monkeypatch.setattr(service, 'render_report_images', report_screenshot.render_report_images)
    monkeypatch.setattr(service, 'send_image', wechat_gateway.send_image)
    client = wechat_client
    base = '/api/projects/1'
    with local_gateway() as (url, calls):
        response = client.post(base + '/wechat-settings', json={
            **d['config'], 'expected_version_no': 1, 'gateway_url': url,
            'account_id': ACCOUNT, 'target': TARGET,
        })
        assert response.status_code == 200, response.text
        assert response.json()['version_no'] == 2
        assert calls == []
        response = client.post(base + '/wechat-preview', json={
            **d['preview_payload'], 'expected_config_version_no': 2,
        })
        assert response.status_code == 200, response.text
        preview = response.json()
        assert 1 <= len(preview['pages']) <= 24
        frozen = []
        for page in preview['pages']:
            image = client.get(page['url'])
            assert image.status_code == 200
            assert image.headers['content-type'] == 'image/png'
            assert image.headers['cache-control'] == 'no-store'
            assert wechat_gateway._valid_png(image.content)
            assert hashlib.sha256(image.content).hexdigest() == page['sha256']
            assert page['width'] == 1680 and 0 < page['height'] <= 2400
            frozen.append(image.content)
        assert calls == []  # Previewing must never invoke the gateway.
        response = client.post(base + '/wechat-send', json={
            'preview_id': preview['preview_id'], 'human_confirmed': True,
        })
        assert response.status_code == 200, response.text
        attempt = response.json()
        assert attempt['state'] == 'accepted'
        assert len(calls) == len(frozen)
        assert [base64.b64decode(c['body']['args']['buffer']) for c in calls] == frozen
        assert all(c['body']['args']['channel'] == 'openclaw-weixin' and
                   c['body']['args']['target'] == TARGET and
                   c['body']['args']['contentType'] == 'image/png' for c in calls)
        assert all(not {'media', 'path', 'filePath', 'forceDocument'} & c['body']['args'].keys() for c in calls)
        response = client.post(base + '/wechat-send', json={
            'preview_id': preview['preview_id'], 'human_confirmed': True,
        })
        assert response.json() == attempt
        assert len(calls) == len(frozen)
        assert client.get(base + '/wechat-history').json() == [attempt]
        assert 'synthetic-secret' not in json.dumps(attempt)
    evidence_dir = os.environ.get('WECHAT_SYNTHETIC_EVIDENCE_DIR')
    if evidence_dir:
        destination = Path(evidence_dir)
        destination.mkdir(parents=True, exist_ok=True)
        for index, png in enumerate(frozen, 1):
            (destination / f'integration-page-{index:02d}.png').write_bytes(png)
        (destination / 'integration-result.json').write_text(json.dumps({
            'scope': 'synthetic formal report, real browser, local fake gateway; NOT real WeChat receipt',
            'report_version_id': preview['report_version_id'],
            'pages': [{k: page[k] for k in ('index', 'width', 'height', 'sha256')} for page in preview['pages']],
            'state': attempt['state'], 'http_submissions': len(calls),
            'duplicate_request_submissions': 0,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
