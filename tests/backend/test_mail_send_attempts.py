from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.db import get_connection, init_db  # noqa: E402
from app.mail_gateway import MailSendResult, RecipientOutcome, RecipientResult  # noqa: E402
from app.mail_send_attempts import (  # noqa: E402
    MailSendAttemptError,
    SendAttemptBinding,
    claim_prepared_attempt,
    close_pre_send_failure,
    create_send_attempt,
    get_send_attempt,
    list_send_attempt_history,
    record_send_result,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(
    *,
    attempt_id: str = "send-attempt-1",
    html: str = "<html><body>daily</body></html>",
    recipients: tuple[str, ...] = ("a@example.test", "b@example.test"),
    predecessor: str | None = None,
    **overrides,
) -> SendAttemptBinding:
    values = {
        "send_attempt_id": attempt_id,
        "project_id": 7,
        "approval_snapshot_id": 11,
        "approval_snapshot_hash": _h("approval-11"),
        "report_version_id": 19,
        "report_content_hash": _h("report-19"),
        "render_identity": "render-19-v1",
        "render_hash": _h("render-19-v1"),
        "html_sha256": _h(html),
        "message_id": "<report-19@example.test>",
        "to_recipients": recipients,
        "subject": "Daily report",
        "from_identity": "reports@example.test",
        "predecessor_send_attempt_id": predecessor,
    }
    values.update(overrides)
    return SendAttemptBinding(**values)


@pytest.fixture()
def mail_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    init_db()
    return path


def _result(message_id: str, recipients: tuple[str, ...], outcomes: tuple[RecipientOutcome, ...]):
    rows = []
    for recipient, outcome in zip(recipients, outcomes, strict=True):
        code = {
            RecipientOutcome.ACCEPTED: "MAIL_GATEWAY_ACCEPTED",
            RecipientOutcome.REJECTED: "MAIL_RECIPIENT_REJECTED",
            RecipientOutcome.UNKNOWN: "MAIL_RECIPIENT_UNKNOWN",
        }[outcome]
        rows.append(RecipientResult(recipient, outcome, code, f"{outcome.value} result"))
    return MailSendResult(message_id, recipients, tuple(rows))


def test_s01_init_is_idempotent_and_attempt_identity_is_frozen(mail_db: Path):
    attempt = create_send_attempt(_binding())
    reopened = get_send_attempt(attempt.send_attempt_id)

    assert reopened == attempt
    assert reopened.state == "prepared"
    assert reopened.recipient_results == ()
    assert reopened.to_recipients == ("a@example.test", "b@example.test")
    assert len(reopened.identity_hash) == 64
    with pytest.raises(FrozenInstanceError):
        reopened.message_id = "<changed@example.test>"  # type: ignore[misc]

    with get_connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"mail_send_attempts", "mail_send_recipient_results"} <= tables
        triggers = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_mail_send_%'"
            ).fetchall()
        }
        assert {
            "trg_mail_send_predecessor_lineage_required",
            "trg_mail_send_same_approval_identity",
            "trg_mail_send_same_report_message_id",
            "trg_mail_send_attempt_identity_immutable",
            "trg_mail_send_attempt_state_transition",
            "trg_mail_send_attempt_no_delete",
            "trg_mail_send_recipient_result_no_update",
            "trg_mail_send_recipient_result_no_delete",
        } <= triggers


def test_s02_recipient_snapshot_is_ordered_deduped_and_hash_closed(mail_db: Path):
    attempt = create_send_attempt(
        _binding(recipients=("b@example.test", "a@example.test", "b@example.test"))
    )
    assert attempt.to_recipients == ("b@example.test", "a@example.test")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT recipients_json, recipients_hash FROM mail_send_attempts WHERE send_attempt_id = ?",
            (attempt.send_attempt_id,),
        ).fetchone()
    expected_json = '["b@example.test","a@example.test"]'
    assert row["recipients_json"] == expected_json
    assert row["recipients_hash"] == _h(expected_json)
    assert attempt.recipients_hash == row["recipients_hash"]


def test_s03_stored_identity_corruption_fails_closed(mail_db: Path):
    attempt = create_send_attempt(_binding())
    with get_connection() as conn:
        conn.execute("DROP TRIGGER trg_mail_send_attempt_identity_immutable")
        conn.execute(
            "UPDATE mail_send_attempts SET recipients_hash = ? WHERE send_attempt_id = ?",
            (_h("tampered"), attempt.send_attempt_id),
        )

    with pytest.raises(MailSendAttemptError) as caught:
        get_send_attempt(attempt.send_attempt_id)
    assert caught.value.code == "MAIL_SEND_STORED_ATTEMPT_INVALID"


