"""Guarded QR endpoints: all network actions require explicit local writes."""
from fastapi import APIRouter,Request
from fastapi.responses import Response
from app.local_session_api import require_local_read_request,require_local_write_request
from app.wechat_delivery_api import _body,_call,_json
from app import wechat_binding_service as service

router=APIRouter(prefix='/api/projects/{project_id}')


@router.get('/wechat-binding')
async def binding(project_id:int,request:Request):
    require_local_read_request(request)
    return _json(await _call(service.get_binding,project_id))


@router.post('/wechat-login/start')
async def start(project_id:int,request:Request):
    require_local_write_request(request,require_idempotency_key=True)
    return _json(await _call(service.start_login,project_id,await _body(request),request.headers.get('local-idempotency-key')))


@router.post('/wechat-login/poll')
async def poll(project_id:int,request:Request):
    require_local_write_request(request,require_idempotency_key=True)
    return _json(await _call(service.poll_login,project_id,await _body(request),request.headers.get('local-idempotency-key')))


@router.get('/wechat-login/{flow_id}/qr.png')
async def qr(project_id:int,flow_id:str,request:Request):
    require_local_read_request(request)
    png=await _call(service.get_qr,project_id,flow_id)
    return Response(png,media_type='image/png',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.post('/wechat-login/cancel')
async def cancel(project_id:int,request:Request):
    require_local_write_request(request,require_idempotency_key=True)
    return _json(await _call(service.cancel_login,project_id,await _body(request)))


@router.post('/wechat-binding/refresh')
async def refresh(project_id:int,request:Request):
    require_local_write_request(request,require_idempotency_key=True)
    return _json(await _call(service.refresh_binding,project_id,await _body(request)))


@router.post('/wechat-binding/disconnect')
async def disconnect(project_id:int,request:Request):
    require_local_write_request(request,require_idempotency_key=True)
    return _json(await _call(service.disconnect,project_id,await _body(request)))
