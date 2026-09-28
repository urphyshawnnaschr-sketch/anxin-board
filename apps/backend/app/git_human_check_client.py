"""Human-triggered Git connection client for owner real-world HTTPS authorization.

The base Git clients remain fail-closed and non-interactive.  This client is wired only
into the explicit user-triggered project Git connection check.  It keeps terminal
credential prompts disabled, preserves the approved credential-helper/config/protocol
boundaries, and only permits the trusted Git Credential Manager GUI/browser flow.
"""

from __future__ import annotations

from pathlib import Path

from app.git_client import GIT_ERROR_SUMMARIES, GitClientError, WorkspaceAccess
from app.git_ssh_client import SshEnabledGitClient


GIT_ERROR_SUMMARIES["GIT_AUTH_FAILED"] = (
    "Git 认证未完成。请重新检查连接，并在凭据管理器提示时完成仓库登录/授权。"
)


class HumanCheckGitClient(SshEnabledGitClient):
    """Controlled Git client used only by the explicit human connection-check action."""

    supports_ssh = True

    def __init__(
        self,
        *,
        git_path: str | None = None,
        timeout_seconds: float = 180,
        size_limit: int = 1024**3,
        allow_local_file: bool = False,
    ):
        super().__init__(
            git_path=git_path,
            timeout_seconds=timeout_seconds,
            size_limit=size_limit,
            allow_local_file=allow_local_file,
        )

    def _environment(self) -> dict[str, str]:
        environment = super()._environment()
        # Keep GIT_TERMINAL_PROMPT=0, stdin=DEVNULL, SSH askpass disabled and the
        # trusted PATH/config allowlist.  Remove only GCM's explicit Never switch so
        # its already-approved helper may launch its normal GUI/browser authorization.
        environment.pop("GCM_INTERACTIVE", None)
        return environment

    def _execution_policy(self) -> list[str]:
        policy = super()._execution_policy()
        return [
            "credential.interactive=true"
            if item == "credential.interactive=false"
            else item
            for item in policy
        ]

    def _run(self, args: list[str], *, cwd: Path) -> str:
        returncode, stdout, stderr = self._exec(args, cwd=cwd)
        output = stdout.decode("utf-8", errors="replace")
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").lower()
            if "ls-remote" in args and returncode == 2 and not output:
                code = "GIT_BRANCH_NOT_FOUND"
            elif "not a valid branch name" in detail or "invalid branch name" in detail:
                code = "GIT_BRANCH_INVALID"
            elif (
                ("remote branch" in detail and "not found" in detail)
                or "couldn't find remote ref" in detail
                or "could not find remote ref" in detail
            ):
                # The single-auth flow lets clone/fetch prove branch existence instead
                # of opening a separate ls-remote authentication opportunity.
                code = "GIT_BRANCH_NOT_FOUND"
            elif any(
                term in detail
                for term in (
                    "host key verification failed",
                    "remote host identification has changed",
                    "no matching host key type found",
                )
            ):
                code = "GIT_SSH_HOST_KEY_FAILED"
            elif any(
                term in detail
                for term in (
                    "authentication failed",
                    "terminal prompts disabled",
                    "could not read username",
                    "cannot prompt because user interactivity has been disabled",
                    "unable to get password from user",
                    "permission denied (publickey",
                    "no supported authentication methods available",
                    "http 401",
                    "http 403",
                )
            ):
                code = "GIT_AUTH_FAILED"
            elif "repository not found" in detail or "does not appear to be a git repository" in detail:
                code = "GIT_REPOSITORY_NOT_FOUND"
            elif any(
                term in detail
                for term in (
                    "could not resolve host",
                    "could not resolve hostname",
                    "failed to connect",
                    "connection refused",
                    "network is unreachable",
                    "no route to host",
                    "connection timed out",
                    "operation timed out",
                )
            ):
                code = "GIT_NETWORK_UNAVAILABLE"
            else:
                code = "GIT_CHECK_FAILED"
            raise GitClientError(code)
        return output.strip()
