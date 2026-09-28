"""Trusted local preparation boundary; never wrap provider execution here."""

from collections.abc import Callable, Mapping
from typing import TypeVar

from fastapi import HTTPException

T = TypeVar("T")

# Values are product-owned messages. Never retain exception text, customer paths,
# raw provider bodies, or an arbitrary error code supplied by an adapter.
_MESSAGES = {
    "PROFILE_RECONCILIATION_BATCH_PLAN_REQUIRED": "代码对账须先完成本地整仓建图和分批预算审核；未发送模型请求。",
    "CONTEXT_REDACTION_UNSAFE_AMBIGUITY": "代码脱敏遇到无法安全识别的赋值表达式；请检查脱敏规则。模型请求尚未发送。",
    "PROFILE_GENERATION_REPO_CONTEXT_TOO_LARGE": "安全代码上下文超过单次上限；请调整项目分析范围。模型请求尚未发送。",
    "PROFILE_GENERATION_REPO_FILE_TOO_LARGE": "代码文件超过安全读取上限；请检查项目代码范围。模型请求尚未发送。",
    "PROFILE_GENERATION_REPO_TEXT_ENCODING_UNSUPPORTED": "代码包含不支持的文本编码；请使用可验证的 UTF-8。模型请求尚未发送。",
    "PROFILE_GENERATION_REPO_TREE_INVALID": "无法验证指定提交的代码树；请重新检查 Git 连接。模型请求尚未发送。",
    "PROFILE_GENERATION_REPO_BLOB_INVALID": "无法验证指定提交的代码对象；请重新检查 Git 连接。模型请求尚未发送。",
    "PROFILE_GENERATION_REPO_TREE_TOO_LARGE": "代码树超过安全上限；请检查项目分析范围。模型请求尚未发送。",
    "PROFILE_GENERATION_GIT_WORKSPACE_REQUIRED": "受控代码工作区无法验证；请重新检查 Git 连接。模型请求尚未发送。",
    "PROFILE_GENERATION_GIT_CHECK_REQUIRED": "请先完成当前 Git 连接检查。模型请求尚未发送。",
    "PROFILE_GENERATION_PRD_REQUIRED": "请先确认项目 PRD。模型请求尚未发送。",
    "PROFILE_GENERATION_PRD_INVALID": "已确认 PRD 无法安全读取；请检查 PRD 状态。模型请求尚未发送。",
    "PROJECT_NOT_FOUND": "项目不存在；请重新选择项目。模型请求尚未发送。",
    "PROFILE_GENERATION_INPUT_READ_FAILED": "无法读取项目输入；请检查本地数据状态。模型请求尚未发送。",
    "PROFILE_GENERATION_CONTEXT_INVALID": "上下文证据无法验证；请检查项目输入。模型请求尚未发送。",
    "PROFILE_GENERATION_PROVIDER_INVALID": "模型配置无法验证；请检查模型设置。模型请求尚未发送。",
    "PROFILE_GENERATION_AUTHORIZATION_REQUIRED": "本次操作需要明确的数据发送授权。模型请求尚未发送。",
    "PROFILE_GENERATION_AUTHORIZATION_SAVE_FAILED": "无法保存数据发送授权；请检查本地存储。模型请求尚未发送。",
    "PROFILE_GENERATION_CREDENTIAL_REQUIRED": "尚未配置模型凭据；请检查模型设置。模型请求尚未发送。",
    "PROFILE_GENERATION_CREDENTIAL_UNSUPPORTED": "当前环境不支持安全凭据读取；请检查运行环境。模型请求尚未发送。",
    "PROFILE_GENERATION_CREDENTIAL_ACCESS_DENIED": "安全凭据访问被拒绝；请检查模型设置。模型请求尚未发送。",
    "PROFILE_GENERATION_CREDENTIAL_READ_FAILED": "无法安全读取模型凭据；请检查模型设置。模型请求尚未发送。",
    "PROFILE_GENERATION_PRE_SEND_FAILED": "本地请求准备失败；请检查项目输入和模型设置。模型请求尚未发送。",
}


class ProfileGenerationPhaseError(HTTPException):
    """Emitted only at a trusted call boundary, not from exception detail fields."""

    phase = "failed_pre_send"

    def __init__(self, code: str, status_code: int = 409) -> None:
        safe_code = code if code in _MESSAGES else "PROFILE_GENERATION_PRE_SEND_FAILED"
        super().__init__(
            status_code=status_code if status_code in {400, 403, 404, 409, 413, 422, 500, 503} else 409,
            detail={"code": safe_code, "message": _MESSAGES[safe_code]},
        )


def run_pre_send(operation: Callable[[], T]) -> T:
    """Execute local preparation only; dispatch must occur after this returns."""
    try:
        return operation()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        code = detail.get("code")
        raise ProfileGenerationPhaseError(
            code if type(code) is str else "PROFILE_GENERATION_PRE_SEND_FAILED",
            exc.status_code,
        ) from None
    except Exception:
        raise ProfileGenerationPhaseError("PROFILE_GENERATION_PRE_SEND_FAILED", 500) from None
