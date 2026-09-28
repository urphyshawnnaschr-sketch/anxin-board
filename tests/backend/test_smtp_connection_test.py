from __future__ import annotations

from pathlib import Path
import smtplib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.secret_store import InMemorySecretStore  # noqa: E402
from app.smtp_connection_test import (  # noqa: E402
    SmtpConnectionTestError,
    test_smtp_connection as run_smtp_connection_test,
)
from app.smtp_mail_gateway import SmtpGatewayConfig, SmtpSecurity  # noqa: E402


def _config(
    *,
    host: str = "smtp.example.test",
    port: int | None = None,
    security: SmtpSecurity = SmtpSecurity.STARTTLS,
) -> SmtpGatewayConfig:
    return SmtpGatewayConfig(
        host=host,
        port=port if port is not None else (587 if security is SmtpSecurity.STARTTLS else 465),
        security=security,
        username="mailer@example.test",
        secret_ref="smtp-password-test",
        timeout_seconds=30,
    )


def _store() -> InMemorySecretStore:
    store = InMemorySecretStore()
    store.put("smtp-password-test", "test-only-secret")
    return store


class _CountingStore(InMemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get(self, secret_ref: str) -> str:
        self.get_calls += 1
        return super().get(secret_ref)


class _Client:
    def __init__(self, calls: list[object], *, noop_code: int = 250):
        self.calls = calls
        self.noop_code = noop_code

    def ehlo(self):
        self.calls.append("ehlo")
        return 250, b"ok"

    def starttls(self, *, context):
        assert context is not None
        self.calls.append("starttls")
        return 220, b"ready"

    def login(self, user: str, password: str):
        self.calls.append(("login", user, password))
        return 235, b"ok"

    def noop(self):
        self.calls.append("noop")
        return self.noop_code, b"ok"

    def quit(self):
        self.calls.append("quit")
        return 221, b"bye"

    def close(self):
        self.calls.append("close")


def test_p01_starttls_preflight_authenticates_and_never_has_message_submission_surface():
    calls: list[object] = []
    client = _Client(calls)

    result = run_smtp_connection_test(
        config=_config(),
        secret_store=_store(),
        client_factory=lambda config: client,
    )

    assert result.credential_read is True
    assert result.smtp_connect is True
    assert result.tls_ready is True
    assert result.smtp_auth is True
    assert result.post_auth_noop is True
    assert calls == [
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "mailer@example.test", "test-only-secret"),
        "noop",
        "quit",
    ]
    assert not hasattr(client, "send_message")
    assert not hasattr(client, "sendmail")


def test_p02_implicit_tls_preflight_does_not_issue_starttls():
    calls: list[object] = []
    client = _Client(calls)

    result = run_smtp_connection_test(
        config=_config(security=SmtpSecurity.IMPLICIT_TLS),
        secret_store=_store(),
        client_factory=lambda config: client,
    )

    assert result.tls_ready is True
    assert calls == [
        ("login", "mailer@example.test", "test-only-secret"),
        "noop",
        "quit",
    ]


def test_p03_missing_secret_fails_before_client_factory():
    called = {"factory": 0}

    def factory(_config):
        called["factory"] += 1
        raise AssertionError("client must not be opened")

    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(
            config=_config(),
            secret_store=InMemorySecretStore(),
            client_factory=factory,
        )

    assert caught.value.code == "MAIL_SMTP_TEST_SECRET_UNAVAILABLE"
    assert called["factory"] == 0


def test_p04_auth_rejection_is_sanitized_and_connection_is_closed():
    calls: list[object] = []

    class AuthRejectClient(_Client):
        def login(self, user: str, password: str):
            self.calls.append(("login", user, "<redacted>"))
            raise smtplib.SMTPAuthenticationError(535, b"auth rejected")

    client = AuthRejectClient(calls)
    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(
            config=_config(),
            secret_store=_store(),
            client_factory=lambda config: client,
        )

    assert caught.value.code == "MAIL_SMTP_TEST_AUTH_FAILED"
    assert str(caught.value) == "MAIL_SMTP_TEST_AUTH_FAILED"
    assert calls[-1] == "quit"


def test_p05_post_auth_noop_rejection_is_not_misreported_as_success():
    calls: list[object] = []
    client = _Client(calls, noop_code=421)

    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(
            config=_config(),
            secret_store=_store(),
            client_factory=lambda config: client,
        )

    assert caught.value.code == "MAIL_SMTP_TEST_POST_AUTH_FAILED"
    assert calls[-1] == "quit"


