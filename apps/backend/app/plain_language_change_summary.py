"""Plain-language daily change summary for the client-facing 安心看板.

This module is deliberately pure and deterministic. It consumes an already-frozen
``development_change_evidence_v1`` mapping and turns those objective facts into
short Chinese copy that a non-technical client can understand.

It must never turn line counts into progress, completion, or quality scores.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re


SCHEMA_VERSION = "plain_language_change_summary_v1"
SOURCE_SCHEMA_VERSION = "development_change_evidence_v1"
LINE_CHANGE_METRIC_ROLE = "supporting_evidence_only"

_RESULT_KEYS = (
    "schema_version",
    "source_evidence_hash",
    "display_state",
    "headline",
    "summary",
    "highlights",
    "scope_note",
    "plain_language_change_summary_hash",
)
_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "plain_language_change_summary_hash")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_ALLOWED_EVIDENCE_STATES = {
    "complete",
    "complete_no_change",
    "unavailable_checkpoint_unreachable",
    "partial_capacity_exceeded",
}

_AREA_LABELS = {
    "backend": "后台功能",
    "frontend": "页面体验",
    "backend_tests": "自动检查",
    "frontend_tests": "自动检查",
    "ci": "自动检查",
    "documentation": "项目说明",
    "tooling": "开发工具",
    "installer": "安装与运行",
    "database_migrations": "数据结构",
    "other": "其他项目文件",
}


class PlainLanguageChangeSummaryError(RuntimeError):
    """Stable fail-closed error for invalid or internally inconsistent source evidence."""

    code = "PLAIN_LANGUAGE_CHANGE_SUMMARY_INVALID"

    def __init__(self, message: str = code):
        super().__init__(message)


def _fail() -> PlainLanguageChangeSummaryError:
    return PlainLanguageChangeSummaryError()


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


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _fail()
    return value


def _validate_source(evidence: object) -> Mapping[str, object]:
    if not isinstance(evidence, Mapping):
        raise _fail()

    required = {
        "schema_version",
        "evidence_state",
        "has_change",
        "commit_count",
        "changed_file_count",
        "added_lines",
        "deleted_lines",
        "line_change_total",
        "line_change_metric_role",
        "production_change_file_count",
        "test_change_file_count",
        "ci_change_file_count",
        "documentation_change_file_count",
        "operations_change_file_count",
        "other_change_file_count",
        "affected_areas",
        "development_change_evidence_hash",
    }
    if not required.issubset(evidence):
        raise _fail()

    if evidence["schema_version"] != SOURCE_SCHEMA_VERSION:
        raise _fail()
    if evidence["line_change_metric_role"] != LINE_CHANGE_METRIC_ROLE:
        raise _fail()
    if type(evidence["has_change"]) is not bool:
        raise _fail()

    evidence_state = evidence["evidence_state"]
    if evidence_state not in _ALLOWED_EVIDENCE_STATES:
        raise _fail()

    integer_fields = (
        "commit_count",
        "changed_file_count",
        "added_lines",
        "deleted_lines",
        "line_change_total",
        "production_change_file_count",
        "test_change_file_count",
        "ci_change_file_count",
        "documentation_change_file_count",
        "operations_change_file_count",
        "other_change_file_count",
    )
    counts = {field: _nonnegative_int(evidence[field]) for field in integer_fields}
    if counts["line_change_total"] != counts["added_lines"] + counts["deleted_lines"]:
        raise _fail()

    if evidence_state == "complete":
        if not evidence["has_change"] or counts["commit_count"] <= 0 or counts["changed_file_count"] <= 0:
            raise _fail()
        partition_total = sum(
            counts[field]
            for field in (
                "production_change_file_count",
                "test_change_file_count",
                "ci_change_file_count",
                "documentation_change_file_count",
                "operations_change_file_count",
                "other_change_file_count",
            )
        )
        if partition_total != counts["changed_file_count"]:
            raise _fail()
    elif evidence_state == "complete_no_change":
        if evidence["has_change"]:
            raise _fail()
        if any(counts[field] != 0 for field in integer_fields):
            raise _fail()
    elif evidence_state == "unavailable_checkpoint_unreachable":
        if evidence["has_change"]:
            raise _fail()
    elif evidence_state == "partial_capacity_exceeded":
        if not evidence["has_change"] or counts["commit_count"] <= 0:
            raise _fail()

    areas = evidence["affected_areas"]
    if not isinstance(areas, list):
        raise _fail()
    seen_areas: set[str] = set()
    for item in areas:
        if not isinstance(item, Mapping):
            raise _fail()
        area = item.get("area")
        file_count = item.get("file_count")
        if type(area) is not str or area not in _AREA_LABELS or area in seen_areas:
            raise _fail()
        if type(file_count) is not int or file_count <= 0:
            raise _fail()
        seen_areas.add(area)

    source_hash = evidence["development_change_evidence_hash"]
    if type(source_hash) is not str or _SHA256_RE.fullmatch(source_hash) is None:
        raise _fail()
    source_payload = {
        key: deepcopy(value)
        for key, value in evidence.items()
        if key != "development_change_evidence_hash"
    }
    if _stable_hash(source_payload) != source_hash:
        raise _fail()

    return evidence


def _plain_area_labels(areas: list[object]) -> list[str]:
    labels: list[str] = []
    for item in areas:
        if not isinstance(item, Mapping):
            raise _fail()
        area = item.get("area")
        if type(area) is not str or area not in _AREA_LABELS:
            raise _fail()
        label = _AREA_LABELS[area]
        if label not in labels:
            labels.append(label)
    return labels


def _join_labels(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]}和{labels[1]}"
    return "、".join(labels[:-1]) + f"和{labels[-1]}"


def _highlight(label: str, value: int, unit: str) -> dict[str, object]:
    return {"label": label, "value": value, "unit": unit}


def _changed_summary(source: Mapping[str, object], *, partial: bool) -> tuple[str, str, list[dict[str, object]], str]:
    commit_count = int(source["commit_count"])
    changed_file_count = int(source["changed_file_count"])
    line_change_total = int(source["line_change_total"])
    test_related = int(source["test_change_file_count"]) + int(source["ci_change_file_count"])
    labels = _plain_area_labels(source["affected_areas"])
    area_copy = _join_labels(labels[:4])

    if partial:
        headline = "本次有研发变化，但当前只能确认一部分"
        summary = (
            "本次的变化较多，已经超过本次安全读取范围。"
            "下面只展示当前确认到的部分，不能当作完整结果。"
        )
        update_label = "当前已确认的代码更新记录"
        file_label = "当前已确认动到的文件"
    else:
        headline = "本次有明确研发变化"
        if area_copy:
            summary = (
                f"本次留下了 {commit_count} 次代码更新记录，涉及 {changed_file_count} 个文件。"
                f"主要动到了{area_copy}。"
            )
        else:
            summary = (
                f"本次留下了 {commit_count} 次代码更新记录，涉及 {changed_file_count} 个文件。"
            )
        update_label = "本次的代码更新记录"
        file_label = "本次动到的文件"

    highlights = [
        _highlight(update_label, commit_count, "次"),
        _highlight(file_label, changed_file_count, "个"),
        _highlight("新增或调整的内容", line_change_total, "行"),
    ]
    if test_related > 0:
        highlights.append(_highlight("自动检查相关文件", test_related, "个"))
    elif labels:
        highlights.append(_highlight("涉及的工作区域", len(labels), "类"))

    scope_note = "行数只用来说明本次的变化量，不代表项目完成度或质量高低。"
    return headline, summary, highlights, scope_note


def build_plain_language_change_summary(*, evidence: Mapping[str, object]) -> dict[str, object]:
    """Turn frozen development-change evidence into client-readable Chinese copy.

    The result contains presentation copy only; it does not expose commit IDs, file paths,
    diffs, provider details, or progress percentages.
    """

    source = _validate_source(evidence)
    state = source["evidence_state"]

    if state == "complete":
        display_state = "ready"
        headline, summary, highlights, scope_note = _changed_summary(source, partial=False)
    elif state == "complete_no_change":
        display_state = "no_change"
        headline = "本次暂未发现新的代码变化"
        summary = "在当前已确认的比较范围内，没有发现新的代码变化。"
        highlights = [
            _highlight("本次的代码更新记录", 0, "次"),
            _highlight("本次动到的文件", 0, "个"),
        ]
        scope_note = "这只说明当前比较范围没有新变化，不代表项目停工或整体没有推进。"
    elif state == "unavailable_checkpoint_unreachable":
        display_state = "unavailable"
        headline = "本次的研发变化暂时无法确认"
        summary = "当前无法连续对比到上次记录，所以不能把未知情况写成“本次没有变化”。"
        highlights = []
        scope_note = "需要先恢复连续的比较依据，再生成本次变化说明。"
    elif state == "partial_capacity_exceeded":
        display_state = "partial"
        headline, summary, highlights, scope_note = _changed_summary(source, partial=True)
    else:  # pragma: no cover - _validate_source already guards this
        raise _fail()

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_evidence_hash": source["development_change_evidence_hash"],
        "display_state": display_state,
        "headline": headline,
        "summary": summary,
        "highlights": highlights,
        "scope_note": scope_note,
    }
    payload = {key: deepcopy(result[key]) for key in _HASH_KEYS}
    result["plain_language_change_summary_hash"] = _stable_hash(payload)
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("plain_language_change_summary_v1 key construction drift")
    return result
