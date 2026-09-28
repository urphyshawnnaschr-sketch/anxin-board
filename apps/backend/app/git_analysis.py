"""纯 Git 范围计算与容量判断；不访问 FastAPI，不直接维护数据库状态。

只负责在已安全打开的受控工作区上执行范围读取与统计，返回客观 RangeCandidate。
所有 Git 子进程与工作区安全由 git_client 提供；数据库与状态映射由 analysis_lineages 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.git_client import GitClient, GitClientError, WorkspaceAccess, _parse_numstat_line

SOFT_CHANGED_FILES = 150
SOFT_LINE_CHANGES = 10_000
# Bounded local inventory limits, independent of provider context windows.
MAX_CHANGED_TEXT_FILES = 2_000
MAX_LINE_CHANGES = 100_000
DIFF_READ_LIMIT = 5 * 1024 * 1024 + 1
COMMIT_COUNT_LIMIT = 500

ContinuityStatus = Literal["no_new_commit", "continuous", "checkpoint_unreachable"]
CapacityStatus = Literal["within", "capacity_exceeded"]


class RangeAnalysisError(RuntimeError):
    """受控范围分析失败；code 只使用 GitClientError 的稳定错误码。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_GIT_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}
_GIT_CANONICAL_BYTE_ESCAPES = {value: key for key, value in _GIT_C_ESCAPES.items()}


def _is_literal_ascii_control(char: str) -> bool:
    """Git canonical quoted pathname 不应以 literal 形式携带 C0/DEL 控制字符。"""
    value = ord(char)
    return value < 0x20 or value == 0x7F


def _canonical_git_numstat_representation(decoded: str) -> str:
    """按 Git core.quotePath=true 的 C-style 规则生成唯一 numstat pathname 表示。"""
    try:
        raw = decoded.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise GitClientError("GIT_CHECK_FAILED") from exc

    if all(0x20 <= value < 0x7F and value not in (0x22, 0x5C) for value in raw):
        return decoded

    body: list[str] = []
    for value in raw:
        escaped = _GIT_CANONICAL_BYTE_ESCAPES.get(value)
        if escaped is not None:
            body.append(f"\\{escaped}")
        elif 0x20 <= value < 0x7F:
            body.append(chr(value))
        else:
            body.append(f"\\{value:03o}")
    return f'"{"".join(body)}"'


