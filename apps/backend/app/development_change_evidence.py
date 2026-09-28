"""Daily Change Evidence Core V1: turn frozen Git range facts into report-ready objective evidence.

This module is intentionally pure. It does not read Git, files, databases, networks, or provider
APIs. It only projects an already-computed ``RangeCandidate`` into deterministic evidence that a
later report renderer / AI explanation layer may consume.

Line additions/deletions are supporting evidence only. They are never a progress percentage,
quality score, completion score, or delivery score.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from app.git_analysis import FileEvidenceCandidate, RangeCandidate


SCHEMA_VERSION = "development_change_evidence_v1"
EVIDENCE_SCOPE = "git_range_objective_facts"
LINE_CHANGE_METRIC_ROLE = "supporting_evidence_only"

_AREA_ORDER = (
    "backend",
    "frontend",
    "backend_tests",
    "frontend_tests",
    "ci",
    "documentation",
    "tooling",
    "installer",
    "database_migrations",
    "other",
)

_RESULT_KEYS = (
    "schema_version",
    "evidence_scope",
    "baseline_commit",
    "remote_head",
    "continuity",
    "capacity",
    "evidence_state",
    "has_change",
    "commit_count",
    "commit_ids",
    "changed_file_count",
    "added_lines",
    "deleted_lines",
    "line_change_total",
    "line_change_metric_role",
    "text_file_count",
    "binary_file_count",
    "production_change_file_count",
    "test_change_file_count",
    "ci_change_file_count",
    "documentation_change_file_count",
    "operations_change_file_count",
    "other_change_file_count",
    "affected_areas",
    "files",
    "development_change_evidence_hash",
)
_HASH_KEYS = tuple(key for key in _RESULT_KEYS if key != "development_change_evidence_hash")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


class DevelopmentChangeEvidenceError(RuntimeError):
    """Stable fail-closed error for structurally inconsistent RangeCandidate input."""

    code = "DEVELOPMENT_CHANGE_EVIDENCE_INVALID"

    def __init__(self, message: str = code):
        super().__init__(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _fail() -> DevelopmentChangeEvidenceError:
    return DevelopmentChangeEvidenceError()


def _is_sha40(value: object) -> bool:
    return isinstance(value, str) and _SHA40_RE.fullmatch(value) is not None


def _area_for_path(path: str) -> str:
    if path.startswith("apps/frontend/tests/"):
        return "frontend_tests"
    if path.startswith("apps/backend/"):
        return "backend"
    if path.startswith("apps/frontend/"):
        return "frontend"
    if path.startswith("tests/backend/"):
        return "backend_tests"
    if path.startswith("tests/frontend/"):
        return "frontend_tests"
    if path.startswith(".github/"):
        return "ci"
    if path.startswith("docs/") or path == "README.md":
        return "documentation"
    if path.startswith("scripts/"):
        return "tooling"
    if path.startswith("installer/"):
        return "installer"
    if path.startswith("migrations/"):
        return "database_migrations"
    return "other"


def _validate_file(file: object) -> FileEvidenceCandidate:
    if not isinstance(file, FileEvidenceCandidate):
        raise _fail()
    if type(file.path) is not str or not file.path or "\x00" in file.path:
        raise _fail()
    if type(file.is_binary) is not bool:
        raise _fail()
    if file.is_binary:
        if file.added_lines is not None or file.deleted_lines is not None:
            raise _fail()
    else:
        if (
            type(file.added_lines) is not int
            or type(file.deleted_lines) is not int
            or file.added_lines < 0
            or file.deleted_lines < 0
        ):
            raise _fail()
    return file


def _validate_candidate(candidate: object) -> RangeCandidate:
    if not isinstance(candidate, RangeCandidate):
        raise _fail()
    if not _is_sha40(candidate.baseline_commit) or not _is_sha40(candidate.remote_head):
        raise _fail()
    if candidate.continuity not in {"no_new_commit", "continuous", "checkpoint_unreachable"}:
        raise _fail()
    if candidate.capacity not in {"within", "capacity_exceeded"}:
        raise _fail()

    integer_fields = (
        candidate.commit_count,
        candidate.changed_file_count,
        candidate.added_lines,
        candidate.deleted_lines,
        candidate.diff_bytes,
    )
    if any(type(value) is not int or value < 0 for value in integer_fields):
        raise _fail()

    if not isinstance(candidate.commits, list) or any(not _is_sha40(value) for value in candidate.commits):
        raise _fail()
    if len(set(candidate.commits)) != len(candidate.commits):
        raise _fail()
    if not isinstance(candidate.files, tuple):
        raise _fail()

    files = tuple(_validate_file(file) for file in candidate.files)
    paths = [file.path for file in files]
    if len(set(paths)) != len(paths):
        raise _fail()

    if candidate.capacity == "within":
        if candidate.commit_count != len(candidate.commits):
            raise _fail()
        if candidate.changed_file_count != len(files):
            raise _fail()
        if candidate.continuity == "continuous":
            expected_added = sum(file.added_lines or 0 for file in files)
            expected_deleted = sum(file.deleted_lines or 0 for file in files)
            if candidate.added_lines != expected_added or candidate.deleted_lines != expected_deleted:
                raise _fail()

    if candidate.continuity == "no_new_commit":
        if (
            candidate.baseline_commit != candidate.remote_head
            or candidate.commit_count != 0
            or candidate.changed_file_count != 0
            or candidate.added_lines != 0
            or candidate.deleted_lines != 0
            or candidate.commits
            or files
        ):
            raise _fail()
    elif candidate.continuity == "checkpoint_unreachable":
        if candidate.baseline_commit == candidate.remote_head:
            raise _fail()
        if (
            candidate.commit_count != 0
            or candidate.changed_file_count != 0
            or candidate.added_lines != 0
            or candidate.deleted_lines != 0
            or candidate.commits
            or files
        ):
            raise _fail()
    else:
        if candidate.baseline_commit == candidate.remote_head or candidate.commit_count <= 0:
            raise _fail()

    return candidate


def _evidence_state(candidate: RangeCandidate) -> str:
    if candidate.continuity == "checkpoint_unreachable":
        return "unavailable_checkpoint_unreachable"
    if candidate.capacity == "capacity_exceeded":
        return "partial_capacity_exceeded"
    if candidate.continuity == "no_new_commit":
        return "complete_no_change"
    return "complete"


def _file_projection(file: FileEvidenceCandidate) -> dict[str, object]:
    return {
        "path": file.path,
        "area": _area_for_path(file.path),
        "added_lines": file.added_lines,
        "deleted_lines": file.deleted_lines,
        "is_binary": file.is_binary,
    }


def _affected_areas(files: tuple[FileEvidenceCandidate, ...]) -> list[dict[str, object]]:
    buckets: dict[str, dict[str, int]] = {
        area: {
            "file_count": 0,
            "text_file_count": 0,
            "binary_file_count": 0,
            "added_lines": 0,
            "deleted_lines": 0,
        }
        for area in _AREA_ORDER
    }
    for file in files:
        area = _area_for_path(file.path)
        bucket = buckets[area]
        bucket["file_count"] += 1
        if file.is_binary:
            bucket["binary_file_count"] += 1
        else:
            bucket["text_file_count"] += 1
            bucket["added_lines"] += file.added_lines or 0
            bucket["deleted_lines"] += file.deleted_lines or 0

    return [
        {"area": area, **buckets[area]}
        for area in _AREA_ORDER
        if buckets[area]["file_count"] > 0
    ]


def build_development_change_evidence(*, candidate: RangeCandidate) -> dict[str, object]:
    """Build deterministic, report-ready objective change evidence from a frozen Git range.

    This function deliberately does not infer feature completion, quality, risk severity, or project
    progress. A later report layer may explain these facts, but it must keep additions/deletions as
    supporting evidence rather than a progress score.
    """
    value = _validate_candidate(candidate)
    files = tuple(value.files)
    projected_files = [_file_projection(file) for file in files]

    production_count = sum(
        1
        for file in files
        if file.path.startswith("apps/") and not file.path.startswith("apps/frontend/tests/")
    )
    test_count = sum(
        1
        for file in files
        if file.path.startswith(("tests/", "apps/frontend/tests/"))
    )
    ci_count = sum(1 for file in files if file.path.startswith(".github/"))
    documentation_count = sum(
        1 for file in files if file.path.startswith("docs/") or file.path == "README.md"
    )
    operations_count = sum(
        1
        for file in files
        if file.path.startswith(("scripts/", "installer/", "migrations/"))
    )
    categorized = production_count + test_count + ci_count + documentation_count + operations_count
    other_count = len(files) - categorized

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_scope": EVIDENCE_SCOPE,
        "baseline_commit": value.baseline_commit,
        "remote_head": value.remote_head,
        "continuity": value.continuity,
        "capacity": value.capacity,
        "evidence_state": _evidence_state(value),
        "has_change": value.continuity == "continuous" and value.commit_count > 0,
        "commit_count": value.commit_count,
        "commit_ids": list(value.commits),
        "changed_file_count": value.changed_file_count,
        "added_lines": value.added_lines,
        "deleted_lines": value.deleted_lines,
        "line_change_total": value.added_lines + value.deleted_lines,
        "line_change_metric_role": LINE_CHANGE_METRIC_ROLE,
        "text_file_count": sum(1 for file in files if not file.is_binary),
        "binary_file_count": sum(1 for file in files if file.is_binary),
        "production_change_file_count": production_count,
        "test_change_file_count": test_count,
        "ci_change_file_count": ci_count,
        "documentation_change_file_count": documentation_count,
        "operations_change_file_count": operations_count,
        "other_change_file_count": other_count,
        "affected_areas": _affected_areas(files),
        "files": projected_files,
    }
    hash_payload = {key: deepcopy(result[key]) for key in _HASH_KEYS}
    result["development_change_evidence_hash"] = _stable_hash(hash_payload)
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("development_change_evidence_v1 key construction drift")
    return result