def test_s04_result_ledger_is_append_only_and_recloses_recipient_hash(mail_db: Path):
    attempt = create_send_attempt(_binding())
    claim_prepared_attempt(attempt.send_attempt_id)
    closed = record_send_result(
        attempt.send_attempt_id,
        _result(
            attempt.message_id,
            attempt.to_recipients,
            (RecipientOutcome.ACCEPTED, RecipientOutcome.REJECTED),
        ),
    )
    assert closed.state == "partial"
    assert [item.ordinal for item in closed.recipient_results] == [1, 2]
    assert [item.recipient for item in closed.recipient_results] == list(attempt.to_recipients)
    assert all(item.recipient_hash == _h(item.recipient) for item in closed.recipient_results)

    with get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE mail_send_recipient_results SET summary = 'overwrite' WHERE send_attempt_id = ?",
                (attempt.send_attempt_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM mail_send_recipient_results WHERE send_attempt_id = ?",
                (attempt.send_attempt_id,),
            )

    with pytest.raises(MailSendAttemptError) as caught:
        record_send_result(
            attempt.send_attempt_id,
            _result(
                attempt.message_id,
                attempt.to_recipients,
                (RecipientOutcome.ACCEPTED, RecipientOutcome.ACCEPTED),
            ),
        )
    assert caught.value.code == "MAIL_SEND_ATTEMPT_STATE_CONFLICT"


def test_s05_result_outcomes_derive_only_frozen_terminal_states(mail_db: Path):
    cases = (
        ((RecipientOutcome.ACCEPTED, RecipientOutcome.ACCEPTED), "sent"),
        ((RecipientOutcome.ACCEPTED, RecipientOutcome.REJECTED), "partial"),
        ((RecipientOutcome.REJECTED, RecipientOutcome.REJECTED), "failed"),
        ((RecipientOutcome.ACCEPTED, RecipientOutcome.UNKNOWN), "unknown"),
    )
    for index, (outcomes, expected) in enumerate(cases, start=1):
        approval_id = 100 + index
        report_id = 200 + index
        attempt = create_send_attempt(
            _binding(
                attempt_id=f"send-attempt-{index}",
                approval_snapshot_id=approval_id,
                approval_snapshot_hash=_h(f"approval-{approval_id}"),
                report_version_id=report_id,
                report_content_hash=_h(f"report-{report_id}"),
                render_identity=f"render-{report_id}-v1",
                render_hash=_h(f"render-{report_id}-v1"),
                message_id=f"<report-{report_id}@example.test>",
            )
        )
        claim_prepared_attempt(attempt.send_attempt_id)
        closed = record_send_result(
            attempt.send_attempt_id,
            _result(attempt.message_id, attempt.to_recipients, outcomes),
        )
        assert closed.state == expected
        assert closed.terminal_code is None


def test_s06_definite_call_failure_closes_failed_without_fabricated_recipient_rows(mail_db: Path):
    attempt = create_send_attempt(_binding())
    claim_prepared_attempt(attempt.send_attempt_id)
    failed = close_pre_send_failure(
        attempt.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED"
    )

    assert failed.state == "failed"
    assert failed.recipient_results == ()
    assert failed.terminal_code == "MAIL_GATEWAY_CALL_FAILED"
    assert failed.terminal_summary is not None


def test_s07_history_is_append_only_and_creation_ordered(mail_db: Path):
    first = create_send_attempt(_binding(attempt_id="send-attempt-1"))
    claim_prepared_attempt(first.send_attempt_id)
    close_pre_send_failure(first.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")
    second = create_send_attempt(
        _binding(attempt_id="send-attempt-2", predecessor=first.send_attempt_id)
    )

    history = list_send_attempt_history(first.project_id)
    assert [item.send_attempt_id for item in history] == [
        first.send_attempt_id,
        second.send_attempt_id,
    ]
    with get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM mail_send_attempts WHERE send_attempt_id = ?",
                (first.send_attempt_id,),
            )


def test_s08_same_approval_resend_preserves_report_render_html_and_to(mail_db: Path):
    first = create_send_attempt(_binding(attempt_id="send-attempt-1"))
    claim_prepared_attempt(first.send_attempt_id)
    close_pre_send_failure(first.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")

    successor = create_send_attempt(
        _binding(attempt_id="send-attempt-2", predecessor=first.send_attempt_id)
    )
    assert successor.recipients_hash == first.recipients_hash
    assert successor.report_content_hash == first.report_content_hash
    assert successor.render_hash == first.render_hash
    assert successor.html_sha256 == first.html_sha256
    assert successor.message_id == first.message_id

    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(
                attempt_id="send-attempt-subset",
                predecessor=first.send_attempt_id,
                recipients=("b@example.test",),
            )
        )
    assert caught.value.code == "MAIL_SEND_SAME_APPROVAL_IDENTITY_CHANGED"


