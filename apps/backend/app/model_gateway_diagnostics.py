"""Closed-world, non-secret pre-send diagnostics. Never echo exception text."""
from collections.abc import Mapping
import logging

from app.deepseek_model_catalog import REPORT_MODEL_ID

_logger = logging.getLogger(__name__)
from fastapi import HTTPException

# Only these application-owned codes may cross the diagnostic boundary.
_DIAGNOSTICS = {
    "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE": ("credential", "未找到可用的 DeepSeek 凭据；请在模型设置中检查已保存的 API Key。"),
    "DEEPSEEK_AUTHORITY_CREDENTIAL_READ_FAILED": ("credential", "无法读取已保存的 DeepSeek 凭据；请检查当前 Windows 用户的凭据设置。"),
    "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE": ("model_availability", "无法读取模型可用性清单；请检查网络和模型服务状态。"),
    "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE": ("model_metadata", "无法读取或验证模型规格；请检查网络或更新软件中的模型规格适配。"),
    "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED": ("model_availability", f"当前凭据的可用模型清单中没有日报资格模型 {REPORT_MODEL_ID}；请在模型设置中重新测试连接并选择该模型。"),
    "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED": ("model_metadata", "所选模型版本已变化；需要更新并重新验证模型配置。"),
    "DEEPSEEK_AUTHORITY_PROVIDER_CONSTRAINT_CHANGED": ("model_metadata", "模型上下文或输出限制已变化；需要更新并重新验证预算。"),
    "DEEPSEEK_AUTHORITY_QUALIFICATION_NOT_ADMITTED": ("qualification", "所选模型尚未通过当前任务的资格验证。"),
    "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED": ("authorization", "本次发送范围尚未获得有效授权；请重新确认当前发送范围。"),
    "MODEL_PROVIDER_AUTHORITY_UNAVAILABLE": ("current_authority", "无法获取当前模型资格或授权依据；请检查模型设置和网络。"),
    "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT": ("qualification", "当前模型资格验证未通过；请检查模型配置。"),
    "MODEL_PROVIDER_AUTHORIZATION_NOT_CURRENT": ("authorization", "本次发送授权与当前资料不匹配；请重新确认发送范围。"),
    "MODEL_PROVIDER_GATEWAY_DIAGNOSTIC_UNAVAILABLE": ("current_authority", "当前模型校验发生未登记的错误，已阻止发送；请反馈此错误码以检查适配器。"),
}

_CREDENTIAL_REASONS = {
    "SECRET_NOT_FOUND": "未找到已保存的 DeepSeek API Key；请在模型设置中保存凭据。",
    "SECRET_ACCESS_DENIED": "当前 Windows 用户无法访问已保存的 DeepSeek 凭据；请检查运行用户和凭据权限。",
    "SECRET_STORE_OS_FAILURE": "系统凭据存储读取失败；请检查 Windows 凭据管理器后重试本地校验。",
    "SECRET_STORE_UNSUPPORTED_PLATFORM": "当前运行环境不支持系统凭据存储；请使用受支持的运行环境。",
    "SECRET_VALUE_INVALID": "已保存的 DeepSeek 凭据格式无效；请在模型设置中重新保存。",
}

def safe_authority_error(detail: object) -> HTTPException:
    detail = detail if isinstance(detail, Mapping) else {}
    outer = detail.get("code")
    cause = detail.get("cause_code")
    # An unregistered outer code cannot smuggle a known cause into a trusted error.
    if not isinstance(outer, str) or outer not in _DIAGNOSTICS:
        outer = "MODEL_PROVIDER_GATEWAY_DIAGNOSTIC_UNAVAILABLE"
        cause = outer
    if not isinstance(cause, str) or cause not in _DIAGNOSTICS:
        cause = outer
    stage, message = _DIAGNOSTICS[cause]
    safe_detail = {
        "code": outer, "cause_code": cause, "stage": stage,
        "message": message, "network_send_state": "not_attempted",
    }
    credential_code = detail.get("credential_error_code")
    if stage == "credential" and isinstance(credential_code, str) and credential_code in _CREDENTIAL_REASONS:
        safe_detail["credential_error_code"] = credential_code
        safe_detail["message"] = _CREDENTIAL_REASONS[credential_code]
    _logger.warning(
        "Model pre-send check blocked: code=%s cause_code=%s stage=%s credential_error_code=%s",
        outer, cause, stage, safe_detail.get("credential_error_code", "not_applicable"),
    )
    return HTTPException(status_code=409, detail=safe_detail)
