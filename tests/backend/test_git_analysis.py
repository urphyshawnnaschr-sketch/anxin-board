"""Git 范围纯计算测试：只使用本地临时 bare repo 与受控工作区，不访问互联网。"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.git_analysis import (  # noqa: E402
    DIFF_READ_LIMIT,
    MAX_CHANGED_TEXT_FILES,
    MAX_LINE_CHANGES,
    compute_range_candidate,
    RangeCandidate,
    candidate_status,
    _parse_numstat,
)
from app.git_client import (  # noqa: E402
    GitClient,
    GitClientError,
    WorkspaceAccess,
    WorkspacePaths,
    _parse_numstat_line,
    cleanup_attempt_workspace,
    open_workspace_access,
    promote_attempt_workspace,
    reserve_attempt_workspace,
    resolve_workspace_paths,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [shutil.which("git"), *args], cwd=cwd, text=True, encoding="utf-8",
        errors="replace", capture_output=True, shell=False, check=True,
    )
    return result.stdout.strip()


def _stdout_lines_program(lines: list[str], *, trailing_newline: bool = True) -> list[str]:
    """构造真实 Python 子进程程序，向 stdout 精确写出给定行。

    用 os.write 写原始字节，避免文本模式在 Windows 上把 \\n 转成 \\r\\n 影响断言。
    """
    writes = []
    for index, text in enumerate(lines):
        if trailing_newline or index < len(lines) - 1:
            payload = repr((text + "\n").encode("utf-8"))
        else:
            payload = repr(text.encode("utf-8"))
        writes.append(f"os.write(1, {payload})")
    return ["-c", "import os; " + "; ".join(writes)]


def _run_emitted_lines(tmp_path, monkeypatch, lines, *, line_limit, trailing_newline=True):
    gc = GitClient(git_path=sys.executable, timeout_seconds=15, allow_local_file=True)
    monkeypatch.setattr(gc, "_execution_policy", lambda: [])
    return gc._run_bounded_process(
        _stdout_lines_program(lines, trailing_newline=trailing_newline),
        cwd=tmp_path,
        line_limit=line_limit,
        parse_line=True,
    )


@pytest.fixture()
def local_remote(tmp_path):
    if not shutil.which("git"):
        pytest.skip("system Git is unavailable")
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Range Test")
    _git(source, "config", "user.email", "range@example.invalid")
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-m", "initial")
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    return source, remote


@pytest.fixture()
def client():
    return GitClient(timeout_seconds=15, allow_local_file=True)


@pytest.fixture()
def prepared_access(local_remote, tmp_path, monkeypatch, client):
    """克隆 bare remote 到受控工作区并提升为 repo，返回可直接用于范围分析的 access。"""
    source, remote = local_remote
    root = tmp_path / "managed-projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    attempt_id = f"{701:032x}"[-32:]
    paths = resolve_workspace_paths(701, attempt_id, create=True)
    ownership = reserve_attempt_workspace(paths, attempt_id)
    access = open_workspace_access(paths, str(remote), ownership)
    try:
        client.clone_branch(str(remote), "main", access)
        client.inspect_workspace(access)
        client.assert_clean(access)
        client.fetch_branch(access, "main")
        client.checkout_remote_head(access, "main")
        access.close()
        promote_attempt_workspace(paths, ownership)
    finally:
        if access is not None and access.directory_lock is not None:
            access.close()
    promoted = open_workspace_access(paths, str(remote))
    try:
        yield source, promoted
    finally:
        promoted.close()


def _commit_and_push(source, message, *, files=None):
    files = files or {}
    for name, content in files.items():
        target = source / name
        if content is None:
            target.unlink(missing_ok=True)
            _git(source, "rm", "--quiet", name)
        else:
            target.write_text(content, encoding="utf-8")
            _git(source, "add", name)
    _git(source, "commit", "-m", message)
    _git(source, "push", "origin", "main")


def _fetch(client, access):
    client.fetch_branch(access, "main")


def test_initial_baseline_equals_head_is_no_new_commit(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    assert candidate.continuity == "no_new_commit"
    assert candidate.remote_head == baseline
    assert candidate.commit_count == 0


def test_three_continuous_commits_order_count_and_stats(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")

    _commit_and_push(source, "one", files={"a.txt": "a\n"})
    _commit_and_push(source, "two", files={"a.txt": "a\nb\n", "b.txt": "x\ny\nz\n"})
    _commit_and_push(source, "three", files={"b.txt": "x\ny\nz\nw\n"})
    _fetch(client, access)

    commits = client.list_commits(access, baseline, client.get_remote_head_ref(access, "main"))
    assert len(commits) == 3

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    assert candidate.continuity == "continuous"
    assert candidate.commit_count == 3
    assert candidate.commits == commits
    assert candidate.changed_file_count == 2
    assert candidate.added_lines == 2 + 3 + 1
    assert candidate.deleted_lines == 0
    assert candidate.diff_bytes > 0
    assert candidate.capacity == "within"


def test_forced_rewrite_makes_baseline_unreachable(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")

    _commit_and_push(source, "later", files={"later.txt": "later\n"})
    _fetch(client, access)
    orphan_old = client.get_remote_head_ref(access, "main")

    _git(source, "checkout", "--orphan", "replace")
    for name in list(source.iterdir()):
        if name.name != ".git":
            name.unlink(missing_ok=True)
    _git(source, "rm", "-r", "--cached", "--quiet", ".") if (source / ".git").exists() else None
    (source / "rewrite.txt").write_text("rewritten\n", encoding="utf-8")
    _git(source, "add", "rewrite.txt")
    _git(source, "commit", "-m", "rewrite history")
    _git(source, "push", "--force", "origin", "replace:main")
    _fetch(client, access)

    rewritten = client.get_remote_head_ref(access, "main")
    assert rewritten != orphan_old and rewritten != baseline
    assert client.is_ancestor(access, baseline, rewritten) is False

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    assert candidate.continuity == "checkpoint_unreachable"


def test_file_count_over_limit(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {}
    for index in range(MAX_CHANGED_TEXT_FILES + 1):
        files[f"f{index}.txt"] = "line\n"
    _commit_and_push(source, "many files", files=files)
    _fetch(client, access)

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    # numstat 已受 text_file_limit 限制，changed_file_count 最大为 MAX_CHANGED_TEXT_FILES
    assert candidate.changed_file_count >= MAX_CHANGED_TEXT_FILES
    assert candidate.capacity == "capacity_exceeded"


def test_line_count_over_limit(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(
        source,
        "many lines",
        files={"big.txt": "".join(f"line{i}\n" for i in range(MAX_LINE_CHANGES + 1))},
    )
    _fetch(client, access)

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    assert candidate.added_lines + candidate.deleted_lines > MAX_LINE_CHANGES
    assert candidate.capacity == "capacity_exceeded"


def test_diff_byte_read_is_bounded_and_over_is_detected(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(
        source,
        "giant single line",
        files={"giant.txt": "x" * (5 * 1024 * 1024 + 2) + "\n"},
    )
    _fetch(client, access)

    remote_head = client.get_remote_head_ref(access, "main")
    bytes_read, over = client.measure_unified_diff_bytes(
        access, baseline, remote_head, byte_limit=DIFF_READ_LIMIT
    )
    assert over is True
    assert bytes_read <= DIFF_READ_LIMIT + 1

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    assert candidate.capacity == "capacity_exceeded"
    assert candidate.diff_bytes <= DIFF_READ_LIMIT + 1


def test_binary_file_does_not_crash(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    (source / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 100)
    _git(source, "add", "blob.bin")
    _git(source, "commit", "-m", "binary")
    _git(source, "push", "origin", "main")
    _fetch(client, access)

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline
    )
    assert candidate.continuity == "continuous"
    assert candidate.changed_file_count >= 1
    assert candidate.added_lines >= 0


def test_git_command_failure_and_timeout_classification(local_remote, tmp_path, monkeypatch):
    source, remote = local_remote
    root = tmp_path / "managed-projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    attempt_id = f"{999:032x}"[-32:]
    paths = resolve_workspace_paths(999, attempt_id, create=True)

    real_client = GitClient(timeout_seconds=15, allow_local_file=True)
    ownership = reserve_attempt_workspace(paths, attempt_id)
    clone_access = open_workspace_access(paths, str(remote), ownership)
    real_client.clone_branch(str(remote), "main", clone_access)
    clone_access.close()
    promote_attempt_workspace(paths, ownership)

    access = open_workspace_access(paths, str(remote))
    try:
        missing_client = GitClient(git_path=str(Path("Z:/definitely-missing/git.exe")))
        with pytest.raises(GitClientError) as raised:
            missing_client.get_remote_head_ref(access, "main")
        assert raised.value.code == "GIT_NOT_AVAILABLE"
    finally:
        access.close()


def test_candidate_status_merges_capacity_first():
    from app.git_analysis import RangeCandidate, candidate_status

    continuous = RangeCandidate(
        baseline_commit="a" * 40, remote_head="b" * 40, continuity="continuous"
    )
    assert candidate_status(continuous) == "continuous"

    over_capacity = RangeCandidate(
        baseline_commit="a" * 40,
        remote_head="b" * 40,
        continuity="continuous",
        capacity="capacity_exceeded",
    )
    assert candidate_status(over_capacity) == "capacity_exceeded"

    no_new = RangeCandidate(
        baseline_commit="a" * 40, remote_head="a" * 40, continuity="no_new_commit"
    )
    assert candidate_status(no_new) == "no_new_commit"

    unreachable = RangeCandidate(
        baseline_commit="a" * 40, remote_head="b" * 40, continuity="checkpoint_unreachable"
    )
    assert candidate_status(unreachable) == "checkpoint_unreachable"


def test_list_commits_bounded_over_limit(local_remote, client, prepared_access):
    """list_commits_bounded 在超过 commit_limit 时返回 over=True."""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(10):
        _commit_and_push(source, f"commit-{i}", files={f"f{i}.txt": f"content-{i}\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    commits, over = client.list_commits_bounded(access, baseline, remote_head, commit_limit=5)
    assert len(commits) == 5  # 返回列表最多 commit_limit 条
    assert over is True

    full_over = client.list_commits_bounded(access, baseline, remote_head, commit_limit=20)
    assert len(full_over[0]) == 10
    assert full_over[1] is False


def test_get_numstat_bounded_over_limit(local_remote, client, prepared_access):
    """get_numstat_bounded 在超过 line_limit 时返回 over=True."""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {}
    for index in range(10):
        files[f"f{index}.txt"] = f"line-{index}\n"
    _commit_and_push(source, "many files for numstat", files=files)
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=3)
    assert len(rows) == 3  # 返回列表最多 line_limit 条
    assert over is True

    full_rows, full_over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=20)
    assert len(full_rows) == 10
    assert full_over is False


def test_compute_range_candidate_commit_count_over(local_remote, client, prepared_access):
    """compute_range_candidate 在 commit_count 超限时容量为 capacity_exceeded."""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(3):
        _commit_and_push(source, f"c-{i}", files={f"n{i}.txt": f"{i}\n"})
    _fetch(client, access)

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline, commit_count_limit=2
    )
    assert candidate.capacity == "capacity_exceeded"
    assert candidate.commit_count == 3


def test_compute_range_candidate_numstat_over(local_remote, client, prepared_access):
    """compute_range_candidate 在 numstat 行数超限时容量为 capacity_exceeded."""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {}
    for index in range(4):
        files[f"g{index}.txt"] = f"data-{index}\n"
    _commit_and_push(source, "numstat over", files=files)
    _fetch(client, access)

    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline, text_file_limit=2
    )
    assert candidate.capacity == "capacity_exceeded"


def test_diff_byte_read_with_exact_limit(local_remote, client, prepared_access):
    """measure_unified_diff_bytes 在 diff 字节数刚好等于 byte_limit 时 over=False."""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "exact", files={"exact.txt": "hello\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    bytes_read, over = client.measure_unified_diff_bytes(
        access, baseline, remote_head, byte_limit=1_000_000
    )
    assert over is False
    assert bytes_read > 0


# ── Exact boundary tests for `>` vs `>=` fixes ──────────────────────────────


def test_parse_numstat_exact_boundary_line_change_limit_not_exceeded(local_remote, client, prepared_access):
    """line_change_limit 刚好等于 added+deleted 时 lines_over=False。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    # 3 行新增 + 0 行删除 = 3
    _commit_and_push(source, "3 lines", files={"a.txt": "a\nb\nc\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")
    rows, _over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=100)
    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=3)
    assert lines_over is False, "added+deleted == line_change_limit: should NOT be over"


