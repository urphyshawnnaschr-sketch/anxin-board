"""受控 Git 子进程与项目工作区边界；不访问 FastAPI 或 SQLite。"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

if os.name == "nt":
    from ctypes import wintypes


GIT_ERROR_SUMMARIES = {
    "GIT_NOT_AVAILABLE": "本机未找到可用的 Git，请先安装 Git 后重试。",
    "GIT_BRANCH_INVALID": "活动分支格式无效，请修正并保存后重试。",
    "GIT_BRANCH_NOT_FOUND": "远端不存在该活动分支，请检查分支名称后重试。",
    "GIT_AUTH_FAILED": "Git 认证失败，请先在本机配置可非交互读取的 HTTPS 凭据。",
    "GIT_REPOSITORY_NOT_FOUND": "Git 仓库不存在或当前账号无权访问，请检查地址与权限。",
    "GIT_NETWORK_UNAVAILABLE": "当前无法访问 Git 服务，请检查网络后重试。",
    "GIT_COMMAND_TIMEOUT": "Git 操作超时并已终止，请检查网络或仓库大小后重试。",
    "GIT_WORKSPACE_CONFLICT": "项目工作区身份不匹配或已有未知内容，未执行覆盖，请人工检查。",
    "GIT_WORKSPACE_ESCAPE": "项目工作区路径不安全，已阻止访问。",
    "GIT_WORKSPACE_DIRTY": "项目工作区存在未提交改动，未执行更新，请先人工处理。",
    "GIT_WORKSPACE_TOO_LARGE": "项目工作区文件总量超过 1 GB，已停止建立连接。",
    "GIT_CHECK_FAILED": "Git 连接检查失败，请检查配置后重试。",
}


STDERR_SUMMARY_LIMIT = 64 * 1024


class GitClientError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        self.summary = GIT_ERROR_SUMMARIES.get(code, GIT_ERROR_SUMMARIES["GIT_CHECK_FAILED"])
        super().__init__(self.summary)


if os.name == "nt":
    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _create_kill_on_close_job(process: subprocess.Popen) -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _JobExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    process_handle = getattr(process, "_handle", None)
    assigned = configured and process_handle is not None and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(process_handle)
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


TASKKILL_FALLBACK_TIMEOUT_SECONDS = 2
WATCHDOG_SETTLE_SECONDS = 4


def _remaining_timeout(deadline: float | None, ceiling: float) -> float:
    """Return only budget that remains inside the caller's absolute lifecycle deadline."""
    if deadline is None:
        return max(0.0, ceiling)
    return max(0.0, min(ceiling, deadline - time.monotonic()))


def _terminate_job(job: int | None) -> bool:
    """Terminate a Windows Job Object without closing its handle.

    The watchdog can run concurrently with its owning thread, so only the owner
    may close the raw Job handle after the watchdog has settled.
    """
    if os.name != "nt" or job is None:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    try:
        return bool(kernel32.TerminateJobObject(wintypes.HANDLE(job), 1))
    except (OSError, ValueError):
        return False


def _close_job(job: int | None) -> None:
    if os.name != "nt" or job is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        kernel32.CloseHandle(wintypes.HANDLE(job))
    except (OSError, ValueError):
        pass


def _fallback_kill_process_tree(
    process: subprocess.Popen, deadline: float | None = None
) -> None:
    """Best-effort fallback without starting waits beyond the caller deadline."""
    if os.name == "nt" and process.poll() is None:
        taskkill_timeout = _remaining_timeout(deadline, TASKKILL_FALLBACK_TIMEOUT_SECONDS)
        if taskkill_timeout > 0:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                    timeout=taskkill_timeout,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _settle_timer(
    timer: threading.Timer | None, deadline: float | None = None
) -> bool:
    """Cancel the watchdog and wait only inside the caller's remaining deadline."""
    if timer is None:
        return True
    timer.cancel()
    if timer is threading.current_thread():
        return False
    wait_timeout = _remaining_timeout(deadline, WATCHDOG_SETTLE_SECONDS)
    if wait_timeout > 0:
        timer.join(timeout=wait_timeout)
    return not timer.is_alive()


def _deferred_close_job_after_timer(timer: threading.Timer, job: int) -> None:
    """Wait outside the caller deadline, then close the raw Job handle exactly once."""
    timer.join()
    _close_job(job)


def _finalize_job_handle(
    job: int | None,
    timer: threading.Timer | None,
    deadline: float | None,
) -> threading.Thread | None:
    """Keep caller cleanup bounded while preserving total single-owner handle disposal."""
    if job is None:
        _settle_timer(timer, deadline)
        return None
    if _settle_timer(timer, deadline):
        _close_job(job)
        return None
    if timer is None:
        _close_job(job)
        return None
    reaper = threading.Thread(
        target=_deferred_close_job_after_timer,
        args=(timer, job),
        name="anxinboard-git-job-close-reaper",
        daemon=True,
    )
    reaper.start()
    return reaper


def _timeout_handler(
    timed_out: threading.Event,
    job: int | None,
    process: subprocess.Popen,
    deadline: float | None = None,
) -> None:
    """Watchdog: mark timeout and terminate the tree; never close the Job handle."""
    timed_out.set()
    if not _terminate_job(job):
        _fallback_kill_process_tree(process, deadline)
    elif process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


