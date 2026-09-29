"""Explicit local configuration, frozen preview and single-attempt image sending."""
import hashlib
import re
from uuid import uuid4

from app.approved_report_delivery import DeliveryError, digest, load_approved_document
from app import wechat_delivery_store as store
from app.report_screenshot import render_report_images
from app.wechat_gateway import normalize_gateway_url, send_image
from app.secret_store import SecretStoreError, validate_secret_value
from app.windows_credential_store import WindowsCredentialStore

_secret_store_factory = WindowsCredentialStore
_CONFIG_FIELDS = ('gateway_url','account_id','target','recipient_label','session_key')
_SCREENSHOT_CODES = {'REPORT_SCREENSHOT_' + code for code in (
    'BLOCK_TOO_TALL','BUSY','DOCUMENT_TOO_LARGE','EMPTY','IMAGE_TOO_LARGE','RENDER_FAILED',
    'RUNTIME_MISSING','RUNTIME_PATH_TOO_LONG','TIMEOUT','TOO_MANY_PAGES','UNSAFE_HTML',
    'ASSET_INVALID','UNSUPPORTED_LAYOUT','HORIZONTAL_OVERFLOW','LAYOUT_INVALID')}
_GATEWAY_CODES = {
    'accepted': {'GATEWAY_ACCEPTED', 'WECHAT_GATEWAY_ACCEPTED','ILINK_ACCEPTED'},
    'rejected': {'GATEWAY_AUTH_REJECTED','GATEWAY_IMAGE_TOO_LARGE','GATEWAY_INPUT_INVALID',
                 'GATEWAY_POLICY_REJECTED','GATEWAY_RATE_LIMITED','GATEWAY_REDIRECT_REJECTED',
                 'GATEWAY_REQUEST_REJECTED','GATEWAY_TOOL_REJECTED','GATEWAY_TOOL_UNAVAILABLE',
                 'GATEWAY_UPLOADS_DISABLED','WECHAT_GATEWAY_REJECTED','ILINK_INPUT_INVALID',
                 'ILINK_UPLOAD_FAILED','ILINK_AUTH_REJECTED','ILINK_SESSION_EXPIRED',
                 'ILINK_RATE_LIMITED','ILINK_REQUEST_REJECTED'},
    'unknown': {'GATEWAY_DELIVERY_UNCERTAIN','GATEWAY_RESPONSE_UNVERIFIED','GATEWAY_SERVER_UNCERTAIN',
                'GATEWAY_TIMEOUT','GATEWAY_TRANSPORT_UNCERTAIN','WECHAT_GATEWAY_UNKNOWN',
                'ILINK_RESPONSE_UNVERIFIED','ILINK_SEND_UNCERTAIN'},
}


