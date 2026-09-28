"""Explicit local operator endpoints for DeepSeek credentials and live model selection.

Secret bytes stay in Windows Credential Manager.  The browser may test the saved key
against DeepSeek's read-only /models endpoint and persist only one exact model id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.deepseek_live_profile_adapter import MODEL_SELECTION_REF, list_deepseek_models_with_api_key
from app.deepseek_model_catalog import REPORT_SELECTABLE_MODEL_IDS
from app.local_session_api import require_local_write_request
from app.secret_store import (
    InvalidSecretValueError,
    SecretAccessDeniedError,
    SecretNotFoundError,
    SecretStoreOSError,
    UnsupportedSecretStorePlatformError,
)
from app.windows_credential_store import WindowsCredentialStore


router = APIRouter()
_DEEPSEEK_SECRET_REF = "deepseek-api-key"
_secret_store_factory = WindowsCredentialStore


class DeepSeekCredentialPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: str = Field(min_length=1, max_length=1024)

    @field_validator("api_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("api_key must not be blank")
        return trimmed


class DeepSeekModelSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_id: str = Field(min_length=1, max_length=100)

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed or trimmed != value:
            raise ValueError("model_id must be exact non-empty text")
        return value


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _read_saved_key(store: object) -> str:
    try:
        return store.get(_DEEPSEEK_SECRET_REF)
    except SecretNotFoundError as exc:
        raise _error(409, "MODEL_CREDENTIAL_REQUIRED", "请先保存 DeepSeek API Key。") from exc
    except UnsupportedSecretStorePlatformError as exc:
        raise _error(409, "MODEL_CREDENTIAL_PLATFORM_UNSUPPORTED", "当前系统不支持 Windows 安全凭据读取。") from exc
    except SecretAccessDeniedError as exc:
        raise _error(403, "MODEL_CREDENTIAL_ACCESS_DENIED", "Windows 拒绝读取该安全凭据。") from exc
    except SecretStoreOSError as exc:
        raise _error(500, "MODEL_CREDENTIAL_READ_FAILED", "读取 DeepSeek API Key 失败，未回显凭据内容。") from exc


def _read_optional_model(store: object) -> str | None:
    try:
        return store.get(MODEL_SELECTION_REF)
    except SecretNotFoundError:
        return None
    except (UnsupportedSecretStorePlatformError, SecretAccessDeniedError, SecretStoreOSError):
        return None


@router.get("/api/settings/deepseek-status")
def get_deepseek_status() -> dict[str, object]:
    """Return only non-secret local configuration state; never tests provider readiness."""
    try:
        store = _secret_store_factory()
        try:
            store.get(_DEEPSEEK_SECRET_REF)
            configured = True
        except SecretNotFoundError:
            configured = False
        selected_model = _read_optional_model(store) if configured else None
    except (UnsupportedSecretStorePlatformError, SecretAccessDeniedError, SecretStoreOSError):
        configured = False
        selected_model = None
    return {
        "provider": "deepseek",
        "configured": configured,
        "selected_model": selected_model,
        "connected": False,
    }


@router.post("/api/settings/deepseek-credential")
def configure_deepseek_credential(
    payload: DeepSeekCredentialPayload,
    request: Request,
) -> dict[str, object]:
    """Store exactly one DeepSeek key after the secure local Human write gate."""
    require_local_write_request(request, require_idempotency_key=False)
    try:
        store = _secret_store_factory()
        store.put(_DEEPSEEK_SECRET_REF, payload.api_key)
        # A replacement key may expose a different account/model set.  Invalidate the old
        # selection so generation cannot silently reuse stale model authority.
        try:
            store.delete(MODEL_SELECTION_REF)
        except SecretNotFoundError:
            pass
    except InvalidSecretValueError as exc:
        raise _error(400, "MODEL_CREDENTIAL_INVALID", "DeepSeek API Key 格式无法安全保存。") from exc
    except UnsupportedSecretStorePlatformError as exc:
        raise _error(409, "MODEL_CREDENTIAL_PLATFORM_UNSUPPORTED", "当前系统不支持 Windows 安全凭据存储。") from exc
    except SecretAccessDeniedError as exc:
        raise _error(403, "MODEL_CREDENTIAL_ACCESS_DENIED", "Windows 拒绝保存该安全凭据。") from exc
    except SecretStoreOSError as exc:
        raise _error(500, "MODEL_CREDENTIAL_SAVE_FAILED", "保存 DeepSeek API Key 失败，未回显凭据内容。") from exc
    return {
        "provider": "deepseek",
        "credential_ref": _DEEPSEEK_SECRET_REF,
        "configured": True,
        "selected_model": None,
        "connected": False,
    }


@router.post("/api/settings/deepseek-connection-test")
def test_deepseek_connection(request: Request) -> dict[str, object]:
    """Human-triggered read-only provider preflight; sends no PRD/code/report content."""
    require_local_write_request(request, require_idempotency_key=False)
    store = _secret_store_factory()
    key = _read_saved_key(store)
    models = [
        model_id
        for model_id in list_deepseek_models_with_api_key(key)
        if model_id in REPORT_SELECTABLE_MODEL_IDS
    ]
    selected = _read_optional_model(store)
    if selected not in models:
        selected = None
    return {
        "provider": "deepseek",
        "configured": True,
        "connected": True,
        "available_models": models,
        "selected_model": selected,
    }


@router.post("/api/settings/deepseek-model-selection")
def select_deepseek_model(
    payload: DeepSeekModelSelectionPayload,
    request: Request,
) -> dict[str, object]:
    """Persist one exact model id only after a fresh /models membership check."""
    require_local_write_request(request, require_idempotency_key=False)
    store = _secret_store_factory()
    key = _read_saved_key(store)
    models = list_deepseek_models_with_api_key(key)
    if payload.model_id not in REPORT_SELECTABLE_MODEL_IDS:
        raise _error(
            409,
            "MODEL_SELECTION_NOT_QUALIFIED",
            "该模型未通过日报链路的资格验证，不能用于日报生成；请选择已验证可用的模型。",
        )
    if payload.model_id not in models:
        raise _error(409, "MODEL_SELECTION_NOT_AVAILABLE", "该模型不在当前 DeepSeek 账号的 /models 列表中，请重新测试连接。")
    try:
        store.put(MODEL_SELECTION_REF, payload.model_id)
    except InvalidSecretValueError as exc:
        raise _error(400, "MODEL_SELECTION_INVALID", "模型 ID 无法安全保存。") from exc
    except UnsupportedSecretStorePlatformError as exc:
        raise _error(409, "MODEL_CREDENTIAL_PLATFORM_UNSUPPORTED", "当前系统不支持保存模型选择。") from exc
    except SecretAccessDeniedError as exc:
        raise _error(403, "MODEL_CREDENTIAL_ACCESS_DENIED", "Windows 拒绝保存当前模型选择。") from exc
    except SecretStoreOSError as exc:
        raise _error(500, "MODEL_SELECTION_SAVE_FAILED", "保存当前模型选择失败。") from exc
    return {
        "provider": "deepseek",
        "configured": True,
        "connected": True,
        "available_models": [
            model_id for model_id in models if model_id in REPORT_SELECTABLE_MODEL_IDS
        ],
        "selected_model": payload.model_id,
    }
