"""01C 前置：Git numstat 路径规范化契约与真实 Git 特殊文件名回归。"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.git_analysis import (  # noqa: E402
    _canonical_git_numstat_path,
    compute_range_candidate,
)
from app.git_client import (  # noqa: E402
    GitClient,
    GitClientError,
    WorkspaceAccess,
    WorkspacePaths,
)


def _git(cwd: Path, *args: str) -> str:
    git_path = shutil.which("git")
    if not git_path:
        pytest.skip("system Git is unavailable")
    result = subprocess.run(
        [git_path, *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        shell=False,
        check=True,
        timeout=15,
    )
    return result.stdout.strip()


def _captured_access(repo_path: Path, expected_url: str) -> WorkspaceAccess:
    root = repo_path.parent
    temporary = root / ".unused-attempt"
    paths = WorkspacePaths(
        approved_ancestor=root,
        root=root,
        project=root,
        repo=repo_path,
        temporary=temporary,
        checkout=temporary / "checkout",
        ownership_marker=temporary / ".anxinboard-attempt-owner",
    )
    info = repo_path.lstat()
    return WorkspaceAccess(
        paths=paths,
        path=repo_path,
        expected_url=expected_url,
        identity=(info.st_dev, info.st_ino),
        attempt_ownership=None,
    )


@pytest.mark.parametrize(
    ("git_path", "expected"),
    [
        (r'"\344\270\255\346\226\207.py"', "中文.py"),
        (r'"bell\aname.txt"', "bell\aname.txt"),
        (r'"backspace\bname.txt"', "backspace\bname.txt"),
        (r'"tab\tname.txt"', "tab\tname.txt"),
        (r'"line\nname.txt"', "line\nname.txt"),
        (r'"vertical\vname.txt"', "vertical\vname.txt"),
        (r'"form\fname.txt"', "form\fname.txt"),
        (r'"carriage\rname.txt"', "carriage\rname.txt"),
        (r'"quote\"name.txt"', 'quote"name.txt'),
        (r'"back\\slash.txt"', "back\\slash.txt"),
        ("folder/plain file.py", "folder/plain file.py"),
    ],
)
def test_git_numstat_path_decoder_returns_raw_repository_identity(git_path, expected):
    assert _canonical_git_numstat_path(git_path) == expected


@pytest.mark.parametrize(
    "git_path",
    [
        "",
        '"unterminated',
        r'"bad\q.txt"',
        r'"\777.txt"',
        r'"\300.txt"',
        r'"\000.txt"',
        'raw"quote.txt',
        "raw\\backslash.txt",
        "raw\tcontrol.txt",
    ],
)
def test_git_numstat_path_decoder_fails_closed_on_ambiguous_or_invalid_input(git_path):
    with pytest.raises(GitClientError) as raised:
        _canonical_git_numstat_path(git_path)
    assert raised.value.code == "GIT_CHECK_FAILED"


def test_git_numstat_path_decoder_rejects_noncanonical_representation_aliases():
    slash = chr(92)
    aliases = (
        '"plain.txt"',
        f'"{slash}160lain.txt"',
        f'"name{slash}007.txt"',
        f'"quote{slash}042.txt"',
        f'"back{slash}134slash.txt"',
    )
    for candidate in aliases:
        with pytest.raises(GitClientError) as raised:
            _canonical_git_numstat_path(candidate)
        assert raised.value.code == "GIT_CHECK_FAILED"


def test_literal_ascii_controls_are_never_accepted_as_git_input_representation():
    for codepoint in [*range(0x20), 0x7F]:
        control = chr(codepoint)
        for candidate in (f"raw{control}name.txt", f'"quoted{control}name.txt"'):
            with pytest.raises(GitClientError) as raised:
                _canonical_git_numstat_path(candidate)
            assert raised.value.code == "GIT_CHECK_FAILED"


@pytest.mark.parametrize(
    ("escaped", "literal"),
    [
        (r'"name\a.txt"', "name\x07.txt"),
        (r'"name\b.txt"', "name\x08.txt"),
        (r'"name\t.txt"', "name\x09.txt"),
        (r'"name\n.txt"', "name\x0a.txt"),
        (r'"name\v.txt"', "name\x0b.txt"),
        (r'"name\f.txt"', "name\x0c.txt"),
        (r'"name\r.txt"', "name\x0d.txt"),
    ],
)
def test_escaped_control_has_one_canonical_input_representation(escaped, literal):
    assert _canonical_git_numstat_path(escaped) == literal
    with pytest.raises(GitClientError):
        _canonical_git_numstat_path(literal)
    with pytest.raises(GitClientError):
        _canonical_git_numstat_path(f'"{literal}"')


def test_real_git_candidate_canonicalizes_quoted_unicode_path(tmp_path):
    if not shutil.which("git"):
        pytest.skip("system Git is unavailable")

    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    source.mkdir()

    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Path Contract Test")
    _git(source, "config", "user.email", "path-contract@example.invalid")

    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-m", "base")
    base_commit = _git(source, "rev-parse", "HEAD").lower()

    special = Path("目录") / "中文 文件.py"
    (source / special.parent).mkdir()
    (source / special).write_text("print('路径')\n", encoding="utf-8")
    _git(source, "add", "--", special.as_posix())
    _git(source, "commit", "-m", "add unicode path")
    head_commit = _git(source, "rev-parse", "HEAD").lower()

    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    _git(
        tmp_path,
        "clone",
        "--no-tags",
        "--single-branch",
        "--branch",
        "main",
        str(remote),
        str(checkout),
    )

    raw_numstat = _git(
        checkout,
        "-c",
        "core.quotePath=true",
        "diff",
        "--numstat",
        "--no-renames",
        base_commit,
        head_commit,
    )
    assert special.as_posix() not in raw_numstat
    assert raw_numstat.split("\t", 2)[2].startswith('"')

    access = _captured_access(checkout, str(remote))
    try:
        client = GitClient(timeout_seconds=10, allow_local_file=True)
        client.inspect_workspace(access)
        candidate = compute_range_candidate(
            client,
            access,
            branch="main",
            baseline_commit=base_commit,
        )
    finally:
        access.close()

    assert candidate.remote_head == head_commit
    assert candidate.capacity == "within"
    assert candidate.changed_file_count == 1
    assert len(candidate.files) == 1
    assert candidate.files[0].path == special.as_posix()
    assert not candidate.files[0].path.startswith('"')