def _canonical_git_numstat_path(path: str) -> str:
    """把 Git C-style quoted pathname 解码为原始 UTF-8 仓库路径身份。

    这里只接受 Git 在受控环境中使用 core.quotePath=true 产生的唯一 canonical
    pathname representation，拒绝 unnecessary quoting、mnemonic/octal alias 等
    可折叠到同一 pathname identity 的替代表达。

    这里仅冻结 Git 仓库内的路径身份，不授予任何文件系统读取权限。
    后续消费者若要把该值用于磁盘读取，仍必须经过受控工作区、根目录包含关系和
    symlink/junction/reparse point 等独立安全校验，不得直接拼接后解引用。
    """
    if not path or "\x00" in path:
        raise GitClientError("GIT_CHECK_FAILED")

    if not path.startswith('"'):
        if any(char in ('"', "\\") or _is_literal_ascii_control(char) for char in path):
            raise GitClientError("GIT_CHECK_FAILED")
        decoded = path
    else:
        if len(path) < 2 or not path.endswith('"'):
            raise GitClientError("GIT_CHECK_FAILED")

        body = path[1:-1]
        raw = bytearray()
        index = 0
        while index < len(body):
            char = body[index]
            if char == '"' or _is_literal_ascii_control(char):
                raise GitClientError("GIT_CHECK_FAILED")
            if char != "\\":
                try:
                    raw.extend(char.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise GitClientError("GIT_CHECK_FAILED") from exc
                index += 1
                continue

            index += 1
            if index >= len(body):
                raise GitClientError("GIT_CHECK_FAILED")
            escaped = body[index]
            mapped = _GIT_C_ESCAPES.get(escaped)
            if mapped is not None:
                raw.append(mapped)
                index += 1
                continue

            if escaped not in "01234567" or index + 3 > len(body):
                raise GitClientError("GIT_CHECK_FAILED")
            octal = body[index : index + 3]
            if len(octal) != 3 or any(digit not in "01234567" for digit in octal):
                raise GitClientError("GIT_CHECK_FAILED")
            value = int(octal, 8)
            if value > 0xFF:
                raise GitClientError("GIT_CHECK_FAILED")
            raw.append(value)
            index += 3

        try:
            decoded = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitClientError("GIT_CHECK_FAILED") from exc
        if not decoded or "\x00" in decoded:
            raise GitClientError("GIT_CHECK_FAILED")

    if _canonical_git_numstat_representation(decoded) != path:
        raise GitClientError("GIT_CHECK_FAILED")
    return decoded


@dataclass(frozen=True)
class FileEvidenceCandidate:
    """与一次 RangeCandidate 同源读取的逐文件客观 numstat 事实。"""

    path: str
    added_lines: int | None
    deleted_lines: int | None
    is_binary: bool


@dataclass(frozen=True)
class RangeCandidate:
    baseline_commit: str
    remote_head: str
    commits: list[str] = field(default_factory=list)
    commit_count: int = 0
    changed_file_count: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    diff_bytes: int = 0
    files: tuple[FileEvidenceCandidate, ...] = field(default_factory=tuple)
    continuity: ContinuityStatus = "no_new_commit"
    capacity: CapacityStatus = "within"
    batch_required: bool = False
    statistics_complete: bool = True
    capacity_reasons: tuple[str, ...] = ()
    capacity_limits: dict[str, int] = field(default_factory=dict)


def _parse_numstat(
    rows: list[tuple[str, str, str]],
    *,
    line_change_limit: int | None = None,
) -> tuple[int, int, int, bool]:
    """从 numstat 行统计变更文件数与新增/删除行数；二进制行（'-'）不计入行数。

    当 line_change_limit 不为 None 且 added + deleted 已超过该值时停止计数并返回
    lines_over=True，避免为容量已超限场景继续遍历全部行。
    返回 (changed_files, added, deleted, lines_over)。
    """
    changed_files = len(rows)
    added = 0
    deleted = 0
    for row in rows:
        _parse_numstat_line("\t".join(row))
        added_raw, deleted_raw, _path = row
        if added_raw != "-":
            added += int(added_raw)
        if deleted_raw != "-":
            deleted += int(deleted_raw)
        if line_change_limit is not None and added + deleted > line_change_limit:
            return changed_files, added, deleted, True
    return changed_files, added, deleted, False


def _file_evidence_candidates(
    rows: list[tuple[str, str, str]],
) -> tuple[FileEvidenceCandidate, ...]:
    """把已经由 numstat parser 校验的同批行转成不可变逐文件候选事实。"""

    files: list[FileEvidenceCandidate] = []
    for row in rows:
        parsed = _parse_numstat_line("\t".join(row))
        if parsed is None:
            continue
        added_raw, deleted_raw, path = parsed
        canonical_path = _canonical_git_numstat_path(path)
        is_binary = added_raw == "-"
        files.append(
            FileEvidenceCandidate(
                path=canonical_path,
                added_lines=None if is_binary else int(added_raw),
                deleted_lines=None if is_binary else int(deleted_raw),
                is_binary=is_binary,
            )
        )
    return tuple(files)


def compute_range_candidate(
    client: GitClient,
    access: WorkspaceAccess,
    *,
    branch: str,
    baseline_commit: str,
    text_file_limit: int = MAX_CHANGED_TEXT_FILES,
    line_change_limit: int = MAX_LINE_CHANGES,
    diff_read_limit: int = DIFF_READ_LIMIT,
    commit_count_limit: int = COMMIT_COUNT_LIMIT,
) -> RangeCandidate:
    """读取活动分支当前 remote HEAD 并计算连续范围与容量；不改变任何 baseline。"""
    remote_head = client.get_remote_head_ref(access, branch)

    if baseline_commit == remote_head:
        continuity: ContinuityStatus = "no_new_commit"
    elif client.is_ancestor(access, baseline_commit, remote_head):
        continuity = "continuous"
    else:
        continuity = "checkpoint_unreachable"

    if continuity == "no_new_commit":
        return RangeCandidate(
            baseline_commit=baseline_commit,
            remote_head=remote_head,
            continuity=continuity,
        )
    if continuity == "checkpoint_unreachable":
        return RangeCandidate(
            baseline_commit=baseline_commit,
            remote_head=remote_head,
            continuity=continuity,
        )

    commits, commits_over = client.list_commits_bounded(
        access, baseline_commit, remote_head, commit_limit=commit_count_limit
    )
    commit_count = client.count_commits(access, baseline_commit, remote_head)
    numstat_rows, numstat_over = client.get_numstat_bounded(
        access, baseline_commit, remote_head, line_limit=text_file_limit
    )
    changed_files, added_lines, deleted_lines, lines_over = _parse_numstat(
        numstat_rows
    )
    file_candidates = _file_evidence_candidates(numstat_rows)
    diff_bytes, diff_over = client.measure_unified_diff_bytes(
        access,
        baseline_commit,
        remote_head,
        byte_limit=diff_read_limit,
    )

    reasons = []
    if changed_files > text_file_limit or numstat_over:
        reasons.append("changed_files_hard_limit")
    if added_lines + deleted_lines > line_change_limit:
        reasons.append("line_changes_hard_limit")
    if commits_over or commit_count > commit_count_limit:
        reasons.append("commits_hard_limit")
    if diff_over:
        reasons.append("diff_bytes_hard_limit")
    capacity: CapacityStatus = "capacity_exceeded" if reasons else "within"

    return RangeCandidate(
        baseline_commit=baseline_commit,
        remote_head=remote_head,
        commits=commits,
        commit_count=commit_count,
        changed_file_count=changed_files,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        diff_bytes=min(diff_bytes, diff_read_limit),
        files=file_candidates,
        continuity=continuity,
        capacity=capacity,
        batch_required=changed_files > SOFT_CHANGED_FILES or added_lines + deleted_lines > SOFT_LINE_CHANGES,
        statistics_complete=not (numstat_over or commits_over or diff_over),
        capacity_reasons=tuple(reasons),
        capacity_limits={"changed_files": text_file_limit, "line_changes": line_change_limit,
                         "diff_bytes": diff_read_limit, "commits": commit_count_limit},
    )


def candidate_status(candidate: RangeCandidate) -> str:
    """把连续性 + 容量合并为范围候选对外状态；branch_changed 由调用方判定。"""
    if candidate.capacity == "capacity_exceeded":
        return "capacity_exceeded"
    return candidate.continuity