def _positive(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 9223372036854775807:
        raise DeliveryError('WECHAT_INPUT_INVALID')
    return value


def _text(value, limit=200):
    if type(value) is not str or not value or value != value.strip() or len(value) > limit or any(ord(c)<32 or 127<=ord(c)<=159 for c in value):
        raise DeliveryError('WECHAT_INPUT_INVALID')
    return value


def _hash(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise DeliveryError('WECHAT_INPUT_INVALID')
    return value


def _payload(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise DeliveryError('WECHAT_INPUT_INVALID')


def _public_config(config):
    if config is None:
        return dict(configured=False,version_no=0,token_configured=False,transport='direct',binding_state='unbound',
                    **dict.fromkeys(_CONFIG_FIELDS,''))
    mode=config.get('transport','openclaw')
    state='ready'
    configured=bool(config['secret_ref'])
    if mode=='direct':
        from app.wechat_binding_store import get_binding
        binding=get_binding(config['project_id'],config['version_no'])
        state=binding['binding_state'] if binding else 'unbound'
        configured=bool(configured and binding and state=='ready' and binding['context_ref'])
    return dict(configured=configured,version_no=config['version_no'],token_configured=bool(config['secret_ref']),
                transport=mode,binding_state=state,
                **{key:config[key] for key in _CONFIG_FIELDS})


def get_settings(project_id):
    return _public_config(store.get_config(_positive(project_id)))


def save_settings(project_id, payload):
    _positive(project_id)
    _payload(payload, ('expected_version_no', *_CONFIG_FIELDS), ('token','transport'))
    if payload.get('transport','openclaw')!='openclaw':raise DeliveryError('WECHAT_INPUT_INVALID')
    expected = _positive(payload['expected_version_no'], zero=True)
    fields = {key:_text(payload[key],2048 if key=='gateway_url' else 200) for key in _CONFIG_FIELDS}
    try:
        fields['gateway_url'] = normalize_gateway_url(fields['gateway_url'])
    except ValueError:
        raise DeliveryError('WECHAT_GATEWAY_URL_INVALID') from None
    for key in ('account_id', 'session_key'):
        if re.fullmatch(r'[A-Za-z0-9_:@.-]{1,200}',fields[key]) is None:
            raise DeliveryError('WECHAT_INPUT_INVALID')
    if store.canonical_account(fields['account_id']) in ('','__proto__','prototype','constructor'):
        raise DeliveryError('WECHAT_INPUT_INVALID')
    if re.fullmatch(r'[A-Za-z0-9_.-]{1,180}@im\.wechat',fields['target']) is None:
        raise DeliveryError('WECHAT_INPUT_INVALID')
    current = store.get_config(project_id)
    if (current['version_no'] if current else 0) != expected:
        raise DeliveryError('WECHAT_CONFIG_STALE')
    token = payload.get('token')
    if token is None:
        if current is None or current.get('transport','openclaw')!='openclaw' or current['gateway_url'] != fields['gateway_url']:
            raise DeliveryError('WECHAT_TOKEN_REQUIRED')
        fields['secret_ref'] = current['secret_ref']
        return _public_config(store.save_config(project_id, fields, expected))
    if type(token) is not str or not token or not all(33 <= ord(c) <=126 for c in token):
        raise DeliveryError('WECHAT_TOKEN_INVALID')
    secret_ref = 'wechat-token-' + uuid4().hex
    try:
        validate_secret_value(token)
        secrets = _secret_store_factory()
        secrets.put(secret_ref, token)
    except SecretStoreError:
        raise DeliveryError('WECHAT_CREDENTIAL_UNAVAILABLE') from None
    try:
        saved=store.save_config(project_id, dict(fields, secret_ref=secret_ref), expected)
    except Exception:
        try:
            secrets.delete(secret_ref)
        except SecretStoreError:
            raise DeliveryError('WECHAT_CREDENTIAL_ROLLBACK_FAILED') from None
        raise
    if current and current.get('transport')=='direct':
        from app.wechat_binding_service import retire_direct_credentials
        retire_direct_credentials(project_id)
    return _public_config(saved)


def _preview_response(preview, images):
    return {**{key:preview[key] for key in ('preview_id','report_version_id','report_label','config_version_no','recipient_label','created_at')},
            'pages':[dict(index=i['page_index'],width=i['width'],height=i['height'],sha256=i['sha256'],
                          url=f"/api/projects/{preview['project_id']}/wechat-preview/{preview['preview_id']}/images/{i['page_index']}")
                     for i in images]}


def create_preview(project_id, payload):
    _positive(project_id)
    _payload(payload, ('expected_report_version_id','expected_report_hash','expected_module_narrative_hash','expected_config_version_no'))
    version = _positive(payload['expected_report_version_id'])
    report_hash = _hash(payload['expected_report_hash'])
    module_hash = _hash(payload['expected_module_narrative_hash'],nullable=True)
    config_version = _positive(payload['expected_config_version_no'])
    config = store.get_config(project_id)
    if config is None or config['version_no'] != config_version:
        raise DeliveryError('WECHAT_CONFIG_STALE')
    if not _public_config(config)['configured']:
        raise DeliveryError('WECHAT_BINDING_CONTEXT_REQUIRED')
    document = load_approved_document(project_id, version, report_hash, module_hash)
    try:
        images = tuple(render_report_images(document.html))
    except Exception as exc:
        # Screenshot errors carry fixed safe codes; never propagate browser logs.
        code = getattr(exc, 'code', '')
        if type(code) is not str or code not in _SCREENSHOT_CODES:
            code = 'WECHAT_SCREENSHOT_FAILED'
        raise DeliveryError(code) from None
    if not 1 <= len(images) <=24:
        raise DeliveryError('WECHAT_IMAGES_INVALID')
    frozen = []
    preview_id = uuid4().hex
    for index, image in enumerate(images, 1):
        if (type(image.png) is not bytes or not image.png.startswith(b'\x89PNG\r\n\x1a\n') or len(image.png)>1_300_000
                or type(image.width) is not int or type(image.height) is not int or min(image.width,image.height)<=0):
            raise DeliveryError('WECHAT_IMAGES_INVALID')
        frozen.append((preview_id,index,image.width,image.height,hashlib.sha256(image.png).hexdigest(),image.png))
    # The document identity intentionally excludes render pagination and config
    # labels/versions: creating a new preview cannot re-send an attempted report.
    value = dict(preview_id=preview_id,project_id=project_id,report_version_id=version,
                 report_hash=report_hash,module_narrative_hash=module_hash,report_label=document.report_label,
                 document_hash=document.document_hash,authority_hash=document.authority_hash,
                 document_target_hash=digest([project_id,document.document_hash,store.canonical_account(config['account_id']),config['target']]),
                 config_version_no=config_version,recipient_label=config['recipient_label'],created_at=store.now())
    store.insert_preview(value,frozen)
    return _preview_response(*store.get_preview(project_id,preview_id))


def get_image(project_id, preview_id, index):
    _positive(project_id); _text(preview_id,128); _positive(index)
    _, images = store.get_preview(project_id,preview_id)
    for image in images:
        if image['page_index'] == index:
            if hashlib.sha256(image['png']).hexdigest() != image['sha256']:
                raise DeliveryError('WECHAT_IMAGE_INVALID')
            return image['png']
    raise DeliveryError('WECHAT_IMAGE_NOT_FOUND')


def send_preview(project_id, preview_id, idempotency_key, *, human_confirmed):
    _positive(project_id); _text(preview_id,128)
    _text(idempotency_key,128)
    if re.fullmatch(r'[A-Za-z0-9._:-]+',idempotency_key) is None:
        raise DeliveryError('WECHAT_INPUT_INVALID')
    if human_confirmed is not True:
        raise DeliveryError('WECHAT_HUMAN_CONFIRMATION_REQUIRED')
    preview, images = store.get_preview(project_id,preview_id)
    existing = store.lookup_attempt(project_id,preview,idempotency_key)
    if existing is not None:
        return existing
    document = load_approved_document(project_id,preview['report_version_id'],preview['report_hash'],preview['module_narrative_hash'])
    if document.document_hash != preview['document_hash'] or document.authority_hash != preview['authority_hash']:
        raise DeliveryError('WECHAT_REPORT_STALE')
    config = store.get_config(project_id)
    if config is None or config['version_no'] != preview['config_version_no']:
        raise DeliveryError('WECHAT_CONFIG_STALE')
    # Secret access happens only on the explicit send boundary, before claiming.
    context_token=None
    direct=config.get('transport','openclaw')=='direct'
    if direct:
        from app.wechat_binding_service import direct_send_credentials
        token,context_token=direct_send_credentials(config)
    else:
        try:
            token = _secret_store_factory().get(config['secret_ref'])
        except SecretStoreError:
            raise DeliveryError('WECHAT_CREDENTIAL_UNAVAILABLE') from None
    for image in images:
        if hashlib.sha256(image['png']).hexdigest() != image['sha256']:
            raise DeliveryError('WECHAT_IMAGE_INVALID')
    attempt, claimed = store.claim_attempt(preview,idempotency_key,len(images))
    if not claimed:
        return attempt
    attempt_id = attempt['attempt_id']
    safe_config = {key:config[key] for key in ('gateway_url','account_id','target','session_key')}
    for image in images:
        index = image['page_index']
        try:
            store.start_page(attempt_id,index,preview)
        except DeliveryError:
            break
        try:
            kwargs=dict(filename=f'report-{index}.png',caption=f"{preview['report_label']}（{index}/{len(images)}）")
            if direct:
                from app.wechat_binding_service import send_direct_image
                outcome=send_direct_image(dict(safe_config,base_url=config['gateway_url']),token,image['png'],
                                          context_token=context_token,**kwargs)
            else:
                outcome = send_image(safe_config,token,image['png'],**kwargs)
            state = outcome.state if outcome.state in ('accepted','rejected','unknown') else 'unknown'
            # Preserve useful fixed reasons, never arbitrary provider response text.
            code = getattr(outcome,'code',None)
            if type(code) is not str or code not in _GATEWAY_CODES[state]:
                code = {'accepted':'WECHAT_GATEWAY_ACCEPTED','rejected':'WECHAT_GATEWAY_REJECTED','unknown':'WECHAT_GATEWAY_UNKNOWN'}[state]
        except Exception:
            state, code = 'unknown', 'WECHAT_GATEWAY_UNKNOWN'
        store.finish_page(attempt_id,index,state,code)
        if state != 'accepted':
            break
    return store.finish_attempt(attempt_id)


def get_history(project_id):
    return store.history(_positive(project_id))
