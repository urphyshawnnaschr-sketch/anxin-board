from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.db import init_db  # noqa: E402
from app.fake_mail_gateway import FakeMailGateway  # noqa: E402
from app.mail_gateway import RecipientOutcome  # noqa: E402
from app.mail_send_attempts import (  # noqa: E402
    SendAttemptBinding,
    claim_prepared_attempt,
    create_send_attempt,
    get_send_attempt,
)
from app.mail_workflow import MailWorkflowError, execute_send_attempt_once  # noqa: E402


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
    return path


def test_w01_exact_frozen_message_is_invoked_once_and_closes_sent(mail_db: Path):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    fake = FakeMailGateway()

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert closed.state == "sent"
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call.recipients == attempt.to_recipients
    assert call.message_id == attempt.message_id
    assert call.body_identity_sha256 == attempt.html_sha256
    assert [item.outcome for item in closed.recipient_results] == [
        RecipientOutcome.ACCEPTED,
        RecipientOutcome.ACCEPTED,
    ]


def test_w02_html_identity_mismatch_is_pre_call_fail_closed(mail_db: Path):
    attempt = create_send_attempt(_binding())
    fake = FakeMailGateway()

    with pytest.raises(MailWorkflowError) as caught:
        execute_send_attempt_once(
            send_attempt_id=attempt.send_attempt_id,
            exact_html="<html>changed</html>",
            gateway=fake,
        )

    assert caught.value.code == "MAIL_SEND_HTML_IDENTITY_MISMATCH"
    assert fake.call_count == 0
    assert get_send_attempt(attempt.send_attempt_id).state == "prepared"


@pytest.mark.parametrize(
    ("recipient_outcomes", "expected_state"),
    [
        ({"b@example.test": "rejected"}, "partial"),
        ({"a@example.test": "rejected", "b@example.test": "rejected"}, "failed"),
        ({"a@example.test": "unknown"}, "unknown"),
    ],
)
def test_w03_gateway_recipient_outcomes_close_deterministically(
    mail_db: Path, recipient_outcomes: dict[str, str], expected_state: str
):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    fake = FakeMailGateway(recipient_outcomes=recipient_outcomes)

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert closed.state == expected_state
    assert fake.call_count == 1
    assert len(closed.recipient_results) == len(attempt.to_recipients)


def test_w04_definite_gateway_call_failure_is_failed_without_retry(mail_db: Path):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    fake = FakeMailGateway(failure_calls={1})

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert closed.state == "failed"
    assert closed.recipient_results == ()
    assert closed.terminal_code == "MAIL_GATEWAY_CALL_FAILED"
    assert fake.call_count == 1

    replay = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )
    assert replay.state == "failed"
    assert fake.call_count == 1


@pytest.mark.parametrize("loss_kwargs", [{"connection_loss_calls": {1}}, {"response_loss_calls": {1}}])
def test_w05_transport_ambiguity_is_unknown_and_terminal_replay_is_zero_call(
    mail_db: Path, loss_kwargs: dict[str, set[int]]
):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    fake = FakeMailGateway(**loss_kwargs)

    unknown = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )
    assert unknown.state == "unknown"
    assert fake.call_count == 1
    assert all(
        row.outcome is RecipientOutcome.UNKNOWN for row in unknown.recipient_results
    )

    replay = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )
    assert replay.state == "unknown"
    assert fake.call_count == 1


def test_w06_restart_with_orphan_sending_claim_closes_unknown_without_gateway(mail_db: Path):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    claimed = claim_prepared_attempt(attempt.send_attempt_id)
    assert claimed.state == "sending"
    fake = FakeMailGateway()

    recovered = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert recovered.state == "unknown"
    assert recovered.terminal_code == "MAIL_SEND_INTERRUPTED_UNKNOWN"
    assert recovered.recipient_results == ()
    assert fake.call_count == 0

    execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )
    assert fake.call_count == 0


class _BlockingFakeMailGateway(FakeMailGateway):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    def send(self, message):
        self._started.set()
        assert self._release.wait(timeout=5)
        return super().send(message)


