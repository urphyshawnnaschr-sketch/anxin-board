"""Fail-closed Git runtime admission for the formal Windows Product runtime."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Callable


MINIMUM_GIT_VERSION = (2, 45, 0)
MINIMUM_GIT_VERSION_TEXT = "2.45"
GIT_RUNTIME_CAPABILITY_ERROR = "GIT_RUNTIME_CAPABILITY_REQUIRED"
GIT_RUNTIME_CAPABILITY_MESSAGE = (
    "AnxinBoard 需要 Git for Windows 2.45 或更高版本，并且必须支持 --no-lazy-fetch。"
    "请先安装或升级 Git，然后重新启动 AnxinBoard。"
)
_VERSION_RE = re.compile(r"^git version ([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:\.|\s|$)")


class GitRuntimeCapabilityError(RuntimeError):
    code = GIT_RUNTIME_CAPABILITY_ERROR

    def __init__(self) -> None:
        self.user_message = GIT_RUNTIME_CAPABILITY_MESSAGE
        super().__init__(self.user_message)


@dataclass(frozen=True, slots=True)
class GitRuntimeCapability:
    executable: str
    version: tuple[int, int, int]
    version_text: str
    no_lazy_fetch: bool = True


def _probe_environment() -> dict[str, str]:
    """Keep ordinary OS state but remove inherited Git behavior controls."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith(("git_", "gcm_", "ssh_"))
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "SSH_ASKPASS_REQUIRE": "never",
        }
    )
    return environment


def _run_probe(
    executable: str,
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            [executable, *args],
            cwd=os.fspath(Path(executable).resolve(strict=True).parent),
            env=_probe_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitRuntimeCapabilityError() from exc


def require_git_runtime_capability(
    git_path: str | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> GitRuntimeCapability:
    """Prove Git >=2.45 and the exact no-lazy-fetch control without repository/network access."""
    executable = git_path if git_path is not None else which("git")
    if not executable:
        raise GitRuntimeCapabilityError()
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError as exc:
        raise GitRuntimeCapabilityError() from exc
    if not resolved.is_file():
        raise GitRuntimeCapabilityError()
    executable = os.fspath(resolved)

    version_probe = _run_probe(executable, ["--version"], runner=runner)
    if version_probe.returncode != 0:
        raise GitRuntimeCapabilityError()
    version_text = version_probe.stdout.strip()
    match = _VERSION_RE.match(version_text)
    if match is None:
        raise GitRuntimeCapabilityError()
    version = tuple(int(value or "0") for value in match.groups())
    if version < MINIMUM_GIT_VERSION:
        raise GitRuntimeCapabilityError()

    capability_probe = _run_probe(
        executable,
        ["--no-lazy-fetch", "--version"],
        runner=runner,
    )
    if capability_probe.returncode != 0 or not capability_probe.stdout.startswith("git version "):
        raise GitRuntimeCapabilityError()

    return GitRuntimeCapability(
        executable=executable,
        version=version,
        version_text=version_text,
    )
