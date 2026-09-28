"""Immutable versioned non-secret SMTP transport configuration authority.

This module owns only the local, non-secret facts needed to construct the existing
stdlib SMTP adapter. Secret bytes remain exclusively behind SecretStore; persisted
rows contain only an opaque ``secret_ref``. The module performs no credential access,
SMTP/network I/O, report approval, recipient binding, SendAttempt execution or UI
routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from app.db import get_connection
from app.secret_store import InvalidSecretReferenceError
from app.smtp_mail_gateway import SmtpGatewayConfig, SmtpSecurity


SCHEMA_VERSION = "mail_transport_profile_v1"
_TABLE = "mail_transport_profile_versions"
_MAX_IDENTITY_CHARS = 200
_MAX_FROM_IDENTITY_CHARS = 320
_LOCAL_ATOM_RE = re.compile(r"^[A-Za-z0-9!#$%&'+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'+/=?^_`{|}~-]+)*$")
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

_COLUMNS = (
    "id, schema_version, version_no, host, port, security, username, from_identity, "
    "secret_ref, timeout_seconds, configured_by, created_at, predecessor_version_id, profile_hash"
)


class MailTransportProfileError(RuntimeError):
    """Stable non-secret transport-profile failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> MailTransportProfileError:
    return MailTransportProfileError(code)


def _identity_text(value: object, *, code: str, max_chars: int = _MAX_IDENTITY_CHARS) -> str:
    if type(value) is not str or not value or len(value) > max_chars or value != value.strip():
        raise _fail(code)
    for char in value:
        point = ord(char)
        if point < 32 or 127 <= point <= 159 or point in (0x2028, 0x2029):
            raise _fail(code)
    return value


def _canonical_sender(value: object) -> str:
    """Validate one conservative ASCII addr-spec used as SMTP envelope/header From."""
    if type(value) is not str or not value or len(value) > _MAX_FROM_IDENTITY_CHARS:
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    if value != value.strip() or not value.isascii() or any(char.isspace() for char in value):
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    if value.count("@") != 1:
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or len(local) > 64 or len(domain) > 253:
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    if _LOCAL_ATOM_RE.fullmatch(local) is None:
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    labels = domain.split(".")
    if len(labels) < 2 or any(_DOMAIN_LABEL_RE.fullmatch(label) is None for label in labels):
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    canonical = f"{local}@{domain.lower()}"
    if len(canonical) > _MAX_FROM_IDENTITY_CHARS:
        raise _fail("MAIL_TRANSPORT_FROM_IDENTITY_INVALID")
    return canonical


