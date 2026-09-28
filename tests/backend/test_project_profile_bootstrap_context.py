from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from fastapi import HTTPException
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import project_profile_bootstrap_context as bootstrap  # noqa: E402
from app import project_profile_generation as core  # noqa: E402
import app.project_profile_generation_api as api  # noqa: E402


ORIGIN = "https://bootstrap.example.invalid/repo.git"


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith(("git_", "gcm_", "ssh_"))
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Bootstrap Test",
            "GIT_AUTHOR_EMAIL": "bootstrap@example.invalid",
            "GIT_COMMITTER_NAME": "Bootstrap Test",
            "GIT_COMMITTER_EMAIL": "bootstrap@example.invalid",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repo), *args],
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="strict").strip()


def _write(repo: Path, path: str, content: bytes) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path, str]:
    if shutil.which("git") is None:
        pytest.skip("git is required")
    project_id = 1
    root = tmp_path / "projects"
    repo = root / str(project_id) / "repo"
    repo.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    subprocess.run(
        [shutil.which("git") or "git", "init", "-b", "main", str(repo)],
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    _git(repo, "remote", "add", "origin", ORIGIN)
    _write(repo, "src/service.py", b"def service():\n    return 'ok'\n")
    _write(repo, "web/app.ts", b"export const app = 'ready'\n")
    _write(repo, "config/settings.txt", b"api_key = \"must-redact\"\nfeature=true\n")
    _write(repo, ".env", b"TOKEN=must-never-send\n")
    _write(repo, "assets/logo.png", b"\x89PNG\r\n\x1a\n\x00binary")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "bootstrap fixture")
    head = _git(repo, "rev-parse", "HEAD")
    return project_id, repo, head


def test_bootstrap_reads_complete_safe_text_from_exact_head(tmp_path, monkeypatch):
    project_id, _repo, head = _state(tmp_path, monkeypatch)
    items, allowed = bootstrap.read_repo_context(
        project_id,
        {"git_url": ORIGIN, "branch": "main"},
        {"remote_head": head, "local_head": head},
    )

    assert items[0]["path"] == "<exact-head-tracked-tree>"
    assert f"exact_head={head}" in items[0]["content"]
    assert "src/service.py" in items[0]["content"]
    assert "web/app.ts" in items[0]["content"]
    assert ".env" not in items[0]["content"]

    by_path = {item["path"]: item for item in items[1:]}
    assert set(by_path) == {"config/settings.txt", "src/service.py", "web/app.ts"}
    assert "must-redact" not in by_path["config/settings.txt"]["content"]
    assert "[REDACTED:CREDENTIAL]" in by_path["config/settings.txt"]["content"]
    assert "must-never-send" not in str(items)
    assert allowed == {item["evidence_id"] for item in items}


def test_bootstrap_fails_visible_instead_of_partial_context(tmp_path, monkeypatch):
    project_id, _repo, head = _state(tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "_MAX_REPO_CONTEXT_BYTES", 32)

    with pytest.raises(HTTPException) as caught:
        bootstrap.read_repo_context(
            project_id,
            {"git_url": ORIGIN, "branch": "main"},
            {"remote_head": head, "local_head": head},
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROFILE_GENERATION_REPO_CONTEXT_TOO_LARGE"


def test_tree_parser_rejects_control_character_path_before_context_framing():
    sha = "a" * 40
    with pytest.raises(HTTPException) as caught:
        bootstrap._parse_tree([f'100644 blob {sha} 1\t"src/line\\nfeed.py"'])

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROFILE_GENERATION_REPO_TREE_INVALID"


def test_mounted_generation_binds_bootstrap_context_without_replacing_core_generator():
    assert core._read_repo_context is bootstrap.read_repo_context
    assert api.core.generate_profile_candidate is core.generate_profile_candidate
