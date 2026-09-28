"""R2 Git SSH remediation tests; no real remote or private credential is used."""

import importlib
import inspect
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import git_connections, main  # noqa: E402
from app.git_client import GitClientError  # noqa: E402
import app.git_ssh_client as git_ssh_client  # noqa: E402
from app.git_ssh_client import (  # noqa: E402
    SshEnabledGitClient,
    _is_ssh_url,
    _ssh_command,
    _trusted_candidate,
)


HEAD = "a" * 40


def _client_with_resolved_ssh(monkeypatch, *, available=True, allow_local_file=False):
    monkeypatch.setattr(
        git_ssh_client,
        "_resolve_ssh_executable",
        lambda _git_path: sys.executable if available else None,
    )
    return SshEnabledGitClient(
        git_path=sys.executable,
        allow_local_file=allow_local_file,
    )


def test_production_constructor_has_no_ssh_executable_injection_parameter():
    assert "ssh_path" not in inspect.signature(SshEnabledGitClient).parameters


def test_trusted_candidate_must_be_regular_file_inside_trusted_root(tmp_path):
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    inside = trusted_root / "ssh.exe"
    inside.write_text("fixture", encoding="utf-8")
    outside = tmp_path / "outside.exe"
    outside.write_text("fixture", encoding="utf-8")

    assert _trusted_candidate(inside, trusted_root) == str(inside.resolve())
    assert _trusted_candidate(outside, trusted_root) is None
    assert _trusted_candidate(trusted_root / "missing.exe", trusted_root) is None


def test_standard_ssh_url_shapes_are_recognized_without_broadening_protocols():
    assert _is_ssh_url("ssh://git@example.invalid/org/repo.git")
    assert _is_ssh_url("git@example.invalid:org/repo.git")
    assert not _is_ssh_url("https://example.invalid/org/repo.git")
    assert not _is_ssh_url("file:///tmp/repo.git")