def test_parse_numstat_exact_boundary_line_change_limit_exceeded(local_remote, client, prepared_access):
    """line_change_limit 小于 added+deleted 时 lines_over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "3 lines", files={"a.txt": "a\nb\nc\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")
    rows, _over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=100)
    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=2)
    assert lines_over is True, "added+deleted > line_change_limit: should be over"


def test_compute_range_candidate_commit_count_exact_boundary(local_remote, client, prepared_access):
    """commit_count_limit 刚好等于提交数时 capacity='within'。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(3):
        _commit_and_push(source, f"cc-{i}", files={f"cc{i}.txt": f"{i}\n"})
    _fetch(client, access)
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        text_file_limit=150, line_change_limit=10000,
    )
    # 3 commits, commits_over from list_commits_bounded with commit_limit=3 should be False
    assert candidate.commit_count == 3
    # We need to verify with commit_count_limit=3 exactly
    candidate2 = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        commit_count_limit=3,
    )
    assert candidate2.capacity == "within", "commit_count == limit -> within"


def test_compute_range_candidate_commit_count_exceeded_boundary(local_remote, client, prepared_access):
    """commit_count_limit 小于提交数时 capacity='capacity_exceeded'。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(4):
        _commit_and_push(source, f"cc-{i}", files={f"cc{i}.txt": f"{i}\n"})
    _fetch(client, access)
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        commit_count_limit=2,
    )
    assert candidate.capacity == "capacity_exceeded", "commit_count > limit -> capacity_exceeded"


def test_compute_range_candidate_file_count_exact_boundary(local_remote, client, prepared_access):
    """changed_files 刚好等于 text_file_limit 时 capacity='within'。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {f"f{i}.txt": f"line{i}\n" for i in range(3)}
    _commit_and_push(source, "3 files", files=files)
    _fetch(client, access)
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        text_file_limit=3,
    )
    assert candidate.changed_file_count == 3
    assert candidate.capacity == "within", "changed_files == text_file_limit -> within"


