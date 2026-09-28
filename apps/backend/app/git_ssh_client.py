"""Frozen-scope SSH transport policy layered on the existing controlled GitClient.

The product reuses a trusted local OpenSSH executable and normal local key-file
public-key credential sources. Authentication-agent use is explicitly disabled.
It never reads/copies private-key bytes and it does not load ssh_config, so
config-driven command execution (ProxyCommand/LocalCommand/etc.) cannot become
part of the Git URL execution surface.
"""

from __future__ import annotations

import ctypes
import os
import re
import shlex
from pathlib import Path

from app.git_client import (
    GIT_ERROR_SUMMARIES,
    GitClient,
    GitClientError,
    WorkspaceAccess,
    _is_reparse,
    _is_within,
)


GIT_ERROR_SUMMARIES.setdefault(
    "GIT_SSH_NOT_AVAILABLE",
    "本机未找到受信任的 OpenSSH 客户端，请先安装系统 OpenSSH 或 Git for Windows 后重试。",
)
GIT_ERROR_SUMMARIES.setdefault(
    "GIT_SSH_HOST_KEY_FAILED",
    "SSH 主机身份校验失败或主机尚未进入本机 known_hosts，已停止连接。",
)

_SCP_STYLE_URL_RE = re.compile(r"^[^@\s:]+@[^/\s:]+:.+$")


def _is_ssh_url(url: str) -> bool:
    return url.startswith("ssh://") or bool(_SCP_STYLE_URL_RE.fullmatch(url))


def _trusted_candidate(candidate: Path, trusted_root: Path) -> str | None:
    try:
        root = trusted_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if (
            not candidate.is_file()
            or _is_reparse(candidate)
            or not _is_within(resolved, root)
        ):
            return None
        return os.fspath(resolved)
    except OSError:
        return None


def _windows_system_directory() -> Path | None:
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        kernel32.GetSystemDirectoryW.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if length == 0 or length >= len(buffer):
            return None
        return Path(buffer.value)
    except (AttributeError, OSError, ValueError):
        return None


def _resolve_ssh_executable(git_path: str | None) -> str | None:
    """Resolve SSH only from OS/Git installation roots, never inherited SSH_* knobs."""
    if os.name == "nt":
        system_directory = _windows_system_directory()
        if system_directory is not None:
            resolved = _trusted_candidate(
                system_directory / "OpenSSH" / "ssh.exe",
                system_directory,
            )
            if resolved:
                return resolved

        if git_path:
            try:
                git_executable = Path(git_path).resolve(strict=True)
                git_root = git_executable.parents[1].resolve(strict=True)
            except (IndexError, OSError):
                git_root = None
            if git_root is not None:
                for relative in (
                    Path("usr/bin/ssh.exe"),
                    Path("mingw64/bin/ssh.exe"),
                ):
                    resolved = _trusted_candidate(git_root / relative, git_root)
                    if resolved:
                        return resolved
        return None

    import shutil

    discovered = shutil.which("ssh")
    if not discovered:
        return None
    try:
        resolved = Path(discovered).resolve(strict=True)
        if not resolved.is_file():
            return None
        return os.fspath(resolved)
    except OSError:
        return None


def _ssh_command(ssh_path: str) -> str:
    executable = ssh_path.replace("\\", "/") if os.name == "nt" else ssh_path
    return " ".join(
        [
            shlex.quote(executable),
            "-F", "none",
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PreferredAuthentications=publickey",
            "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ProxyCommand=none",
            "-o", "PermitLocalCommand=no",
            "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes",
            "-o", "RequestTTY=no",
        ]
    )


class SshEnabledGitClient(GitClient):
    supports_ssh = True

    def __init__(
        self,
        *,
        git_path: str | None = None,
        timeout_seconds: float = 30,
        size_limit: int = 1024**3,
        allow_local_file: bool = False,
    ):
        super().__init__(
            git_path=git_path,
            timeout_seconds=timeout_seconds,
            size_limit=size_limit,
            allow_local_file=allow_local_file,
        )
        self.ssh_path = _resolve_ssh_executable(self.git_path)
        protocols = ["https", "ssh"]
        if allow_local_file:
            protocols.append("file")
        self.allowed_protocols = ":".join(protocols)

    def _environment(self) -> dict[str, str]:
        environment = super()._environment()
        if self.ssh_path:
            environment["GIT_SSH_COMMAND"] = _ssh_command(self.ssh_path)
            environment["GIT_SSH_VARIANT"] = "ssh"
        return environment

    def _require_ssh_transport(self, url: str) -> None:
        if _is_ssh_url(url) and not self.ssh_path:
            raise GitClientError("GIT_SSH_NOT_AVAILABLE")

    def get_remote_head(self, url: str, branch: str) -> str:
        self._require_ssh_transport(url)
        return super().get_remote_head(url, branch)

    def clone_branch(self, url: str, branch: str, access: WorkspaceAccess) -> None:
        self._require_ssh_transport(url)
        return super().clone_branch(url, branch, access)

    def fetch_branch(self, access: WorkspaceAccess, branch: str) -> None:
        self._require_ssh_transport(access.expected_url)
        return super().fetch_branch(access, branch)

    def _run(self, args: list[str], *, cwd: Path) -> str:
        returncode, stdout, stderr = self._exec(args, cwd=cwd)
        output = stdout.decode("utf-8", errors="replace")
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").lower()
            if "ls-remote" in args and returncode == 2 and not output:
                code = "GIT_BRANCH_NOT_FOUND"
            elif "not a valid branch name" in detail or "invalid branch name" in detail:
                code = "GIT_BRANCH_INVALID"
            elif any(term in detail for term in (
                "host key verification failed",
                "remote host identification has changed",
                "no matching host key type found",
            )):
                code = "GIT_SSH_HOST_KEY_FAILED"
            elif any(term in detail for term in (
                "authentication failed",
                "terminal prompts disabled",
                "could not read username",
                "permission denied (publickey",
                "no supported authentication methods available",
                "http 401",
                "http 403",
            )):
                code = "GIT_AUTH_FAILED"
            elif "repository not found" in detail or "does not appear to be a git repository" in detail:
                code = "GIT_REPOSITORY_NOT_FOUND"
            elif any(term in detail for term in (
                "could not resolve host",
                "could not resolve hostname",
                "failed to connect",
                "connection refused",
                "network is unreachable",
                "no route to host",
                "connection timed out",
                "operation timed out",
            )):
                code = "GIT_NETWORK_UNAVAILABLE"
            else:
                code = "GIT_CHECK_FAILED"
            raise GitClientError(code)
        return output.strip()
