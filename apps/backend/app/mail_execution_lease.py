"""Durable cross-process execution lease for one MailWorkflow transport invocation.

This module is coordination-only. It does not own SendAttempt lifecycle, recipient
policy, approval authority, credentials, retry policy, or network I/O. A lease makes
an active ``sending`` owner visible across local processes so another process cannot
misclassify a genuinely live transport call as an orphan.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time

from app.db import get_connection


DEFAULT_LEASE_TTL_MS = 120_000
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
_OWNER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_MAX_ATTEMPT_ID_CHARS = 256


class MailExecutionLeaseError(RuntimeError):
    """Stable coordination failure that never carries recipients or credentials."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    send_attempt_id: str
    owner_token: str
    lease_expires_at_ms: int
    updated_at_ms: int


def _fail(code: str) -> MailExecutionLeaseError:
    return MailExecutionLeaseError(code)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _attempt_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_ATTEMPT_ID_CHARS:
        raise _fail("MAIL_SEND_ATTEMPT_ID_INVALID")
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail("MAIL_SEND_ATTEMPT_ID_INVALID")
    return value


def _owner_token(value: object) -> str:
    if type(value) is not str or _OWNER_TOKEN_RE.fullmatch(value) is None:
        raise _fail("MAIL_SEND_EXECUTION_OWNER_INVALID")
    return value


