"""Assemble and validate client-facing 安心看板 formal report data.

This module is deliberately pure. It does not read Git, files, databases, networks,
or AI providers. It validates and assembles client-readable module-stage summaries,
a plain-language daily-change summary, and project-manager supplement text.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import hashlib
import json
import re


SCHEMA_VERSION = "anxin_board_report_v1"
TITLE = "安心看板"
MOTTO = "非己所安，不加于物"
DEFAULT_MANAGER_SUPPLEMENT = "同意大模型的日报。"
STAGE_SUMMARY_SCHEMA = "client_stage_summary_v1"
DAILY_CHANGE_SCHEMA = "plain_language_change_summary_v1"

MODULES = (
    ("local_app", "Windows 本地软件"),
    ("project_management", "项目管理"),
    ("git", "代码版本读取"),
    ("prd", "需求文档读取"),
    ("project_profile", "项目基础资料"),
    ("anxin_board", "安心看板自动生成"),
    ("ai_correction", "AI 结果纠正"),
    ("formal_report", "正式 AI 报告"),
    ("email", "邮件发送"),
    ("report_evidence", "报告依据与留痕"),
    ("history", "历史报告查看"),
)
_ALLOWED_STAGES = {
    "开发中",
    "等待联调",
    "等待测试",
    "测试中",
    "已完成",
    "暂时无法确认",
}
_ALLOWED_TONES = {"active", "waiting", "checking", "done", "unknown"}
_ALLOWED_CHANGE_STATES = {"ready", "no_change", "unavailable", "partial"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_CLIENT_TERMS = (
    "pr",
    "ci",
    "sha",
    "token",
    "framing",
    "commit",
    "diff",
    "runner",
    "provider",
    "workflow",
    "pull request",
    "百分比",
    "完成率",
    "质量评分",
    "个人绩效评分",
)

_RESULT_KEYS = (
    "schema_version",
    "title",
    "motto",
    "project_name",
    "report_date",
    "module_count",
    "completed_module_count",
    "active_module_count",
    "unknown_module_count",
    "overall_message",
    "modules",
    "daily_change",
    "manager_supplement",
    "anxin_board_report_hash",
)
_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "anxin_board_report_hash")
_STAGE_KEYS = (
    "schema_version",
    "module_name",
    "stage",
    "display_stage",
    "tone",
    "summary",
    "next_step",
    "client_stage_summary_hash",
)
_CHANGE_KEYS = (
    "schema_version",
    "source_evidence_hash",
    "display_state",
    "headline",
    "summary",
    "highlights",
    "scope_note",
    "plain_language_change_summary_hash",
)
_REPORT_MODULE_KEYS = (
    "module_id",
    "name",
    "stage",
    "display_stage",
    "tone",
    "summary",
    "next_step",
    "client_stage_summary_hash",
)
_REPORT_DAILY_CHANGE_KEYS = (
    "display_state",
    "headline",
    "summary",
    "highlights",
    "scope_note",
    "plain_language_change_summary_hash",
)


class AnxinBoardReportError(ValueError):
    code = "ANXIN_BOARD_REPORT_INVALID"

    def __init__(self, message: str = code):
        super().__init__(message)


def _fail() -> AnxinBoardReportError:
    return AnxinBoardReportError()


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


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _clean_text(value: object, *, max_len: int, allow_empty: bool = False) -> str:
    if type(value) is not str or value != value.strip() or len(value) > max_len:
        raise _fail()
    if not allow_empty and not value:
        raise _fail()
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise _fail()
    return value


def _assert_client_language(value: str) -> None:
    lowered = value.lower()
    if "%" in value:
        raise _fail()
    for term in _FORBIDDEN_CLIENT_TERMS:
        if term in lowered:
            raise _fail()


def _validate_project_name(value: object) -> str:
    text = _clean_text(value, max_len=120)
    _assert_client_language(text)
    return text


def _validate_report_date(value: object) -> str:
    text = _clean_text(value, max_len=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise _fail() from exc
    if parsed.isoformat() != text:
        raise _fail()
    return text


def _validate_stage_summary(value: object, *, expected_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _STAGE_KEYS:
        raise _fail()
    if (
        value["schema_version"] != STAGE_SUMMARY_SCHEMA
        or value["module_name"] != expected_name
        or value["stage"] not in _ALLOWED_STAGES
        or value["display_stage"] != value["stage"]
        or value["tone"] not in _ALLOWED_TONES
        or not _is_sha256(value["client_stage_summary_hash"])
    ):
        raise _fail()

    for field in ("module_name", "stage", "display_stage", "tone", "summary", "next_step"):
        if type(value[field]) is not str:
            raise _fail()
    summary = _clean_text(value["summary"], max_len=300)
    next_step = _clean_text(value["next_step"], max_len=300)
    _assert_client_language(summary)
    _assert_client_language(next_step)

    payload = {key: deepcopy(value[key]) for key in _STAGE_KEYS if key != "client_stage_summary_hash"}
    if _stable_hash(payload) != value["client_stage_summary_hash"]:
        raise _fail()
    return {key: deepcopy(value[key]) for key in _STAGE_KEYS}


def _validate_highlight(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != ("label", "value", "unit"):
        raise _fail()
    label = _clean_text(value["label"], max_len=80)
    unit = _clean_text(value["unit"], max_len=20)
    number = value["value"]
    if type(number) is not int or number < 0:
        raise _fail()
    _assert_client_language(label)
    _assert_client_language(unit)
    return {"label": label, "value": number, "unit": unit}


def _validate_daily_change(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _CHANGE_KEYS:
        raise _fail()
    if (
        value["schema_version"] != DAILY_CHANGE_SCHEMA
        or value["display_state"] not in _ALLOWED_CHANGE_STATES
        or not _is_sha256(value["source_evidence_hash"])
        or not _is_sha256(value["plain_language_change_summary_hash"])
        or not isinstance(value["highlights"], list)
    ):
        raise _fail()

    headline = _clean_text(value["headline"], max_len=160)
    summary = _clean_text(value["summary"], max_len=500)
    scope_note = _clean_text(value["scope_note"], max_len=300)
    for text in (headline, summary, scope_note):
        _assert_client_language(text)
    highlights = [_validate_highlight(item) for item in value["highlights"]]
    if len(highlights) > 6:
        raise _fail()

    payload = {key: deepcopy(value[key]) for key in _CHANGE_KEYS if key != "plain_language_change_summary_hash"}
    if _stable_hash(payload) != value["plain_language_change_summary_hash"]:
        raise _fail()

    return {
        "display_state": value["display_state"],
        "headline": headline,
        "summary": summary,
        "highlights": highlights,
        "scope_note": scope_note,
        "plain_language_change_summary_hash": value["plain_language_change_summary_hash"],
    }


def _overall_message(*, completed: int, active: int, unknown: int) -> str:
    if completed == len(MODULES) and active == 0 and unknown == 0:
        return "当前约定范围已完成。"

    parts: list[str] = []
    if active:
        parts.append(f"当前有 {active} 项工作正在推进或等待后续检查")
    if completed:
        parts.append(f"有 {completed} 项已经完成当前约定范围内的开发和检查")
    if unknown:
        parts.append(f"另有 {unknown} 项暂时缺少足够依据，暂不下结论")
    if not parts:
        raise _fail()
    return "；".join(parts) + "。"


def _validate_report_module(
    value: object,
    *,
    expected_id: str,
    expected_name: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _REPORT_MODULE_KEYS:
        raise _fail()
    if (
        value["module_id"] != expected_id
        or value["name"] != expected_name
        or value["stage"] not in _ALLOWED_STAGES
        or value["display_stage"] != value["stage"]
        or value["tone"] not in _ALLOWED_TONES
        or not _is_sha256(value["client_stage_summary_hash"])
    ):
        raise _fail()

    summary = _clean_text(value["summary"], max_len=300)
    next_step = _clean_text(value["next_step"], max_len=300)
    _assert_client_language(summary)
    _assert_client_language(next_step)

    stage_payload = {
        "schema_version": STAGE_SUMMARY_SCHEMA,
        "module_name": expected_name,
        "stage": value["stage"],
        "display_stage": value["display_stage"],
        "tone": value["tone"],
        "summary": summary,
        "next_step": next_step,
    }
    if _stable_hash(stage_payload) != value["client_stage_summary_hash"]:
        raise _fail()

    return {
        "module_id": expected_id,
        "name": expected_name,
        "stage": value["stage"],
        "display_stage": value["display_stage"],
        "tone": value["tone"],
        "summary": summary,
        "next_step": next_step,
        "client_stage_summary_hash": value["client_stage_summary_hash"],
    }


def _validate_report_daily_change(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _REPORT_DAILY_CHANGE_KEYS:
        raise _fail()
    if (
        value["display_state"] not in _ALLOWED_CHANGE_STATES
        or not isinstance(value["highlights"], list)
        or not _is_sha256(value["plain_language_change_summary_hash"])
    ):
        raise _fail()

    headline = _clean_text(value["headline"], max_len=160)
    summary = _clean_text(value["summary"], max_len=500)
    scope_note = _clean_text(value["scope_note"], max_len=300)
    for text in (headline, summary, scope_note):
        _assert_client_language(text)
    highlights = [_validate_highlight(item) for item in value["highlights"]]
    if len(highlights) > 6:
        raise _fail()

    return {
        "display_state": value["display_state"],
        "headline": headline,
        "summary": summary,
        "highlights": highlights,
        "scope_note": scope_note,
        "plain_language_change_summary_hash": value["plain_language_change_summary_hash"],
    }


def validate_anxin_board_report(value: object) -> dict[str, object]:
    """Validate an existing formal report independently and return a detached copy."""

    if not isinstance(value, Mapping) or tuple(value) != _RESULT_KEYS:
        raise _fail()
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["title"] != TITLE
        or value["motto"] != MOTTO
        or not _is_sha256(value["anxin_board_report_hash"])
        or not isinstance(value["modules"], list)
        or len(value["modules"]) != len(MODULES)
    ):
        raise _fail()

    project = _validate_project_name(value["project_name"])
    day = _validate_report_date(value["report_date"])
    modules = [
        _validate_report_module(item, expected_id=module_id, expected_name=module_name)
        for (module_id, module_name), item in zip(MODULES, value["modules"], strict=True)
    ]
    daily_change = _validate_report_daily_change(value["daily_change"])
    supplement = _clean_text(value["manager_supplement"], max_len=1000)
    _assert_client_language(supplement)

    completed = sum(1 for item in modules if item["stage"] == "已完成")
    unknown = sum(1 for item in modules if item["stage"] == "暂时无法确认")
    active = len(modules) - completed - unknown
    expected_overall = _overall_message(completed=completed, active=active, unknown=unknown)

    for count_key, expected in (
        ("module_count", len(modules)),
        ("completed_module_count", completed),
        ("active_module_count", active),
        ("unknown_module_count", unknown),
    ):
        if type(value[count_key]) is not int or value[count_key] != expected:
            raise _fail()
    if value["overall_message"] != expected_overall:
        raise _fail()

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "title": TITLE,
        "motto": MOTTO,
        "project_name": project,
        "report_date": day,
        "module_count": len(modules),
        "completed_module_count": completed,
        "active_module_count": active,
        "unknown_module_count": unknown,
        "overall_message": expected_overall,
        "modules": modules,
        "daily_change": daily_change,
        "manager_supplement": supplement,
        "anxin_board_report_hash": value["anxin_board_report_hash"],
    }
    if _stable_hash({key: deepcopy(result[key]) for key in _HASH_KEYS}) != result[
        "anxin_board_report_hash"
    ]:
        raise _fail()
    return result


def build_anxin_board_report(
    *,
    project_name: str,
    report_date: str,
    module_summaries: list[Mapping[str, object]],
    daily_change: Mapping[str, object],
    manager_supplement: str = DEFAULT_MANAGER_SUPPLEMENT,
) -> dict[str, object]:
    """Assemble one deterministic client-facing 安心看板 report payload."""

    project = _validate_project_name(project_name)
    day = _validate_report_date(report_date)
    if not isinstance(module_summaries, list) or len(module_summaries) != len(MODULES):
        raise _fail()

    modules: list[dict[str, object]] = []
    for (module_id, expected_name), source in zip(MODULES, module_summaries, strict=True):
        summary = _validate_stage_summary(source, expected_name=expected_name)
        modules.append(
            {
                "module_id": module_id,
                "name": expected_name,
                "stage": summary["stage"],
                "display_stage": summary["display_stage"],
                "tone": summary["tone"],
                "summary": summary["summary"],
                "next_step": summary["next_step"],
                "client_stage_summary_hash": summary["client_stage_summary_hash"],
            }
        )

    change = _validate_daily_change(daily_change)
    supplement = _clean_text(manager_supplement, max_len=1000)
    _assert_client_language(supplement)

    completed = sum(1 for item in modules if item["stage"] == "已完成")
    unknown = sum(1 for item in modules if item["stage"] == "暂时无法确认")
    active = len(modules) - completed - unknown
    overall = _overall_message(completed=completed, active=active, unknown=unknown)
    _assert_client_language(overall)

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "title": TITLE,
        "motto": MOTTO,
        "project_name": project,
        "report_date": day,
        "module_count": len(modules),
        "completed_module_count": completed,
        "active_module_count": active,
        "unknown_module_count": unknown,
        "overall_message": overall,
        "modules": modules,
        "daily_change": change,
        "manager_supplement": supplement,
    }
    result["anxin_board_report_hash"] = _stable_hash(
        {key: deepcopy(result[key]) for key in _HASH_KEYS}
    )
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("anxin_board_report_v1 key construction drift")
    return result
