"""Client-facing stage summary for the 简洁书法版 安心看板.

This module is deliberately pure and deterministic. It translates the project's
six formal development stages into short Chinese copy that a non-technical client
can understand. It does not infer progress percentages or expose internal delivery
mechanics.
"""

from __future__ import annotations

import hashlib
import json


SCHEMA_VERSION = "client_stage_summary_v1"

_ALLOWED_STAGES = (
    "开发中",
    "等待联调",
    "等待测试",
    "测试中",
    "已完成",
    "暂时无法确认",
)

_STAGE_COPY = {
    "开发中": {
        "tone": "active",
        "summary": "这项工作正在推进，已经进入实际开发。",
        "next_step": "继续完成当前功能，并准备进入后续检查。",
    },
    "等待联调": {
        "tone": "waiting",
        "summary": "这部分主体工作已经完成，正在等相关功能连起来一起验证。",
        "next_step": "把相关功能连接起来，确认它们能够一起正常工作。",
    },
    "等待测试": {
        "tone": "waiting",
        "summary": "这部分开发内容已经准备好，正在等待进入完整检查。",
        "next_step": "进入完整检查，确认功能和原有内容没有出现新的问题。",
    },
    "测试中": {
        "tone": "checking",
        "summary": "这项功能已经进入检查阶段，正在确认是否稳定、有没有遗漏。",
        "next_step": "完成当前检查；发现问题就修正，没有问题再进入下一阶段。",
    },
    "已完成": {
        "tone": "done",
        "summary": "这项工作已经完成当前约定范围内的开发和检查，可以作为已完成内容展示。",
        "next_step": "保持现有结果，后续只有需求变化或发现新问题时再调整。",
    },
    "暂时无法确认": {
        "tone": "unknown",
        "summary": "目前掌握的信息还不足以判断这项工作的真实状态，因此暂不下结论。",
        "next_step": "先补齐能够核实的依据，再更新这项工作的状态。",
    },
}

_RESULT_KEYS = (
    "schema_version",
    "module_name",
    "stage",
    "display_stage",
    "tone",
    "summary",
    "next_step",
    "client_stage_summary_hash",
)
_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "client_stage_summary_hash")


class ClientStageSummaryError(ValueError):
    """Stable fail-closed error for unsupported or malformed stage input."""

    code = "CLIENT_STAGE_SUMMARY_INVALID"

    def __init__(self, message: str = code):
        super().__init__(message)


def _fail() -> ClientStageSummaryError:
    return ClientStageSummaryError()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fail() from exc


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_module_name(value: object) -> str:
    if type(value) is not str:
        raise _fail()
    if value != value.strip() or not value or len(value) > 80:
        raise _fail()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _fail()
    return value


def _validate_stage(value: object) -> str:
    if type(value) is not str or value not in _ALLOWED_STAGES:
        raise _fail()
    return value


def build_client_stage_summary(*, module_name: str, stage: str) -> dict[str, object]:
    """Translate one formal module stage into client-readable copy.

    The function intentionally accepts only the six project-level formal stages.
    It does not guess missing state, calculate completion, or expose PR/CI/SHA and
    other engineering workflow details.
    """

    name = _validate_module_name(module_name)
    formal_stage = _validate_stage(stage)
    copy = _STAGE_COPY[formal_stage]

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "module_name": name,
        "stage": formal_stage,
        "display_stage": formal_stage,
        "tone": copy["tone"],
        "summary": copy["summary"],
        "next_step": copy["next_step"],
    }
    result["client_stage_summary_hash"] = _stable_hash(
        {key: result[key] for key in _HASH_KEYS}
    )

    if tuple(result) != _RESULT_KEYS:
        raise _fail()
    return result
