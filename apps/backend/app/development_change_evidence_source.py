"""Read-only seam from a frozen GitSnapshot to Daily Change Evidence Core V1.

This module deliberately does not run Git, inspect a workspace, mutate SQLite, expose
an HTTP route, or call any model/provider/network service.  It only reconstructs the
already-frozen ``RangeCandidate`` identity from local SQLite facts, verifies those
facts fail-closed, and delegates the final evidence schema/hash construction to the
existing ``build_development_change_evidence`` core.
"""

from __future__ import annotations

import json
import re
import sqlite3

from fastapi import HTTPException

from app.db import get_connection
from app.development_change_evidence import (
    DevelopmentChangeEvidenceError,
    build_development_change_evidence,
)
from app.git_analysis import FileEvidenceCandidate, RangeCandidate
from app.git_snapshots import _file_manifest_records


_PROJECT_NOT_FOUND = "DEVELOPMENT_CHANGE_EVIDENCE_PROJECT_NOT_FOUND"
_SNAPSHOT_NOT_FOUND = "DEVELOPMENT_CHANGE_EVIDENCE_SNAPSHOT_NOT_FOUND"
_SOURCE_INVALID = "DEVELOPMENT_CHANGE_EVIDENCE_SOURCE_INVALID"
_SQLITE_INTEGER_MAX = 2**63 - 1
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DevelopmentChangeEvidenceSourceError(RuntimeError):
    """Stable internal error for invalid or unavailable frozen source facts."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _error(code: str) -> DevelopmentChangeEvidenceSourceError:
    return DevelopmentChangeEvidenceSourceError(code)


def _project_not_found() -> DevelopmentChangeEvidenceSourceError:
    return _error(_PROJECT_NOT_FOUND)


def _snapshot_not_found() -> DevelopmentChangeEvidenceSourceError:
    return _error(_SNAPSHOT_NOT_FOUND)


def _source_invalid() -> DevelopmentChangeEvidenceSourceError:
    return _error(_SOURCE_INVALID)


def _is_sha40(value: object) -> bool:
    return type(value) is str and _SHA40_RE.fullmatch(value) is not None


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _source_invalid()
    return value


def _parse_commits(value: object, *, expected_count: int) -> list[str]:
    if type(value) is not str:
        raise _source_invalid()
    try:
        commits = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _source_invalid() from None
    if not isinstance(commits, list) or len(commits) != expected_count:
        raise _source_invalid()
    if any(not _is_sha40(commit) for commit in commits):
        raise _source_invalid()
    if len(set(commits)) != len(commits):
        raise _source_invalid()
    return list(commits)


def _reconstruct_files(
    rows: list[sqlite3.Row],
    *,
    expected_count: int,
    expected_added: int,
    expected_deleted: int,
) -> tuple[tuple[FileEvidenceCandidate, ...], list[str]]:
    if len(rows) != expected_count:
        raise _source_invalid()

    files: list[FileEvidenceCandidate] = []
    stored_fact_hashes: list[str] = []
    added_total = 0
    deleted_total = 0

    for expected_ordinal, row in enumerate(rows, start=1):
        ordinal = row["ordinal"]
        if type(ordinal) is not int or ordinal != expected_ordinal:
            raise _source_invalid()

        path = row["path"]
        if type(path) is not str or not path or "\x00" in path:
            raise _source_invalid()

        is_binary_raw = row["is_binary"]
        if type(is_binary_raw) is not int or is_binary_raw not in (0, 1):
            raise _source_invalid()
        is_binary = is_binary_raw == 1

        added_lines = row["added_lines"]
        deleted_lines = row["deleted_lines"]
        if is_binary:
            if added_lines is not None or deleted_lines is not None:
                raise _source_invalid()
        else:
            added_lines = _nonnegative_int(added_lines)
            deleted_lines = _nonnegative_int(deleted_lines)
            added_total += added_lines
            deleted_total += deleted_lines

        file_facts_hash = row["file_facts_hash"]
        if not _is_sha256(file_facts_hash):
            raise _source_invalid()

        files.append(
            FileEvidenceCandidate(
                path=path,
                added_lines=added_lines,
                deleted_lines=deleted_lines,
                is_binary=is_binary,
            )
        )
        stored_fact_hashes.append(file_facts_hash)

    if added_total != expected_added or deleted_total != expected_deleted:
        raise _source_invalid()
    return tuple(files), stored_fact_hashes


def _reconstruct_candidate(
    snapshot: sqlite3.Row,
    file_rows: list[sqlite3.Row],
) -> tuple[RangeCandidate, list[str]]:
    from_commit = snapshot["from_commit"]
    to_commit = snapshot["to_commit"]
    if not _is_sha40(from_commit) or not _is_sha40(to_commit) or from_commit == to_commit:
        raise _source_invalid()

    commit_count = _nonnegative_int(snapshot["commit_count"])
    changed_file_count = _nonnegative_int(snapshot["changed_file_count"])
    added_lines = _nonnegative_int(snapshot["added_lines"])
    deleted_lines = _nonnegative_int(snapshot["deleted_lines"])
    diff_bytes = _nonnegative_int(snapshot["diff_bytes"])
    commits = _parse_commits(snapshot["commits_json"], expected_count=commit_count)

    files, stored_fact_hashes = _reconstruct_files(
        file_rows,
        expected_count=changed_file_count,
        expected_added=added_lines,
        expected_deleted=deleted_lines,
    )

    return (
        RangeCandidate(
            baseline_commit=from_commit,
            remote_head=to_commit,
            commits=commits,
            commit_count=commit_count,
            changed_file_count=changed_file_count,
            added_lines=added_lines,
            deleted_lines=deleted_lines,
            diff_bytes=diff_bytes,
            files=files,
            continuity="continuous",
            capacity="within",
        ),
        stored_fact_hashes,
    )


def _validate_file_manifest(
    snapshot: sqlite3.Row,
    candidate: RangeCandidate,
    stored_fact_hashes: list[str],
) -> None:
    frozen_manifest_hash = snapshot["file_manifest_hash"]
    if not _is_sha256(frozen_manifest_hash):
        raise _source_invalid()

    try:
        expected_manifest_hash, expected_records = _file_manifest_records(candidate)
    except HTTPException:
        raise _source_invalid() from None

    if expected_manifest_hash != frozen_manifest_hash:
        raise _source_invalid()
    if len(expected_records) != len(stored_fact_hashes):
        raise _source_invalid()
    for record, stored_hash in zip(expected_records, stored_fact_hashes, strict=True):
        if record["file_facts_hash"] != stored_hash:
            raise _source_invalid()


def _load_frozen_candidate(*, project_id: int, git_snapshot_id: int) -> RangeCandidate:
    if (
        type(project_id) is not int
        or project_id <= 0
        or project_id > _SQLITE_INTEGER_MAX
    ):
        raise _project_not_found()
    if (
        type(git_snapshot_id) is not int
        or git_snapshot_id <= 0
        or git_snapshot_id > _SQLITE_INTEGER_MAX
    ):
        raise _snapshot_not_found()

    try:
        with get_connection() as conn:
            # One read transaction keeps project/snapshot/file facts on the same SQLite snapshot.
            conn.execute("BEGIN")
            project = conn.execute(
                "SELECT id FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise _project_not_found()

            snapshot = conn.execute(
                """
                SELECT project_id, from_commit, to_commit, commits_json, commit_count,
                       changed_file_count, added_lines, deleted_lines, diff_bytes,
                       file_manifest_hash
                FROM git_snapshots
                WHERE id = ?
                """,
                (git_snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise _snapshot_not_found()
            if type(snapshot["project_id"]) is not int:
                raise _source_invalid()
            if snapshot["project_id"] != project_id:
                # Do not leak another project's snapshot existence through this service seam.
                raise _snapshot_not_found()

            file_rows = conn.execute(
                """
                SELECT ordinal, path, added_lines, deleted_lines, is_binary, file_facts_hash
                FROM git_file_evidence
                WHERE git_snapshot_id = ?
                ORDER BY ordinal
                """,
                (git_snapshot_id,),
            ).fetchall()

            candidate, stored_fact_hashes = _reconstruct_candidate(snapshot, file_rows)
            _validate_file_manifest(snapshot, candidate, stored_fact_hashes)
            conn.commit()
    except DevelopmentChangeEvidenceSourceError:
        raise
    except sqlite3.Error:
        raise _source_invalid() from None

    return candidate


def build_development_change_evidence_from_snapshot(
    *,
    project_id: int,
    git_snapshot_id: int,
) -> dict[str, object]:
    """Build exact Daily Change Evidence V1 from one explicit frozen GitSnapshot."""

    candidate = _load_frozen_candidate(
        project_id=project_id,
        git_snapshot_id=git_snapshot_id,
    )
    try:
        return build_development_change_evidence(candidate=candidate)
    except DevelopmentChangeEvidenceError:
        raise _source_invalid() from None
