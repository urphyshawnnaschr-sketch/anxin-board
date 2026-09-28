"""Real-world private Git authorization boundary tests; no real credentials/network."""

import importlib
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import git_connections, main  # noqa: E402
from app.git_client import GitClientError  # noqa: E402
from app.git_human_check_client import HumanCheckGitClient  # noqa: E402
from app.git_ssh_client import SshEnabledGitClient  # noqa: E402


def _git_path():
    value = shutil.which("git")
    if not value:
        pytest.skip("system Git unavailable")
    return value


def test_base_client_stays_noninteractive():
    client = SshEnabledGitClient(git_path=_git_path())
    env = client._environment()
    policy = client._execution_policy()
    assert client.timeout_seconds == 30
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "Never"
    assert "credential.interactive=false" in policy
    assert "credential.interactive=true" not in policy


def test_human_check_allows_only_gcm_interaction_with_bounded_window():
    client = HumanCheckGitClient(git_path=_git_path())
    env = client._environment()
    policy = client._execution_policy()
    assert client.timeout_seconds == 180
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GCM_INTERACTIVE" not in env
    assert env["SSH_ASKPASS_REQUIRE"] == "never"
    assert "credential.interactive=true" in policy
    assert "credential.interactive=false" not in policy


def test_main_wires_human_client_only_when_default_factory_is_present(monkeypatch):
    monkeypatch.setattr(git_connections, "_git_client_factory", SshEnabledGitClient)
    importlib.reload(main)
    assert git_connections._git_client_factory is HumanCheckGitClient

    class InjectedClient:
        pass

    monkeypatch.setattr(git_connections, "_git_client_factory", InjectedClient)
    importlib.reload(main)
    assert git_connections._git_client_factory is InjectedClient


@pytest.mark.parametrize(
    "detail",
    [
        "fatal: Cannot prompt because user interactivity has been disabled.",
        "fatal: unable to get password from user",
    ],
)
def test_observed_gcm_prompt_failures_are_auth_failures(monkeypatch, detail):
    client = HumanCheckGitClient(git_path=_git_path())
    monkeypatch.setattr(
        client,
        "_exec",
        lambda args, cwd: (128, b"", detail.encode("utf-8")),
    )
    with pytest.raises(GitClientError) as raised:
        client._run(
            ["ls-remote", "https://github.com/example/private.git"],
            cwd=Path.cwd(),
        )
    assert raised.value.code == "GIT_AUTH_FAILED"
    assert "登录/授权" in raised.value.summary


@pytest.mark.parametrize(
    "args, detail",
    [
        (
            ["clone", "--branch", "missing", "https://github.com/example/private.git"],
            "fatal: Remote branch missing not found in upstream origin",
        ),
        (
            ["fetch", "origin", "+refs/heads/missing:refs/remotes/origin/missing"],
            "fatal: couldn't find remote ref refs/heads/missing",
        ),
    ],
)
def test_single_auth_clone_or_fetch_preserves_branch_not_found(monkeypatch, args, detail):
    client = HumanCheckGitClient(git_path=_git_path())
    monkeypatch.setattr(
        client,
        "_exec",
        lambda command, cwd: (128, b"", detail.encode("utf-8")),
    )
    with pytest.raises(GitClientError) as raised:
        client._run(args, cwd=Path.cwd())
    assert raised.value.code == "GIT_BRANCH_NOT_FOUND"
