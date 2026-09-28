"""Immutable To-only recipient configuration authority for FR-35 R1.

This module owns mailbox legality, Human-visible canonical order, immutable per-project
versioning and stored-row integrity. It deliberately owns no HTTP route, SMTP/network,
credential access, ApprovalSnapshot mutation, SendAttempt execution or checkpoint state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from app.db import get_connection


SCHEMA_VERSION = "recipient_config_v1"
MAX_RECIPIENTS = 100
MAX_ADDRESS_CHARS = 320
MAX_LOCAL_PART_CHARS = 64
MAX_DOMAIN_CHARS = 253
MAX_CREATED_BY_CHARS = 200

_LOCAL_ATOM_RE = re.compile(r"^[A-Za-z0-9!#$%&'+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'+/=?^_`{|}~-]+)*$")
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class RecipientConfigError(RuntimeError):
    """Stable, non-secret recipient authority failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _input_invalid() -> RecipientConfigError:
    return RecipientConfigError("RECIPIENT_CONFIG_INPUT_INVALID")


def _project_not_found() -> RecipientConfigError:
    return RecipientConfigError("RECIPIENT_CONFIG_PROJECT_NOT_FOUND")


def _version_conflict() -> RecipientConfigError:
    return RecipientConfigError("RECIPIENT_CONFIG_VERSION_CONFLICT")


def _stored_invalid() -> RecipientConfigError:
    return RecipientConfigError("RECIPIENT_CONFIG_STORED_INVALID")


