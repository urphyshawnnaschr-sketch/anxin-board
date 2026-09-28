from __future__ import annotations

import hashlib
from pathlib import Path
import smtplib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.db import init_db  # noqa: E402
from app.mail_gateway import RecipientOutcome  # noqa: E402
from app.mail_send_attempts import SendAttemptBinding, create_send_attempt  # noqa: E402
from app.mail_workflow import execute_send_attempt_once  # noqa: E402
from app.secret_store import InMemorySecretStore  # noqa: E402
from app.smtp_mail_gateway import (  # noqa: E402
    SmtpGatewayConfig,
    SmtpSecurity,
    StdlibSmtpMailGateway,
)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _LifecycleSmtpClient:
    def __init__(
        self,
        *,
        send_result=None,
        send_error: BaseException | None = None,
        login_error: BaseException | None = None,
    ) -> None:
        self.send_result = {} if send_result is None else send_result
        self.send_error = send_error
        self.login_error = login_error
        self.send_count = 0
        self.login_count = 0

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, *, context):
        assert context is not None
        return 220, b"ready"

    def login(self, user: str, password: str):
        self.login_count += 1
        assert user == "sender@example.test"
        assert password == "unit-test-secret"
        if self.login_error is not None:
            raise self.login_error
        return 235, b"ok"

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.send_count += 1
        assert msg["Message-ID"].startswith("<attempt-")
        assert from_addr == "reports@example.test"
        assert tuple(to_addrs or ()) == ("a@example.test", "b@example.test")
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None


@pytest.fixture()
def mail_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    return path


def _html() -> str:
    return "<html><body>formal report bytes</body></html>"


def _attempt(attempt_id: str):
    html = _html()
    return create_send_attempt(
        SendAttemptBinding(
            send_attempt_id=attempt_id,
            project_id=7,
            approval_snapshot_id=11,
            approval_snapshot_hash=_h("approval-11"),
            report_version_id=19,
            report_content_hash=_h("report-19"),
            render_identity="render-19-v1",
            render_hash=_h("render-19-v1"),
            html_sha256=_h(html),
            message_id=f"<{attempt_id}@example.test>",
            to_recipients=("a@example.test", "b@example.test"),
            subject="Daily report",
            from_identity="reports@example.test",
        )
    )


def _gateway(client: _LifecycleSmtpClient) -> StdlibSmtpMailGateway:
    store = InMemorySecretStore()
    store.put("mail.smtp.password", "unit-test-secret")
    return StdlibSmtpMailGateway(
        config=SmtpGatewayConfig(
            host="smtp.example.test",
            port=587,
            security=SmtpSecurity.STARTTLS,
            username="sender@example.test",
            secret_ref="mail.smtp.password",
        ),
        secret_store=store,
        client_factory=lambda _config: client,
    )


def _execute(attempt_id: str, gateway: StdlibSmtpMailGateway):
    return execute_send_attempt_once(
        send_attempt_id=attempt_id,
        exact_html=_html(),
        gateway=gateway,
    )


def test_i01_acceptance_persists_sent_and_terminal_reentry_never_resends(mail_db: Path):
    attempt = _attempt("attempt-accept")
    client = _LifecycleSmtpClient()
    gateway = _gateway(client)

    first = _execute(attempt.send_attempt_id, gateway)
    second = _execute(attempt.send_attempt_id, gateway)

    assert first.state == "sent"
    assert second.state == "sent"
    assert client.send_count == 1
    assert tuple(item.outcome for item in first.recipient_results) == (
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.ACCEPTED,
    )


def test_i02_partial_refusal_persists_exact_partial_and_never_resends(mail_db: Path):
    attempt = _attempt("attempt-partial")
    client = _LifecycleSmtpClient(send_result={"b@example.test": (550, b"refused")})
    gateway = _gateway(client)

    first = _execute(attempt.send_attempt_id, gateway)
    second = _execute(attempt.send_attempt_id, gateway)

    assert first.state == "partial"
    assert second.state == "partial"
    assert client.send_count == 1
    assert tuple(item.outcome for item in first.recipient_results) == (
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.REJECTED,
    )


def test_i03_post_submission_timeout_persists_unknown_and_never_blind_retries(mail_db: Path):
    attempt = _attempt("attempt-unknown")
    client = _LifecycleSmtpClient(send_error=TimeoutError("response lost"))
    gateway = _gateway(client)

    first = _execute(attempt.send_attempt_id, gateway)
    second = _execute(attempt.send_attempt_id, gateway)

    assert first.state == "unknown"
    assert second.state == "unknown"
    assert client.send_count == 1
    assert all(item.outcome is RecipientOutcome.UNKNOWN for item in first.recipient_results)


def test_i04_pre_submission_failure_persists_failed_without_send_and_never_reenters(mail_db: Path):
    attempt = _attempt("attempt-presubmit")
    client = _LifecycleSmtpClient(login_error=RuntimeError("test login boundary failure"))
    gateway = _gateway(client)

    first = _execute(attempt.send_attempt_id, gateway)
    second = _execute(attempt.send_attempt_id, gateway)

    assert first.state == "failed"
    assert second.state == "failed"
    assert first.terminal_code == "MAIL_SMTP_PRE_SUBMIT_FAILED"
    assert client.login_count == 1
    assert client.send_count == 0


def test_i05_all_explicit_recipient_refusal_persists_failed_not_unknown(mail_db: Path):
    attempt = _attempt("attempt-rejected")
    refused = {
        "a@example.test": (550, b"refused"),
        "b@example.test": (550, b"refused"),
    }
    client = _LifecycleSmtpClient(send_error=smtplib.SMTPRecipientsRefused(refused))
    gateway = _gateway(client)

    first = _execute(attempt.send_attempt_id, gateway)
    second = _execute(attempt.send_attempt_id, gateway)

    assert first.state == "failed"
    assert second.state == "failed"
    assert client.send_count == 1
    assert all(item.outcome is RecipientOutcome.REJECTED for item in first.recipient_results)
