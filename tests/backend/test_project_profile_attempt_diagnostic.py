from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.db import get_connection  # noqa: E402
from app.project_profile_attempt_diagnostic_api import (  # noqa: E402
    latest_profile_generation_attempt,
)


def test_latest_profile_generation_attempt_is_sanitized(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "attempt.sqlite"))
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE profile_generation_attempts (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_status INTEGER,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                idempotency_key_hash TEXT,
                attempt_identity_hash TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO profile_generation_attempts VALUES (
                1, 2, 'unknown', 409, 'PROFILE_GENERATION_RESULT_UNKNOWN',
                'safe diagnostic', '2026-09-11T00:00:00Z', '2026-09-11T00:01:00Z',
                'secret-hash', 'identity-hash'
            )
            """
        )
        conn.commit()

    result = latest_profile_generation_attempt(2)

    assert result == {
        "attempt": {
            "status": "unknown",
            "error_status": 409,
            "error_code": "PROFILE_GENERATION_RESULT_UNKNOWN",
            "error_message": "safe diagnostic",
            "created_at": "2026-09-11T00:00:00Z",
            "finished_at": "2026-09-11T00:01:00Z",
        }
    }
    rendered = repr(result)
    assert "secret-hash" not in rendered
    assert "identity-hash" not in rendered


def test_latest_profile_generation_attempt_handles_missing_table(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "empty.sqlite"))
    with get_connection():
        pass
    assert latest_profile_generation_attempt(2) == {"attempt": None}