def _timestamp(value: object, *, code: str) -> str:
    text = _identity_text(value, code=code, max_chars=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _fail(code) from exc
    if parsed.tzinfo is None:
        raise _fail(code)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _profile_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MailTransportProfile:
    id: int
    schema_version: str
    version_no: int
    host: str
    port: int
    security: SmtpSecurity
    username: str
    from_identity: str
    secret_ref: str
    timeout_seconds: float
    configured_by: str
    created_at: str
    predecessor_version_id: int | None
    profile_hash: str

    def gateway_config(self) -> SmtpGatewayConfig:
        """Materialize non-secret adapter config only; never access SecretStore."""
        return SmtpGatewayConfig(
            host=self.host,
            port=self.port,
            security=self.security,
            username=self.username,
            secret_ref=self.secret_ref,
            timeout_seconds=self.timeout_seconds,
        )


def _normalized_input(
    *,
    host: object,
    port: object,
    security: object,
    username: object,
    from_identity: object,
    secret_ref: object,
    timeout_seconds: object,
    configured_by: object,
) -> dict[str, object]:
    try:
        if isinstance(security, str):
            security = SmtpSecurity(security)
        config = SmtpGatewayConfig(
            host=host,  # type: ignore[arg-type]
            port=port,  # type: ignore[arg-type]
            security=security,  # type: ignore[arg-type]
            username=username,  # type: ignore[arg-type]
            secret_ref=secret_ref,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, InvalidSecretReferenceError) as exc:
        raise _fail("MAIL_TRANSPORT_PROFILE_INPUT_INVALID") from exc
    return {
        "host": config.host,
        "port": config.port,
        "security": config.security.value,
        "username": config.username,
        "from_identity": _canonical_sender(from_identity),
        "secret_ref": config.secret_ref,
        "timeout_seconds": config.timeout_seconds,
        "configured_by": _identity_text(configured_by, code="MAIL_TRANSPORT_PROFILE_INPUT_INVALID"),
    }


def _ensure_schema() -> None:
    try:
        with get_connection() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version TEXT NOT NULL CHECK (schema_version = '{SCHEMA_VERSION}'),
                    version_no INTEGER NOT NULL UNIQUE CHECK (version_no > 0),
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
                    security TEXT NOT NULL CHECK (security IN ('implicit_tls','starttls')),
                    username TEXT NOT NULL,
                    from_identity TEXT NOT NULL,
                    secret_ref TEXT NOT NULL,
                    timeout_seconds REAL NOT NULL CHECK (timeout_seconds BETWEEN 1 AND 60),
                    configured_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    predecessor_version_id INTEGER NULL,
                    profile_hash TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_update
                BEFORE UPDATE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'mail transport profile history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_delete
                BEFORE DELETE ON {_TABLE}
                BEGIN SELECT RAISE(ABORT, 'mail transport profile history is append-only'); END;
                """
            )
            columns = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()
            }
            required = {
                "id", "schema_version", "version_no", "host", "port", "security",
                "username", "from_identity", "secret_ref", "timeout_seconds",
                "configured_by", "created_at", "predecessor_version_id", "profile_hash",
            }
            if columns != required:
                raise sqlite3.IntegrityError("mail transport profile schema is obsolete")
            conn.commit()
    except sqlite3.Error as exc:
        raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID") from exc


def _row_payload(row: sqlite3.Row) -> tuple[dict[str, object], int, int | None]:
    stored_code = "MAIL_TRANSPORT_PROFILE_STORED_INVALID"
    if row["schema_version"] != SCHEMA_VERSION:
        raise _fail(stored_code)
    if type(row["id"]) is not int or row["id"] <= 0:
        raise _fail(stored_code)
    if type(row["version_no"]) is not int or row["version_no"] <= 0:
        raise _fail(stored_code)
    predecessor = row["predecessor_version_id"]
    if predecessor is not None and (type(predecessor) is not int or predecessor <= 0):
        raise _fail(stored_code)
    try:
        normalized = _normalized_input(
            host=row["host"],
            port=row["port"],
            security=row["security"],
            username=row["username"],
            from_identity=row["from_identity"],
            secret_ref=row["secret_ref"],
            timeout_seconds=row["timeout_seconds"],
            configured_by=row["configured_by"],
        )
    except MailTransportProfileError as exc:
        raise _fail(stored_code) from exc
    canonical_stored = (
        row["host"] == normalized["host"]
        and row["port"] == normalized["port"]
        and row["security"] == normalized["security"]
        and row["username"] == normalized["username"]
        and row["from_identity"] == normalized["from_identity"]
        and row["secret_ref"] == normalized["secret_ref"]
        and float(row["timeout_seconds"]) == normalized["timeout_seconds"]
        and row["configured_by"] == normalized["configured_by"]
    )
    if not canonical_stored:
        raise _fail(stored_code)
    created_at = _timestamp(row["created_at"], code=stored_code)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "version_no": row["version_no"],
        **normalized,
        "created_at": created_at,
        "predecessor_version_id": predecessor,
    }
    if row["profile_hash"] != _profile_hash(payload):
        raise _fail(stored_code)
    return payload, row["id"], predecessor


def _profile_from_row(row: sqlite3.Row) -> MailTransportProfile:
    payload, row_id, predecessor = _row_payload(row)
    try:
        security = SmtpSecurity(str(payload["security"]))
    except ValueError as exc:
        raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID") from exc
    return MailTransportProfile(
        id=row_id,
        schema_version=SCHEMA_VERSION,
        version_no=int(payload["version_no"]),
        host=str(payload["host"]),
        port=int(payload["port"]),
        security=security,
        username=str(payload["username"]),
        from_identity=str(payload["from_identity"]),
        secret_ref=str(payload["secret_ref"]),
        timeout_seconds=float(payload["timeout_seconds"]),
        configured_by=str(payload["configured_by"]),
        created_at=str(payload["created_at"]),
        predecessor_version_id=predecessor,
        profile_hash=_profile_hash(payload),
    )


def _read_history_in_tx(conn: sqlite3.Connection) -> list[MailTransportProfile]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM {_TABLE} ORDER BY version_no ASC").fetchall()
    history = [_profile_from_row(row) for row in rows]
    previous: MailTransportProfile | None = None
    for profile in history:
        expected_version = 1 if previous is None else previous.version_no + 1
        expected_predecessor = None if previous is None else previous.id
        if profile.version_no != expected_version or profile.predecessor_version_id != expected_predecessor:
            raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID")
        previous = profile
    return history


def get_mail_transport_profile_history() -> list[MailTransportProfile]:
    _ensure_schema()
    try:
        with get_connection() as conn:
            return _read_history_in_tx(conn)
    except MailTransportProfileError:
        raise
    except sqlite3.Error as exc:
        raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID") from exc


def get_current_mail_transport_profile() -> MailTransportProfile | None:
    history = get_mail_transport_profile_history()
    return history[-1] if history else None


def save_mail_transport_profile(
    *,
    host: str,
    port: int,
    security: SmtpSecurity | str,
    username: str,
    from_identity: str,
    secret_ref: str,
    timeout_seconds: float = 30.0,
    configured_by: str,
    expected_version_no: int,
) -> MailTransportProfile:
    """Append one immutable non-secret profile version or replay a canonical no-op."""
    if type(expected_version_no) is not int or expected_version_no < 0:
        raise _fail("MAIL_TRANSPORT_PROFILE_INPUT_INVALID")
    normalized = _normalized_input(
        host=host,
        port=port,
        security=security,
        username=username,
        from_identity=from_identity,
        secret_ref=secret_ref,
        timeout_seconds=timeout_seconds,
        configured_by=configured_by,
    )
    _ensure_schema()
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            history = _read_history_in_tx(conn)
            current = history[-1] if history else None
            current_version = current.version_no if current else 0
            if expected_version_no != current_version:
                raise _fail("MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT")

            if current is not None:
                same_config = (
                    current.host == normalized["host"]
                    and current.port == normalized["port"]
                    and current.security.value == normalized["security"]
                    and current.username == normalized["username"]
                    and current.from_identity == normalized["from_identity"]
                    and current.secret_ref == normalized["secret_ref"]
                    and current.timeout_seconds == normalized["timeout_seconds"]
                )
                if same_config:
                    conn.rollback()
                    return current

            version_no = current_version + 1
            predecessor = current.id if current else None
            created_at = _now()
            payload = {
                "schema_version": SCHEMA_VERSION,
                "version_no": version_no,
                **normalized,
                "created_at": created_at,
                "predecessor_version_id": predecessor,
            }
            profile_hash = _profile_hash(payload)
            cursor = conn.execute(
                f"""
                INSERT INTO {_TABLE} (
                    schema_version, version_no, host, port, security, username,
                    from_identity, secret_ref, timeout_seconds, configured_by, created_at,
                    predecessor_version_id, profile_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION, version_no, normalized["host"], normalized["port"],
                    normalized["security"], normalized["username"], normalized["from_identity"],
                    normalized["secret_ref"], normalized["timeout_seconds"],
                    normalized["configured_by"], created_at, predecessor, profile_hash,
                ),
            )
            row = conn.execute(f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?", (cursor.lastrowid,)).fetchone()
            if row is None:
                raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID")
            profile = _profile_from_row(row)
            conn.commit()
            return profile
    except MailTransportProfileError:
        raise
    except sqlite3.IntegrityError as exc:
        raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID") from exc
    except sqlite3.Error as exc:
        raise _fail("MAIL_TRANSPORT_PROFILE_STORED_INVALID") from exc