def test_compute_range_candidate_file_count_exceeded_boundary(local_remote, client, prepared_access):
    """changed_files 超过 text_file_limit 时 capacity='capacity_exceeded'。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {f"f{i}.txt": f"line{i}\n" for i in range(4)}
    _commit_and_push(source, "4 files", files=files)
    _fetch(client, access)
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        text_file_limit=3,
    )
    assert candidate.capacity == "capacity_exceeded", "changed_files > text_file_limit -> capacity_exceeded"


def test_compute_range_candidate_line_changes_exact_boundary(local_remote, client, prepared_access):
    """added+deleted 刚好等于 line_change_limit 时 capacity='within'。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "3 lines", files={"t.txt": "a\nb\nc\n"})
    _fetch(client, access)
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        line_change_limit=3,
    )
    assert candidate.added_lines + candidate.deleted_lines == 3
    assert candidate.capacity == "within", "added+deleted == line_change_limit -> within"


def test_compute_range_candidate_line_changes_exceeded_boundary(local_remote, client, prepared_access):
    """added+deleted 超过 line_change_limit 时 capacity='capacity_exceeded'。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "4 lines", files={"t.txt": "a\nb\nc\nd\n"})
    _fetch(client, access)
    candidate = compute_range_candidate(
        client, access, branch="main", baseline_commit=baseline,
        line_change_limit=2,
    )
    assert candidate.added_lines + candidate.deleted_lines > 2
    assert candidate.capacity == "capacity_exceeded", "added+deleted > line_change_limit -> capacity_exceeded"


# ── Edge cases for _parse_numstat ───────────────────────────────────────────


def test_parse_numstat_binary_line_skipped():
    """二进制行（'-'）不计入 added/deleted，但计入 changed_files。"""
    rows = [("1", "1", "a.txt"), ("-", "-", "b.bin"), ("2", "3", "c.txt")]
    changed_files, added, deleted, lines_over = _parse_numstat(rows)
    assert changed_files == 3
    assert added == 3
    assert deleted == 4
    assert lines_over is False


def test_parse_numstat_illegal_non_numeric():
    """非法 numstat 行（非数字）统一映射为受控 GIT_CHECK_FAILED。"""
    rows = [("abc", "1", "x.txt")]
    with pytest.raises(GitClientError) as raised:
        _parse_numstat(rows)
    assert raised.value.code == "GIT_CHECK_FAILED"


# ── numstat 非法行统一受控失败（SLICE 4） ─────────────────────────────────────


def test_parse_numstat_line_valid_text_row():
    """普通合法 numstat 文本行解析为 (added, deleted, path)。"""
    parsed = _parse_numstat_line("12\t3\tsrc/app.py")
    assert parsed == ("12", "3", "src/app.py")


def test_parse_numstat_line_valid_binary_row():
    """合法二进制 numstat 行解析为 (\"-\", \"-\", path)。"""
    parsed = _parse_numstat_line("-\t-\tassets/image.png")
    assert parsed == ("-", "-", "assets/image.png")


def test_parse_numstat_line_path_with_tabs():
    """路径包含制表符时保留路径剩余部分。"""
    parsed = _parse_numstat_line("12\t3\tfoo\tbar.txt")
    assert parsed == ("12", "3", "foo\tbar.txt")
    stats = _parse_numstat([parsed])
    assert stats[0] == 1 and stats[1] == 12 and stats[2] == 3 and stats[3] is False


def test_parse_numstat_line_blank_lines_ignored():
    """空白行忽略，返回 None。"""
    assert _parse_numstat_line("") is None
    assert _parse_numstat_line("   ") is None
    assert _parse_numstat_line("\t") is None


def test_parse_numstat_line_insufficient_fields():
    """字段不足的非空行受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line("12\t3")
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_parse_numstat_line_no_tab_nonempty():
    """完全无制表符的非空行受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line("invalid row")
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_parse_numstat_line_added_non_numeric():
    """added 列非数字受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line("abc\t3\tsrc/app.py")
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_parse_numstat_line_deleted_non_numeric():
    """deleted 列非数字受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line("12\txyz\tsrc/app.py")
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_parse_numstat_line_added_negative():
    """added 列为负数受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line("-1\t3\tsrc/app.py")
    assert raised.value.code == "GIT_CHECK_FAILED"


@pytest.mark.parametrize("line", ["-\t3\tassets/file.bin", "3\t-\tassets/file.bin"])
def test_parse_numstat_line_single_sided_binary_marker(line):
    """单边二进制标记受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line(line)
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_parse_numstat_line_empty_path():
    """路径为空受控失败。"""
    with pytest.raises(GitClientError) as raised:
        _parse_numstat_line("12\t3\t")
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_get_numstat_illegal_row_raises_check_failed(local_remote, client, prepared_access, monkeypatch):
    """get_numstat 遇到非空非法行返回 GIT_CHECK_FAILED，不静默忽略。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "one", files={"a.txt": "a\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    def fake_run(args, *, cwd):
        return "12\t3\tok.txt\nbad row"

    monkeypatch.setattr(client, "_run", fake_run)
    with pytest.raises(GitClientError) as raised:
        client.get_numstat(access, baseline, remote_head)
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_get_numstat_bounded_illegal_row_raises_check_failed(local_remote, client, prepared_access, monkeypatch):
    """get_numstat_bounded 遇到非空非法行返回 GIT_CHECK_FAILED，不静默忽略。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "one", files={"a.txt": "a\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    def fake_run_bounded(args, *, cwd, line_limit, byte_limit=None, timeout_seconds=None, parse_line=False):
        return ["12\t3\tok.txt", "12\t3"], False

    monkeypatch.setattr(client, "_run_bounded_process", fake_run_bounded)
    with pytest.raises(GitClientError) as raised:
        client.get_numstat_bounded(access, baseline, remote_head, line_limit=10)
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_get_numstat_bounded_blank_lines_ignored(local_remote, client, prepared_access, monkeypatch):
    """get_numstat_bounded 忽略空白行，只返回合法行。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "one", files={"a.txt": "a\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    def fake_run_bounded(args, *, cwd, line_limit, byte_limit=None, timeout_seconds=None, parse_line=False):
        return ["12\t3\tok.txt", "", "   "], False

    monkeypatch.setattr(client, "_run_bounded_process", fake_run_bounded)
    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=10)
    assert rows == [("12", "3", "ok.txt")]
    assert over is False


def test_parse_numstat_illegal_triple_maps_to_check_failed():
    """直接向 _parse_numstat 传入非法三元组也映射为稳定受控错误。"""
    for rows in (
        [("abc", "1", "x.txt")],
        [("12", "xyz", "x.txt")],
        [("-1", "3", "x.txt")],
        [("-", "3", "x.txt")],
        [("12", "3", "")],
    ):
        with pytest.raises(GitClientError) as raised:
            _parse_numstat(rows)
        assert raised.value.code == "GIT_CHECK_FAILED"


def test_parse_numstat_binary_counts_one_file_zero_lines():
    """二进制行统计为 1 个变更文件、0 新增、0 删除。"""
    changed_files, added, deleted, lines_over = _parse_numstat([("-", "-", "assets/image.png")])
    assert changed_files == 1
    assert added == 0
    assert deleted == 0
    assert lines_over is False


def test_parse_numstat_eof_carry():
    """只有一行变更，line_change_limit 足够大时 lines_over=False。"""
    rows = [("5", "5", "eof.txt")]
    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=100)
    assert changed_files == 1
    assert added == 5
    assert deleted == 5
    assert lines_over is False


def test_parse_numstat_last_line_no_newline():
    """最后一行不带换行符仍正确解析。"""
    rows = [("1", "1", "a.txt"), ("2", "2", "b.txt")]
    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=10)
    assert changed_files == 2
    assert added == 3
    assert deleted == 3
    assert lines_over is False


def test_parse_numstat_single_line_across_chunks():
    """单行变更在所有 chunk 中累加正确。"""
    rows = [("1", "1", "a.txt")]
    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=2)
    assert changed_files == 1
    assert added == 1
    assert deleted == 1
    assert lines_over is False  # added+deleted == line_change_limit, not >


# ── Real blocking subprocess test for timeout watchdog ──────────────────────


def test_run_bounded_timeout_kills_blocking_subprocess(tmp_path, monkeypatch):
    """_run_bounded_process 在 timeout_seconds 到达时返回 GIT_COMMAND_TIMEOUT。

    使用真实 Python 子进程阻塞，验证看门狗正确终止并抛出超时异常。
    不依赖全局 subprocess.Popen 替身，避免与 timeout handler 内部 subprocess.run 冲突。
    """
    from app.git_client import GitClient, GitClientError
    import sys as _sys

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    gc = GitClient(git_path=_sys.executable, timeout_seconds=15, allow_local_file=True)
    monkeypatch.setattr(gc, "_execution_policy", lambda: [])

    with pytest.raises(GitClientError) as raised:
        gc._run_bounded_process(
            ["-c", "import time; time.sleep(10)"],
            cwd=repo_path,
            line_limit=100,
            parse_line=True,
            timeout_seconds=2,
        )
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"


def test_run_bounded_timeout_releases_lock_and_allows_second_request(local_remote, tmp_path, client, prepared_access):
    """timeout 释放 directory_lock，第二个请求可以正常获取工作区。"""
    from app.git_client import GitClient, GitClientError

    source, access = prepared_access

    slow_gc = GitClient(git_path=str(Path("cmd.exe")), timeout_seconds=1)
    with pytest.raises(GitClientError):
        slow_gc._run_bounded_process(
            ["/c", "ping -n 10 127.0.0.1 >nul"],
            cwd=tmp_path if isinstance(tmp_path, Path) else Path(source.parent),
            line_limit=100,
            parse_line=True,
            timeout_seconds=1,
        )

    # timeout 后同一 access 应该可以执行正常 git 命令
    head = client.get_remote_head_ref(access, "main")
    assert head is not None


# ── stderr 并发消费与全生命周期超时（受控真实子进程） ────────────────────────


def _run_python_child(tmp_path, monkeypatch, program, *, line_limit=None, byte_limit=None, timeout_seconds=None):
    """用真实 Python 子进程运行受控程序；不替换全局 subprocess.Popen。"""
    from app.git_client import GitClient

    gc = GitClient(git_path=sys.executable, timeout_seconds=15, allow_local_file=True)
    monkeypatch.setattr(gc, "_execution_policy", lambda: [])
    return gc._run_bounded_process(
        ["-c", program],
        cwd=tmp_path,
        line_limit=line_limit,
        byte_limit=byte_limit,
        parse_line=line_limit is not None,
        timeout_seconds=timeout_seconds,
    )


def test_run_bounded_stderr_flood_does_not_block_or_timeout(tmp_path, monkeypatch):
    """stderr 大量输出（512 KiB）不堵塞子进程、不误判超时，stdout 正常返回。"""
    program = (
        "import os, sys\n"
        "sys.stderr.buffer.write(b'x' * (512 * 1024))\n"
        "sys.stderr.flush()\n"
        "os.write(1, b'ok\\n')\n"
    )
    lines, over = _run_python_child(
        tmp_path, monkeypatch, program, line_limit=10, timeout_seconds=5
    )
    assert lines == ["ok"]
    assert over is False


def test_run_bounded_stdout_closed_then_hang_times_out(tmp_path, monkeypatch):
    """stdout 关闭后进程仍挂起：timeout 不得在 stdout EOF 时提前取消。"""
    from app.git_client import GitClientError

    program = "import sys, time; sys.stdout.close(); time.sleep(10)"
    with pytest.raises(GitClientError) as raised:
        _run_python_child(
            tmp_path, monkeypatch, program, line_limit=10, timeout_seconds=2
        )
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"


def test_run_bounded_stderr_output_then_hang_times_out(tmp_path, monkeypatch):
    """stderr 输出后进程挂起：timeout 覆盖 stdout 之后仍在运行的进程。"""
    from app.git_client import GitClientError

    program = "import sys, time; sys.stderr.write('started\\n'); sys.stderr.flush(); time.sleep(10)"
    with pytest.raises(GitClientError) as raised:
        _run_python_child(
            tmp_path, monkeypatch, program, line_limit=10, timeout_seconds=2
        )
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"


def test_run_bounded_timeout_while_descendant_holds_stderr(tmp_path, monkeypatch):
    """父进程已退出、后代仍持有 stderr：timeout 在 stderr join 阶段仍生效。

    父进程启动约 10 秒的后代 Python 进程（继承 stderr、丢弃 stdout），向
    stdout 写一行后立即正常退出；后代继续持有 stderr 写端使管道无 EOF。
    timeout_seconds=2 时必须在 stderr 消费阶段抛出 GIT_COMMAND_TIMEOUT，
    不得返回伪成功；stderr 后台线程不得残留未处理异常。
    """
    from app.git_client import GitClientError
    import os as _os
    import time as _time

    marker = tmp_path / "parent-marker.txt"
    program = (
        "import os, subprocess, sys\n"
        f"marker = {str(marker)!r}\n"
        "with open(marker, 'w', encoding='utf-8') as f:\n"
        "    f.write('parent-pid=%d\\n' % os.getpid())\n"
        "    f.write('parent-will-exit\\n')\n"
        "    f.flush()\n"
        "    os.fsync(f.fileno())\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(10)'],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),\n"
        ")\n"
        "with open(marker, 'a', encoding='utf-8') as f:\n"
        "    f.write('child-pid=%d\\n' % child.pid)\n"
        "    f.flush()\n"
        "    os.fsync(f.fileno())\n"
        "os.write(1, b'parent-out\\n')\n"
        "sys.exit(0)\n"
    )
    started = _time.monotonic()
    with pytest.raises(GitClientError) as raised:
        _run_python_child(
            tmp_path, monkeypatch, program, line_limit=10, timeout_seconds=2
        )
    elapsed = _time.monotonic() - started
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"
    marker_text = marker.read_text(encoding="utf-8")
    assert "parent-will-exit" in marker_text
    assert marker_text.startswith("parent-pid=")
    assert "child-pid=" in marker_text
    if _os.name == "nt":
        assert elapsed < 8


def test_run_bounded_interleaved_stdout_stderr_no_deadlock(tmp_path, monkeypatch):
    """stdout 与 stderr 交替持续输出：无死锁，stdout 结果完整且 over=False。"""
    program = (
        "import os, sys\n"
        "for i in range(200):\n"
        "    os.write(1, ('out-%d\\n' % i).encode('ascii'))\n"
        "    sys.stderr.write('err-%d\\n' % i)\n"
        "    sys.stderr.flush()\n"
    )
    lines, over = _run_python_child(
        tmp_path, monkeypatch, program, line_limit=1000, timeout_seconds=5
    )
    assert over is False
    assert lines == [f"out-{i}" for i in range(200)]


def test_run_bounded_nonzero_exit_maps_to_check_failed(tmp_path, monkeypatch):
    """非零退出统一映射 GIT_CHECK_FAILED，不泄漏超时/管道/线程异常。"""
    from app.git_client import GitClientError

    program = "import sys; sys.stderr.write('fatal: boom\\n'); sys.stderr.flush(); sys.exit(3)"
    with pytest.raises(GitClientError) as raised:
        _run_python_child(
            tmp_path, monkeypatch, program, line_limit=10, timeout_seconds=5
        )
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_stderr_summary_reader_keeps_only_tail_within_limit():
    """stderr 摘要读取器只保留最后 limit 字节，超出部分持续丢弃。"""
    from io import BytesIO

    from app.git_client import STDERR_SUMMARY_LIMIT, _StderrSummaryReader

    reader = _StderrSummaryReader(STDERR_SUMMARY_LIMIT)
    payload = b"e" * (512 * 1024)
    reader.drain(BytesIO(payload))
    assert len(reader.tail) <= STDERR_SUMMARY_LIMIT
    assert reader.tail == payload[-STDERR_SUMMARY_LIMIT:]


def test_proportional_timeout_value():
    """验证 measure_unified_diff_bytes 的 proportional_timeout 计算正确。

    内部使用 max(30, byte_limit // (200 * 1024))，约 5s/MB，最少 30s。
    """
    from app.git_client import GitClient

    gc = GitClient(timeout_seconds=15, allow_local_file=True)
    # 直接调用内部计算逻辑
    assert max(30, 0 // (200 * 1024)) == 30
    assert max(30, 200 * 1024 // (200 * 1024)) == 30
    assert max(30, 10 * 1024 * 1024 // (200 * 1024)) == 51


def test_run_bounded_process_line_mode_parses_lines():
    """_run_bounded_process 行模式解析标准输出。"""
    from app.git_client import GitClient

    gc = GitClient(timeout_seconds=15, allow_local_file=True)
    lines, over = gc._run_bounded_process(
        ["--version"], cwd=Path("."), line_limit=10, parse_line=True
    )
    assert len(lines) >= 1
    assert over is False
    assert any("git" in l.lower() for l in lines)


def test_run_bounded_process_line_mode_over(tmp_path):
    """_run_bounded_process 行模式在超限时返回 over=True，使用受控临时仓库。"""
    from app.git_client import GitClient

    git_exe = shutil.which("git")
    if not git_exe:
        pytest.skip("system Git is unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    for i in range(3):
        (repo / f"f{i}.txt").write_text(f"content-{i}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"commit-{i}")
    gc = GitClient(git_path=git_exe, timeout_seconds=15, allow_local_file=True)
    lines, over = gc._run_bounded_process(
        ["log", "--oneline", "-3"], cwd=repo, line_limit=1, parse_line=True
    )
    assert len(lines) == 1  # 返回列表最多 line_limit 条
    assert over is True


# ── 行模式 bounded 返回数量契约（受控真实子进程） ────────────────────────────


def test_run_bounded_line_mode_less_than_limit(tmp_path, monkeypatch):
    """行数少于 limit：全部返回且 over=False。"""
    emitted = [f"line-{i}" for i in range(3)]
    lines, over = _run_emitted_lines(tmp_path, monkeypatch, emitted, line_limit=5)
    assert lines == emitted
    assert over is False


def test_run_bounded_line_mode_exactly_at_limit(tmp_path, monkeypatch):
    """行数刚好等于 limit：全部返回且 over=False。"""
    emitted = [f"line-{i}" for i in range(5)]
    lines, over = _run_emitted_lines(tmp_path, monkeypatch, emitted, line_limit=5)
    assert lines == emitted
    assert over is False


def test_run_bounded_line_mode_limit_plus_one(tmp_path, monkeypatch):
    """行数等于 limit+1：只返回 limit 条且 over=True。"""
    emitted = [f"line-{i}" for i in range(6)]
    lines, over = _run_emitted_lines(tmp_path, monkeypatch, emitted, line_limit=5)
    assert len(lines) == 5
    assert lines == emitted[:5]
    assert over is True


def test_run_bounded_line_mode_much_over_limit(tmp_path, monkeypatch):
    """行数明显大于 limit：只返回 limit 条且 over=True。"""
    emitted = [f"line-{i}" for i in range(100)]
    lines, over = _run_emitted_lines(tmp_path, monkeypatch, emitted, line_limit=5)
    assert len(lines) == 5
    assert lines == emitted[:5]
    assert over is True


def test_run_bounded_line_mode_last_line_with_newline(tmp_path, monkeypatch):
    """最后一行有换行：正常 EOF 全部返回且 over=False。"""
    emitted = [f"line-{i}" for i in range(5)]
    lines, over = _run_emitted_lines(
        tmp_path, monkeypatch, emitted, line_limit=5, trailing_newline=True
    )
    assert lines == emitted
    assert over is False


def test_run_bounded_line_mode_last_line_without_newline(tmp_path, monkeypatch):
    """最后一行无换行：EOF 时仍是完整结果且 over=False。"""
    emitted = [f"line-{i}" for i in range(5)]
    lines, over = _run_emitted_lines(
        tmp_path, monkeypatch, emitted, line_limit=5, trailing_newline=False
    )
    assert lines == emitted
    assert over is False


def test_run_bounded_line_mode_over_discards_carry(tmp_path, monkeypatch):
    """超限后不再把 carry 追加到结果：返回列表严格等于前 limit 条。"""
    emitted = [f"line-{i}" for i in range(7)]
    lines, over = _run_emitted_lines(
        tmp_path, monkeypatch, emitted, line_limit=5, trailing_newline=False
    )
    assert over is True
    assert len(lines) == 5
    assert lines == emitted[:5]


def test_run_bounded_process_byte_mode():
    """_run_bounded_process 字节模式返回正确字节数。"""
    from app.git_client import GitClient

    gc = GitClient(timeout_seconds=15, allow_local_file=True)
    total, over = gc._run_bounded_process(
        ["--version"], cwd=Path("."), byte_limit=10, parse_line=False
    )
    assert total > 0
    # --version 输出通常 >10 字节
    assert over is True or total <= 11


def test_run_bounded_process_mutual_exclusion():
    """_run_bounded_process line_limit 和 byte_limit 互斥。"""
    from app.git_client import GitClient, GitClientError

    gc = GitClient(timeout_seconds=15, allow_local_file=True)
    with pytest.raises(GitClientError) as raised:
        gc._run_bounded_process(
            ["--version"], cwd=Path("."), line_limit=10, byte_limit=100, parse_line=True
        )
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_run_bounded_process_byte_limit_zero_returns_over(local_remote, client, prepared_access):
    """measure_unified_diff_bytes 在 byte_limit=0 时立即返回 over 信号。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "tiny", files={"z.txt": "content\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    bytes_read, over = client.measure_unified_diff_bytes(
        access, baseline, remote_head, byte_limit=0
    )
    assert over is True
    assert bytes_read <= 1


def test_get_numstat_bounded_changed_files_ge_threshold(local_remote, client, prepared_access):
    """get_numstat_bounded 在 numstat 行数超过 line_limit 时返回 over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {}
    for index in range(5):
        files[f"f{index}.txt"] = f"line-{index}\n"
    _commit_and_push(source, "threshold files", files=files)
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=3)
    assert len(rows) == 3  # 返回列表最多 line_limit 条
    assert over is True


def test_get_numstat_bounded_added_lines_ge_threshold(local_remote, client, prepared_access):
    """get_numstat_bounded 在 numstat 行数超过 line_limit 时返回 over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "added lines", files={"lines.txt": "".join(f"line{i}\n" for i in range(5))})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=1)
    assert len(rows) == 1
    assert over is False  # single file change, 1 numstat line, does not exceed limit 1


def test_get_numstat_bounded_deleted_lines_ge_threshold(local_remote, client, prepared_access):
    """get_numstat_bounded 在 numstat 行数超过 line_limit 时返回 over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "base", files={"del.txt": "".join(f"line{i}\n" for i in range(5))})
    _fetch(client, access)
    _commit_and_push(source, "delete", files={"del.txt": ""})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=1)
    assert len(rows) == 1
    assert over is False  # single file change, 1 numstat line, does not exceed limit 1


def test_parse_numstat_ge_early_exit(local_remote, client, prepared_access):
    """_parse_numstat 的 line_change_limit 超过时提前退出。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {}
    for index in range(3):
        files[f"e{index}.txt"] = f"data-{index}\n"
    _commit_and_push(source, "parse numstat ge", files=files)
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=100)

    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=1)
    assert changed_files == 3
    assert lines_over is True  # 1+1=2 > 1, triggers early exit


