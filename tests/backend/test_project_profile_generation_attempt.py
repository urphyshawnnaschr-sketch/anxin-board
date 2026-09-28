from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import project_profile_generation_attempt as attempt  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "profile-attempt.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    return db_path


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def test_success_replay_returns_same_response_without_second_operation_and_hides_raw_key(
    isolated_db: Path,
) -> None:
    calls = []
    key = "idem-profile-success-001"

    def operation():
        calls.append("send")
        return {
            "profile": {"id": 11, "status": "candidate"},
            "generation": {"provider": "synthetic"},
        }

    first = attempt.execute_profile_generation_once(
        project_id=7, idempotency_key=key, operation=operation
    )
    second = attempt.execute_profile_generation_once(
        project_id=7, idempotency_key=key, operation=operation
    )

    assert first == second
    assert calls == ["send"]
    with sqlite3.connect(isolated_db) as conn:
        row = conn.execute(
            "SELECT idempotency_key_hash, status, response_json FROM profile_generation_attempts"
        ).fetchone()
    assert row is not None
    assert row[1] == "succeeded"
    assert len(row[0]) == 64
    assert key not in row[0]
    assert key not in row[2]
    assert json.loads(row[2]) == first


def test_ambiguous_provider_outcome_becomes_durable_unknown_and_replay_does_not_send(
    isolated_db: Path,
) -> None:
    calls = []

    def operation():
        calls.append("send")
        raise _http_error(
            502,
            "PROFILE_GENERATION_PROVIDER_FAILED",
            "provider outcome cannot be classified",
        )

    for _ in range(2):
        with pytest.raises(HTTPException) as caught:
            attempt.execute_profile_generation_once(
                project_id=8,
                idempotency_key="idem-profile-unknown-001",
                operation=operation,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "PROFILE_GENERATION_RESULT_UNKNOWN"

    assert calls == ["send"]
    with sqlite3.connect(isolated_db) as conn:
        row = conn.execute(
            "SELECT status, error_code FROM profile_generation_attempts"
        ).fetchone()
    assert row == ("unknown", "PROFILE_GENERATION_RESULT_UNKNOWN")


def test_safe_pre_send_failure_replays_same_error_without_second_operation(
    isolated_db: Path,
) -> None:
    calls = []

    def operation():
        calls.append("preflight")
        raise _http_error(
            409,
            "PROFILE_GENERATION_CREDENTIAL_REQUIRED",
            "credential missing",
        )

    for _ in range(2):
        with pytest.raises(HTTPException) as caught:
            attempt.execute_profile_generation_once(
                project_id=9,
                idempotency_key="idem-profile-pre-send-001",
                operation=operation,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == {
            "code": "PROFILE_GENERATION_CREDENTIAL_REQUIRED",
            "message": "credential missing",
        }

    assert calls == ["preflight"]
    with sqlite3.connect(isolated_db) as conn:
        row = conn.execute(
            "SELECT status, error_code FROM profile_generation_attempts"
        ).fetchone()
    assert row == ("failed_pre_send", "PROFILE_GENERATION_CREDENTIAL_REQUIRED")


def test_abrupt_process_interruption_leaves_claimed_and_replay_fails_unknown_without_resend(
    isolated_db: Path,
) -> None:
    calls = []

    def operation():
        calls.append("send")
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        attempt.execute_profile_generation_once(
            project_id=10,
            idempotency_key="idem-profile-interrupt-001",
            operation=operation,
        )

    with sqlite3.connect(isolated_db) as conn:
        row = conn.execute("SELECT status FROM profile_generation_attempts").fetchone()
    assert row == ("claimed",)

    with pytest.raises(HTTPException) as caught:
        attempt.execute_profile_generation_once(
            project_id=10,
            idempotency_key="idem-profile-interrupt-001",
            operation=operation,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROFILE_GENERATION_RESULT_UNKNOWN"
    assert calls == ["send"]


def test_provider_network_unknown_preserves_safe_reason_and_replay_does_not_resend(
    isolated_db: Path,
) -> None:
    calls = []
    message = "DeepSeek 请求的网络结果无法确认；系统不会自动重发以避免重复扣费。"

    def operation():
        calls.append("send")
        raise _http_error(502, "PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN", message)

    for _ in range(2):
        with pytest.raises(HTTPException) as caught:
            attempt.execute_profile_generation_once(
                project_id=18,
                idempotency_key="idem-profile-network-unknown-001",
                operation=operation,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == {
            "code": "PROFILE_GENERATION_RESULT_UNKNOWN",
            "message": message,
        }

    assert calls == ["send"]


def test_one_idempotency_key_cannot_cross_project_boundary(isolated_db: Path) -> None:
    calls = []
    key = "idem-profile-project-binding-001"

    def operation():
        calls.append("send")
        return {"profile": {"id": 1, "status": "candidate"}}

    attempt.execute_profile_generation_once(
        project_id=21, idempotency_key=key, operation=operation
    )

    with pytest.raises(HTTPException) as caught:
        attempt.execute_profile_generation_once(
            project_id=22, idempotency_key=key, operation=operation
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROFILE_GENERATION_IDEMPOTENCY_CONFLICT"
    assert calls == ["send"]