def test_w07_two_local_callers_reach_gateway_only_once(mail_db: Path):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    started = threading.Event()
    release = threading.Event()
    fake = _BlockingFakeMailGateway(started, release)
    first_result: list[object] = []
    first_error: list[BaseException] = []

    def first_call() -> None:
        try:
            first_result.append(
                execute_send_attempt_once(
                    send_attempt_id=attempt.send_attempt_id,
                    exact_html=html,
                    gateway=fake,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion surface below
            first_error.append(exc)

    thread = threading.Thread(target=first_call)
    thread.start()
    assert started.wait(timeout=5)

    with pytest.raises(MailWorkflowError) as caught:
        execute_send_attempt_once(
            send_attempt_id=attempt.send_attempt_id,
            exact_html=html,
            gateway=fake,
        )
    assert caught.value.code == "MAIL_SEND_ATTEMPT_IN_PROGRESS"
    assert fake.call_count == 0

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_error == []
    assert len(first_result) == 1
    assert fake.call_count == 1
    assert get_send_attempt(attempt.send_attempt_id).state == "sent"


class _InvalidResultFakeMailGateway(FakeMailGateway):
    def send(self, message):
        super().send(message)
        return object()


def test_w08_post_call_invalid_result_becomes_unknown_without_fabrication(mail_db: Path):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    fake = _InvalidResultFakeMailGateway()

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert fake.call_count == 1
    assert closed.state == "unknown"
    assert closed.terminal_code == "MAIL_SEND_GATEWAY_RESULT_UNKNOWN"
    assert closed.recipient_results == ()

    execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )
    assert fake.call_count == 1


class _RaisingAfterInvocationFakeMailGateway(FakeMailGateway):
    def send(self, message):
        super().send(message)
        raise RuntimeError("opaque transport-side exception")


def test_w09_unclassified_post_invocation_exception_is_unknown_not_retry(mail_db: Path):
    html = "<html><body>daily</body></html>"
    attempt = create_send_attempt(_binding(html=html))
    fake = _RaisingAfterInvocationFakeMailGateway()

    closed = execute_send_attempt_once(
        send_attempt_id=attempt.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert fake.call_count == 1
    assert closed.state == "unknown"
    assert closed.terminal_code == "MAIL_SEND_GATEWAY_EXCEPTION_UNKNOWN"
    assert closed.recipient_results == ()


def test_w10_explicit_same_report_resend_replays_full_frozen_to_and_message_id(mail_db: Path):
    html = "<html><body>daily</body></html>"
    first = create_send_attempt(_binding(attempt_id="send-attempt-1", html=html))
    fake = FakeMailGateway()
    execute_send_attempt_once(
        send_attempt_id=first.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    second = create_send_attempt(
        _binding(
            attempt_id="send-attempt-2",
            html=html,
            predecessor=first.send_attempt_id,
        )
    )
    execute_send_attempt_once(
        send_attempt_id=second.send_attempt_id,
        exact_html=html,
        gateway=fake,
    )

    assert fake.call_count == 2
    assert fake.calls[1].recipients == fake.calls[0].recipients
    assert fake.calls[1].message_id == fake.calls[0].message_id
    assert fake.calls[1].body_identity_sha256 == fake.calls[0].body_identity_sha256


def test_w11_slice_has_no_network_secret_checkpoint_route_or_retry_loop_dependency():
    root = Path(__file__).resolve().parents[2]
    forbidden_import_roots = {"smtplib", "socket", "http", "urllib", "requests"}
    forbidden_call_text = {
        "secret_store.",
        "get_secret(",
        "advance_checkpoint(",
        "fastapi",
        "apirouter(",
        "sleep(",
        "backoff",
    }
    for relative in (
        Path("apps/backend/app/mail_send_attempts.py"),
        Path("apps/backend/app/mail_workflow.py"),
    ):
        source = (root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots.isdisjoint(forbidden_import_roots)
        lowered = source.lower()
        assert all(token not in lowered for token in forbidden_call_text)

        for node in ast.walk(tree):
            assert not isinstance(node, (ast.While, ast.AsyncFor))
