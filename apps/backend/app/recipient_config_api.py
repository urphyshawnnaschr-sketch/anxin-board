"""Project-scoped HTTP adapter for the accepted immutable RecipientConfig R1 authority."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from app.local_session_api import require_local_write_request
from app.recipient_config import (
    RecipientConfigError,
    get_current_recipient_config,
    save_recipient_config,
)


router = APIRouter()


class SaveRecipientConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    to_recipients: list[StrictStr]
    expected_version_no: StrictInt


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _raise_recipient(error: RecipientConfigError) -> None:
    if error.code == "RECIPIENT_CONFIG_PROJECT_NOT_FOUND":
        raise _error(404, error.code, "项目不存在。") from error
    if error.code == "RECIPIENT_CONFIG_VERSION_CONFLICT":
        raise _error(409, error.code, "收件人配置已经变化，请刷新后重新保存。") from error
    if error.code == "RECIPIENT_CONFIG_INPUT_INVALID":
        raise _error(400, error.code, "收件人格式无效；当前仅支持标准 To 邮箱地址。") from error
    raise _error(409, error.code, "收件人配置的本地持久化事实无法完成自校验。") from error


def _response(current: dict[str, object] | None) -> dict[str, object]:
    if current is None:
        return {
            "schema_version": "recipient_config_settings_v1",
            "configured": False,
            "version_no": 0,
            "to_recipients": [],
        }
    recipients = current.get("to_recipients")
    if type(recipients) is not list or type(current.get("version_no")) is not int:
        raise _error(409, "RECIPIENT_CONFIG_STORED_INVALID", "收件人配置无法完成自校验。")
    return {
        "schema_version": "recipient_config_settings_v1",
        "configured": True,
        "version_no": current["version_no"],
        "to_recipients": list(recipients),
    }


@router.get(
    "/api/projects/{project_id}/recipient-config",
    status_code=status.HTTP_200_OK,
)
def get_recipient_config_http(project_id: int) -> dict[str, object]:
    try:
        return _response(get_current_recipient_config(project_id))
    except RecipientConfigError as exc:
        _raise_recipient(exc)
    raise AssertionError("unreachable")


@router.post(
    "/api/projects/{project_id}/recipient-config",
    status_code=status.HTTP_200_OK,
)
def save_recipient_config_http(
    project_id: int,
    payload: SaveRecipientConfigRequest,
    request: Request,
) -> dict[str, object]:
    require_local_write_request(request, require_idempotency_key=True)
    try:
        current = save_recipient_config(
            project_id=project_id,
            to_recipients=payload.to_recipients,
            created_by="local-settings-api",
            expected_version_no=payload.expected_version_no,
        )
        return _response(current)
    except RecipientConfigError as exc:
        _raise_recipient(exc)
    raise AssertionError("unreachable")