def test_s09_same_report_resend_cannot_rotate_message_id(mail_db: Path):
    first = create_send_attempt(_binding(attempt_id="send-attempt-1"))
    claim_prepared_attempt(first.send_attempt_id)
    close_pre_send_failure(first.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")

    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(
                attempt_id="send-attempt-2",
                predecessor=first.send_attempt_id,
                approval_snapshot_id=12,
                approval_snapshot_hash=_h("approval-12"),
                message_id="<rotated@example.test>",
            )
        )
    assert caught.value.code == "MAIL_SEND_SAME_REPORT_MESSAGE_ID_CHANGED"


def test_s10_predecessor_must_be_terminal_and_same_project(mail_db: Path):
    first = create_send_attempt(_binding(attempt_id="send-attempt-1"))
    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(attempt_id="send-attempt-2", predecessor=first.send_attempt_id)
        )
    assert caught.value.code == "MAIL_SEND_PREDECESSOR_NOT_TERMINAL"

    claim_prepared_attempt(first.send_attempt_id)
    close_pre_send_failure(first.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")
    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(
                attempt_id="send-attempt-3",
                predecessor=first.send_attempt_id,
                project_id=8,
            )
        )
    assert caught.value.code == "MAIL_SEND_PREDECESSOR_PROJECT_MISMATCH"


def test_s11_invalid_frozen_hash_or_identity_is_rejected_before_persistence(mail_db: Path):
    for overrides in (
        {"approval_snapshot_hash": "bad"},
        {"html_sha256": "BAD"},
        {"message_id": "not-a-message-id"},
        {"from_identity": "from@example.test\r\nBcc:hidden@example.test"},
    ):
        with pytest.raises(MailSendAttemptError):
            create_send_attempt(_binding(**overrides))
    assert list_send_attempt_history(7) == ()


def test_s12_read_path_does_not_install_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "uninitialized.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))

    with pytest.raises(sqlite3.OperationalError):
        get_send_attempt("send-attempt-1")
    with get_connection() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'mail_send_%'"
        ).fetchall()
    assert tables == []


def test_s13_same_approval_cannot_bypass_to_freeze_by_omitting_predecessor(mail_db: Path):
    create_send_attempt(_binding(attempt_id="send-attempt-1"))

    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(
                attempt_id="send-attempt-2",
                recipients=("b@example.test",),
                predecessor=None,
            )
        )
    assert caught.value.code == "MAIL_SEND_ATTEMPT_CONFLICT"


def test_s14_same_report_cannot_rotate_message_id_without_predecessor(mail_db: Path):
    create_send_attempt(_binding(attempt_id="send-attempt-1"))

    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(
                attempt_id="send-attempt-2",
                approval_snapshot_id=12,
                approval_snapshot_hash=_h("approval-12"),
                message_id="<rotated@example.test>",
                predecessor=None,
            )
        )
    assert caught.value.code == "MAIL_SEND_ATTEMPT_CONFLICT"


def test_s15_storage_rejects_illegal_state_rollback_to_reopen_send_window(mail_db: Path):
    attempt = create_send_attempt(_binding())
    claim_prepared_attempt(attempt.send_attempt_id)
    close_pre_send_failure(attempt.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")

    with get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE mail_send_attempts
                SET state = 'prepared', terminal_code = NULL, terminal_summary = NULL
                WHERE send_attempt_id = ?
                """,
                (attempt.send_attempt_id,),
            )
    assert get_send_attempt(attempt.send_attempt_id).state == "failed"


def test_s16_later_matching_attempt_requires_explicit_predecessor_lineage(mail_db: Path):
    first = create_send_attempt(_binding(attempt_id="send-attempt-1"))
    claim_prepared_attempt(first.send_attempt_id)
    close_pre_send_failure(first.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")

    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(_binding(attempt_id="send-attempt-2", predecessor=None))
    assert caught.value.code == "MAIL_SEND_ATTEMPT_CONFLICT"

    successor = create_send_attempt(
        _binding(attempt_id="send-attempt-2", predecessor=first.send_attempt_id)
    )
    assert successor.predecessor_send_attempt_id == first.send_attempt_id
    assert successor.state == "prepared"


def test_s17_unrelated_same_project_terminal_predecessor_is_rejected(mail_db: Path):
    first = create_send_attempt(_binding(attempt_id="send-attempt-1"))
    claim_prepared_attempt(first.send_attempt_id)
    close_pre_send_failure(first.send_attempt_id, code="MAIL_GATEWAY_CALL_FAILED")

    with pytest.raises(MailSendAttemptError) as caught:
        create_send_attempt(
            _binding(
                attempt_id="send-attempt-2",
                predecessor=first.send_attempt_id,
                approval_snapshot_id=12,
                approval_snapshot_hash=_h("approval-12"),
                report_version_id=20,
                report_content_hash=_h("report-20"),
                render_identity="render-20-v1",
                render_hash=_h("render-20-v1"),
                message_id="<report-20@example.test>",
            )
        )
    assert caught.value.code == "MAIL_SEND_PREDECESSOR_IDENTITY_MISMATCH"
    assert [item.send_attempt_id for item in list_send_attempt_history(7)] == [
        first.send_attempt_id
    ]
