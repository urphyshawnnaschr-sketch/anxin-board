"""Loopback-only browser session adapter for protected local browser actions."""

from __future__ import annotations

import os
import threading

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from app import local_session_guard


router = APIRouter()
_BOOTSTRAP_ENV = "ANXINBOARD_LOCAL_BOOTSTRAP_SECRET"
_guard_lock = threading.Lock()
_guard: local_session_guard.LocalSessionGuard | None = None
_handoff_server = None


class LocalSessionExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_secret: StrictStr


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def configure_local_session_guard(bootstrap_secret: str | None = None, *, enable_handoff: bool = False) -> bool:
    """Install one runtime guard and remove launcher bootstrap material from process env."""
    global _guard, _handoff_server
    invalidate_local_session_guard()
    if bootstrap_secret is None:
        bootstrap_secret = os.environ.pop(_BOOTSTRAP_ENV, None)
    else:
        os.environ.pop(_BOOTSTRAP_ENV, None)

    replacement: local_session_guard.LocalSessionGuard | None = None
    if isinstance(bootstrap_secret, str) and bootstrap_secret:
        try:
            replacement = local_session_guard.LocalSessionGuard(bootstrap_secret)
        except local_session_guard.LocalSessionGuardError:
            replacement = None

    with _guard_lock:
        if _guard is not None:
            _guard.invalidate()
        _guard = replacement
    if replacement is not None and enable_handoff:
        from app.local_session_handoff import start_handoff_server
        try:
            _handoff_server = start_handoff_server(replacement.mint_handoff)
        except Exception:
            invalidate_local_session_guard()
            raise RuntimeError("LOCAL_SESSION_HANDOFF_START_FAILED") from None
    return replacement is not None


def invalidate_local_session_guard() -> None:
    global _guard, _handoff_server
    server, _handoff_server = _handoff_server, None
    if server is not None:
        server.stop()
    with _guard_lock:
        current = _guard
        _guard = None
    if current is not None:
        current.invalidate()


def _current_guard() -> local_session_guard.LocalSessionGuard:
    with _guard_lock:
        current = _guard
    if current is None:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "LOCAL_SESSION_UNAVAILABLE",
            "当前浏览器会话没有由安心看板本地启动器建立，受保护的本地操作保持关闭。",
        )
    return current


def _request_identity(request: Request) -> tuple[str | None, str | None]:
    return request.headers.get("host"), request.headers.get("origin")


def _raise_guard(error: local_session_guard.LocalSessionGuardError) -> None:
    raise _error(
        status.HTTP_403_FORBIDDEN,
        f"LOCAL_SESSION_{error.code}",
        "本地浏览器会话校验失败；受保护的本地操作没有执行。",
    ) from error


def require_local_read_request(request: Request) -> None:
    """Protect sensitive local reads with the launcher's live loopback session.

    Same-origin GET requests do not reliably carry Origin in all supported browsers, so
    Origin is validated when present while loopback Host, live session identity and a
    bounded request id are always required.
    """

    guard = _current_guard()
    host, origin = _request_identity(request)
    try:
        local_session_guard.validate_loopback_host(host)
        if origin is not None:
            local_session_guard.validate_exact_http_origin(origin, host)
        guard.validate_session(request.headers.get("x-anxin-session"))
        local_session_guard.validate_request_id(request.headers.get("x-request-id"))
    except local_session_guard.LocalSessionGuardError as error:
        _raise_guard(error)


def require_local_write_request(
    request: Request,
    *,
    require_idempotency_key: bool,
) -> None:
    """Fail closed unless this is the launcher's live same-origin browser session."""
    guard = _current_guard()
    host, origin = _request_identity(request)
    try:
        guard.validate_write_request(
            session_token=request.headers.get("x-anxin-session"),
            host=host,
            origin=origin,
            request_id=request.headers.get("x-request-id"),
            idempotency_key=request.headers.get("local-idempotency-key"),
            require_idempotency_key=require_idempotency_key,
        )
    except local_session_guard.LocalSessionGuardError as error:
        _raise_guard(error)


@router.post("/api/local-session/exchange", status_code=status.HTTP_200_OK)
def exchange_local_session(
    payload: LocalSessionExchangeRequest,
    request: Request,
) -> JSONResponse:
    """Consume the launcher bootstrap exactly once and return one in-memory session token."""
    guard = _current_guard()
    host, origin = _request_identity(request)
    try:
        local_session_guard.validate_loopback_host(host)
        local_session_guard.validate_exact_http_origin(origin, host)
        session_token = guard.exchange_bootstrap(payload.bootstrap_secret)
    except local_session_guard.LocalSessionGuardError as error:
        _raise_guard(error)

    return JSONResponse(
        {
            "schema_version": "local_browser_session_v1",
            "session_state": "active",
            "session_token": session_token,
        },
        headers={"Cache-Control": "no-store"},
    )
