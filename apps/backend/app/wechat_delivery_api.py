"""Session-protected WeChat API; bodies are validated without echoing credentials."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.local_session_api import require_local_read_request, require_local_write_request
from app import wechat_delivery_service as service

router = APIRouter(prefix='/api/projects/{project_id}')


def _http_error(exc):
    code = exc.code
    status = 404 if code.endswith('_NOT_FOUND') else 400 if code in {
        'WECHAT_INPUT_INVALID','WECHAT_TOKEN_INVALID','WECHAT_GATEWAY_URL_INVALID',
        'WECHAT_HUMAN_CONFIRMATION_REQUIRED'} else 409
    return HTTPException(status_code=status, detail={'code':code,'message':'微信图片操作未完成，请检查配置或刷新正式报告。'})


async def _body(request):
    # No Pydantic request model: validation errors must never repeat a token.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>16384:
            raise _http_error(service.DeliveryError('WECHAT_INPUT_INVALID'))
    try:
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise _http_error(service.DeliveryError('WECHAT_INPUT_INVALID')) from None


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except service.DeliveryError as exc:
        raise _http_error(exc) from None


def _json(value):
    return JSONResponse(value, headers={'Cache-Control':'no-store'})


@router.get('/wechat-settings')
async def get_settings(project_id: int, request: Request):
    require_local_read_request(request)
    return _json(await _call(service.get_settings,project_id))


@router.post('/wechat-settings')
async def save_settings(project_id: int, request: Request):
    require_local_write_request(request,require_idempotency_key=False)
    return _json(await _call(service.save_settings,project_id,await _body(request)))


@router.post('/wechat-preview')
async def preview(project_id: int, request: Request):
    require_local_write_request(request,require_idempotency_key=False)
    return _json(await _call(service.create_preview,project_id,await _body(request)))


@router.get('/wechat-preview/{preview_id}/images/{index}')
async def image(project_id: int, preview_id: str, index: int, request: Request):
    require_local_read_request(request)
    png = await _call(service.get_image,project_id,preview_id,index)
    return Response(png,media_type='image/png',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.post('/wechat-send')
async def send(project_id: int, request: Request):
    require_local_write_request(request,require_idempotency_key=True)
    payload = await _body(request)
    if set(payload) != {'preview_id','human_confirmed'}:
        raise _http_error(service.DeliveryError('WECHAT_INPUT_INVALID'))
    return _json(await _call(service.send_preview,project_id,payload['preview_id'],
                            request.headers.get('local-idempotency-key'),human_confirmed=payload['human_confirmed']))


@router.get('/wechat-history')
async def history(project_id: int, request: Request):
    require_local_read_request(request)
    return _json(await _call(service.get_history,project_id))