def _positive_ms(value: object, *, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise _fail(code)
    return value


def _ttl_ms(value: object) -> int:
    ttl = _positive_ms(value, code="MAIL_SEND_EXECUTION_LEASE_TTL_INVALID")
    if ttl > 24 * 60 * 60 * 1000:
        raise _fail("MAIL_SEND_EXECUTION_LEASE_TTL_INVALID")
    return ttl


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_execution_leases (
            send_attempt_id TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL,
            lease_expires_at_ms INTEGER NOT NULL CHECK (lease_expires_at_ms > 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms > 0)
        )
        """
    )
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(mail_execution_leases)").fetchall()
    }
    if columns != {
        "send_attempt_id",
        "owner_token",
        "lease_expires_at_ms",
        "updated_at_ms",
    }:
        raise _fail("MAIL_SEND_EXECUTION_LEASE_SCHEMA_INVALID")


def _from_row(row) -> ExecutionLease:
    attempt_id = _attempt_id(row["send_attempt_id"])
    owner = _owner_token(row["owner_token"])
    expires = _positive_ms(
        row["lease_expires_at_ms"], code="MAIL_SEND_EXECUTION_LEASE_STORED_INVALID"
    )
    updated = _positive_ms(row["updated_at_ms"], code="MAIL_SEND_EXECUTION_LEASE_STORED_INVALID")
    if expires < updated:
        raise _fail("MAIL_SEND_EXECUTION_LEASE_STORED_INVALID")
    return ExecutionLease(
        send_attempt_id=attempt_id,
        owner_token=owner,
        lease_expires_at_ms=expires,
        updated_at_ms=updated,
    )


def acquire_execution_lease(
    send_attempt_id: str,
    owner_token: str,
    *,
    ttl_ms: int = DEFAULT_LEASE_TTL_MS,
    now_ms: int | None = None,
) -> ExecutionLease:
    """Acquire or take over an expired pre-claim lease under one SQLite write lock."""
    attempt_id = _attempt_id(send_attempt_id)
    owner = _owner_token(owner_token)
    ttl = _ttl_ms(ttl_ms)
    now = _positive_ms(now_ms if now_ms is not None else _now_ms(), code="MAIL_SEND_EXECUTION_TIME_INVALID")
    expires = now + ttl

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT send_attempt_id, owner_token, lease_expires_at_ms, updated_at_ms "
            "FROM mail_execution_leases WHERE send_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mail_execution_leases "
                "(send_attempt_id, owner_token, lease_expires_at_ms, updated_at_ms) "
                "VALUES (?, ?, ?, ?)",
                (attempt_id, owner, expires, now),
            )
        else:
            current = _from_row(row)
            if current.lease_expires_at_ms > now and current.owner_token != owner:
                raise _fail("MAIL_SEND_ATTEMPT_IN_PROGRESS")
            conn.execute(
                "UPDATE mail_execution_leases "
                "SET owner_token = ?, lease_expires_at_ms = ?, updated_at_ms = ? "
                "WHERE send_attempt_id = ?",
                (owner, expires, now, attempt_id),
            )
        row = conn.execute(
            "SELECT send_attempt_id, owner_token, lease_expires_at_ms, updated_at_ms "
            "FROM mail_execution_leases WHERE send_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise _fail("MAIL_SEND_EXECUTION_LEASE_STORED_INVALID")
        return _from_row(row)


def renew_execution_lease(
    send_attempt_id: str,
    owner_token: str,
    *,
    ttl_ms: int = DEFAULT_LEASE_TTL_MS,
    now_ms: int | None = None,
) -> ExecutionLease:
    """Renew only a still-live lease owned by the exact execution token."""
    attempt_id = _attempt_id(send_attempt_id)
    owner = _owner_token(owner_token)
    ttl = _ttl_ms(ttl_ms)
    now = _positive_ms(now_ms if now_ms is not None else _now_ms(), code="MAIL_SEND_EXECUTION_TIME_INVALID")
    expires = now + ttl

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        cursor = conn.execute(
            "UPDATE mail_execution_leases "
            "SET lease_expires_at_ms = ?, updated_at_ms = ? "
            "WHERE send_attempt_id = ? AND owner_token = ? AND lease_expires_at_ms > ?",
            (expires, now, attempt_id, owner, now),
        )
        if cursor.rowcount != 1:
            raise _fail("MAIL_SEND_EXECUTION_LEASE_LOST")
        row = conn.execute(
            "SELECT send_attempt_id, owner_token, lease_expires_at_ms, updated_at_ms "
            "FROM mail_execution_leases WHERE send_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise _fail("MAIL_SEND_EXECUTION_LEASE_LOST")
        return _from_row(row)


def permit_orphan_recovery(
    send_attempt_id: str,
    *,
    now_ms: int | None = None,
) -> bool:
    """Atomically decide whether persisted ``sending`` has no live cross-process owner.

    Missing lease means legacy/unowned sending and is recoverable. An expired lease is
    deleted under the same write lock, permanently fencing its stale owner from later
    renewal. A live lease returns False and must be treated as in-progress.
    """
    attempt_id = _attempt_id(send_attempt_id)
    now = _positive_ms(now_ms if now_ms is not None else _now_ms(), code="MAIL_SEND_EXECUTION_TIME_INVALID")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT send_attempt_id, owner_token, lease_expires_at_ms, updated_at_ms "
            "FROM mail_execution_leases WHERE send_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return True
        current = _from_row(row)
        if current.lease_expires_at_ms > now:
            return False
        cursor = conn.execute(
            "DELETE FROM mail_execution_leases "
            "WHERE send_attempt_id = ? AND owner_token = ? AND lease_expires_at_ms <= ?",
            (attempt_id, current.owner_token, now),
        )
        if cursor.rowcount != 1:
            return False
        return True


def release_execution_lease(send_attempt_id: str, owner_token: str) -> None:
    """Best-effort owner-matched release; never deletes a successor owner's lease."""
    attempt_id = _attempt_id(send_attempt_id)
    owner = _owner_token(owner_token)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        conn.execute(
            "DELETE FROM mail_execution_leases WHERE send_attempt_id = ? AND owner_token = ?",
            (attempt_id, owner),
        )


def get_execution_lease(send_attempt_id: str) -> ExecutionLease | None:
    """Read the current coordination row for tests/diagnostics without exposing it via UI."""
    attempt_id = _attempt_id(send_attempt_id)
    with get_connection() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT send_attempt_id, owner_token, lease_expires_at_ms, updated_at_ms "
            "FROM mail_execution_leases WHERE send_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return None if row is None else _from_row(row)


class ExecutionLeaseHeartbeat:
    """Renew one durable lease while the synchronous transport call is in flight."""

    def __init__(
        self,
        send_attempt_id: str,
        owner_token: str,
        *,
        ttl_ms: int = DEFAULT_LEASE_TTL_MS,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._send_attempt_id = _attempt_id(send_attempt_id)
        self._owner_token = _owner_token(owner_token)
        self._ttl_ms = _ttl_ms(ttl_ms)
        if not isinstance(interval_seconds, (int, float)) or not 0 < float(interval_seconds) < 60:
            raise _fail("MAIL_SEND_EXECUTION_HEARTBEAT_INVALID")
        self._interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._failure_lock = threading.Lock()
        self._failure: MailExecutionLeaseError | None = None
        self._thread: threading.Thread | None = None

    def _set_failure(self, failure: MailExecutionLeaseError) -> None:
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                renew_execution_lease(
                    self._send_attempt_id,
                    self._owner_token,
                    ttl_ms=self._ttl_ms,
                )
            except MailExecutionLeaseError as exc:
                self._set_failure(exc)
                return
            except Exception:
                self._set_failure(_fail("MAIL_SEND_EXECUTION_LEASE_RENEW_FAILED"))
                return

    def __enter__(self) -> "ExecutionLeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name="mail-execution-lease-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def assert_owned(self) -> None:
        """Synchronously prove ownership immediately before terminal persistence."""
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise failure
        try:
            renew_execution_lease(
                self._send_attempt_id,
                self._owner_token,
                ttl_ms=self._ttl_ms,
            )
        except MailExecutionLeaseError as exc:
            self._set_failure(exc)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self._interval_seconds + 0.5))


def execution_lease_heartbeat(
    send_attempt_id: str,
    owner_token: str,
    *,
    ttl_ms: int = DEFAULT_LEASE_TTL_MS,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> ExecutionLeaseHeartbeat:
    return ExecutionLeaseHeartbeat(
        send_attempt_id,
        owner_token,
        ttl_ms=ttl_ms,
        interval_seconds=interval_seconds,
    )
