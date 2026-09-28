"""Profile-bound dynamic Anxin Board report V2.

V1 remains in ``anxin_board_report.py`` unchanged.  V2 replaces only the module
identity authority: module id/name/order come from one hash-bound Project Profile.
The module stage summaries and daily-change payload keep the existing formal
validation semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from pydantic import ValidationError

from app.anxin_board_report import (
    DEFAULT_MANAGER_SUPPLEMENT,
    MOTTO,
    TITLE,
    AnxinBoardReportError,
    _assert_client_language,
    _clean_text,
    _is_sha256,
    _stable_hash,
    _validate_daily_change,
    _validate_project_name,
    _validate_report_daily_change,
    _validate_report_date,
    _validate_report_module,
    _validate_stage_summary,
)
from app.project_profiles import (
    ProjectProfileContent,
    parse_profile_content,
    profile_planned_modules,
    _canonicalize,
    _completeness_missing,
    _path_pattern_error,
)


SCHEMA_VERSION = "anxin_board_report_v2"
PROFILE_SCHEMA_VERSION = "project_profile_manual_v1"

_RESULT_KEYS = (
    "schema_version",
    "title",
    "motto",
    "project_name",
    "report_date",
    "profile_id",
    "profile_version_no",
    "profile_content_hash",
    "source_prd_id",
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


class AnxinBoardReportV2Error(AnxinBoardReportError):
    code = "ANXIN_BOARD_REPORT_V2_INVALID"


def _fail() -> AnxinBoardReportV2Error:
    return AnxinBoardReportV2Error()


def _from_v1(func, /, *args, **kwargs):
    """Reuse frozen V1 semantic validators without leaking the V1 report error contract."""

    try:
        return func(*args, **kwargs)
    except AnxinBoardReportV2Error:
        raise
    except AnxinBoardReportError as exc:
        raise _fail() from exc


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise _fail()
    return value


def _validated_profile_authority(
    value: object,
    *,
    require_current_confirmed: bool,
) -> dict[str, object]:
    """Re-close the exact stored Profile identity/content used as report authority.

    ``superseded`` is accepted only for historical report validation.  New report
    construction must always use the current ``confirmed`` Profile.
    """

    if not isinstance(value, Mapping):
        raise _fail()

    profile_id = _positive_int(value.get("id"))
    project_id = _positive_int(value.get("project_id"))
    version_no = _positive_int(value.get("version_no"))
    source_prd_id = _positive_int(value.get("source_prd_id"))
    status = value.get("status")
    allowed_statuses = {"confirmed"} if require_current_confirmed else {"confirmed", "superseded"}
    if status not in allowed_statuses:
        raise _fail()

    content_hash = value.get("content_hash")
    if not _is_sha256(content_hash):
        raise _fail()

    try:
        content = parse_profile_content(value.get("content"))
    except (ValidationError, TypeError, ValueError) as exc:
        raise _fail() from exc
    if content.schema_version not in {PROFILE_SCHEMA_VERSION, "project_profile_v2"} or not profile_planned_modules(content):
        raise _fail()

    dumped_content = content.model_dump()
    if _completeness_missing(dumped_content):
        raise _fail()

    seen: set[str] = set()
    modules: list[dict[str, str]] = []
    for raw_module in profile_planned_modules(content):
        from app.project_profiles import ProfileModule
        module = ProfileModule.model_validate(raw_module)
        if module.client_id in seen or not module.name:
            raise _fail()
        seen.add(module.client_id)
        for path in module.paths:
            if _path_pattern_error(path.pattern) is not None:
                raise _fail()
        modules.append({"module_id": module.client_id, "name": module.name})

    _, recomputed_hash = _canonicalize(content)
    if recomputed_hash != content_hash:
        raise _fail()

    return {
        "id": profile_id,
        "project_id": project_id,
        "version_no": version_no,
        "source_prd_id": source_prd_id,
        "status": status,
        "content_hash": content_hash,
        "modules": modules,
    }


def _overall_message(*, total: int, completed: int, active: int, unknown: int, development_only: bool = False) -> str:
    if completed == total and active == 0 and unknown == 0:
        return "当前约定范围已完成开发。" if development_only else "当前约定范围已完成。"

    parts: list[str] = []
    if active:
        parts.append(f"当前有 {active} 项工作正在推进或等待后续检查")
    if completed:
        parts.append(f"有 {completed} 项已经完成当前约定范围内的开发" + ("" if development_only else "和检查"))
    if unknown:
        parts.append(f"另有 {unknown} 项暂时缺少足够依据，暂不下结论")
    if not parts:
        raise _fail()
    return "；".join(parts) + "。"


def build_anxin_board_report_v2(
    *,
    project_name: str,
    report_date: str,
    profile: Mapping[str, object],
    module_summaries: list[Mapping[str, object]],
    daily_change: Mapping[str, object],
    manager_supplement: str = DEFAULT_MANAGER_SUPPLEMENT,
) -> dict[str, object]:
    """Build one V2 report whose module truth comes only from a confirmed Profile."""

    project = _from_v1(_validate_project_name, project_name)
    day = _from_v1(_validate_report_date, report_date)
    authority = _validated_profile_authority(profile, require_current_confirmed=True)

    expected_modules = authority["modules"]
    if not isinstance(module_summaries, list) or len(module_summaries) != len(expected_modules):
        raise _fail()

    modules: list[dict[str, object]] = []
    for expected, source in zip(expected_modules, module_summaries, strict=True):
        expected_id = expected["module_id"]
        expected_name = expected["name"]
        summary = _from_v1(_validate_stage_summary, source, expected_name=expected_name)
        modules.append(
            {
                "module_id": expected_id,
                "name": expected_name,
                "stage": summary["stage"],
                "display_stage": summary["display_stage"],
                "tone": summary["tone"],
                "summary": summary["summary"],
                "next_step": summary["next_step"],
                "client_stage_summary_hash": summary["client_stage_summary_hash"],
            }
        )

    change = _from_v1(_validate_daily_change, daily_change)
    supplement = _from_v1(_clean_text, manager_supplement, max_len=1000)
    _from_v1(_assert_client_language, supplement)

    completed = sum(1 for item in modules if item["stage"] == "已完成")
    unknown = sum(1 for item in modules if item["stage"] == "暂时无法确认")
    active = len(modules) - completed - unknown
    overall = _overall_message(
        total=len(modules),
        completed=completed,
        active=active,
        unknown=unknown,
    )
    _from_v1(_assert_client_language, overall)

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "title": TITLE,
        "motto": MOTTO,
        "project_name": project,
        "report_date": day,
        "profile_id": authority["id"],
        "profile_version_no": authority["version_no"],
        "profile_content_hash": authority["content_hash"],
        "source_prd_id": authority["source_prd_id"],
        "module_count": len(modules),
        "completed_module_count": completed,
        "active_module_count": active,
        "unknown_module_count": unknown,
        "overall_message": overall,
        "modules": modules,
        "daily_change": change,
        "manager_supplement": supplement,
    }
    result["anxin_board_report_hash"] = _from_v1(
        _stable_hash,
        {key: deepcopy(result[key]) for key in _HASH_KEYS},
    )
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("anxin_board_report_v2 key construction drift")
    return result


def validate_anxin_board_report_v2(
    value: object,
    *,
    profile: Mapping[str, object],
) -> dict[str, object]:
    """Validate V2 against the exact bound Profile and return a detached copy."""

    if not isinstance(value, Mapping) or tuple(value) != _RESULT_KEYS:
        raise _fail()
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["title"] != TITLE
        or value["motto"] != MOTTO
        or not _is_sha256(value["anxin_board_report_hash"])
        or not isinstance(value["modules"], list)
    ):
        raise _fail()

    authority = _validated_profile_authority(profile, require_current_confirmed=False)
    if (
        value["profile_id"] != authority["id"]
        or value["profile_version_no"] != authority["version_no"]
        or value["profile_content_hash"] != authority["content_hash"]
        or value["source_prd_id"] != authority["source_prd_id"]
    ):
        raise _fail()

    expected_modules = authority["modules"]
    if len(value["modules"]) != len(expected_modules):
        raise _fail()

    project = _from_v1(_validate_project_name, value["project_name"])
    day = _from_v1(_validate_report_date, value["report_date"])
    modules = [
        _from_v1(
            _validate_report_module,
            item,
            expected_id=expected["module_id"],
            expected_name=expected["name"],
        )
        for expected, item in zip(expected_modules, value["modules"], strict=True)
    ]
    daily_change = _from_v1(_validate_report_daily_change, value["daily_change"])
    supplement = _from_v1(_clean_text, value["manager_supplement"], max_len=1000)
    _from_v1(_assert_client_language, supplement)

    completed = sum(1 for item in modules if item["stage"] == "已完成")
    unknown = sum(1 for item in modules if item["stage"] == "暂时无法确认")
    active = len(modules) - completed - unknown
    expected_overall = _overall_message(
        total=len(modules),
        completed=completed,
        active=active,
        unknown=unknown,
    )

    for count_key, expected in (
        ("module_count", len(modules)),
        ("completed_module_count", completed),
        ("active_module_count", active),
        ("unknown_module_count", unknown),
    ):
        if type(value[count_key]) is not int or value[count_key] != expected:
            raise _fail()
    development_overall = _overall_message(total=len(modules), completed=completed, active=active,
                                           unknown=unknown, development_only=True)
    if value["overall_message"] not in (expected_overall, development_overall):
        raise _fail()
    expected_overall = value["overall_message"]

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "title": TITLE,
        "motto": MOTTO,
        "project_name": project,
        "report_date": day,
        "profile_id": authority["id"],
        "profile_version_no": authority["version_no"],
        "profile_content_hash": authority["content_hash"],
        "source_prd_id": authority["source_prd_id"],
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
    if _from_v1(
        _stable_hash,
        {key: deepcopy(result[key]) for key in _HASH_KEYS},
    ) != result["anxin_board_report_hash"]:
        raise _fail()
    return result