def _validate_created_by(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_CREATED_BY_CHARS:
        raise _input_invalid()
    if value != value.strip():
        raise _input_invalid()
    for char in value:
        codepoint = ord(char)
        if (
            codepoint < 32
            or 127 <= codepoint <= 159
            or codepoint in (0x2028, 0x2029)
        ):
            raise _input_invalid()
    return value


def _canonical_mailbox(value: object) -> str:
    """Validate one conservative ASCII dot-atom mailbox and canonicalize domain case only.

    Local-part case is preserved because SMTP local-part case semantics are not ours to
    rewrite. Display names, comments, groups, quoted local-parts, domain literals,
    internationalized addresses and wildcard-like forms are intentionally outside R1.
    """
    if type(value) is not str or not value or len(value) > MAX_ADDRESS_CHARS:
        raise _input_invalid()
    if value != value.strip() or not value.isascii() or any(char.isspace() for char in value):
        raise _input_invalid()
    if value.count("@") != 1:
        raise _input_invalid()
    local, domain = value.rsplit("@", 1)
    if not local or not domain or len(local) > MAX_LOCAL_PART_CHARS or len(domain) > MAX_DOMAIN_CHARS:
        raise _input_invalid()
    if _LOCAL_ATOM_RE.fullmatch(local) is None:
        raise _input_invalid()
    labels = domain.split(".")
    if len(labels) < 2 or any(_DOMAIN_LABEL_RE.fullmatch(label) is None for label in labels):
        raise _input_invalid()
    canonical = f"{local}@{domain.lower()}"
    if len(canonical) > MAX_ADDRESS_CHARS:
        raise _input_invalid()
    return canonical


def canonicalize_to_recipients(value: object) -> tuple[str, ...]:
    """Canonicalize To recipients preserving Human order and first occurrence.

    Exact duplicates after domain-case normalization are collapsed deterministically.
    Local-part case is identity-significant and is therefore not silently folded.
    """
    if not isinstance(value, (list, tuple)) or not value or len(value) > MAX_RECIPIENTS:
        raise _input_invalid()
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in value:
        mailbox = _canonical_mailbox(raw)
        if mailbox not in seen:
            seen.add(mailbox)
            ordered.append(mailbox)
    if not ordered:
        raise _input_invalid()
    return tuple(ordered)


def _canonical_recipients_json(recipients: tuple[str, ...]) -> str:
    return json.dumps(list(recipients), ensure_ascii=True, separators=(",", ":"))


def _recipients_hash(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _row_hash_payload(
    *,
    project_id: int,
    version_no: int,
    to_recipients_json: str,
    recipients_hash: str,
    created_by: str,
    created_at: str,
    predecessor_version_id: int | None,
) -> str:
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "version_no": version_no,
            "to_recipients_json": to_recipients_json,
            "recipients_hash": recipients_hash,
            "created_by": created_by,
            "created_at": created_at,
            "predecessor_version_id": predecessor_version_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_created_at(value: object) -> str:
    if type(value) is not str or not value:
        raise _stored_invalid()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _stored_invalid() from exc
    if parsed.tzinfo is None:
        raise _stored_invalid()
    return value


def _validate_stored_row(row: sqlite3.Row) -> dict[str, object]:
    data = dict(row)
    try:
        row_id = int(data["id"])
        project_id = int(data["project_id"])
        version_no = int(data["version_no"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _stored_invalid() from exc
    if row_id <= 0 or project_id <= 0 or version_no <= 0 or data.get("schema_version") != SCHEMA_VERSION:
        raise _stored_invalid()

    canonical_json = data.get("to_recipients_json")
    stored_recipients_hash = data.get("recipients_hash")
    created_by = data.get("created_by")
    created_at = _parse_created_at(data.get("created_at"))
    predecessor = data.get("predecessor_version_id")
    if predecessor is not None:
        if type(predecessor) is not int or predecessor <= 0:
            raise _stored_invalid()
    if type(canonical_json) is not str or type(stored_recipients_hash) is not str:
        raise _stored_invalid()
    if type(created_by) is not str:
        raise _stored_invalid()
    try:
        raw_recipients = json.loads(canonical_json)
        canonical_recipients = canonicalize_to_recipients(raw_recipients)
    except (ValueError, TypeError, RecipientConfigError) as exc:
        raise _stored_invalid() from exc
    rebuilt_json = _canonical_recipients_json(canonical_recipients)
    rebuilt_recipients_hash = _recipients_hash(rebuilt_json)
    if rebuilt_json != canonical_json or rebuilt_recipients_hash != stored_recipients_hash:
        raise _stored_invalid()
    try:
        _validate_created_by(created_by)
    except RecipientConfigError as exc:
        raise _stored_invalid() from exc
    expected_row_hash = _row_hash_payload(
        project_id=project_id,
        version_no=version_no,
        to_recipients_json=canonical_json,
        recipients_hash=stored_recipients_hash,
        created_by=created_by,
        created_at=created_at,
        predecessor_version_id=predecessor,
    )
    if data.get("row_hash") != expected_row_hash:
        raise _stored_invalid()

    return {
        "id": row_id,
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "version_no": version_no,
        "to_recipients": list(canonical_recipients),
        "to_recipients_json": canonical_json,
        "recipients_hash": stored_recipients_hash,
        "created_by": created_by,
        "created_at": created_at,
        "predecessor_version_id": predecessor,
        "row_hash": expected_row_hash,
    }


def _read_history_in_tx(conn: sqlite3.Connection, project_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, schema_version, project_id, version_no, to_recipients_json,
               recipients_hash, created_by, created_at, predecessor_version_id, row_hash
        FROM recipient_config_versions
        WHERE project_id = ?
        ORDER BY version_no ASC
        """,
        (project_id,),
    ).fetchall()
    history = [_validate_stored_row(row) for row in rows]
    previous: dict[str, object] | None = None
    for item in history:
        expected_version = 1 if previous is None else int(previous["version_no"]) + 1
        expected_predecessor = None if previous is None else int(previous["id"])
        if item["version_no"] != expected_version or item["predecessor_version_id"] != expected_predecessor:
            raise _stored_invalid()
        previous = item
    return history


def get_recipient_config_history(project_id: int) -> list[dict[str, object]]:
    if type(project_id) is not int or project_id <= 0:
        raise _input_invalid()
    try:
        with get_connection() as conn:
            if conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise _project_not_found()
            return _read_history_in_tx(conn, project_id)
    except RecipientConfigError:
        raise
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc


def get_current_recipient_config(project_id: int) -> dict[str, object] | None:
    history = get_recipient_config_history(project_id)
    return history[-1] if history else None


def save_recipient_config(
    *,
    project_id: int,
    to_recipients: list[str] | tuple[str, ...],
    created_by: str,
    expected_version_no: int,
) -> dict[str, object]:
    """Append a new immutable version or replay the exact current canonical no-op."""
    if type(project_id) is not int or project_id <= 0:
        raise _input_invalid()
    if type(expected_version_no) is not int or expected_version_no < 0:
        raise _input_invalid()
    creator = _validate_created_by(created_by)
    if not isinstance(to_recipients, (list, tuple)):
        raise _input_invalid()
    recipients = canonicalize_to_recipients(to_recipients)
    recipients_json = _canonical_recipients_json(recipients)
    recipients_hash = _recipients_hash(recipients_json)

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise _project_not_found()
            history = _read_history_in_tx(conn, project_id)
            current = history[-1] if history else None
            current_version = int(current["version_no"]) if current else 0
            if expected_version_no != current_version:
                raise _version_conflict()
            if current is not None and current["recipients_hash"] == recipients_hash and current["to_recipients_json"] == recipients_json:
                conn.rollback()
                return current

            version_no = current_version + 1
            predecessor = int(current["id"]) if current else None
            created_at = datetime.now(timezone.utc).isoformat()
            row_hash = _row_hash_payload(
                project_id=project_id,
                version_no=version_no,
                to_recipients_json=recipients_json,
                recipients_hash=recipients_hash,
                created_by=creator,
                created_at=created_at,
                predecessor_version_id=predecessor,
            )
            cursor = conn.execute(
                """
                INSERT INTO recipient_config_versions (
                    schema_version, project_id, version_no, to_recipients_json,
                    recipients_hash, created_by, created_at, predecessor_version_id, row_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    project_id,
                    version_no,
                    recipients_json,
                    recipients_hash,
                    creator,
                    created_at,
                    predecessor,
                    row_hash,
                ),
            )
            row = conn.execute(
                """
                SELECT id, schema_version, project_id, version_no, to_recipients_json,
                       recipients_hash, created_by, created_at, predecessor_version_id, row_hash
                FROM recipient_config_versions WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            if row is None:
                raise _stored_invalid()
            result = _validate_stored_row(row)
            conn.commit()
            return result
    except RecipientConfigError:
        raise
    except sqlite3.IntegrityError as exc:
        raise _stored_invalid() from exc
    except sqlite3.Error as exc:
        raise _stored_invalid() from exc