def test_p06_known_outlook_transport_fails_before_secret_read_or_network_factory():
    store = _CountingStore()
    store.put("smtp-password-test", "test-only-secret")
    calls: list[object] = []

    def forbidden_factory(_config):
        calls.append("factory")
        raise AssertionError("blocked transport must not open network client")

    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(
            config=_config(
                host="SMTP-MAIL.OUTLOOK.COM",
                port=587,
                security=SmtpSecurity.STARTTLS,
            ),
            secret_store=store,
            client_factory=forbidden_factory,
        )

    assert caught.value.code == "MAIL_SMTP_TEST_TRANSPORT_UNSUPPORTED"
    assert store.get_calls == 0
    assert calls == []


@pytest.mark.parametrize(
    ("host", "port", "security"),
    [
        ("smtp.qq.com", 587, SmtpSecurity.STARTTLS),
        ("smtp.163.com", 465, SmtpSecurity.IMPLICIT_TLS),
        ("smtp.126.com", 465, SmtpSecurity.IMPLICIT_TLS),
        ("smtp.gmail.com", 587, SmtpSecurity.STARTTLS),
        ("smtp.mail.me.com", 587, SmtpSecurity.STARTTLS),
    ],
)
def test_p07_existing_supported_presets_keep_bounded_preflight(
    host: str,
    port: int,
    security: SmtpSecurity,
):
    calls: list[object] = []
    client = _Client(calls)

    result = run_smtp_connection_test(
        config=_config(host=host, port=port, security=security),
        secret_store=_store(),
        client_factory=lambda config: client,
    )

    assert result.smtp_auth is True
    assert any(isinstance(item, tuple) and item[0] == "login" for item in calls)
    assert "noop" in calls

@pytest.mark.parametrize('failure,expected', [
    (smtplib.SMTPServerDisconnected('private server detail'), 'MAIL_SMTP_TEST_AUTH_CONNECTION_LOST'),
    (TimeoutError('private timeout detail'), 'MAIL_SMTP_TEST_AUTH_TIMEOUT'),
    (smtplib.SMTPNotSupportedError('private capabilities'), 'MAIL_SMTP_TEST_AUTH_UNSUPPORTED'),
    (smtplib.SMTPAuthenticationError(535, b'private rejection'), 'MAIL_SMTP_TEST_AUTH_FAILED'),
])
def test_login_failure_classification_is_safe_without_retry(failure, expected):
    calls = []
    class Client(_Client):
        def login(self, user, password):
            self.calls.append('login')
            raise failure
    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(config=_config(), secret_store=_store(), client_factory=lambda config: Client(calls))
    assert caught.value.code == expected
    assert str(caught.value) == expected
    assert calls.count('login') == 1
    assert 'noop' not in calls and calls[-1] == 'quit'


@pytest.mark.parametrize('auth', [None, 'XOAUTH2'])
def test_actual_smtplib_login_without_supported_auth_is_classified(auth):
    calls = []
    class LocalSMTP(smtplib.SMTP):
        def __init__(self):
            self.esmtp_features = {} if auth is None else {'auth': auth}
            self.ehlo_resp = b'synthetic'
            self.helo_resp = None
        def ehlo(self):
            calls.append('ehlo')
            return 250, b'synthetic'
        def starttls(self, *, context):
            calls.append('starttls')
        def quit(self):
            calls.append('quit')
        def docmd(self, *args):
            pytest.fail('no supported mechanism must not send AUTH')
    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(config=_config(), secret_store=_store(), client_factory=lambda config: LocalSMTP())
    assert caught.value.code == 'MAIL_SMTP_TEST_AUTH_UNSUPPORTED'
    assert calls[-1] == 'quit'

def test_actual_smtplib_wrapped_socket_timeout_remains_timeout():
    class TimedOutReader:
        def readline(self, limit):
            raise TimeoutError('private transport detail')
        def close(self):
            pass
    class LocalSMTP(smtplib.SMTP):
        def __init__(self):
            self.file = TimedOutReader()
            self.sock = None
        def ehlo(self):
            return 250, b'synthetic'
        def starttls(self, *, context):
            pass
        def login(self, user, password):
            self.getreply()
        def quit(self):
            pass
    with pytest.raises(SmtpConnectionTestError) as caught:
        run_smtp_connection_test(config=_config(), secret_store=_store(), client_factory=lambda config: LocalSMTP())
    assert caught.value.code == 'MAIL_SMTP_TEST_AUTH_TIMEOUT'
