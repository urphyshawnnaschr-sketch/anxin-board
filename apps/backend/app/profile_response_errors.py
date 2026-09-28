"""Product-owned classifications for a received profile POST response.

These codes never describe /models discovery or ambiguous network outcomes.
Only bounded codes and static messages may cross the durable error boundary.
"""
from fastapi import HTTPException

_MESSAGES = {
    "ENVELOPE_INVALID": "DeepSeek 响应为空、超限或不是严格 JSON object。",
    "ID_INVALID": "DeepSeek 响应缺少有效 response id。",
    "CHOICES_INVALID": "DeepSeek 响应 choice 数量或结构无效。",
    "OUTPUT_TRUNCATED": "DeepSeek 输出达到长度上限，结果被截断；请检查输出预算。",
    "FINISH_INVALID": "DeepSeek 未以完整 stop 结果结束。",
    "CONTENT_MISSING": "DeepSeek 响应缺少有效的结构化内容。",
    "CONTENT_JSON_INVALID": "DeepSeek 返回内容不是严格 JSON。",
    "CONTENT_NOT_OBJECT": "DeepSeek 返回内容不是 JSON object。",
    "USAGE_MISSING": "DeepSeek 响应缺少 usage。",
    "USAGE_INVALID": "DeepSeek usage token 字段无效。",
    "USAGE_MISMATCH": "DeepSeek usage token 总数不一致。",
}
PROFILE_RESPONSE_ERROR_CODES = frozenset("PROFILE_GENERATION_RESPONSE_" + suffix for suffix in _MESSAGES)


def profile_response_error(suffix: str) -> HTTPException:
    return HTTPException(status_code=502, detail={
        "code": "PROFILE_GENERATION_RESPONSE_" + suffix,
        "message": _MESSAGES[suffix] + " 本次未形成候选，系统不会自动重发。",
    })
