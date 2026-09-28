"""Local Settings API for versioned SMTP transport configuration and credential writes.

Secret bytes are accepted only on the explicit local write action and are immediately
handed to SecretStore. Responses and SQLite persistence expose only non-secret profile
metadata; historical profile versions bind unique opaque secret references.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, StrictStr

from app.local_session_api import require_local_write_request
from app.mail_transport_profile import (
    MailTransportProfile,
    MailTransportProfileError,
    get_current_mail_transport_profile,
    save_mail_transport_profile,
)
from app.secret_store import (
    InvalidSecretValueError,
    SecretAccessDeniedError,
    SecretStoreError,
    SecretStoreOSError,
    UnsupportedSecretStorePlatformError,
)
from app.smtp_connection_test import (
    SmtpConnectionTestError,
    require_smtp_connection_test_capability,
    test_smtp_connection,
)
from app.windows_credential_store import WindowsCredentialStore


router = APIRouter()
_secret_store_factory = WindowsCredentialStore
_smtp_connection_test_factory = test_smtp_connection


def _new_secret_ref() -> str:
    return f"smtp-password-{uuid4().hex}"


_secret_ref_factory = _new_secret_ref


class MailTransportCredentialPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    host: StrictStr = Field(min_length=1, max_length=253)
    port: StrictInt = Field(ge=1, le=65535)
    security: StrictStr
    username: StrictStr = Field(min_length=1, max_length=320)
    from_identity: StrictStr = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)
    timeout_seconds: StrictInt = Field(default=30, ge=1, le=60)
    expected_version_no: StrictInt = Field(ge=0)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _profile_response(profile: MailTransportProfile | None) -> dict[str, object]:
    if profile is None:
        return {
            "schema_version": "mail_transport_settings_v1",
            "configured": False,
            "credential_reference_bound": False,
            "profile": None,
        }
    return {
        "schema_version": "mail_transport_settings_v1",
        "configured": True,
        "credential_reference_bound": True,
        "profile": {
            "version_no": profile.version_no,
            "host": profile.host,
            "port": profile.port,
            "security": profile.security.value,
            "username": profile.username,
            "from_identity": profile.from_identity,
            "timeout_seconds": profile.timeout_seconds,
            "configured_by": profile.configured_by,
            "created_at": profile.created_at,
        },
    }


def _raise_profile(error: MailTransportProfileError) -> None:
    if error.code == "MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT":
        raise _error(409, error.code, "邮件发送配置已经变化，请刷新后重新保存。") from error
    if error.code in {
        "MAIL_TRANSPORT_PROFILE_INPUT_INVALID",
        "MAIL_TRANSPORT_FROM_IDENTITY_INVALID",
    }:
        raise _error(400, error.code, "邮件发送配置格式无效，请检查后重试。") from error
    raise _error(409, error.code, "邮件发送配置的本地持久化事实无法完成自校验。") from error


def _current_profile_or_http() -> MailTransportProfile | None:
    try:
        return get_current_mail_transport_profile()
    except MailTransportProfileError as exc:
        _raise_profile(exc)
    raise AssertionError("unreachable")


def _secret_store_or_http():
    try:
        return _secret_store_factory()
    except UnsupportedSecretStorePlatformError as exc:
        raise _error(409, "MAIL_CREDENTIAL_PLATFORM_UNSUPPORTED", "当前系统不支持 Windows 安全凭据存储。") from exc
    except SecretAccessDeniedError as exc:
        raise _error(403, "MAIL_CREDENTIAL_ACCESS_DENIED", "Windows 拒绝访问 SMTP 凭据。") from exc
    except SecretStoreOSError as exc:
        raise _error(500, "MAIL_CREDENTIAL_ACCESS_FAILED", "访问 SMTP 凭据失败，未回显凭据内容。") from exc


def _raise_connection_test(error: SmtpConnectionTestError) -> None:
    mapping = {
        "MAIL_SMTP_TEST_TRANSPORT_UNSUPPORTED": (
            409,
            "当前保存的 SMTP 参数需要本版本未支持的认证方式，连接测试保持关闭。",
        ),
        "MAIL_SMTP_TEST_SECRET_UNAVAILABLE": (409, "已保存的 SMTP 凭据无法读取，请重新保存配置后再试。"),
        "MAIL_SMTP_TEST_CONNECT_FAILED": (502, "无法连接 SMTP 服务器，请检查服务器地址、端口和网络。"),
        "MAIL_SMTP_TEST_TLS_FAILED": (502, "SMTP TLS 握手失败，请检查端口和连接安全方式。"),
        "MAIL_SMTP_TEST_AUTH_FAILED": (422, "SMTP 身份验证失败，请检查邮箱账号和授权码 / 应用专用密码。"),
        "MAIL_SMTP_TEST_AUTH_PROTOCOL_FAILED": (502, "SMTP 身份验证协议未能完成。"),
        "MAIL_SMTP_TEST_AUTH_CONNECTION_LOST": (502, "SMTP 身份验证时连接中断，未能确认登录结果，未发送邮件。请检查网络及邮箱服务状态；系统不会自动重试。"),
        "MAIL_SMTP_TEST_AUTH_TIMEOUT": (502, "SMTP 身份验证等待超时，未能确认登录结果，未发送邮件。请检查网络及邮箱服务状态；系统不会自动重试。"),
        "MAIL_SMTP_TEST_AUTH_UNSUPPORTED": (502, "SMTP 服务器未提供本版本支持的认证方式，未发送邮件。请核对邮箱的 SMTP 服务和服务器配置。"),
        "MAIL_SMTP_TEST_POST_AUTH_FAILED": (502, "SMTP 已登录，但连接健康检查未通过。"),
    }
    status_code, message = mapping.get(error.code, (502, "SMTP 连接测试未能完成。"))
    raise _error(status_code, error.code, message) from error


@router.get("/api/settings/mail-transport", status_code=status.HTTP_200_OK)
def get_mail_transport_settings() -> dict[str, object]:
    """Return non-secret configuration state without opening or enumerating SecretStore."""
    return _profile_response(_current_profile_or_http())


@router.post("/api/settings/mail-transport", status_code=status.HTTP_200_OK)
def configure_mail_transport(
    payload: MailTransportCredentialPayload,
    request: Request,
) -> dict[str, object]:
    """Persist one new credential identity and its immutable non-secret transport profile."""
    require_local_write_request(request, require_idempotency_key=False)

    current = _current_profile_or_http()
    current_version = current.version_no if current is not None else 0
    if payload.expected_version_no != current_version:
        raise _error(
            409,
            "MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT",
            "邮件发送配置已经变化，请刷新后重新保存。",
        )

    secret_ref = _secret_ref_factory()
    try:
        store = _secret_store_factory()
        store.put(secret_ref, payload.password.get_secret_value())
    except InvalidSecretValueError as exc:
        raise _error(400, "MAIL_CREDENTIAL_INVALID", "SMTP 密码格式无法安全保存。") from exc
    except UnsupportedSecretStorePlatformError as exc:
        raise _error(409, "MAIL_CREDENTIAL_PLATFORM_UNSUPPORTED", "当前系统不支持 Windows 安全凭据存储。") from exc
    except SecretAccessDeniedError as exc:
        raise _error(403, "MAIL_CREDENTIAL_ACCESS_DENIED", "Windows 拒绝保存 SMTP 凭据。") from exc
    except SecretStoreOSError as exc:
        raise _error(500, "MAIL_CREDENTIAL_SAVE_FAILED", "保存 SMTP 凭据失败，未回显凭据内容。") from exc

    try:
        profile = save_mail_transport_profile(
            host=payload.host,
            port=payload.port,
            security=payload.security,
            username=payload.username,
            from_identity=payload.from_identity,
            secret_ref=secret_ref,
            timeout_seconds=payload.timeout_seconds,
            configured_by="local-settings-api",
            expected_version_no=payload.expected_version_no,
        )
    except MailTransportProfileError as exc:
        try:
            store.delete(secret_ref)
        except SecretStoreError:
            raise _error(
                500,
                "MAIL_CREDENTIAL_ROLLBACK_FAILED",
                "邮件配置未保存，但新凭据无法完成安全回滚；发送能力保持关闭。",
            ) from exc
        _raise_profile(exc)
        raise AssertionError("unreachable")

    return _profile_response(profile)


@router.post("/api/settings/mail-transport/test", status_code=status.HTTP_200_OK)
def test_mail_transport_connection(request: Request, expected_version_no: int) -> dict[str, object]:
    """Test one exact saved SMTP profile through TLS/auth/NOOP without submission."""

    require_local_write_request(request, require_idempotency_key=False)

    if type(expected_version_no) is not int or expected_version_no <= 0:
        raise _error(400, "MAIL_TRANSPORT_TEST_VERSION_INVALID", "邮件配置版本无效，请刷新后重试。")

    profile = _current_profile_or_http()
    if profile is None:
        raise _error(409, "MAIL_TRANSPORT_NOT_CONFIGURED", "请先保存 SMTP 配置，再测试连接。")
    if profile.version_no != expected_version_no:
        raise _error(
            409,
            "MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT",
            "邮件发送配置已经变化，请刷新后再测试连接。",
        )

    config = profile.gateway_config()
    try:
        require_smtp_connection_test_capability(config)
    except SmtpConnectionTestError as exc:
        _raise_connection_test(exc)

    store = _secret_store_or_http()
    try:
        result = _smtp_connection_test_factory(
            config=config,
            secret_store=store,
        )
    except SmtpConnectionTestError as exc:
        _raise_connection_test(exc)
    if result is None:
        raise AssertionError("unreachable")

    return {
        "schema_version": "mail_transport_connection_test_v1",
        "status": "passed",
        "profile_version": profile.version_no,
        "checks": {
            "credential_read": result.credential_read,
            "smtp_connect": result.smtp_connect,
            "tls_ready": result.tls_ready,
            "smtp_auth": result.smtp_auth,
            "post_auth_noop": result.post_auth_noop,
            "message_submission": False,
        },
    }
