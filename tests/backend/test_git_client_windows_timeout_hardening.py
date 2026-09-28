from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import git_client
from app.git_client import GitClient, GitClientError


class _AlreadyExitedProcess:
    pid = 1

    @staticmethod
    def poll():
        return 0


def test_timeout_handler_terminates_but_never_closes_job(monkeypatch):
    calls = {"terminate": 0, "close": 0, "fallback": 0}
    monkeypatch.setattr(
        git_client,
        "_terminate_job",
        lambda _job: calls.__setitem__("terminate", calls["terminate"] + 1) or True,
    )
    monkeypatch.setattr(
        git_client,
        "_close_job",
        lambda _job: calls.__setitem__("close", calls["close"] + 1),
    )
    monkeypatch.setattr(
        git_client,
        "_fallback_kill_process_tree",
        lambda _process: calls.__setitem__("fallback", calls["fallback"] + 1),
    )
    timed_out = threading.Event()
    git_client._timeout_handler(timed_out, 123, _AlreadyExitedProcess())
    assert timed_out.is_set()
    assert calls == {"terminate": 1, "close": 0, "fallback": 0}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_exec_wall_clock_timeout_does_not_poison_followup_process(tmp_path, monkeypatch):
    client = GitClient(git_path=sys.executable, timeout_seconds=1, allow_local_file=True)
    monkeypatch.setattr(client, "_execution_policy", lambda: [])
    child_program = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(GitClientError) as raised:
        client._exec(["-c", child_program], cwd=Path(tmp_path))
    elapsed = time.monotonic() - started
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"
    assert elapsed < 8

    for index in range(5):
        returncode, stdout, stderr = client._exec(
            ["-c", f"print('ok-{index}')"], cwd=Path(tmp_path)
        )
        assert returncode == 0
        assert stdout.strip() == f"ok-{index}".encode("ascii")
        assert stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill fallback regression")
def test_discard_children_does_not_leak_taskkill_timeout(monkeypatch):
    client = GitClient(git_path=sys.executable, timeout_seconds=1, allow_local_file=True)
    state = {"kills": 0, "waits": 0}

    class FakeProcess:
        pid = 424242

        def poll(self):
            return None

        def kill(self):
            state["kills"] += 1

        def wait(self, timeout=None):
            state["waits"] += 1
            if state["waits"] == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    monkeypatch.setattr(git_client, "_terminate_job", lambda _job: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
        ),
    )
    assert client._discard_children(FakeProcess(), None) is True
    assert state["kills"] >= 1
    assert state["waits"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows absolute deadline regression")
def test_run_bounded_deadline_includes_descendant_cleanup(tmp_path, monkeypatch):
    client = GitClient(git_path=sys.executable, timeout_seconds=1, allow_local_file=True)
    monkeypatch.setattr(client, "_execution_policy", lambda: [])
    child_program = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print('ready', flush=True)"
    )
    started = time.monotonic()
    with pytest.raises(GitClientError) as raised:
        client._run_bounded_process(
            ["-c", child_program],
            cwd=Path(tmp_path),
            line_limit=10,
            timeout_seconds=1,
            parse_line=True,
        )
    elapsed = time.monotonic() - started
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"
    assert elapsed < 2.25


@pytest.mark.skipif(os.name != "nt", reason="Windows absolute deadline regression")
def test_discard_children_starts_no_blocking_wait_after_deadline(monkeypatch):
    client = GitClient(git_path=sys.executable, timeout_seconds=1, allow_local_file=True)
    state = {"waits": 0, "taskkills": 0, "kills": 0}

    class FakeProcess:
        pid = 424243

        def poll(self):
            return None

        def kill(self):
            state["kills"] += 1

        def wait(self, timeout=None):
            state["waits"] += 1
            raise AssertionError("wait must not start after deadline")

    monkeypatch.setattr(git_client, "_terminate_job", lambda _job: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: state.__setitem__("taskkills", state["taskkills"] + 1),
    )
    assert client._discard_children(FakeProcess(), None, time.monotonic() - 1) is False
    assert state["waits"] == 0
    assert state["taskkills"] == 0
    assert state["kills"] >= 1


def test_job_handle_close_is_deferred_until_watchdog_finishes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    close_calls: list[int] = []

    def blocked_watchdog() -> None:
        entered.set()
        release.wait(timeout=5)

    timer = threading.Timer(0, blocked_watchdog)
    timer.daemon = True
    timer.start()
    assert entered.wait(timeout=1)
    assert timer.is_alive()

    def fake_close(job: int | None) -> None:
        assert job is not None
        close_calls.append(job)
        closed.set()

    monkeypatch.setattr(git_client, "_close_job", fake_close)

    started = time.monotonic()
    reaper = git_client._finalize_job_handle(123, timer, time.monotonic() - 1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert reaper is not None
    assert close_calls == []

    release.set()
    assert closed.wait(timeout=2)
    reaper.join(timeout=2)
    assert not reaper.is_alive()
    assert close_calls == [123]