def test_ssh_environment_replaces_inherited_overrides_with_fixed_fail_closed_policy(
    monkeypatch,
):
    monkeypatch.setenv("GIT_SSH_COMMAND", "malicious-command")
    monkeypatch.setenv("GIT_SSH", "malicious-ssh")
    monkeypatch.setenv("GIT_SSH_VARIANT", "plink")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ext")
    monkeypatch.setenv("SSH_ASKPASS", "malicious-askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "malicious-agent")

    client = _client_with_resolved_ssh(monkeypatch)
    environment = client._environment()

    assert environment["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert environment["GIT_SSH_VARIANT"] == "ssh"
    command = environment["GIT_SSH_COMMAND"]
    assert "malicious" not in command
    for required in (
        "-F none",
        "BatchMode=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "PreferredAuthentications=publickey",
        "IdentityAgent=none",
        "StrictHostKeyChecking=yes",
        "ProxyCommand=none",
        "PermitLocalCommand=no",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
        "RequestTTY=no",
    ):
        assert required in command
    assert "GIT_SSH" not in environment
    assert environment["SSH_ASKPASS_REQUIRE"] == "never"
    assert "SSH_ASKPASS" not in environment
    assert "SSH_AUTH_SOCK" not in environment


def test_fixed_ssh_command_explicitly_disables_local_authentication_agent():
    command = _ssh_command(sys.executable)
    assert "IdentityAgent=none" in command
    assert "ForwardAgent=no" in command


def test_local_file_test_mode_does_not_remove_ssh_or_add_other_protocols(monkeypatch):
    client = _client_with_resolved_ssh(monkeypatch, allow_local_file=True)
    assert client.allowed_protocols == "https:ssh:file"


@pytest.mark.parametrize(
    "url",
    ["ssh://git@example.invalid/org/repo.git", "git@example.invalid:org/repo.git"],
)
def test_ssh_requires_a_trusted_local_executable_before_git_starts(monkeypatch, url):
    client = _client_with_resolved_ssh(monkeypatch, available=False)
    called = False

    def forbidden_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("git must not start without a trusted SSH executable")

    client._run = forbidden_run
    with pytest.raises(GitClientError) as raised:
        client.get_remote_head(url, "main")
    assert raised.value.code == "GIT_SSH_NOT_AVAILABLE"
    assert called is False


def test_https_still_works_when_ssh_is_unavailable(monkeypatch):
    url = "https://example.invalid/org/repo.git"
    client = _client_with_resolved_ssh(monkeypatch, available=False)

    def fake_run(args, *, cwd):
        if "--get-url" in args:
            return url
        return HEAD

    client._run = fake_run
    assert client.get_remote_head(url, "main") == HEAD


@pytest.mark.parametrize(
    ("stderr", "expected_code"),
    [
        (b"git@example.invalid: Permission denied (publickey).\n", "GIT_AUTH_FAILED"),
        (b"Host key verification failed.\n", "GIT_SSH_HOST_KEY_FAILED"),
        (b"ssh: Could not resolve hostname example.invalid\n", "GIT_NETWORK_UNAVAILABLE"),
        (b"ssh: connect to host example.invalid port 22: Connection refused\n", "GIT_NETWORK_UNAVAILABLE"),
    ],
)
def test_ssh_failures_map_to_stable_non_secret_codes(
    monkeypatch, tmp_path, stderr, expected_code
):
    client = _client_with_resolved_ssh(monkeypatch)
    client._exec = lambda args, cwd: (255, b"", stderr)
    with pytest.raises(GitClientError) as raised:
        client._run(["ls-remote", "ssh://git@example.invalid/org/repo.git"], cwd=tmp_path)
    assert raised.value.code == expected_code
    assert "example.invalid" not in raised.value.summary


class SuccessfulSshGitClient:
    supports_ssh = True
    calls = []

    def __init__(self):
        type(self).calls.append("init")

    def get_version(self):
        self.calls.append("version")
        return "git version 2.49.0.windows.1"

    def validate_branch(self, branch):
        self.calls.append(("validate", branch))

    def get_remote_head(self, url, branch):
        self.calls.append(("remote", url, branch))
        return HEAD

    def get_remote_head_ref(self, access, branch):
        self.calls.append(("remote_ref", branch))
        return HEAD

    def clone_branch(self, url, branch, access):
        self.calls.append(("clone", url, branch))
        (access.path / ".git").mkdir()

    def inspect_workspace(self, access):
        self.calls.append("inspect")
        assert (access.path / ".git").is_dir()

    def assert_clean(self, access):
        self.calls.append("clean")

    def fetch_branch(self, access, branch):
        self.calls.append(("fetch", access.expected_url, branch))

    def checkout_remote_head(self, access, branch):
        self.calls.append(("checkout", branch))

    def assert_size_limit(self, access):
        self.calls.append("size")

    def get_local_head(self, access):
        return HEAD


@pytest.fixture()
def ssh_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(tmp_path / "projects"))
    SuccessfulSshGitClient.calls = []
    monkeypatch.setattr(git_connections, "_git_client_factory", SuccessfulSshGitClient)
    importlib.reload(main)
    with TestClient(main.app) as api:
        yield api


@pytest.mark.parametrize(
    "url",
    ["ssh://git@example.invalid/org/repo.git", "git@example.invalid:org/repo.git"],
)
def test_git_connection_orchestrator_allows_ssh_capable_client(ssh_api, url):
    created = ssh_api.post("/api/projects", json={"name": "SSH 项目"}).json()
    configured = ssh_api.put(
        f"/api/projects/{created['id']}",
        json={
            "name": created["name"],
            "git_url": url,
            "branch": "main",
            "version": created["version"],
        },
    )
    assert configured.status_code == 200, configured.text

    response = ssh_api.post(f"/api/projects/{created['id']}/git/check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["error_code"] is None
    assert ("remote_ref", "main") in SuccessfulSshGitClient.calls
    assert not any(isinstance(call, tuple) and call[0] == "remote"
                   for call in SuccessfulSshGitClient.calls)
    assert any(
        isinstance(call, tuple) and call[:2] == ("clone", url)
        for call in SuccessfulSshGitClient.calls
    )
