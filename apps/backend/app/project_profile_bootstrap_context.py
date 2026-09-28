"""Exact-HEAD bootstrap repository context for first Project Profile generation.

This module is intentionally an adapter, not a second repository-analysis stack. It reuses the
existing controlled Git workspace/GitClient execution boundary, the frozen sensitive-path policy,
and the credential redaction transform. Its only job is to bridge those existing primitives into
the pre-profile bootstrap lane, which cannot yet depend on a confirmed Project Profile or an
EvidenceSnapshot.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from fastapi import HTTPException

from app.context_redaction import _redact_text
from app.git_analysis import _canonical_git_numstat_path
from app.git_client import (
    GitClient,
    GitClientError,
    _assert_workspace_access,
    _locked_safe_local_config,
    open_workspace_access,
    resolve_workspace_paths,
    validate_workspace_paths,
)
from app.model_send_admission import (
    _first_match as _sensitive_path_first_match,
    _validate_internal_policy as _validate_sensitive_path_policy,
)


_MAX_TRACKED_FILES = 10_000
_MAX_SINGLE_TEXT_FILE_BYTES = 512 * 1024
_MAX_REPO_CONTEXT_BYTES = 2 * 1024 * 1024
_MAX_TREE_MANIFEST_BYTES = 512 * 1024

_LS_TREE_RE = re.compile(
    r"^(?P<mode>[0-7]{6}) (?P<type>blob|commit) (?P<sha>[0-9a-f]{40}) +(?P<size>-|[0-9]+)\t(?P<path>.+)$"
)
_BINARY_SUFFIXES = frozenset(
    {
        ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".db", ".dll", ".dylib",
        ".eot", ".exe", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg", ".mov",
        ".mp3", ".mp4", ".o", ".obj", ".otf", ".pdf", ".png", ".pyc", ".sqlite",
        ".sqlite3", ".tar", ".tgz", ".ttf", ".wav", ".webm", ".webp", ".woff",
        ".woff2", ".xls", ".xlsx", ".zip",
    }
)
_METADATA_ONLY_BASENAMES = frozenset(
    {
        "cargo.lock",
        "composer.lock",
        "package-lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "yarn.lock",
    }
)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _git_context_error(code: str = "PROFILE_GENERATION_GIT_WORKSPACE_REQUIRED") -> HTTPException:
    return _error(
        409,
        code,
        "受控 Git 工作区无法按当前已确认 HEAD 安全读取完整代码上下文，请重新执行 Git 连接检查。",
    )


def _is_obvious_binary_or_bulk_metadata(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    return (
        suffix in _BINARY_SUFFIXES
        or name in _METADATA_ONLY_BASENAMES
        or name.endswith(".min.js")
        or name.endswith(".min.css")
        or name.endswith(".map")
    )


def _path_has_unsafe_framing_char(path: str) -> bool:
    """Reject C0/DEL path identities before placing them in line-framed model context."""
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in path)


def _exact_object_prefix(access) -> list[str]:
    """Reuse GitClient execution policy while disabling object-replacement/lazy-fetch drift."""
    return [
        "--no-optional-locks",
        "-C",
        str(access.path),
        "--no-replace-objects",
        "--no-lazy-fetch",
    ]


def _parse_tree(lines: list[str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for line in lines:
        match = _LS_TREE_RE.fullmatch(line)
        if match is None:
            raise _git_context_error("PROFILE_GENERATION_REPO_TREE_INVALID")
        try:
            path = _canonical_git_numstat_path(match.group("path"))
        except GitClientError as exc:
            raise _git_context_error("PROFILE_GENERATION_REPO_TREE_INVALID") from exc
        if _path_has_unsafe_framing_char(path):
            raise _git_context_error("PROFILE_GENERATION_REPO_TREE_INVALID")
        if path in seen:
            raise _git_context_error("PROFILE_GENERATION_REPO_TREE_INVALID")
        seen.add(path)
        raw_size = match.group("size")
        object_type = match.group("type")
        if object_type == "blob":
            if raw_size == "-":
                raise _git_context_error("PROFILE_GENERATION_REPO_TREE_INVALID")
            size = int(raw_size)
        else:
            size = None
        records.append(
            {
                "path": path,
                "mode": match.group("mode"),
                "object_type": object_type,
                "object_sha": match.group("sha"),
                "size": size,
            }
        )
    return records


def _list_exact_head_tree(client: GitClient, access, head: str) -> list[dict[str, object]]:
    _assert_workspace_access(access)
    with _locked_safe_local_config(access.path, access.expected_url):
        lines, over = client._run_bounded_process(
            [
                *_exact_object_prefix(access),
                "ls-tree",
                "-r",
                "-l",
                head,
            ],
            cwd=access.path,
            line_limit=_MAX_TRACKED_FILES,
            parse_line=True,
            timeout_seconds=client.timeout_seconds,
        )
    _assert_workspace_access(access)
    if over or not isinstance(lines, list):
        raise _error(
            409,
            "PROFILE_GENERATION_REPO_TREE_TOO_LARGE",
            "仓库 tracked file 数量超过首次功能模块识别的安全上限；系统不会静默漏读代码。",
        )
    return _parse_tree(lines)


def _read_exact_head_blob(client: GitClient, access, *, head: str, path: str, expected_size: int) -> bytes:
    _assert_workspace_access(access)
    if expected_size < 0 or expected_size > _MAX_SINGLE_TEXT_FILE_BYTES:
        raise _error(
            409,
            "PROFILE_GENERATION_REPO_FILE_TOO_LARGE",
            f"代码文件 {path} 超过单文件安全上限；系统不会截断后冒充完整代码分析。",
        )
    with _locked_safe_local_config(access.path, access.expected_url):
        returncode, raw, _stderr = client._exec(
            [*_exact_object_prefix(access), "show", f"{head}:{path}"],
            cwd=access.path,
        )
    _assert_workspace_access(access)
    if returncode != 0 or len(raw) != expected_size:
        raise _git_context_error("PROFILE_GENERATION_REPO_BLOB_INVALID")
    return raw


def _tree_manifest(records: list[dict[str, object]]) -> tuple[str, int]:
    lines: list[str] = []
    sensitive_count = 0
    for record in records:
        path = str(record["path"])
        if _sensitive_path_first_match(path) is not None:
            sensitive_count += 1
            continue
        if record["object_type"] == "commit":
            state = "submodule_not_read"
        elif _is_obvious_binary_or_bulk_metadata(path):
            state = "metadata_only"
        else:
            state = "text_candidate"
        size = "-" if record["size"] is None else str(record["size"])
        lines.append(f"{path}\t{record['mode']}\t{size}\t{state}")
    manifest = "\n".join(lines)
    manifest_bytes = len(manifest.encode("utf-8"))
    if manifest_bytes > _MAX_TREE_MANIFEST_BYTES:
        raise _error(
            409,
            "PROFILE_GENERATION_REPO_TREE_TOO_LARGE",
            "仓库完整 tracked tree 超过首次功能模块识别的安全上限；系统不会静默截断。",
        )
    return manifest, sensitive_count


def read_repo_context(
    project_id: int,
    project: dict[str, object],
    git_state: dict[str, object],
) -> tuple[list[dict[str, str]], set[str]]:
    """Return exact-HEAD safe text repository context for the bootstrap Project Profile call.

    Every non-sensitive tracked blob that is not obvious binary/bulk metadata is read from the exact
    Git object at ``git_state.remote_head``. If the complete safe text set cannot fit the bounded
    bootstrap envelope, the function fails visibly instead of silently selecting a subset.
    """

    _validate_sensitive_path_policy()
    head = git_state.get("remote_head")
    if type(head) is not str or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise _git_context_error()

    try:
        paths = resolve_workspace_paths(project_id, "0" * 32, create=False)
        validate_workspace_paths(paths)
        access = open_workspace_access(paths, str(project["git_url"]))
    except (GitClientError, OSError, KeyError, TypeError) as exc:
        raise _git_context_error() from exc

    client = GitClient()
    items: list[dict[str, str]] = []
    allowed: set[str] = set()
    try:
        client.inspect_workspace(access)
        client.assert_clean(access)
        if client.get_local_head(access) != head:
            raise _git_context_error()

        records = _list_exact_head_tree(client, access, head)
        manifest, sensitive_count = _tree_manifest(records)
        manifest_id = "repo-tree-" + hashlib.sha256(
            (head + "\n" + manifest).encode("utf-8")
        ).hexdigest()[:24]
        tree_content = (
            f"exact_head={head}\n"
            f"sensitive_path_count={sensitive_count}\n"
            "path\tmode\tsize\tcontent_state\n"
            + manifest
        )
        items.append(
            {
                "evidence_id": manifest_id,
                "path": "<exact-head-tracked-tree>",
                "content": tree_content,
            }
        )
        allowed.add(manifest_id)
        total_bytes = len(tree_content.encode("utf-8"))

        for record in records:
            path = str(record["path"])
            if _sensitive_path_first_match(path) is not None:
                continue
            if record["object_type"] != "blob" or _is_obvious_binary_or_bulk_metadata(path):
                continue
            size = record["size"]
            if type(size) is not int:
                raise _git_context_error("PROFILE_GENERATION_REPO_TREE_INVALID")
            raw = _read_exact_head_blob(
                client,
                access,
                head=head,
                path=path,
                expected_size=size,
            )
            if b"\x00" in raw:
                continue
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise _error(
                    409,
                    "PROFILE_GENERATION_REPO_TEXT_ENCODING_UNSUPPORTED",
                    f"代码文件 {path} 不是可验证的 UTF-8 文本；系统不会静默漏读。",
                ) from exc
            redacted, _stats = _redact_text(text, include_assignments=True)
            encoded_size = len(redacted.encode("utf-8"))
            if total_bytes + encoded_size > _MAX_REPO_CONTEXT_BYTES:
                raise _error(
                    409,
                    "PROFILE_GENERATION_REPO_CONTEXT_TOO_LARGE",
                    "完整安全文本代码超过首次功能模块识别的单次模型上下文上限；系统不会只挑一部分代码冒充全量分析。",
                )
            evidence_seed = f"{head}\n{path}\n{record['object_sha']}"
            evidence_id = "repo-code-" + hashlib.sha256(evidence_seed.encode("utf-8")).hexdigest()[:24]
            items.append({"evidence_id": evidence_id, "path": path, "content": redacted})
            allowed.add(evidence_id)
            total_bytes += encoded_size

        client.assert_clean(access)
        if client.get_local_head(access) != head:
            raise _git_context_error()
    except HTTPException:
        raise
    except GitClientError as exc:
        raise _git_context_error() from exc
    finally:
        access.close()

    return items, allowed
