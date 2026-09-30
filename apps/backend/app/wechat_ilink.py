"""Bounded direct iLink QR and PNG transport; never runs an agent or replies.

Wire contract: Tencent/openclaw-weixin 2.4.9, commit 24de5c9e.
Only fixed official HTTPS endpoints are permitted. All transports are one-shot;
in particular a lost sendmessage response never triggers another submission.
"""

from __future__ import annotations

import base64
from contextvars import ContextVar
import hashlib
import json
import logging
import re
import secrets
from urllib.parse import urlencode, urlsplit

import httpx

from .wechat_gateway import GatewayResult, _valid_png


DEFAULT_API_BASE = 'https://ilinkai.weixin.qq.com'
DEFAULT_CDN_BASE = 'https://novac2c.cdn.weixin.qq.com/c2c'
_API_HOSTS = frozenset({'ilinkai.weixin.qq.com'})
_CDN_HOSTS = frozenset({'novac2c.cdn.weixin.qq.com'})
_BASE_INFO = {'channel_version': '2.4.9', 'bot_agent': 'AnxinBoard/1.0'}
_MAX_RESPONSE = 65_536
_MAX_UPDATES = 262_144
_OWNER = re.compile(r'[A-Za-z0-9_.-]{1,180}@im\.wechat\Z')
_BOT = re.compile(r'[A-Za-z0-9_.-]{1,180}@im\.bot\Z')
_FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.png\Z')
_STATUSES = frozenset({'wait', 'scaned', 'confirmed', 'expired', 'need_verifycode',
                       'verify_code_blocked', 'scaned_but_redirect', 'binded_redirect'})
ILINK_RESULT_CODES = frozenset({
    'ILINK_ACCEPTED', 'ILINK_INPUT_INVALID', 'ILINK_UPLOAD_FAILED', 'ILINK_AUTH_REJECTED',
    'ILINK_SESSION_EXPIRED', 'ILINK_RATE_LIMITED', 'ILINK_REQUEST_REJECTED',
    'ILINK_RESPONSE_UNVERIFIED', 'ILINK_SEND_UNCERTAIN',
})
_ERROR_CODES = ILINK_RESULT_CODES | {'ILINK_TIMEOUT', 'ILINK_NETWORK_ERROR', 'ILINK_REDIRECT_REJECTED'}


class IlinkError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else 'ILINK_REQUEST_REJECTED'
        super().__init__(self.code)


# httpx logs request URLs at INFO; the official polling URL contains the QR
# session and possibly a verification code. Suppress transport diagnostics only
# in this call context, preserving unrelated HTTP diagnostics in other threads.
_sensitive_request = ContextVar('ilink_sensitive_request', default=False)


class _SensitiveRequestFilter(logging.Filter):
    def filter(self, record):
        return not _sensitive_request.get()


for _logger_name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2',
                     'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(_logger_name).addFilter(_SensitiveRequestFilter())


def _opaque(value, maximum=8192, *, empty=False, spaces=False) -> bool:
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and all((32 if spaces else 33) <= ord(char) <= 126 for char in value))


def _url(value: str, hosts: frozenset[str], *, cdn=False) -> str:
    try:
        if not _opaque(value, 8192) or '\\' in value:
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or '#' in value):
            raise ValueError
        if cdn:
            if parsed.path != '/c2c/upload' or not parsed.query:
                raise ValueError
        elif parsed.path not in ('', '/') or '?' in value:
            raise ValueError
        if parsed.netloc not in (parsed.hostname, parsed.hostname + ':443'):
            raise ValueError
        return value if cdn else 'https://' + parsed.hostname
    except (ValueError, TypeError, AttributeError):
        raise IlinkError('ILINK_INPUT_INVALID') from None


def normalize_base_url(value: str) -> str:
    return _url(value, _API_HOSTS)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise IlinkError('ILINK_RESPONSE_UNVERIFIED') from None


def _check_result(body: dict, *, require_ret=False) -> None:
    if require_ret and 'ret' not in body:
        raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
    for key in ('ret', 'errcode'):
        if key not in body:
            continue
        value = body[key]
        if type(value) is not int:
            raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
        if value == -14:
            raise IlinkError('ILINK_SESSION_EXPIRED')
        if value != 0:
            raise IlinkError('ILINK_REQUEST_REJECTED')


