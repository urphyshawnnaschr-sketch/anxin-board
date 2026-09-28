from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.mail_workflow as mail_workflow_module  # noqa: E402
from app.db import init_db  # noqa: E402
from app.fake_mail_gateway import FakeMailGateway  # noqa: E402
from app.mail_execution_lease import (  # noqa: E402
    MailExecutionLeaseError,
    acquire_execution_lease,
    get_execution_lease,
    permit_orphan_recovery,
    release_execution_lease,
    renew_execution_lease,
)
from app.mail_send_attempts import (  # noqa: E402
    MailSendAttemptError,
    SendAttemptBinding,
    claim_prepared_attempt,
    close_post_call_unknown,
    create_send_attempt,
    get_send_attempt,
)
from app.mail_workflow import MailWorkflowError, execute_send_attempt_once  # noqa: E402


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(
    *,
    attempt_id: str = "lease-attempt-1",
    html: str = "<html><body>lease</body></html>",
) -> SendAttemptBinding:
    return SendAttemptBinding(
        send_attempt_id=attempt_id,
        project_id=7,
        approval_snapshot_id=11,
        approval_snapshot_hash=_h("approval-11"),
        report_version_id=19,
        report_content_hash=_h("report-19"),
        render_identity="render-19-v1",
        render_hash=_h("render-19-v1"),
        html_sha256=_h(html),
        message_id="<report-19@example.test>",
        to_recipients=("a@example.test", "b@example.test"),
        subject="Daily report",
        from_identity="reports@example.test",
    )


@pytest.fixture()
def mail_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    return path


def test_x01_live_durable_owner_blocks_cross_process_orphan_recovery(mail_db: Path):
    html = "<html><body>lease</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    owner = "owner_A_0123456789"
    acquire_execution_lease(attempt.send_attempt_id, owner)
    claimed = claim_prepared_attempt(attempt.send_attempt_id)
    assert claimed.state == "sending"

    fake = FakeMailGateway()
    with pytest.raises(MailWorkflowError) as caught:
        execute_send_attempt_once(
            send_attempt_id=attempt.send_attempt_id,
            exact_html=html,
            gateway=fake,
        )

    assert caught.value.code == "MAIL_SEND_ATTEMPT_IN_PROGRESS"
    assert fake.call_count == 0
    assert get_send_attempt(attempt.send_attempt_id).state == "sending"
    assert get_execution_lease(attempt.send_attempt_id) is not None
    release_execution_lease(attempt.send_attempt_id, owner)


def test_x02_expired_owner_is_fenced_then_sending_recovers_unknown(mail_db: Path):
    html = "<html><body>lease</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    owner = "owner_B_0123456789"
    acquire_execution_lease(attempt.send_attempt_id, owner, ttl_ms=1, now_ms=1)
    claim_prepared_attempt(attempt.send_attempt_id)

    fake = FakeMailGateway()
    recovered = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert recovered.state == "unknown"
    assert recovered.terminal_code == "MAIL_SEND_INTERRUPTED_UNKNOWN"
    assert fake.call_count == 0
    assert get_execution_lease(attempt.send_attempt_id) is None
    with pytest.raises(MailExecutionLeaseError) as caught:
        renew_execution_lease(attempt.send_attempt_id, owner)
    assert caught.value.code == "MAIL_SEND_EXECUTION_LEASE_LOST"


def test_x03_expired_preclaim_lease_can_be_safely_taken_over_without_duplicate_send(mail_db: Path):
    html = "<html><body>lease</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    acquire_execution_lease(
        attempt.send_attempt_id,
        "owner_C_0123456789",
        ttl_ms=1,
        now_ms=1,
    )
    fake = FakeMailGateway()

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert closed.state == "sent"
    assert fake.call_count == 1
    assert get_execution_lease(attempt.send_attempt_id) is None


def test_x04_renewal_keeps_live_owner_authoritative_and_expiry_fences_it(mail_db: Path):
    attempt = create_send_attempt(_binding())
    owner = "owner_D_0123456789"
    acquire_execution_lease(
        attempt.send_attempt_id,
        owner,
        ttl_ms=1_000,
        now_ms=1,
    )
    renewed = renew_execution_lease(
        attempt.send_attempt_id,
        owner,
        ttl_ms=1_000,
        now_ms=100,
    )
    assert renewed.lease_expires_at_ms == 1_100

    assert permit_orphan_recovery(attempt.send_attempt_id, now_ms=1_000) is False
    assert permit_orphan_recovery(attempt.send_attempt_id, now_ms=1_101) is True
    assert get_execution_lease(attempt.send_attempt_id) is None


def test_x05_terminal_truth_wins_if_another_process_closes_after_last_owner_proof(
    mail_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    html = "<html><body>lease</body></html>"
    attempt = create_send_attempt(_binding(attempt_id="lease-race-5", html=html))
    fake = FakeMailGateway()
    original_record = mail_workflow_module.record_send_result
    injected = {"done": False}

    def terminalize_before_record(send_attempt_id, result):
        if not injected["done"]:
            injected["done"] = True
            close_post_call_unknown(
                send_attempt_id,
                code="MAIL_SEND_INTERRUPTED_UNKNOWN",
                summary="another process already preserved uncertain terminal truth",
            )
            raise MailSendAttemptError("MAIL_SEND_ATTEMPT_STATE_CONFLICT")
        return original_record(send_attempt_id, result)

    monkeypatch.setattr(mail_workflow_module, "record_send_result", terminalize_before_record)

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert closed.state == "unknown"
    assert closed.terminal_code == "MAIL_SEND_INTERRUPTED_UNKNOWN"
    assert fake.call_count == 1
    assert get_send_attempt(attempt.send_attempt_id).state == "unknown"