class _StderrSummaryReader:
    """后台持续消费 stderr 直到 EOF；只保留最后 limit 字节作错误摘要。

    超过 limit 的内容继续读取并丢弃，防止管道填满阻塞子进程，也不无限占用内存。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.tail = bytearray()

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(128 * 1024)
                if not chunk:
                    break
                self.tail.extend(chunk)
                if len(self.tail) > self.limit:
                    del self.tail[: len(self.tail) - self.limit]
        except (OSError, ValueError):
            return


@dataclass(frozen=True)
class WorkspacePaths:
    approved_ancestor: Path
    root: Path
    project: Path
    repo: Path
    temporary: Path
    checkout: Path
    ownership_marker: Path


@dataclass(frozen=True)
class AttemptWorkspaceOwnership:
    attempt_id: str
    token: str
    temporary_identity: tuple[int, int]
    checkout_identity: tuple[int, int]
    marker_identity: tuple[int, int]
    temporary_lock: int | None = None
    checkout_lock: int | None = None
    marker_lock: int | None = None


@dataclass
class WorkspaceAccess:
    """把 Git 工作树路径绑定到已验证身份；GitClient 不接受裸路径执行工作树操作。"""

    paths: WorkspacePaths
    path: Path
    expected_url: str
    identity: tuple[int, int]
    attempt_ownership: AttemptWorkspaceOwnership | None
    directory_lock: int | None = None

    def close(self) -> None:
        _close_identity_lock(self.directory_lock)
        self.directory_lock = None


_APPROVED_CREDENTIAL_HELPERS = frozenset({"manager", "manager-core", "wincred"})


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _absolute_without_resolve(path: Path) -> Path:
    """规范化绝对路径但保留 symlink/junction/reparse point 身份。"""
    return Path(os.path.abspath(os.fspath(path)))


def _validate_existing_components(approved_ancestor: Path, target: Path) -> None:
    """在 resolve 前逐个检查批准祖先到目标之间已存在的原始组件。"""
    ancestor = _absolute_without_resolve(approved_ancestor)
    candidate = _absolute_without_resolve(target)
    if not _is_within(candidate, ancestor):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    current = ancestor
    components = [current]
    for part in candidate.relative_to(ancestor).parts:
        current = current / part
        components.append(current)
    for current in components:
        try:
            if _is_reparse(current):
                raise GitClientError("GIT_WORKSPACE_ESCAPE")
            if current.exists() and not current.is_dir():
                raise GitClientError("GIT_WORKSPACE_ESCAPE")
        except OSError as exc:
            raise GitClientError("GIT_WORKSPACE_ESCAPE") from exc


def _validate_workspace_target(
    approved_ancestor: Path,
    root: Path,
    target: Path,
) -> None:
    ancestor = _absolute_without_resolve(approved_ancestor)
    raw_root = _absolute_without_resolve(root)
    candidate = _absolute_without_resolve(target)
    if not _is_within(raw_root, ancestor) or not _is_within(candidate, raw_root):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    _validate_existing_components(ancestor, candidate)
    resolved_ancestor = ancestor.resolve(strict=False)
    resolved_root = raw_root.resolve(strict=False)
    resolved_target = candidate.resolve(strict=False)
    if not _is_within(resolved_root, resolved_ancestor) or not _is_within(
        resolved_target, resolved_root
    ):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")


def _approved_projects_root() -> tuple[Path, Path]:
    """返回未 resolve 的批准祖先与工作区根，供后续每次复检原始链。"""
    override = os.environ.get("ANXINBOARD_PROJECTS_ROOT")
    if override:
        configured = Path(override)
        if not configured.is_absolute() or ".." in configured.parts:
            raise GitClientError("GIT_WORKSPACE_ESCAPE")
        approved_ancestor = _absolute_without_resolve(Path(tempfile.gettempdir()))
        root = _absolute_without_resolve(configured)
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise GitClientError("GIT_WORKSPACE_ESCAPE")
        configured = Path(local_app_data)
        if not configured.is_absolute() or ".." in configured.parts:
            raise GitClientError("GIT_WORKSPACE_ESCAPE")
        approved_ancestor = _absolute_without_resolve(configured)
        root = approved_ancestor / "AnxinBoard" / "projects"

    _validate_workspace_target(approved_ancestor, root, root)
    resolved = root.resolve(strict=False)
    source_root = Path.cwd().resolve(strict=False)
    if _is_within(resolved, source_root):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    return approved_ancestor, root


def validate_workspace_paths(paths: WorkspacePaths) -> None:
    """复检工作区原始路径链；不得用 resolve 后的路径替代此检查。"""
    for target in (
        paths.root,
        paths.project,
        paths.repo,
        paths.temporary,
        paths.checkout,
    ):
        _validate_workspace_target(paths.approved_ancestor, paths.root, target)


def resolve_workspace_paths(project_id: int, attempt_id: str, *, create: bool) -> WorkspacePaths:
    if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")

    approved_ancestor, root = _approved_projects_root()
    project = root / str(project_id)
    repo = project / "repo"
    temporary = project / f".repo-clone-{attempt_id}"
    checkout = temporary / "checkout"
    ownership_marker = temporary / ".anxinboard-attempt-owner"
    for target in (root, project, repo, temporary, checkout):
        _validate_workspace_target(approved_ancestor, root, target)
    if root.exists() and (not root.is_dir() or _is_reparse(root)):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    if project.exists() and (not project.is_dir() or _is_reparse(project)):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        _validate_workspace_target(approved_ancestor, root, project)
        project.mkdir(exist_ok=True)
        _validate_workspace_target(approved_ancestor, root, project)
    paths = WorkspacePaths(
        approved_ancestor=approved_ancestor,
        root=root,
        project=project,
        repo=repo,
        temporary=temporary,
        checkout=checkout,
        ownership_marker=ownership_marker,
    )
    validate_workspace_paths(paths)
    return paths


def _identity(path: Path, *, directory: bool) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc
    if _is_reparse(path) or (directory and not path.is_dir()) or (
        not directory and not path.is_file()
    ):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    return info.st_dev, info.st_ino


def _open_identity_lock(path: Path, *, directory: bool) -> int | None:
    """持有 Windows identity handle，缩小普通 rename/delete 窗口；前后 identity 复检仍是判据。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = kernel32.CreateFileW(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        (0x00000001 | 0x00000002) if directory else 0x00000001,
        # 目录允许内容读写；配置文件只允许共享读；不授予普通 delete sharing。
        None,
        3,  # OPEN_EXISTING
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    return int(handle)


def _close_identity_lock(handle: int | None) -> None:
    if os.name != "nt" or handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _release_attempt_locks(ownership: AttemptWorkspaceOwnership) -> None:
    for handle in (
        ownership.marker_lock,
        ownership.checkout_lock,
        ownership.temporary_lock,
    ):
        _close_identity_lock(handle)
    object.__setattr__(ownership, "marker_lock", None)
    object.__setattr__(ownership, "checkout_lock", None)
    object.__setattr__(ownership, "temporary_lock", None)


def _parse_direct_git_config(path: Path) -> list[tuple[str, str | None, str, str]]:
    """解析本产品需要的简单配置形态；include 永不展开。"""
    try:
        info = path.lstat()
        if _is_reparse(path) or not path.is_file() or info.st_size > 64 * 1024:
            raise GitClientError("GIT_CHECK_FAILED")
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise GitClientError("GIT_CHECK_FAILED") from exc

    entries: list[tuple[str, str | None, str, str]] = []
    section: str | None = None
    subsection: str | None = None
    section_pattern = re.compile(
        r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"([^"\\]*)")?\s*\]$'
    )
    key_pattern = re.compile(r"^([A-Za-z][A-Za-z0-9.-]*)\s*(?:=\s*)?(.*)$")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        section_match = section_pattern.fullmatch(line)
        if section_match:
            section = section_match.group(1).lower()
            subsection = section_match.group(2)
            continue
        if section is None or raw_line[:1].isspace() and raw_line.rstrip().endswith("\\"):
            raise GitClientError("GIT_CHECK_FAILED")
        key_match = key_pattern.fullmatch(line)
        if not key_match:
            raise GitClientError("GIT_CHECK_FAILED")
        value = key_match.group(2).strip()
        if value.startswith('"') or value.endswith('"'):
            if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
                raise GitClientError("GIT_CHECK_FAILED")
            value = value[1:-1]
        entries.append((section, subsection, key_match.group(1).lower(), value))
    return entries


