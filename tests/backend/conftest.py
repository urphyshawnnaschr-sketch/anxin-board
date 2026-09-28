"""Shared pytest compatibility fixtures for clean CI runners."""

from __future__ import annotations

import os
import sys
import tempfile
import socket
import ipaddress
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.git_client import GitClient  # noqa: E402


@pytest.fixture(autouse=True)
def block_real_external_network(monkeypatch):
    """Synthetic tests may use loopback, never a paid provider or SMTP server."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def allowed(host):
        if host == 'localhost':
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def check(address):
        if isinstance(address, tuple) and not allowed(address[0]):
            raise AssertionError('Real external network is forbidden in backend tests')

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def resolve(host, *args, **kwargs):
        if host is not None and not allowed(host):
            raise AssertionError('External DNS is forbidden in backend tests')
        return original_resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)


if os.name == "nt":
    # GitHub-hosted Windows runners can expose the same temporary directory
    # once as an 8.3 short path (RUNNER~1) and once as its long spelling
    # (runneradmin).  Python's realpath expands the short spelling on Windows,
    # so product path checks and pytest's tmp_path use one canonical spelling.
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())


@pytest.fixture(autouse=True)
def python_subprocess_test_double_has_no_git_policy(monkeypatch):
    """Do not prepend Git-only ``-c`` options when Python is the test double.

    Timeout tests intentionally use ``sys.executable`` instead of git so they
    can launch a deterministic sleeping process.  The test double must receive
    only Python arguments; real GitClient instances retain the complete Git
    execution policy.
    """

    original = GitClient._execution_policy
    python_path = os.path.normcase(os.path.abspath(sys.executable))

    def execution_policy(client: GitClient) -> list[str]:
        candidate = os.path.normcase(os.path.abspath(client.git_path or ""))
        if candidate == python_path:
            return []
        return original(client)

    monkeypatch.setattr(GitClient, "_execution_policy", execution_policy)