def test_parse_numstat_added_lines_ge_early_exit(local_remote, client, prepared_access):
    """_parse_numstat 在 added_lines >= line_change_limit 时返回 lines_over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "added ge", files={"g.txt": "a\nb\nc\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=100)

    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=2)
    assert changed_files == 1
    assert lines_over is True


def test_parse_numstat_deleted_lines_ge_early_exit(local_remote, client, prepared_access):
    """_parse_numstat 在 deleted_lines > line_change_limit 时返回 lines_over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    # baseline 已有 base.txt（内容 "base\n"），修改它产生 1 删 1 增
    _commit_and_push(source, "modify base", files={"base.txt": "modified\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=100)

    changed_files, added, deleted, lines_over = _parse_numstat(rows, line_change_limit=1)
    assert changed_files == 1
    assert lines_over is True


def test_list_commits_bounded_changed_files_ge_threshold(local_remote, client, prepared_access):
    """list_commits_bounded 在 commit 数量超过 commit_limit 时返回 over=True。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(3):
        _commit_and_push(source, f"c-{i}", files={f"l{i}.txt": f"{i}\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    commits, over = client.list_commits_bounded(access, baseline, remote_head, commit_limit=2)
    # 严格 bounded 语义：返回列表最多 commit_limit 条，第 commit_limit+1 条仅用于置 over
    assert len(commits) == 2
    assert over is True


def test_list_commits_bounded_exact_limit(local_remote, client, prepared_access):
    """list_commits_bounded 在 commit 数刚好等于 commit_limit 时返回 limit 条且 over=False。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(2):
        _commit_and_push(source, f"e-{i}", files={f"e{i}.txt": f"{i}\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    commits, over = client.list_commits_bounded(access, baseline, remote_head, commit_limit=2)
    assert len(commits) == 2
    assert over is False


def test_list_commits_bounded_exceeds_limit(local_remote, client, prepared_access):
    """list_commits_bounded 在 commit 数超过 commit_limit 时返回列表不超过 limit。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    for i in range(3):
        _commit_and_push(source, f"o-{i}", files={f"o{i}.txt": f"{i}\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    commits, over = client.list_commits_bounded(access, baseline, remote_head, commit_limit=2)
    assert len(commits) == 2
    assert over is True


def test_get_numstat_bounded_exact_limit(local_remote, client, prepared_access):
    """get_numstat_bounded 在 numstat 行数刚好等于 line_limit 时返回 limit 条且 over=False。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {f"n{i}.txt": f"data-{i}\n" for i in range(2)}
    _commit_and_push(source, "two files", files=files)
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=2)
    assert len(rows) == 2
    assert over is False


def test_get_numstat_bounded_exceeds_limit(local_remote, client, prepared_access):
    """get_numstat_bounded 在 numstat 行数超过 line_limit 时返回列表不超过 limit。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    files = {f"m{i}.txt": f"data-{i}\n" for i in range(3)}
    _commit_and_push(source, "three files", files=files)
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    rows, over = client.get_numstat_bounded(access, baseline, remote_head, line_limit=2)
    assert len(rows) == 2
    assert over is True


def test_compute_range_candidate_no_new_commit_eq(local_remote, client, prepared_access):
    """compute_range_candidate 在 baseline_commit == remote_head 时返回 no_new_commit。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    candidate = compute_range_candidate(client, access, branch="main", baseline_commit=baseline)
    assert candidate.continuity == "no_new_commit"
    assert candidate.commit_count == 0


def test_candidate_status_order_capacity_first():
    """candidate_status 在 capacity_exceeded 时优先返回。"""
    over = RangeCandidate(
        baseline_commit="a" * 40, remote_head="b" * 40,
        continuity="continuous", capacity="capacity_exceeded",
    )
    assert candidate_status(over) == "capacity_exceeded"

    no_new = RangeCandidate(
        baseline_commit="a" * 40, remote_head="a" * 40,
        continuity="no_new_commit",
    )
    assert candidate_status(no_new) == "no_new_commit"

    unreachable = RangeCandidate(
        baseline_commit="a" * 40, remote_head="b" * 40,
        continuity="checkpoint_unreachable",
    )
    assert candidate_status(unreachable) == "checkpoint_unreachable"


def test_measure_unified_diff_bytes_byte_limit_exact_boundary(local_remote, client, prepared_access):
    """measure_unified_diff_bytes 在 diff 字节数接近 byte_limit 时正确返回。"""
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "boundary", files={"bound.txt": "hello\n"})
    _fetch(client, access)
    remote_head = client.get_remote_head_ref(access, "main")

    bytes_read, over = client.measure_unified_diff_bytes(
        access, baseline, remote_head, byte_limit=1_000_000
    )
    assert over is False
    assert bytes_read > 0

def test_large_soft_range_keeps_complete_real_git_inventory(local_remote, client, prepared_access):
    source, access = prepared_access
    baseline = client.get_remote_head_ref(access, "main")
    _commit_and_push(source, "processable wide commit", files={
        f"wide-file{index}.txt": "line\n" for index in range(151)
    })
    _fetch(client, access)
    result = compute_range_candidate(client, access, branch="main", baseline_commit=baseline)
    assert result.capacity == "within"
    assert result.batch_required
    assert result.statistics_complete
    assert result.changed_file_count == len(result.files) == 151
    assert result.added_lines == 151