def _configured_credential_helpers(git_path: str | None) -> tuple[str, ...]:
    """只保留已批准的 Windows 非交互 helper 名称，不执行 include 或任意命令。"""
    candidates: list[Path] = []
    for variable in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        configured = os.environ.get(variable)
        if configured and configured != os.devnull:
            candidates.append(Path(configured))
    if git_path:
        try:
            git_root = Path(git_path).resolve(strict=False).parents[1]
            candidates.extend((git_root / "etc" / "gitconfig", git_root / "mingw64" / "etc" / "gitconfig"))
        except (IndexError, OSError):
            pass
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile) / ".gitconfig")
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        candidates.append(Path(xdg_config) / "git" / "config")

    approved: list[str] = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        path_key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        try:
            entries = _parse_direct_git_config(candidate)
        except GitClientError:
            continue
        for section, _subsection, key, value in entries:
            if section == "credential" and key == "helper" and value in _APPROVED_CREDENTIAL_HELPERS:
                if value not in approved:
                    approved.append(value)
    return tuple(approved)


def _resolved_credential_helper_commands(
    git_path: str | None, configured: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    if not git_path:
        return ()
    try:
        git_root = Path(git_path).resolve(strict=True).parents[1]
    except (IndexError, OSError):
        return ()
    relative_candidates = {
        "manager": (
            Path("mingw64/bin/git-credential-manager.exe"),
            Path("mingw64/libexec/git-core/git-credential-manager.exe"),
        ),
        "manager-core": (
            Path("mingw64/bin/git-credential-manager-core.exe"),
            Path("mingw64/libexec/git-core/git-credential-manager-core.exe"),
        ),
        "wincred": (Path("mingw64/libexec/git-core/git-credential-wincred.exe"),),
    }
    commands: list[tuple[str, str]] = []
    for helper in configured:
        for relative in relative_candidates.get(helper, ()):
            candidate = git_root / relative
            try:
                resolved = candidate.resolve(strict=True)
                if (
                    candidate.is_file()
                    and not _is_reparse(candidate)
                    and _is_within(resolved, git_root.resolve(strict=True))
                ):
                    # credential.helper values are helper names, not executable paths:
                    # Git prepends ``git credential-`` to non-shell values.  Keep the
                    # approved name and separately pin PATH to this validated directory.
                    commands.append((helper, os.fspath(resolved.parent)))
                    break
            except OSError:
                continue
    return tuple(commands)


def _assert_safe_local_config(repo_path: Path, expected_url: str) -> Path:
    config_path = repo_path / ".git" / "config"
    try:
        entries = _parse_direct_git_config(config_path)
    except GitClientError as exc:
        raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc
    origin_urls: list[str] = []
    allowed_core = {
        "repositoryformatversion",
        "filemode",
        "bare",
        "logallrefupdates",
        "symlinks",
        "ignorecase",
        "precomposeunicode",
    }
    for section, subsection, key, value in entries:
        if section == "core" and subsection is None and key in allowed_core:
            if key == "bare" and value.lower() not in {"false", "no", "0"}:
                raise GitClientError("GIT_WORKSPACE_CONFLICT")
            continue
        if section == "remote" and subsection == "origin":
            if key == "url":
                origin_urls.append(value)
                continue
            if key == "fetch" and re.fullmatch(
                r"\+refs/heads/[^\s:]+:refs/remotes/origin/[^\s:]+", value
            ):
                continue
            if key == "tagopt" and value == "--no-tags":
                continue
        # A repository-scoped GitHub account name selects an existing GCM account.
        # Never admit helpers, passwords, tokens, or broader host/global bindings here.
        if (section == "credential" and subsection == expected_url
                and expected_url.startswith("https://github.com/")
                and key == "username"
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", value)):
            continue
        if section == "branch" and subsection and key in {"remote", "merge"}:
            if key == "remote" and value == "origin":
                continue
            if key == "merge" and re.fullmatch(r"refs/heads/[^\s]+", value):
                continue
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    origin_matches = origin_urls == [expected_url]
    if not expected_url.startswith("https://") and len(origin_urls) == 1:
        try:
            origin_matches = os.path.normcase(os.path.abspath(origin_urls[0])) == os.path.normcase(
                os.path.abspath(expected_url)
            )
        except OSError:
            origin_matches = False
    if not origin_matches:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    return config_path


@contextmanager
def _locked_safe_local_config(repo_path: Path, expected_url: str) -> Iterator[None]:
    config_path = _assert_safe_local_config(repo_path, expected_url)
    handle = _open_identity_lock(config_path, directory=False)
    try:
        _assert_safe_local_config(repo_path, expected_url)
        yield
        _assert_safe_local_config(repo_path, expected_url)
    finally:
        _close_identity_lock(handle)


def _ownership_payload(
    attempt_id: str,
    token: str,
    temporary_identity: tuple[int, int],
    checkout_identity: tuple[int, int],
) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "attempt_id": attempt_id,
            "token": token,
            "temporary_identity": list(temporary_identity),
            "checkout_identity": list(checkout_identity),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def reserve_attempt_workspace(
    paths: WorkspacePaths,
    attempt_id: str,
) -> AttemptWorkspaceOwnership:
    """原子占有本 attempt 的 clone 容器；失败时不接管任何已存在路径。"""
    validate_workspace_paths(paths)
    if paths.temporary.name != f".repo-clone-{attempt_id}":
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    try:
        paths.temporary.mkdir(mode=0o700, exist_ok=False)
        _validate_workspace_target(paths.approved_ancestor, paths.root, paths.temporary)
        temporary_identity = _identity(paths.temporary, directory=True)
        paths.checkout.mkdir(mode=0o700, exist_ok=False)
        _validate_workspace_target(paths.approved_ancestor, paths.root, paths.checkout)
        checkout_identity = _identity(paths.checkout, directory=True)
        token = secrets.token_hex(32)
        payload = _ownership_payload(
            attempt_id,
            token,
            temporary_identity,
            checkout_identity,
        )
        descriptor = os.open(
            paths.ownership_marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as marker:
            marker.write(payload)
            marker.flush()
            os.fsync(marker.fileno())
            marker_info = os.fstat(marker.fileno())
        locks: list[int | None] = []
        try:
            locks.append(_open_identity_lock(paths.temporary, directory=True))
            locks.append(_open_identity_lock(paths.checkout, directory=True))
            locks.append(_open_identity_lock(paths.ownership_marker, directory=False))
        except GitClientError:
            for handle in reversed(locks):
                _close_identity_lock(handle)
            raise
        ownership = AttemptWorkspaceOwnership(
            attempt_id=attempt_id,
            token=token,
            temporary_identity=temporary_identity,
            checkout_identity=checkout_identity,
            marker_identity=(marker_info.st_dev, marker_info.st_ino),
            temporary_lock=locks[0],
            checkout_lock=locks[1],
            marker_lock=locks[2],
        )
        try:
            _assert_attempt_workspace_owned(paths, ownership)
            return ownership
        except GitClientError:
            _release_attempt_locks(ownership)
            raise
    except FileExistsError as exc:
        raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc
    except GitClientError:
        raise
    except OSError as exc:
        raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc


def _read_owned_marker(
    paths: WorkspacePaths,
    ownership: AttemptWorkspaceOwnership,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(paths.ownership_marker, flags)
        with os.fdopen(descriptor, "rb") as marker:
            marker_info = os.fstat(marker.fileno())
            if (marker_info.st_dev, marker_info.st_ino) != ownership.marker_identity:
                raise GitClientError("GIT_WORKSPACE_CONFLICT")
            if marker_info.st_size > 4096:
                raise GitClientError("GIT_WORKSPACE_CONFLICT")
            return marker.read(4097)
    except GitClientError:
        raise
    except OSError as exc:
        raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc


def _assert_attempt_workspace_owned(
    paths: WorkspacePaths,
    ownership: AttemptWorkspaceOwnership,
) -> None:
    validate_workspace_paths(paths)
    if (
        paths.temporary.parent != paths.project
        or paths.temporary.name != f".repo-clone-{ownership.attempt_id}"
        or paths.checkout.parent != paths.temporary
        or paths.checkout.name != "checkout"
        or paths.ownership_marker.parent != paths.temporary
        or paths.ownership_marker.name != ".anxinboard-attempt-owner"
    ):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    if _identity(paths.temporary, directory=True) != ownership.temporary_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    if _identity(paths.checkout, directory=True) != ownership.checkout_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    if _is_reparse(paths.ownership_marker):
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    expected = _ownership_payload(
        ownership.attempt_id,
        ownership.token,
        ownership.temporary_identity,
        ownership.checkout_identity,
    )
    if not secrets.compare_digest(_read_owned_marker(paths, ownership), expected):
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    if _identity(paths.temporary, directory=True) != ownership.temporary_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    if _identity(paths.checkout, directory=True) != ownership.checkout_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")


def open_workspace_access(
    paths: WorkspacePaths,
    expected_url: str,
    ownership: AttemptWorkspaceOwnership | None = None,
) -> WorkspaceAccess:
    validate_workspace_paths(paths)
    target = paths.checkout if ownership is not None else paths.repo
    if ownership is not None:
        _assert_attempt_workspace_owned(paths, ownership)
        identity = ownership.checkout_identity
        directory_lock = None
    else:
        identity = _identity(target, directory=True)
        directory_lock = _open_identity_lock(target, directory=True)
    access = WorkspaceAccess(
        paths=paths,
        path=target,
        expected_url=expected_url,
        identity=identity,
        attempt_ownership=ownership,
        directory_lock=directory_lock,
    )
    _assert_workspace_access(access)
    return access


def _assert_workspace_access(access: WorkspaceAccess) -> None:
    validate_workspace_paths(access.paths)
    expected_path = (
        access.paths.checkout
        if access.attempt_ownership is not None
        else access.paths.repo
    )
    if access.path != expected_path:
        raise GitClientError("GIT_WORKSPACE_ESCAPE")
    if access.attempt_ownership is not None:
        _assert_attempt_workspace_owned(access.paths, access.attempt_ownership)
    if _identity(access.path, directory=True) != access.identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")


def cleanup_attempt_workspace(
    paths: WorkspacePaths,
    ownership: AttemptWorkspaceOwnership | None,
) -> None:
    if ownership is None:
        return
    try:
        _assert_attempt_workspace_owned(paths, ownership)
    except GitClientError:
        _release_attempt_locks(ownership)
        raise
    _release_attempt_locks(ownership)
    _assert_attempt_workspace_owned(paths, ownership)

    def remove_readonly(function, path, _error_info):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(paths.temporary, onerror=remove_readonly)


def promote_attempt_workspace(
    paths: WorkspacePaths,
    ownership: AttemptWorkspaceOwnership,
) -> None:
    _assert_attempt_workspace_owned(paths, ownership)
    _validate_workspace_target(paths.approved_ancestor, paths.root, paths.temporary)
    _validate_workspace_target(paths.approved_ancestor, paths.root, paths.repo)
    if paths.repo.exists():
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    _close_identity_lock(ownership.checkout_lock)
    object.__setattr__(ownership, "checkout_lock", None)
    _assert_attempt_workspace_owned(paths, ownership)
    paths.checkout.rename(paths.repo)
    if _identity(paths.repo, directory=True) != ownership.checkout_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    _validate_workspace_target(paths.approved_ancestor, paths.root, paths.repo)
    if _identity(paths.temporary, directory=True) != ownership.temporary_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    if not secrets.compare_digest(
        _read_owned_marker(paths, ownership),
        _ownership_payload(
            ownership.attempt_id,
            ownership.token,
            ownership.temporary_identity,
            ownership.checkout_identity,
        ),
    ):
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    _close_identity_lock(ownership.marker_lock)
    object.__setattr__(ownership, "marker_lock", None)
    if _identity(paths.ownership_marker, directory=False) != ownership.marker_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    paths.ownership_marker.unlink()
    _close_identity_lock(ownership.temporary_lock)
    object.__setattr__(ownership, "temporary_lock", None)
    if _identity(paths.temporary, directory=True) != ownership.temporary_identity:
        raise GitClientError("GIT_WORKSPACE_CONFLICT")
    paths.temporary.rmdir()


_NUMSTAT_NUMBER = re.compile(r"\d+")


def _parse_numstat_line(line: str) -> tuple[str, str, str] | None:
    """解析单行 numstat；空白行返回 None，非空非法行抛出 GIT_CHECK_FAILED。

    合法形态只有 ``数字<TAB>数字<TAB>路径`` 与 ``-<TAB>-<TAB>路径``；
    added/deleted 必须为十进制非负整数，或两者同为 ``-``（二进制文件）。
    """
    if not line.strip():
        return None
    parts = line.split("\t")
    if len(parts) < 3:
        raise GitClientError("GIT_CHECK_FAILED")
    added_raw, deleted_raw = parts[0], parts[1]
    path = "\t".join(parts[2:])
    if added_raw == "-" or deleted_raw == "-":
        if added_raw != "-" or deleted_raw != "-":
            raise GitClientError("GIT_CHECK_FAILED")
        if not path:
            raise GitClientError("GIT_CHECK_FAILED")
        return added_raw, deleted_raw, path
    if not _NUMSTAT_NUMBER.fullmatch(added_raw) or not _NUMSTAT_NUMBER.fullmatch(deleted_raw):
        raise GitClientError("GIT_CHECK_FAILED")
    if not path:
        raise GitClientError("GIT_CHECK_FAILED")
    return added_raw, deleted_raw, path


def _parse_numstat_lines(lines: list[str]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in lines:
        parsed = _parse_numstat_line(line)
        if parsed is not None:
            rows.append(parsed)
    return rows


class GitClient:
    """只开放任务书白名单中的只读远端与受控本地更新动作。"""

    def __init__(
        self,
        *,
        git_path: str | None = None,
        timeout_seconds: float = 30,
        size_limit: int = 1024**3,
        allow_local_file: bool = False,
    ):
        self.git_path = git_path if git_path is not None else shutil.which("git")
        self.timeout_seconds = timeout_seconds
        self.size_limit = size_limit
        self.allowed_protocols = "https:file" if allow_local_file else "https"
        self.command_cwd = Path(tempfile.gettempdir()).resolve()
        self.credential_helpers = _configured_credential_helpers(self.git_path)
        resolved_helpers = _resolved_credential_helper_commands(
            self.git_path, self.credential_helpers
        )
        self.credential_helper_commands = tuple(name for name, _path in resolved_helpers)
        self.credential_helper_directories = tuple(
            dict.fromkeys(path for _name, path in resolved_helpers)
        )

    def _environment(self) -> dict[str, str]:
        # Git exposes many behavior-changing environment variables.  Inherit
        # ordinary OS state, but make the Git namespace default-deny before
        # adding the fixed values required by this client.  casefold() matters
        # on Windows, where environment variable names are case-insensitive.
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
                "GIT_PROTOCOL_FROM_USER": "0",
                "GIT_ALLOW_PROTOCOL": self.allowed_protocols,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
            }
        )
        if os.name == "nt" and self.credential_helper_directories:
            trusted_path: list[str] = list(self.credential_helper_directories)
            try:
                git_path = Path(self.git_path or "").resolve(strict=True)
                git_root = git_path.parents[1]
                trusted_path.extend(
                    os.fspath(path)
                    for path in (
                        git_path.parent,
                        git_root / "mingw64" / "bin",
                        git_root / "mingw64" / "libexec" / "git-core",
                        git_root / "usr" / "bin",
                    )
                    if path.is_dir() and not _is_reparse(path)
                )
            except (IndexError, OSError):
                pass
            system_root = os.environ.get("SystemRoot")
            if system_root:
                for path in (Path(system_root), Path(system_root) / "System32"):
                    if path.is_dir() and not _is_reparse(path):
                        trusted_path.append(os.fspath(path))
            environment["PATH"] = os.pathsep.join(dict.fromkeys(trusted_path))
            environment["NoDefaultCurrentDirectoryInExePath"] = "1"
        return environment

    def _execution_policy(self) -> list[str]:
        policy = [
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-c",
            "credential.interactive=false",
        ]
        for helper in self.credential_helper_commands:
            policy.extend(("-c", f"credential.helper={helper}"))
        return policy

    @staticmethod
    def _http_redirect_policy(url: str) -> list[str]:
        policy = ["-c", "http.followRedirects=false"]
        if url.startswith("https://"):
            if re.search(r"[\s\x00-\x1f\x7f]", url):
                raise GitClientError("GIT_CHECK_FAILED")
            policy.extend(["-c", f"http.{url}.followRedirects=false"])
        return policy

    def _assert_effective_https_url(
        self,
        approved_url: str,
        *,
        cwd: Path,
        repository: str | None = None,
    ) -> None:
        if not approved_url.startswith("https://"):
            return
        args = ["ls-remote", "--get-url", repository or approved_url]
        if repository is not None:
            args = ["-C", str(cwd), *args]
            with _locked_safe_local_config(cwd, approved_url):
                effective_url = self._run(args, cwd=cwd)
        else:
            effective_url = self._run(args, cwd=cwd)
        if effective_url != approved_url:
            raise GitClientError("GIT_CHECK_FAILED")

    def _exec(
        self, args: list[str], *, cwd: Path, input_data: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        """Run one controlled Git command inside one absolute wall-clock deadline."""
        # Exact bytes avoids subclass length overrides bypassing this input bound.
        if input_data is not None and (
            type(input_data) is not bytes or len(input_data) > 64 * 1024
        ):
            raise GitClientError("GIT_CHECK_FAILED")
        if not self.git_path:
            raise GitClientError("GIT_NOT_AVAILABLE")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise GitClientError("GIT_CHECK_FAILED")
        environment = self._environment()
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                [self.git_path, *self._execution_policy(), *args],
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL if input_data is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
            )
        except (FileNotFoundError, OSError) as exc:
            raise GitClientError("GIT_NOT_AVAILABLE") from exc
        job = _create_kill_on_close_job(process)
        deadline = time.monotonic() + self.timeout_seconds
        timed_out = threading.Event()
        watchdog = threading.Timer(
            _remaining_timeout(deadline, self.timeout_seconds),
            _timeout_handler,
            args=[timed_out, job, process, deadline],
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                if input_data is None:
                    stdout, stderr = process.communicate(
                        timeout=_remaining_timeout(deadline, self.timeout_seconds)
                    )
                else:
                    stdout, stderr = process.communicate(
                        input=input_data,
                        timeout=_remaining_timeout(deadline, self.timeout_seconds),
                    )
            except subprocess.TimeoutExpired as exc:
                timed_out.set()
                self._discard_children(process, job, deadline)
                raise GitClientError("GIT_COMMAND_TIMEOUT") from exc
            if timed_out.is_set():
                self._discard_children(process, job, deadline)
                raise GitClientError("GIT_COMMAND_TIMEOUT")
            return process.returncode, stdout, stderr
        finally:
            _finalize_job_handle(job, watchdog, deadline)

    def _run(self, args: list[str], *, cwd: Path) -> str:
        returncode, stdout, stderr = self._exec(args, cwd=cwd)
        output = stdout.decode("utf-8", errors="replace")
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").lower()
            if "ls-remote" in args and returncode == 2 and not output:
                code = "GIT_BRANCH_NOT_FOUND"
            elif "not a valid branch name" in detail or "invalid branch name" in detail:
                code = "GIT_BRANCH_INVALID"
            elif any(term in detail for term in ("authentication failed", "terminal prompts disabled", "could not read username", "cannot prompt because user interactivity has been disabled", "unable to get password from user", "http 401", "http 403")):
                code = "GIT_AUTH_FAILED"
            elif "repository not found" in detail or "does not appear to be a git repository" in detail:
                code = "GIT_REPOSITORY_NOT_FOUND"
            elif any(term in detail for term in ("could not resolve host", "failed to connect", "network is unreachable", "connection timed out")):
                code = "GIT_NETWORK_UNAVAILABLE"
            else:
                code = "GIT_CHECK_FAILED"
            raise GitClientError(code)
        return output.strip()

    def _discard_children(
        self,
        process: subprocess.Popen,
        job: int | None = None,
        deadline: float | None = None,
    ) -> bool:
        """Best-effort tree cleanup that never starts a blocking wait after deadline."""
        if not _terminate_job(job):
            _fallback_kill_process_tree(process, deadline)
        elif process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

        first_wait = _remaining_timeout(deadline, WATCHDOG_SETTLE_SECONDS)
        if first_wait <= 0:
            return process.poll() is not None
        try:
            process.wait(timeout=first_wait)
            return True
        except (OSError, subprocess.TimeoutExpired):
            _fallback_kill_process_tree(process, deadline)

        second_wait = _remaining_timeout(deadline, TASKKILL_FALLBACK_TIMEOUT_SECONDS)
        if second_wait <= 0:
            return process.poll() is not None
        try:
            process.wait(timeout=second_wait)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _run_bounded_process(
        self,
        args: list[str],
        *,
        cwd: Path,
        line_limit: int | None = None,
        byte_limit: int | None = None,
        timeout_seconds: int | None = None,
        parse_line: bool = False,
    ) -> tuple[list[str] | int, bool]:
        """读取 Git 命令 stdout，最多 line_limit 行或 byte_limit 字节；超限即停止。

        line_limit 与 byte_limit 不能同时设置（互斥）。
        byte_limit 模式下返回 (bytes_read, over)，总字节数始终 <= byte_limit + 1。
        line_limit 模式下返回 (lines, over)，lines 始终不超过 line_limit 条；
        第 line_limit+1 条（含 EOF 时 carry 构成的最后一行）只用于判断超限，
        置 over=True 后立即终止读取，不进入返回列表。

        设置 timeout_seconds 时用 threading.Timer 作看门狗保护子进程全生命周期：
        覆盖 stdout 读取、stderr 消费与进程退出，timer 只在进程结束并完成资源
        回收后取消，stdout 提前 EOF 不会提前取消超时。
        进程退出后后代进程仍可能持有 stderr 管道，使 stderr 线程无法 EOF；
        stderr join 使用生命周期 deadline 的剩余时间，join 后再次检查 timed_out
        与线程存活，超时已触发或线程在允许时间内未结束时统一抛出
        GIT_COMMAND_TIMEOUT，不得返回伪成功。
        stderr 由独立后台线程持续消费直到 EOF，只保留最后 STDERR_SUMMARY_LIMIT
        字节摘要，防止管道填满阻塞子进程，也不无限占用内存。
        parse_line=True 时按 \n 拆分行（行模式），parse_line=False 时仅计数（字节模式）。

        调用方根据 line_limit / byte_limit 二选一后忽略另一返回值类型。
        """
        if line_limit is not None and byte_limit is not None:
            raise GitClientError("GIT_CHECK_FAILED")
        environment = self._environment()
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                [self.git_path, *self._execution_policy(), *args],
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
            )
        except (FileNotFoundError, OSError) as exc:
            raise GitClientError("GIT_NOT_AVAILABLE") from exc
        job = _create_kill_on_close_job(process)
        stderr_summary = _StderrSummaryReader(STDERR_SUMMARY_LIMIT)
        stderr_thread = threading.Thread(
            target=stderr_summary.drain,
            args=(process.stderr,),
            daemon=True,
        )
        stderr_thread.start()
        timed_out = threading.Event()
        timer: threading.Timer | None = None
        deadline: float | None = None
        if timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds
            timer = threading.Timer(
                _remaining_timeout(deadline, float(timeout_seconds)),
                _timeout_handler,
                args=[timed_out, job, process, deadline],
            )
            timer.daemon = True
            timer.start()
        try:
            if byte_limit is not None:
                result, over = self._read_bounded_bytes(process, byte_limit, timed_out)
            else:
                result, over = self._read_bounded_lines(
                    process, line_limit or 0, timed_out
                )
            if timed_out.is_set():
                raise GitClientError("GIT_COMMAND_TIMEOUT")
            if over:
                if not self._discard_children(process, job, deadline):
                    raise GitClientError("GIT_COMMAND_TIMEOUT")
                self._join_stderr_thread(stderr_thread, deadline)
                if timed_out.is_set() or (
                    deadline is not None and stderr_thread.is_alive()
                ):
                    raise GitClientError("GIT_COMMAND_TIMEOUT")
                process.stdout.close()
                process.stderr.close()
                return result, over
            wait_limit = self.timeout_seconds
            if deadline is not None:
                wait_limit = max(0.0, deadline - time.monotonic())
            returncode = process.wait(timeout=wait_limit)
            if timed_out.is_set():
                raise GitClientError("GIT_COMMAND_TIMEOUT")
            self._join_stderr_thread(stderr_thread, deadline)
            if timed_out.is_set() or (
                deadline is not None and stderr_thread.is_alive()
            ):
                raise GitClientError("GIT_COMMAND_TIMEOUT")
            process.stdout.close()
            process.stderr.close()
            if returncode != 0:
                detail = stderr_summary.tail.decode("utf-8", errors="replace").lower()
                if any(term in detail for term in ("bad revision", "unknown revision", "ambiguous argument")):
                    raise GitClientError("GIT_CHECK_FAILED")
                raise GitClientError("GIT_CHECK_FAILED")
            return result, over
        except GitClientError:
            self._abandon_bounded_process(process, stderr_thread, job, timer, deadline)
            raise
        except subprocess.TimeoutExpired as exc:
            self._abandon_bounded_process(process, stderr_thread, job, timer, deadline)
            raise GitClientError("GIT_COMMAND_TIMEOUT") from exc
        finally:
            _finalize_job_handle(job, timer, deadline)

    def _read_bounded_bytes(
        self,
        process: subprocess.Popen,
        byte_limit: int,
        timed_out: threading.Event,
    ) -> tuple[int, bool]:
        """字节模式：最多读取 byte_limit+1 字节，超出即置 over 并停止。"""
        total = 0
        over = False
        while True:
            if timed_out.is_set():
                raise GitClientError("GIT_COMMAND_TIMEOUT")
            to_read = min(128 * 1024, byte_limit + 1 - total)
            chunk = process.stdout.read(to_read)
            if not chunk:
                break
            total += len(chunk)
            if total > byte_limit:
                over = True
                break
        return total, over

    def _read_bounded_lines(
        self,
        process: subprocess.Popen,
        limit: int,
        timed_out: threading.Event,
    ) -> tuple[list[str], bool]:
        """行模式：最多返回 limit 条完整行，第 limit+1 条（含 carry 尾部）只置 over。"""
        lines: list[str] = []
        over = False
        carry = b""
        while True:
            if timed_out.is_set():
                raise GitClientError("GIT_COMMAND_TIMEOUT")
            chunk = process.stdout.read(128 * 1024)
            if not chunk:
                break
            data = carry + chunk
            parts = data.split(b"\n")
            carry = parts.pop()
            for part in parts:
                if limit > 0 and len(lines) >= limit:
                    over = True
                    break
                lines.append(part.decode("utf-8", errors="replace"))
            if over:
                break
        if not over and carry:
            if limit > 0 and len(lines) >= limit:
                over = True
            else:
                lines.append(carry.decode("utf-8", errors="replace"))
        return lines, over

    def _abandon_bounded_process(
        self,
        process: subprocess.Popen,
        stderr_thread: threading.Thread,
        job: int | None,
        timer: threading.Timer | None,
        deadline: float | None,
    ) -> None:
        """Clean up without opening any blocking window beyond the lifecycle deadline."""
        if timer is not None:
            timer.cancel()
        try:
            if process.poll() is None or stderr_thread.is_alive():
                self._discard_children(process, job, deadline)
            self._join_stderr_thread(stderr_thread, deadline)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        try:
            process.stdout.close()
            process.stderr.close()
        except (OSError, ValueError):
            pass

    def _join_stderr_thread(
        self, stderr_thread: threading.Thread, deadline: float | None
    ) -> None:
        """按生命周期 deadline 的剩余时间 join stderr 线程，不另开固定超时窗口。

        deadline 为 None（未启用看门狗）时沿用固定 10 秒等待。调用方必须在
        join 后再次检查 timed_out 与 stderr_thread.is_alive()，超时已触发或
        线程在允许时间内未结束时不得返回成功。
        """
        if deadline is None:
            stderr_thread.join(timeout=10)
            return
        remaining = deadline - time.monotonic()
        stderr_thread.join(timeout=max(0.0, remaining))

    def get_version(self) -> str:
        value = self._run(["--version"], cwd=self.command_cwd)
        if not value.startswith("git version "):
            raise GitClientError("GIT_CHECK_FAILED")
        return value

    def validate_branch(self, branch: str) -> None:
        if branch.startswith("-"):
            raise GitClientError("GIT_BRANCH_INVALID")
        try:
            self._run(["check-ref-format", "--branch", branch], cwd=self.command_cwd)
        except GitClientError as exc:
            if exc.code == "GIT_CHECK_FAILED":
                raise GitClientError("GIT_BRANCH_INVALID") from exc
            raise

    def get_remote_head(self, url: str, branch: str) -> str:
        self._assert_effective_https_url(url, cwd=self.command_cwd)
        output = self._run(
            [
                *self._http_redirect_policy(url),
                "ls-remote",
                "--exit-code",
                "--heads",
                url,
                f"refs/heads/{branch}",
            ],
            cwd=self.command_cwd,
        )
        first = output.split(maxsplit=1)[0] if output else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", first):
            raise GitClientError("GIT_BRANCH_NOT_FOUND")
        return first.lower()

    def clone_branch(self, url: str, branch: str, access: WorkspaceAccess) -> None:
        if access.attempt_ownership is None or access.expected_url != url:
            raise GitClientError("GIT_WORKSPACE_CONFLICT")
        _assert_workspace_access(access)
        temp_target = access.path
        if not temp_target.is_dir() or _is_reparse(temp_target):
            raise GitClientError("GIT_WORKSPACE_CONFLICT")
        try:
            if next(temp_target.iterdir(), None) is not None:
                raise GitClientError("GIT_WORKSPACE_CONFLICT")
        except OSError as exc:
            raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc
        if not temp_target.parent.is_dir():
            raise GitClientError("GIT_WORKSPACE_CONFLICT")
        self._assert_effective_https_url(url, cwd=temp_target.parent)
        _assert_workspace_access(access)
        self._run(
            [
                *self._http_redirect_policy(url),
                "clone",
                "--no-tags",
                "--single-branch",
                "--branch",
                branch,
                "--",
                url,
                str(temp_target),
            ],
            cwd=temp_target.parent,
        )
        _assert_workspace_access(access)
        _assert_safe_local_config(temp_target, url)

    def inspect_workspace(self, access: WorkspaceAccess) -> None:
        _assert_workspace_access(access)
        repo_path = access.path
        expected_url = access.expected_url
        if not repo_path.is_dir() or not (repo_path / ".git").is_dir():
            raise GitClientError("GIT_WORKSPACE_CONFLICT")
        if _is_reparse(repo_path) or _is_reparse(repo_path / ".git"):
            raise GitClientError("GIT_WORKSPACE_ESCAPE")
        with _locked_safe_local_config(repo_path, expected_url):
            origin = self._run(["-C", str(repo_path), "remote", "get-url", "origin"], cwd=repo_path)
        if origin != expected_url:
            raise GitClientError("GIT_WORKSPACE_CONFLICT")
        _assert_workspace_access(access)

    def assert_clean(self, access: WorkspaceAccess) -> None:
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            dirty = self._run(["-C", str(repo_path), "status", "--porcelain"], cwd=repo_path)
        _assert_workspace_access(access)
        if dirty:
            raise GitClientError("GIT_WORKSPACE_DIRTY")

    def fetch_branch(self, access: WorkspaceAccess, branch: str) -> None:
        _assert_workspace_access(access)
        repo_path = access.path
        expected_url = access.expected_url
        self._assert_effective_https_url(
            expected_url,
            cwd=repo_path,
            repository="origin",
        )
        _assert_workspace_access(access)
        with _locked_safe_local_config(repo_path, expected_url):
            self._run(
                [
                    *self._http_redirect_policy(expected_url),
                    "-C",
                    str(repo_path),
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                ],
                cwd=repo_path,
            )
        _assert_workspace_access(access)

    def checkout_remote_head(self, access: WorkspaceAccess, branch: str) -> None:
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            self._run(["-C", str(repo_path), "checkout", "--detach", f"refs/remotes/origin/{branch}"], cwd=repo_path)
        _assert_workspace_access(access)

    def get_local_head(self, access: WorkspaceAccess) -> str:
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            value = self._run(["-C", str(repo_path), "rev-parse", "HEAD"], cwd=repo_path).lower()
        _assert_workspace_access(access)
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise GitClientError("GIT_CHECK_FAILED")
        return value

    def get_remote_head_ref(self, access: WorkspaceAccess, branch: str) -> str:
        """读取已 fetch 的活动分支远端 ref 的当前 HEAD（纯本地读取，不访问网络）。"""
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            value = self._run(
                ["-C", str(repo_path), "rev-parse", f"refs/remotes/origin/{branch}"],
                cwd=repo_path,
            ).lower()
        _assert_workspace_access(access)
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise GitClientError("GIT_CHECK_FAILED")
        return value

    def is_ancestor(
        self,
        access: WorkspaceAccess,
        ancestor_commit: str,
        descendant_commit: str,
    ) -> bool:
        """判断 ancestor_commit 是否可达于 descendant_commit（merge-base --is-ancestor）。"""
        _assert_workspace_access(access)
        repo_path = access.path
        for commit in (ancestor_commit, descendant_commit):
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise GitClientError("GIT_CHECK_FAILED")
        with _locked_safe_local_config(repo_path, access.expected_url):
            returncode, stdout, stderr = self._exec(
                [
                    "-C",
                    str(repo_path),
                    "merge-base",
                    "--is-ancestor",
                    ancestor_commit,
                    descendant_commit,
                ],
                cwd=repo_path,
            )
        _assert_workspace_access(access)
        if returncode == 0:
            return True
        if returncode == 1:
            return False
        detail = (stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")).lower()
        if any(term in detail for term in ("unknown revision", "bad revision", "not a valid")):
            return False
        raise GitClientError("GIT_CHECK_FAILED")

    def list_commits(
        self,
        access: WorkspaceAccess,
        base_commit: str,
        head_commit: str,
    ) -> list[str]:
        """返回自 base_commit（不含）到 head_commit 的有序提交（先旧后新）。"""
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            value = self._run(
                [
                    "-C",
                    str(repo_path),
                    "rev-list",
                    "--reverse",
                    f"{base_commit}..{head_commit}",
                ],
                cwd=repo_path,
            )
        _assert_workspace_access(access)
        commits = [line.lower() for line in value.splitlines() if line]
        for commit in commits:
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise GitClientError("GIT_CHECK_FAILED")
        return commits

    def get_numstat(
        self,
        access: WorkspaceAccess,
        base_commit: str,
        head_commit: str,
    ) -> list[tuple[str, str, str]]:
        """返回 base_commit..head_commit 的 numstat 行：(added|'-', deleted|'-', path)。"""
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            value = self._run(
                [
                    "-C",
                    str(repo_path),
                    "diff",
                    "--numstat",
                    "--no-renames",
                    base_commit,
                    head_commit,
                ],
                cwd=repo_path,
            )
        _assert_workspace_access(access)
        return _parse_numstat_lines(value.splitlines())

    def count_commits(
        self,
        access: WorkspaceAccess,
        base_commit: str,
        head_commit: str,
    ) -> int:
        """返回 base_commit（不含）到 head_commit 的提交总数（不受上限截断）。

        使用 rev-list --count 只输出一个整数，不产生提交列表，避免内存开销。
        """
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            output = self._run(
                [
                    "-C",
                    str(repo_path),
                    "rev-list",
                    "--count",
                    f"{base_commit}..{head_commit}",
                ],
                cwd=repo_path,
            )
        _assert_workspace_access(access)
        if not re.fullmatch(r"\d+", output):
            raise GitClientError("GIT_CHECK_FAILED")
        return int(output)

    def list_commits_bounded(
        self,
        access: WorkspaceAccess,
        base_commit: str,
        head_commit: str,
        commit_limit: int,
    ) -> tuple[list[str], bool]:
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            lines, over = self._run_bounded_process(
                [
                    "-C",
                    str(repo_path),
                    "rev-list",
                    "--reverse",
                    f"{base_commit}..{head_commit}",
                ],
                cwd=repo_path,
                line_limit=commit_limit,
                parse_line=True,
                timeout_seconds=self.timeout_seconds,
            )
        _assert_workspace_access(access)
        commits: list[str] = []
        for line in lines:
            line = line.strip().lower()
            if line:
                if not re.fullmatch(r"[0-9a-f]{40}", line):
                    raise GitClientError("GIT_CHECK_FAILED")
                commits.append(line)
        return commits, over

    def get_numstat_bounded(
        self,
        access: WorkspaceAccess,
        base_commit: str,
        head_commit: str,
        line_limit: int,
    ) -> tuple[list[tuple[str, str, str]], bool]:
        _assert_workspace_access(access)
        repo_path = access.path
        with _locked_safe_local_config(repo_path, access.expected_url):
            lines, over = self._run_bounded_process(
                [
                    "-C",
                    str(repo_path),
                    "diff",
                    "--numstat",
                    "--no-renames",
                    base_commit,
                    head_commit,
                ],
                cwd=repo_path,
                line_limit=line_limit,
                parse_line=True,
                timeout_seconds=self.timeout_seconds,
            )
        _assert_workspace_access(access)
        return _parse_numstat_lines(lines), over

    def measure_unified_diff_bytes(
        self,
        access: WorkspaceAccess,
        base_commit: str,
        head_commit: str,
        byte_limit: int,
    ) -> tuple[int, bool]:
        _assert_workspace_access(access)
        repo_path = access.path
        proportional_timeout = max(30, byte_limit // (200 * 1024))
        with _locked_safe_local_config(repo_path, access.expected_url):
            total, over = self._run_bounded_process(
                [
                    "-C",
                    str(repo_path),
                    "diff",
                    "--no-renames",
                    base_commit,
                    head_commit,
                ],
                cwd=repo_path,
                byte_limit=byte_limit,
                timeout_seconds=proportional_timeout,
                parse_line=False,
            )
        _assert_workspace_access(access)
        return total, over

    def assert_size_limit(self, access: WorkspaceAccess) -> None:
        _assert_workspace_access(access)
        repo_path = access.path
        total = 0
        for current, dirs, files in os.walk(repo_path, followlinks=False):
            current_path = Path(current)
            if current_path == repo_path:
                dirs[:] = [name for name in dirs if name != ".git"]
            for name in list(dirs) + files:
                item = current_path / name
                if _is_reparse(item):
                    raise GitClientError("GIT_WORKSPACE_ESCAPE")
            for name in files:
                try:
                    total += (current_path / name).stat().st_size
                except OSError as exc:
                    raise GitClientError("GIT_WORKSPACE_CONFLICT") from exc
                if total > self.size_limit:
                    raise GitClientError("GIT_WORKSPACE_TOO_LARGE")
        _assert_workspace_access(access)