def _headers(*, post=False, token=None) -> dict:
    result = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': '132105', 'Accept-Encoding': 'identity'}
    if post:
        result.update({'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                       'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
    if token is not None:
        if not _opaque(token):
            raise IlinkError('ILINK_INPUT_INVALID')
        result['Authorization'] = 'Bearer ' + token
    return result


class IlinkClient:
    def __init__(self, *, transport=None):
        self._transport = transport

    def _request(self, method, url, *, headers, body=None, data=None, timeout=15, maximum=_MAX_RESPONSE):
        guard = _sensitive_request.set(True)
        try:
            transport = self._transport if self._transport is not None else httpx.HTTPTransport(retries=0, trust_env=False)
            with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(timeout)) as client:
                with client.stream(method, url, headers=headers, json=body, content=data) as response:
                    if 300 <= response.status_code < 400:
                        raise IlinkError('ILINK_REDIRECT_REJECTED')
                    if response.status_code in (401, 403):
                        raise IlinkError('ILINK_AUTH_REJECTED')
                    if response.status_code == 429:
                        raise IlinkError('ILINK_RATE_LIMITED')
                    if response.status_code != 200:
                        raise IlinkError('ILINK_NETWORK_ERROR' if response.status_code >= 500 or response.status_code == 408
                                         else 'ILINK_REQUEST_REJECTED')
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
                    raw = bytearray()
                    for part in response.iter_bytes():
                        if len(raw) + len(part) > maximum:
                            raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
                        raw.extend(part)
                    return bytes(raw), response.headers
        except IlinkError:
            raise
        except httpx.TimeoutException:
            raise IlinkError('ILINK_TIMEOUT') from None
        except (httpx.HTTPError, OSError, ValueError):
            raise IlinkError('ILINK_NETWORK_ERROR') from None
        finally:
            _sensitive_request.reset(guard)

    def _api(self, endpoint, *, base_url=DEFAULT_API_BASE, token=None, payload=None, query=None, timeout=15, maximum=_MAX_RESPONSE):
        base = normalize_base_url(base_url)
        url = base + '/ilink/bot/' + endpoint
        if query:
            url += '?' + urlencode(query)
        post = payload is not None
        raw, _ = self._request('POST' if post else 'GET', url, headers=_headers(post=post, token=token),
                               body=payload, timeout=timeout, maximum=maximum)
        result = _json(raw)
        _check_result(result)
        return result

    def start_login(self) -> dict:
        body = self._api('get_bot_qrcode', query={'bot_type': '3'}, payload={'local_token_list': []})
        if not _opaque(body.get('qrcode'), 4096) or not _opaque(body.get('qrcode_img_content'), 8192):
            raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
        return {'qrcode': body['qrcode'], 'qrcode_content': body['qrcode_img_content']}

    def poll_login(self, qrcode, *, base_url=DEFAULT_API_BASE, verification_code=None) -> dict:
        if not _opaque(qrcode, 4096):
            raise IlinkError('ILINK_INPUT_INVALID')
        query = {'qrcode': qrcode}
        if verification_code is not None:
            if not isinstance(verification_code, str) or re.fullmatch(r'[0-9]{4,12}', verification_code) is None:
                raise IlinkError('ILINK_INPUT_INVALID')
            query['verify_code'] = verification_code
        try:
            body = self._api('get_qrcode_status', base_url=base_url, query=query, timeout=35)
        except IlinkError as error:
            # Official long polls can expire normally while the QR stays valid.
            # Keep the same session; the caller controls its deadline and polls.
            if error.code == 'ILINK_TIMEOUT':
                return {'status': 'wait'}
            raise
        status = body.get('status')
        if not isinstance(status, str) or status not in _STATUSES:
            raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
        result = {'status': status}
        if status == 'confirmed':
            if (not _opaque(body.get('bot_token')) or not isinstance(body.get('ilink_bot_id'), str)
                    or not _BOT.fullmatch(body['ilink_bot_id']) or not isinstance(body.get('ilink_user_id'), str)
                    or not _OWNER.fullmatch(body['ilink_user_id'])):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            try:
                base = normalize_base_url(body.get('baseurl'))
            except IlinkError:
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED') from None
            result.update({key: body[key] for key in ('bot_token', 'ilink_bot_id', 'ilink_user_id')})
            result['baseurl'] = base
        elif status == 'scaned_but_redirect':
            host = body.get('redirect_host')
            if not isinstance(host, str) or host not in _API_HOSTS:
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            result['redirect_host'] = host
        return result

    def get_updates(self, *, base_url, token, cursor='') -> dict:
        if not _opaque(cursor, 16384, empty=True):
            raise IlinkError('ILINK_INPUT_INVALID')
        try:
            body = self._api('getupdates', base_url=base_url, token=token,
                             payload={'get_updates_buf': cursor, 'base_info': dict(_BASE_INFO)}, timeout=35, maximum=_MAX_UPDATES)
        except IlinkError as error:
            # A timed-out read cannot advance the cursor or supply a context.
            if error.code == 'ILINK_TIMEOUT':
                return {'cursor': cursor, 'messages': []}
            raise
        # GetUpdatesResp.ret is optional in the official proto JSON contract.
        # Without it, require an update field whose type/bounds we validate below;
        # an empty or unrelated JSON object is not a verified poll response.
        _check_result(body)
        if 'ret' not in body and not {'msgs', 'get_updates_buf'} & body.keys():
            raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
        next_cursor = body.get('get_updates_buf', cursor)
        # The official GetUpdatesResp makes msgs optional for an empty poll.
        # A present null or malformed value is still not a valid message list.
        messages = body.get('msgs', [])
        if not _opaque(next_cursor, 16384, empty=True) or not isinstance(messages, list) or len(messages) > 1000:
            raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
        safe = []
        for item in messages:
            if not isinstance(item, dict):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            sender = item.get('from_user_id', '')
            kind = item.get('message_type', 0)
            context = item.get('context_token', '')
            group = item.get('group_id', '')
            if (not _opaque(sender, 200, empty=True) or type(kind) is not int or kind not in (0, 1, 2)
                    or not _opaque(context, 8192, empty=True) or not _opaque(group, 256, empty=True)):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            projection = {'from_user_id': sender, 'message_type': kind, 'context_token': context}
            if group:
                projection['group_id'] = group
            safe.append(projection)
        return {'cursor': next_cursor, 'messages': safe}

    def send_image(self, config, token, png, *, context_token, filename, caption) -> GatewayResult:
        try:
            if (not isinstance(config, dict) or not _opaque(token)
                    or (context_token is not None and not _opaque(context_token))
                    or not isinstance(config.get('target'), str) or not _OWNER.fullmatch(config['target'])
                    or not isinstance(filename, str) or not _FILENAME.fullmatch(filename) or '..' in filename
                    or not _valid_png(png)):
                raise IlinkError('ILINK_INPUT_INVALID')
            base = normalize_base_url(config.get('base_url'))
        except IlinkError:
            return GatewayResult('rejected', 'ILINK_INPUT_INVALID')
        # Caption is intentionally not sent: the frozen PNG already contains the
        # report. Tencent's optional caption is another message and side effect.
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            key = secrets.token_bytes(16)
            key_hex = key.hex()
            filekey = secrets.token_hex(16)
            padder = padding.PKCS7(128).padder()
            padded = padder.update(png) + padder.finalize()
            encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
            ciphertext = encryptor.update(padded) + encryptor.finalize()
            upload = self._api('getuploadurl', base_url=base, token=token, payload={
                'filekey': filekey, 'media_type': 1, 'to_user_id': config['target'],
                'rawsize': len(png), 'rawfilemd5': hashlib.md5(png, usedforsecurity=False).hexdigest(),
                'filesize': len(ciphertext), 'no_need_thumb': True, 'aeskey': key_hex,
                'base_info': dict(_BASE_INFO),
            })
            full_url = upload.get('upload_full_url')
            if full_url is not None and (not isinstance(full_url, str) or not full_url):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            if not full_url:
                param = upload.get('upload_param')
                if not _opaque(param, 8192, spaces=True):
                    raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
                full_url = DEFAULT_CDN_BASE + '/upload?' + urlencode({'encrypted_query_param': param, 'filekey': filekey})
            cdn_url = _url(full_url, _CDN_HOSTS, cdn=True)
            _, response_headers = self._request('POST', cdn_url,
                headers={'Content-Type': 'application/octet-stream', 'Accept-Encoding': 'identity'}, data=ciphertext)
            download = response_headers.get('x-encrypted-param')
            if not _opaque(download, 8192):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
        except IlinkError as error:
            code = error.code if error.code in {'ILINK_AUTH_REJECTED', 'ILINK_SESSION_EXPIRED', 'ILINK_RATE_LIMITED'} else 'ILINK_UPLOAD_FAILED'
            return GatewayResult('rejected', code)
        except (ImportError, ValueError):
            return GatewayResult('rejected', 'ILINK_UPLOAD_FAILED')
        client_id = 'anxin-' + secrets.token_hex(16)
        payload = {'msg': {
            'from_user_id': '', 'to_user_id': config['target'], 'client_id': client_id,
            'message_type': 2, 'message_state': 2,
            'item_list': [{'type': 2, 'image_item': {
                'media': {'encrypt_query_param': download,
                          'aes_key': base64.b64encode(key_hex.encode()).decode(), 'encrypt_type': 1},
                'mid_size': len(ciphertext),
            }}],
        }, 'base_info': dict(_BASE_INFO)}
        if context_token is not None:
            payload['msg']['context_token'] = context_token
        try:
            body = self._api('sendmessage', base_url=base, token=token, payload=payload)
            # The official response can omit zero-valued ret. In that case only
            # a valid server message_id below confirms acceptance; {} stays unknown.
            _check_result(body, require_ret='message_id' not in body)
            message_id = body.get('message_id', client_id)
            if type(message_id) is int and 0 < message_id < 2**64:
                message_id = str(message_id)
            if ('message_id' in body and (not isinstance(message_id, str)
                    or not re.fullmatch(r'[0-9]{1,20}', message_id) or not 0 < int(message_id) < 2**64)):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            if token in message_id or (context_token is not None and context_token in message_id):
                raise IlinkError('ILINK_RESPONSE_UNVERIFIED')
            return GatewayResult('accepted', 'ILINK_ACCEPTED', message_id)
        except IlinkError as error:
            if error.code in {'ILINK_AUTH_REJECTED', 'ILINK_SESSION_EXPIRED', 'ILINK_RATE_LIMITED', 'ILINK_REQUEST_REJECTED'}:
                return GatewayResult('rejected', error.code)
            code = 'ILINK_RESPONSE_UNVERIFIED' if error.code == 'ILINK_RESPONSE_UNVERIFIED' else 'ILINK_SEND_UNCERTAIN'
            return GatewayResult('unknown', code)
