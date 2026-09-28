"""GitClient 契约测试：只使用临时本地仓库与临时工作区。"""

import os
import inspect
import http.server
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.git_client as git_client_module  # noqa: E402
from app.git_client import (  # noqa: E402
    GitClient,
    GitClientError,
    WorkspaceAccess,
    WorkspacePaths,
    cleanup_attempt_workspace,
    open_workspace_access,
    promote_attempt_workspace,
    reserve_attempt_workspace,
    resolve_workspace_paths,
)


@contextmanager
def _reserved_access(tmp_path, monkeypatch, expected_url, *, project_id=701):
    root = tmp_path / f"managed-projects-{project_id}"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    attempt_id = f"{project_id:032x}"[-32:]
    paths = resolve_workspace_paths(project_id, attempt_id, create=True)
    ownership = reserve_attempt_workspace(paths, attempt_id)
    access = open_workspace_access(paths, expected_url, ownership)
    try:
        yield paths, ownership, access
    finally:
        access.close()
        if paths.temporary.exists():
            try:
                cleanup_attempt_workspace(paths, ownership)
            except GitClientError:
                pass


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


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [shutil.which("git"), *args], cwd=cwd, text=True, encoding="utf-8",
        errors="replace", capture_output=True, shell=False, check=True,
    )
    return result.stdout.strip()


def _find_openssl(git_path: str) -> str | None:
    discovered = shutil.which("openssl")
    if discovered:
        return discovered
    git_root = Path(git_path).resolve().parents[1]
    for relative in (Path("usr/bin/openssl.exe"), Path("mingw64/bin/openssl.exe")):
        candidate = git_root / relative
        if candidate.is_file():
            return str(candidate)
    return None


@pytest.fixture()
def https_marker(tmp_path):
    git_path = shutil.which("git")
    if not git_path:
        pytest.skip("system Git is unavailable")
    openssl = _find_openssl(git_path)
    if not openssl:
        pytest.skip("OpenSSL for the local HTTPS marker is unavailable")
    certificate = tmp_path / "marker-cert.pem"
    private_key = tmp_path / "marker-key.pem"
    generated = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-days",
            "1",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
        timeout=10,
    )
    if generated.returncode != 0:
        pytest.skip("OpenSSL could not create the local HTTPS marker certificate")

    counters = {
        "health": 0,
        "approved": 0,
        "rewritten": 0,
        "redirect": 0,
        "auth": 0,
    }

    class MarkerHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                counters["health"] += 1
                self.send_response(204)
                self.end_headers()
                return
            if self.path.startswith("/rewritten.git"):
                counters["rewritten"] += 1
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"https://127.0.0.1:{self.server.server_port}/redirect-target",
                )
                self.end_headers()
                return
            if self.path.startswith("/approved.git"):
                counters["approved"] += 1
                self.send_response(404)
                self.end_headers()
                return
            if self.path.startswith("/auth.git"):
                counters["auth"] += 1
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git-test"')
                self.end_headers()
                return
            if self.path.startswith("/redirect-target"):
                counters["redirect"] += 1
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MarkerHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"https://127.0.0.1:{server.server_port}"
    with urllib.request.urlopen(
        f"{base_url}/health",
        context=ssl._create_unverified_context(),
        timeout=3,
    ) as response:
        assert response.status == 204
    git_probe_environment = os.environ.copy()
    git_probe_environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    git_probe = subprocess.run(
        [
            git_path,
            "-c",
            "http.sslBackend=openssl",
            "-c",
            "http.sslVerify=false",
            "-c",
            "http.followRedirects=false",
            "ls-remote",
            f"{base_url}/rewritten.git",
        ],
        cwd=tmp_path,
        env=git_probe_environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
        timeout=5,
    )
    assert counters["rewritten"] >= 1, git_probe.stderr
    assert counters["redirect"] == 0
    counters["rewritten"] = 0
    counters["redirect"] = 0
    counters["approved"] = 0
    try:
        yield {
            "counters": counters,
            "rewritten_url": f"{base_url}/rewritten.git",
            "approved_url": f"{base_url}/approved.git",
            "auth_url": f"{base_url}/auth.git",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def local_remote(tmp_path):
    if not shutil.which("git"):
        pytest.skip("system Git is unavailable")
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Contract Test")
    _git(source, "config", "user.email", "contract@example.invalid")
    (source / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-m", "initial")
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    return source, remote


def test_reads_system_git_version():
    client = GitClient()
    assert client.get_version().startswith("git version ")


def test_missing_git_is_classified():
    client = GitClient(git_path=str(Path("Z:/definitely-missing/git.exe")))
    with pytest.raises(GitClientError, match="本机未找到") as raised:
        client.get_version()
    assert raised.value.code == "GIT_NOT_AVAILABLE"


@pytest.mark.parametrize("branch", ["main", "feature/x", "release/1.0", "a.b-c_1"])
def test_valid_branch_uses_git_contract(branch):
    GitClient().validate_branch(branch)


@pytest.mark.parametrize("branch", ["-danger", "a..b", "bad name", "main.lock"])
def test_invalid_branch_is_rejected(branch):
    with pytest.raises(GitClientError) as raised:
        GitClient().validate_branch(branch)
    assert raised.value.code == "GIT_BRANCH_INVALID"


def test_local_bare_repo_clone_fetch_and_head_contract(local_remote, tmp_path, monkeypatch):
    source, remote = local_remote
    client = GitClient(timeout_seconds=10, allow_local_file=True)
    first_remote = client.get_remote_head(str(remote), "main")
    with _reserved_access(tmp_path, monkeypatch, str(remote)) as (_, _, access):
        client.clone_branch(str(remote), "main", access)
        client.inspect_workspace(access)
        client.assert_clean(access)
        assert client.get_local_head(access) == first_remote

        (source / "tracked.txt").write_text("two\n", encoding="utf-8")
        _git(source, "add", "tracked.txt")
        _git(source, "commit", "-m", "second")
        _git(source, "push", "origin", "main")

        client.fetch_branch(access, "main")
        client.checkout_remote_head(access, "main")
        second_remote = client.get_remote_head(str(remote), "main")
        assert second_remote != first_remote
        assert client.get_local_head(access) == second_remote


def test_missing_remote_branch_is_stable_error(local_remote):
    _, remote = local_remote
    with pytest.raises(GitClientError) as raised:
        GitClient(allow_local_file=True).get_remote_head(str(remote), "missing")
    assert raised.value.code == "GIT_BRANCH_NOT_FOUND"


def test_existing_non_repo_is_conflict(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(GitClientError) as raised:
        GitClient().inspect_workspace(_captured_access(target, "unused"))
    assert raised.value.code == "GIT_WORKSPACE_CONFLICT"


def test_origin_mismatch_is_conflict(local_remote, tmp_path, monkeypatch):
    _, remote = local_remote
    client = GitClient(allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=702) as (_, _, access):
        client.clone_branch(str(remote), "main", access)
        access.expected_url = str(tmp_path / "other.git")
        with pytest.raises(GitClientError) as raised:
            client.inspect_workspace(access)
        assert raised.value.code == "GIT_WORKSPACE_CONFLICT"


def test_dirty_workspace_is_not_overwritten(local_remote, tmp_path, monkeypatch):
    _, remote = local_remote
    client = GitClient(allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=703) as (_, _, access):
        client.clone_branch(str(remote), "main", access)
        (access.path / "tracked.txt").write_text("local change\n", encoding="utf-8")
        with pytest.raises(GitClientError) as raised:
            client.assert_clean(access)
        assert raised.value.code == "GIT_WORKSPACE_DIRTY"


def test_timeout_is_terminated_and_classified(tmp_path, monkeypatch):
    started = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", [])
        if command and command[0] == sys.executable:
            started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    client = GitClient(git_path=sys.executable, timeout_seconds=0.05)
    with pytest.raises(GitClientError) as raised:
        client._run(["-c", "import time; time.sleep(30)"], cwd=tmp_path)
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"
    assert len(started) == 1
    assert started[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup contract")
def test_timeout_terminates_spawned_child_process(tmp_path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(30)"
    )
    client = GitClient(git_path=sys.executable, timeout_seconds=5.0)
    monkeypatch.setattr(client, "_execution_policy", lambda: [])
    with pytest.raises(GitClientError) as raised:
        client._run(["-c", child_code, str(child_pid_file)], cwd=tmp_path)
    assert raised.value.code == "GIT_COMMAND_TIMEOUT"
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    probe = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", f"if (Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) {{ exit 1 }}"],
        capture_output=True, shell=False, check=False,
    )
    if probe.returncode != 0:
        subprocess.run(
            ["taskkill", "/PID", str(child_pid), "/T", "/F"],
            capture_output=True, shell=False, check=False,
        )
    assert probe.returncode == 0


def test_size_limit_stops_early(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    (target / "large.bin").write_bytes(b"x" * 32)
    with pytest.raises(GitClientError) as raised:
        GitClient(size_limit=16).assert_size_limit(_captured_access(target, "unused"))
    assert raised.value.code == "GIT_WORKSPACE_TOO_LARGE"


def test_cleanup_removes_only_attempt_temp(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    paths = resolve_workspace_paths(7, "a" * 32, create=True)
    ownership = reserve_attempt_workspace(paths, "a" * 32)
    (paths.checkout / "partial").write_text("x", encoding="utf-8")
    paths.repo.mkdir()
    (paths.repo / "unknown").write_text("preserve", encoding="utf-8")
    cleanup_attempt_workspace(paths, ownership)
    assert not paths.temporary.exists()
    assert (paths.repo / "unknown").read_text(encoding="utf-8") == "preserve"


def test_attempt_reservation_loses_race_without_deleting_unknown_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    paths = resolve_workspace_paths(8, "b" * 32, create=True)
    original_mkdir = Path.mkdir
    sentinel = paths.temporary / "competitor.txt"
    raced = False

    def racing_mkdir(path, *args, **kwargs):
        nonlocal raced
        if path == paths.temporary and not raced:
            raced = True
            original_mkdir(path, mode=0o700, exist_ok=False)
            sentinel.write_text("competitor-owned", encoding="utf-8")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(GitClientError) as raised:
        reserve_attempt_workspace(paths, "b" * 32)

    assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
    assert sentinel.read_text(encoding="utf-8") == "competitor-owned"


@pytest.mark.parametrize(
    "tamper",
    ["marker-missing", "marker-wrong", "marker-replaced", "directory-replaced"],
)
def test_cleanup_refuses_missing_wrong_or_replaced_ownership(
    tmp_path, monkeypatch, tamper
):
    root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    paths = resolve_workspace_paths(9, "c" * 32, create=True)
    ownership = reserve_attempt_workspace(paths, "c" * 32)
    sentinel = paths.checkout / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    if tamper == "marker-missing":
        paths.ownership_marker.unlink()
    elif tamper == "marker-wrong":
        paths.ownership_marker.write_text("wrong-owner", encoding="utf-8")
    elif tamper == "marker-replaced":
        original = paths.ownership_marker.read_bytes()
        paths.ownership_marker.unlink()
        paths.ownership_marker.write_bytes(original)
    else:
        shutil.rmtree(paths.checkout)
        paths.checkout.mkdir()
        sentinel.write_text("replacement-owned", encoding="utf-8")

    with pytest.raises(GitClientError) as raised:
        cleanup_attempt_workspace(paths, ownership)

    assert raised.value.code in {"GIT_WORKSPACE_CONFLICT", "GIT_WORKSPACE_ESCAPE"}
    assert paths.temporary.exists()
    if tamper == "directory-replaced":
        assert sentinel.read_text(encoding="utf-8") == "replacement-owned"
    else:
        assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_clone_refuses_checkout_replaced_by_another_empty_directory(
    local_remote, tmp_path, monkeypatch
):
    _, remote = local_remote
    client = GitClient(timeout_seconds=10, allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=740) as (
        paths,
        _,
        access,
    ):
        original = paths.temporary / "original-checkout"
        paths.checkout.rename(original)
        paths.checkout.mkdir()
        sentinel = paths.checkout / "competitor.txt"
        sentinel.write_text("competitor-owned", encoding="utf-8")

        with pytest.raises(GitClientError) as raised:
            client.clone_branch(str(remote), "main", access)

        assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
        assert sentinel.read_text(encoding="utf-8") == "competitor-owned"
        assert not (paths.checkout / ".git").exists()
        assert original.is_dir()


def test_clone_refuses_checkout_replaced_by_symlink_without_touching_target(
    local_remote, tmp_path, monkeypatch
):
    _, remote = local_remote
    client = GitClient(timeout_seconds=10, allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=741) as (
        paths,
        _,
        access,
    ):
        outside = tmp_path / "competitor-target"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        original = paths.temporary / "original-checkout"
        paths.checkout.rename(original)
        try:
            os.symlink(outside, paths.checkout, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable")

        with pytest.raises(GitClientError) as raised:
            client.clone_branch(str(remote), "main", access)

        assert raised.value.code in {"GIT_WORKSPACE_ESCAPE", "GIT_WORKSPACE_CONFLICT"}
        assert sentinel.read_text(encoding="utf-8") == "preserve"
        assert not (outside / ".git").exists()


def test_normal_attempt_can_promote_owned_checkout(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    paths = resolve_workspace_paths(742, "d" * 32, create=True)
    ownership = reserve_attempt_workspace(paths, "d" * 32)
    (paths.checkout / "owned.txt").write_text("owned", encoding="utf-8")
    promote_attempt_workspace(paths, ownership)
    assert (paths.repo / "owned.txt").read_text(encoding="utf-8") == "owned"
    assert not paths.temporary.exists()


def test_promotion_refuses_replaced_checkout_and_preserves_competitor(
    tmp_path, monkeypatch
):
    root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    paths = resolve_workspace_paths(743, "e" * 32, create=True)
    ownership = reserve_attempt_workspace(paths, "e" * 32)
    original = paths.temporary / "original-checkout"
    paths.checkout.rename(original)
    paths.checkout.mkdir()
    sentinel = paths.checkout / "competitor.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(GitClientError) as raised:
        promote_attempt_workspace(paths, ownership)
    assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not paths.repo.exists()
    with pytest.raises(GitClientError):
        cleanup_attempt_workspace(paths, ownership)


def test_promotion_detects_marker_replacement_after_rename(
    tmp_path, monkeypatch
):
    root = tmp_path / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    paths = resolve_workspace_paths(744, "f" * 32, create=True)
    ownership = reserve_attempt_workspace(paths, "f" * 32)
    (paths.checkout / "owned.txt").write_text("owned", encoding="utf-8")
    real_rename = Path.rename

    def replace_marker_after_rename(path, target):
        result = real_rename(path, target)
        if path == paths.checkout:
            content = paths.ownership_marker.read_bytes()
            paths.ownership_marker.unlink()
            paths.ownership_marker.write_bytes(content)
        return result

    monkeypatch.setattr(Path, "rename", replace_marker_after_rename)
    with pytest.raises(GitClientError) as raised:
        promote_attempt_workspace(paths, ownership)
    assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
    assert (paths.repo / "owned.txt").read_text(encoding="utf-8") == "owned"
    assert paths.ownership_marker.exists()
    with pytest.raises(GitClientError):
        cleanup_attempt_workspace(paths, ownership)


def test_projects_root_override_must_stay_in_temp(monkeypatch):
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(Path.cwd() / "runtime"))
    with pytest.raises(GitClientError) as raised:
        resolve_workspace_paths(1, "b" * 32, create=False)
    assert raised.value.code == "GIT_WORKSPACE_ESCAPE"


def test_symlink_or_reparse_project_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "1", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(root))
    with pytest.raises(GitClientError) as raised:
        resolve_workspace_paths(1, "c" * 32, create=False)
    assert raised.value.code == "GIT_WORKSPACE_ESCAPE"


@pytest.mark.parametrize("link_position", ["root", "middle"])
def test_raw_root_chain_symlink_is_rejected_without_touching_target(
    tmp_path, monkeypatch, link_position
):
    approved = tmp_path / "approved"
    target = tmp_path / "link-target"
    approved.mkdir()
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    link = approved / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    configured_root = link if link_position == "root" else link / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(configured_root))

    with pytest.raises(GitClientError) as raised:
        resolve_workspace_paths(1, "d" * 32, create=True)

    assert raised.value.code == "GIT_WORKSPACE_ESCAPE"
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert sorted(path.name for path in target.iterdir()) == ["sentinel.txt"]


def _create_windows_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        cwd=link.parent,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")


@pytest.mark.skipif(os.name != "nt", reason="Windows real junction/reparse contract")
@pytest.mark.parametrize("link_position", ["root", "middle"])
def test_windows_junction_in_raw_root_chain_is_rejected_without_touching_target(
    tmp_path, monkeypatch, link_position
):
    approved = tmp_path / "junction-approved"
    target = tmp_path / "junction-target"
    approved.mkdir()
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    junction = approved / "linked"
    _create_windows_junction(junction, target)
    configured_root = junction if link_position == "root" else junction / "projects"
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(configured_root))
    try:
        with pytest.raises(GitClientError) as raised:
            resolve_workspace_paths(1, "e" * 32, create=True)
        assert raised.value.code == "GIT_WORKSPACE_ESCAPE"
        assert sentinel.read_text(encoding="utf-8") == "preserve"
        assert sorted(path.name for path in target.iterdir()) == ["sentinel.txt"]
    finally:
        if junction.exists():
            os.rmdir(junction)


@pytest.mark.skipif(os.name != "nt", reason="Windows default LOCALAPPDATA junction contract")
def test_default_localappdata_middle_junction_is_rejected_without_touching_target(
    tmp_path, monkeypatch
):
    local_app_data = tmp_path / "local-app-data"
    target = tmp_path / "default-junction-target"
    local_app_data.mkdir()
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    junction = local_app_data / "AnxinBoard"
    _create_windows_junction(junction, target)
    monkeypatch.delenv("ANXINBOARD_PROJECTS_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    try:
        with pytest.raises(GitClientError) as raised:
            resolve_workspace_paths(1, "f" * 32, create=True)
        assert raised.value.code == "GIT_WORKSPACE_ESCAPE"
        assert sentinel.read_text(encoding="utf-8") == "preserve"
        assert sorted(path.name for path in target.iterdir()) == ["sentinel.txt"]
    finally:
        if junction.exists():
            os.rmdir(junction)


def test_https_instead_of_rewrite_cannot_start_ssh_marker(tmp_path, monkeypatch):
    marker = tmp_path / "ssh-started.txt"
    marker_program = tmp_path / "ssh_marker.py"
    marker_program.write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    config = tmp_path / "gitconfig"
    config.write_text(
        '[url "ssh://blocked.invalid/"]\n'
        "\tinsteadOf = https://rewrite.invalid/\n"
        '[protocol "ssh"]\n'
        "\tallow = always\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(
        "GIT_SSH_COMMAND",
        subprocess.list2cmdline([sys.executable, str(marker_program), str(marker)]),
    )

    try:
        with pytest.raises(GitClientError) as raised:
            GitClient(timeout_seconds=5).get_remote_head(
                "https://rewrite.invalid/repository.git", "main"
            )
    finally:
        assert not marker.exists()

    assert raised.value.code in {
        "GIT_NETWORK_UNAVAILABLE",
        "GIT_COMMAND_TIMEOUT",
        "GIT_CHECK_FAILED",
    }


def test_remote_commands_share_https_and_redirect_contract(tmp_path, monkeypatch):
    class RecordingGitClient(GitClient):
        def __init__(self):
            super().__init__(git_path="git")
            self.calls = []

        def _run(self, args, *, cwd):
            self.calls.append((args, cwd))
            if "--get-url" in args:
                if args[-1] == "origin":
                    return "https://example.invalid/repo.git"
                return args[-1]
            if "ls-remote" in args:
                return f"{'1' * 40}\trefs/heads/main"
            if "clone" in args:
                target = Path(args[-1])
                (target / ".git").mkdir()
                (target / ".git" / "config").write_text(
                    '[core]\n\trepositoryformatversion = 0\n\tbare = false\n'
                    '[remote "origin"]\n\turl = https://example.invalid/repo.git\n'
                    '\tfetch = +refs/heads/main:refs/remotes/origin/main\n',
                    encoding="utf-8",
                )
            return ""

    client = RecordingGitClient()
    client.get_remote_head("https://example.invalid/repo.git", "main")
    with _reserved_access(
        tmp_path, monkeypatch, "https://example.invalid/repo.git", project_id=704
    ) as (_, _, access):
        client.clone_branch("https://example.invalid/repo.git", "main", access)
        client.fetch_branch(access, "main")
    preflight_commands = [args for args, _ in client.calls if "--get-url" in args]
    remote_commands = [args for args, _ in client.calls if "--get-url" not in args]

    assert len(preflight_commands) == 3
    assert all("ls-remote" in args for args in preflight_commands)
    assert len(remote_commands) == 3
    for args in remote_commands:
        assert args[:4] == [
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.https://example.invalid/repo.git.followRedirects=false",
        ]
    assert "ls-remote" in remote_commands[0]
    assert "clone" in remote_commands[1]
    assert "fetch" in remote_commands[2]


def _configure_https_test_scope(
    tmp_path,
    probe_repo,
    monkeypatch,
    *,
    scope,
    url,
    rewritten_url=None,
):
    system_config = tmp_path / "system.gitconfig"
    global_config = tmp_path / "global.gitconfig"
    _git(
        tmp_path,
        "config",
        "--file",
        str(global_config),
        "credential.helper",
        "manager",
    )
    _git(
        tmp_path,
        "config",
        "--file",
        str(global_config),
        "http.sslBackend",
        "openssl",
    )

    def set_value(key, value):
        if scope == "system":
            _git(tmp_path, "config", "--file", str(system_config), key, value)
        elif scope == "global":
            _git(tmp_path, "config", "--file", str(global_config), key, value)
        else:
            _git(probe_repo, "config", key, value)

    effective_url = rewritten_url or url
    if rewritten_url:
        set_value(f"url.{rewritten_url}.insteadOf", url)
    set_value(f"http.{effective_url}.followRedirects", "true")
    set_value(f"http.{effective_url}.sslVerify", "false")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)


def _read_credential_helpers(client, git_path, probe_repo):
    result = subprocess.run(
        [
            git_path,
            *client._execution_policy(),
            "-C",
            str(probe_repo),
            "config",
            "--get-all",
            "credential.helper",
        ],
        env=client._environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=True,
    )
    normalized = []
    for value in result.stdout.splitlines():
        value = value.strip('"')
        if not value:
            continue
        executable = Path(value).name.lower()
        if executable == "git-credential-manager.exe":
            normalized.append("manager")
        elif executable == "git-credential-manager-core.exe":
            normalized.append("manager-core")
        elif executable == "git-credential-wincred.exe":
            normalized.append("wincred")
        else:
            normalized.append(value)
    return normalized


class _LocalTlsGitClient(GitClient):
    """仅测试本地自签名标记服务；产品代码不放宽 TLS 校验。"""

    @staticmethod
    def _http_redirect_policy(url):
        return [
            *GitClient._http_redirect_policy(url),
            "-c",
            "http.sslBackend=openssl",
            "-c",
            "http.sslVerify=false",
        ]


class _OpenSslTlsGitClient(GitClient):
    """仅固定测试 TLS backend；不关闭证书校验。"""

    @staticmethod
    def _http_redirect_policy(url):
        return [
            *GitClient._http_redirect_policy(url),
            "-c",
            "http.sslBackend=openssl",
        ]


@pytest.mark.parametrize(
    ("operation", "malicious_scope"),
    [
        ("ls-remote", "system"),
        ("ls-remote", "global"),
        ("clone", "system"),
        ("clone", "global"),
        ("fetch", "system"),
        ("fetch", "global"),
        ("fetch", "repo"),
    ],
)
def test_https_instead_of_rewrite_is_blocked_before_any_https_request(
    tmp_path,
    monkeypatch,
    https_marker,
    operation,
    malicious_scope,
):
    git_path = shutil.which("git")
    approved_url = https_marker["approved_url"]
    rewritten_url = https_marker["rewritten_url"]
    probe_repo = tmp_path / "rewrite-probe"
    probe_repo.mkdir()
    _git(probe_repo, "init")
    _git(probe_repo, "remote", "add", "origin", approved_url)
    _configure_https_test_scope(
        tmp_path,
        probe_repo,
        monkeypatch,
        scope=malicious_scope,
        url=approved_url,
        rewritten_url=rewritten_url,
    )
    client = _LocalTlsGitClient(git_path=git_path, timeout_seconds=5)
    repository = "origin" if operation == "fetch" else approved_url
    effective = subprocess.run(
        [git_path, "-C", str(probe_repo), "ls-remote", "--get-url", repository],
        env=os.environ.copy(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=True,
    )
    assert effective.stdout.strip() == rewritten_url

    with pytest.raises(GitClientError) as raised:
        if operation == "ls-remote":
            client.get_remote_head(approved_url, "main")
        elif operation == "clone":
            with _reserved_access(
                tmp_path, monkeypatch, approved_url, project_id=710
            ) as (_, _, access):
                client.clone_branch(approved_url, "main", access)
        else:
            client.fetch_branch(_captured_access(probe_repo, approved_url), "main")

    assert raised.value.code in {"GIT_CHECK_FAILED", "GIT_WORKSPACE_CONFLICT"}
    assert https_marker["counters"]["rewritten"] == 0
    assert https_marker["counters"]["redirect"] == 0
    if malicious_scope == "repo":
        assert https_marker["counters"]["approved"] == 0
    else:
        assert https_marker["counters"]["approved"] >= 1
    assert _read_credential_helpers(client, git_path, probe_repo) == [
        "manager"
    ]


@pytest.mark.parametrize(
    ("operation", "malicious_scope"),
    [
        ("ls-remote", "system"),
        ("clone", "global"),
        ("fetch", "global"),
    ],
)
def test_real_https_redirect_is_not_followed_for_remote_operations(
    tmp_path,
    monkeypatch,
    https_marker,
    operation,
    malicious_scope,
):
    git_path = shutil.which("git")
    approved_url = https_marker["rewritten_url"]
    probe_repo = tmp_path / "redirect-probe"
    probe_repo.mkdir()
    _git(probe_repo, "init")
    _git(probe_repo, "remote", "add", "origin", approved_url)
    _configure_https_test_scope(
        tmp_path,
        probe_repo,
        monkeypatch,
        scope=malicious_scope,
        url=approved_url,
    )
    client = _LocalTlsGitClient(git_path=git_path, timeout_seconds=5)
    repository = "origin" if operation == "fetch" else approved_url
    effective = subprocess.run(
        [git_path, "-C", str(probe_repo), "ls-remote", "--get-url", repository],
        env=client._environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=True,
    )
    assert effective.stdout.strip() == approved_url

    with pytest.raises(GitClientError) as raised:
        if operation == "ls-remote":
            client.get_remote_head(approved_url, "main")
        elif operation == "clone":
            with _reserved_access(
                tmp_path, monkeypatch, approved_url, project_id=711
            ) as (_, _, access):
                client.clone_branch(approved_url, "main", access)
        else:
            client.fetch_branch(_captured_access(probe_repo, approved_url), "main")

    assert raised.value.code == "GIT_CHECK_FAILED"
    assert https_marker["counters"]["health"] == 1
    assert https_marker["counters"]["rewritten"] >= 1
    assert https_marker["counters"]["redirect"] == 0
    assert _read_credential_helpers(client, git_path, probe_repo) == [
        "manager"
    ]


def test_system_and_global_config_are_isolated_but_approved_helper_is_retained(
    tmp_path, monkeypatch
):
    git_path = shutil.which("git")
    system_config = tmp_path / "system.gitconfig"
    global_config = tmp_path / "global.gitconfig"
    _git(tmp_path, "config", "--file", str(system_config), "core.fsmonitor", "blocked")
    _git(tmp_path, "config", "--file", str(global_config), "filter.bad.process", "blocked")
    _git(tmp_path, "config", "--file", str(global_config), "credential.helper", "manager")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("PATH", str(tmp_path / "attacker-path"))
    client = GitClient(git_path=git_path)
    result = subprocess.run(
        [git_path, *client._execution_policy(), "config", "--list"],
        env=client._environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=True,
    )
    values = result.stdout.splitlines()
    assert "credential.helper=manager" in values
    assert not any("filter.bad" in value or "fsmonitor=blocked" in value for value in values)
    assert client.credential_helper_commands == ("manager",)
    assert str(tmp_path / "attacker-path") not in client._environment()["PATH"]
    assert client._environment()["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_inherited_git_environment_is_default_deny_and_rebuilt_from_allowlist(
    monkeypatch, tmp_path
):
    injected = {
        "GIT_SSL_NO_VERIFY": "true",
        "GIT_TRACE": str(tmp_path / "trace.log"),
        "git_trace2_event": str(tmp_path / "trace2.json"),
        "GIT_CURL_VERBOSE": "1",
        "GIT_TRACE_CURL": str(tmp_path / "curl.log"),
        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='attacker'",
        "GIT_DIR": str(tmp_path / "attacker.git"),
        "GIT_WORK_TREE": str(tmp_path / "attacker-worktree"),
        "GIT_EXEC_PATH": str(tmp_path / "attacker-exec"),
        "GIT_OBJECT_DIRECTORY": str(tmp_path / "attacker-objects"),
        "gcm_trace": str(tmp_path / "gcm-trace.log"),
        "SSH_ASKPASS": str(tmp_path / "attacker-askpass.exe"),
        "ssh_auth_sock": str(tmp_path / "attacker-ssh.sock"),
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ORDINARY_ENV_SENTINEL", "preserved")

    environment = GitClient(allow_local_file=True)._environment()

    approved = {
        "GIT_TERMINAL_PROMPT",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_ALLOW_PROTOCOL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_ATTR_NOSYSTEM",
    }
    actual = {
        key.upper() for key in environment if key.casefold().startswith("git_")
    }
    assert actual == approved
    assert {
        key.upper() for key in environment if key.casefold().startswith("gcm_")
    } == {"GCM_INTERACTIVE"}
    assert {
        key.upper() for key in environment if key.casefold().startswith("ssh_")
    } == {"SSH_ASKPASS_REQUIRE"}
    assert environment["GIT_ALLOW_PROTOCOL"] == "https:file"
    assert environment["ORDINARY_ENV_SENTINEL"] == "preserved"


def test_real_trace2_external_write_is_blocked_with_positive_control(
    local_remote, tmp_path, monkeypatch
):
    _source, remote = local_remote
    client = GitClient(allow_local_file=True)
    with _reserved_access(
        tmp_path, monkeypatch, str(remote), project_id=741
    ) as (_paths, _ownership, access):
        client.clone_branch(str(remote), "main", access)
        marker = tmp_path / "outside-worktree-trace2.json"
        unprotected_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.casefold().startswith("git_")
        }
        unprotected_environment["GIT_TRACE2_EVENT"] = str(marker)
        result = subprocess.run(
            [
                shutil.which("git"),
                "-C",
                str(access.path),
                "status",
                "--porcelain",
            ],
            env=unprotected_environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            shell=False,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0
        assert marker.is_file() and marker.stat().st_size > 0
        marker.unlink()

        monkeypatch.setenv("GIT_TRACE2_EVENT", str(marker))
        monkeypatch.setenv("GIT_TRACE", str(tmp_path / "outside-trace.log"))
        monkeypatch.setenv("GIT_TRACE_CURL", str(tmp_path / "outside-curl.log"))
        client.assert_clean(access)

        assert not marker.exists()
        assert not (tmp_path / "outside-trace.log").exists()
        assert not (tmp_path / "outside-curl.log").exists()


def test_real_ssl_no_verify_inheritance_is_blocked_with_positive_control(
    https_marker, tmp_path, monkeypatch
):
    git_path = shutil.which("git")
    approved_url = https_marker["approved_url"]
    unprotected_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith("git_")
    }
    unprotected_environment.update(
        {
            "GIT_SSL_NO_VERIFY": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "https",
        }
    )
    subprocess.run(
        [
            git_path,
            "-c",
            "http.sslBackend=openssl",
            "-c",
            "http.followRedirects=false",
            "ls-remote",
            approved_url,
        ],
        cwd=tmp_path,
        env=unprotected_environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
        timeout=10,
    )
    assert https_marker["counters"]["approved"] >= 1
    https_marker["counters"]["approved"] = 0

    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "true")
    client = _OpenSslTlsGitClient(git_path=git_path, timeout_seconds=5)
    with pytest.raises(GitClientError):
        client.get_remote_head(approved_url, "main")

    assert "GIT_SSL_NO_VERIFY" not in client._environment()
    assert https_marker["counters"]["approved"] == 0


def test_unapproved_shell_credential_helper_cannot_execute(
    https_marker, tmp_path, monkeypatch
):
    git_path = shutil.which("git")
    marker = tmp_path / "credential-helper-marker.txt"
    program = tmp_path / "credential-helper-sentinel.sh"
    _write_git_program(program)
    global_config = tmp_path / "credential-attack.gitconfig"
    _git(
        tmp_path,
        "config",
        "--file",
        str(global_config),
        "credential.helper",
        f'!"{program.as_posix()}"',
    )
    monkeypatch.setenv("EXEC_MARKER", marker.as_posix())
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    unprotected_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith("git_")
    }
    unprotected_environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    subprocess.run(
        [
            git_path,
            "-c",
            "http.sslBackend=openssl",
            "-c",
            "http.sslVerify=false",
            "ls-remote",
            https_marker["auth_url"],
        ],
        cwd=tmp_path,
        env=unprotected_environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
        timeout=10,
    )
    assert marker.is_file()
    marker.unlink()

    monkeypatch.setattr(
        git_client_module, "_configured_credential_helpers", lambda _git_path: ()
    )
    client = _LocalTlsGitClient(git_path=git_path, timeout_seconds=5)
    assert client.credential_helper_commands == ()
    with pytest.raises(GitClientError):
        client.get_remote_head(https_marker["auth_url"], "main")
    assert not marker.exists()


def test_subprocess_is_array_noninteractive_and_shell_false(monkeypatch, tmp_path):
    observed = {}
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "credential-config-sentinel")

    class FakeProcess:
        returncode = 0
        pid = 12345

        def communicate(self, timeout):
            observed["timeout"] = timeout
            return b"git version 9.9.9\n", b""

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert GitClient(git_path="git").get_version() == "git version 9.9.9"
    assert isinstance(observed["command"], list)
    assert observed["command"][1:3] == ["-c", f"core.hooksPath={os.devnull}"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert observed["kwargs"]["env"]["GCM_INTERACTIVE"] == "Never"
    assert observed["kwargs"]["env"]["SSH_ASKPASS_REQUIRE"] == "never"
    assert observed["kwargs"]["env"]["GIT_PROTOCOL_FROM_USER"] == "0"
    assert observed["kwargs"]["env"]["GIT_ALLOW_PROTOCOL"] == "https"
    assert observed["kwargs"]["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert observed["kwargs"]["env"]["GIT_CONFIG_SYSTEM"] == os.devnull
    assert observed["kwargs"]["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not any(item in observed["command"] for item in ("push", "merge", "rebase", "tag", "commit"))


def _write_hook(hooks_dir: Path, name: str) -> None:
    hook = hooks_dir / name
    hook.write_text(
        '#!/bin/sh\nprintf "%s" "$0" > "$HOOK_MARKER"\n',
        encoding="utf-8",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_git_program(path: Path, *, passthrough: bool = False) -> None:
    body = '#!/bin/sh\nprintf "started\\n" >> "$EXEC_MARKER"\n'
    if passthrough:
        body += "cat\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _set_scoped_config(tmp_path, repo_path, monkeypatch, scope, key, value):
    system_config = tmp_path / f"{scope}-system.gitconfig"
    global_config = tmp_path / f"{scope}-global.gitconfig"
    system_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    _git(tmp_path, "config", "--file", str(global_config), "credential.helper", "manager")
    if scope == "system":
        _git(tmp_path, "config", "--file", str(system_config), key, value)
    elif scope == "global":
        _git(tmp_path, "config", "--file", str(global_config), key, value)
    else:
        _git(repo_path, "config", key, value)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)


@pytest.mark.parametrize("scope", ["system", "global"])
@pytest.mark.parametrize("driver_key", ["filter.sentinel.smudge", "filter.sentinel.process"])
def test_real_filter_driver_cannot_start_during_clone(
    local_remote, tmp_path, monkeypatch, scope, driver_key
):
    source, remote = local_remote
    (source / ".gitattributes").write_text("*.txt filter=sentinel\n", encoding="utf-8")
    _git(source, "add", ".gitattributes")
    _git(source, "commit", "-m", "add filter attribute")
    _git(source, "push", "origin", "main")
    marker = tmp_path / "filter-marker.txt"
    program = tmp_path / "filter-sentinel.sh"
    _write_git_program(program, passthrough=True)
    monkeypatch.setenv("EXEC_MARKER", marker.as_posix())
    _set_scoped_config(
        tmp_path,
        source,
        monkeypatch,
        scope,
        driver_key,
        f'"{program.as_posix()}"',
    )
    client = GitClient(timeout_seconds=10, allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=730) as (_, _, access):
        client.clone_branch(str(remote), "main", access)
        assert not marker.exists()
        assert _read_credential_helpers(client, shutil.which("git"), access.path) == ["manager"]

    control = tmp_path / "unprotected-filter-clone"
    subprocess.run(
        [shutil.which("git"), "clone", "--branch", "main", str(remote), str(control)],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        shell=False,
        check=False,
        timeout=10,
    )
    assert marker.exists()


@pytest.mark.parametrize("scope", ["global", "repo"])
def test_real_filter_process_cannot_start_during_detached_checkout(
    local_remote, tmp_path, monkeypatch, scope
):
    source, remote = local_remote
    (source / ".gitattributes").write_text("*.txt filter=sentinel\n", encoding="utf-8")
    _git(source, "add", ".gitattributes")
    _git(source, "commit", "-m", "add checkout filter attribute")
    (source / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-m", "checkout filter fixture")
    _git(source, "push", "origin", "main")
    marker = tmp_path / "checkout-filter-marker.txt"
    program = tmp_path / "checkout-filter-sentinel.sh"
    _write_git_program(program, passthrough=True)
    monkeypatch.setenv("EXEC_MARKER", marker.as_posix())
    setup_client = GitClient(timeout_seconds=10, allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=733) as (_, _, access):
        setup_client.clone_branch(str(remote), "main", access)
        prior_head = _git(access.path, "rev-parse", "HEAD~1")
        _git(access.path, "checkout", "--force", "--detach", prior_head)
        _git(access.path, "reset", "--hard", "HEAD")
        _set_scoped_config(
            tmp_path,
            access.path,
            monkeypatch,
            scope,
            "filter.sentinel.process",
            program.as_posix(),
        )
        client = GitClient(timeout_seconds=10, allow_local_file=True)
        if scope == "repo":
            with pytest.raises(GitClientError) as raised:
                client.checkout_remote_head(access, "main")
            assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
        else:
            try:
                client.checkout_remote_head(access, "main")
            except GitClientError as exc:
                assert exc.code == "GIT_CHECK_FAILED"
        assert not marker.exists()
        if scope != "repo":
            subprocess.run(
                [
                    shutil.which("git"),
                    *client._execution_policy(),
                    "-C",
                    str(access.path),
                    "checkout",
                    "--force",
                    "--detach",
                    prior_head,
                ],
                env=client._environment(),
                capture_output=True,
                shell=False,
                check=True,
                timeout=10,
            )

        subprocess.run(
            [
                shutil.which("git"),
                "-C",
                str(access.path),
                "checkout",
                "--detach",
                "refs/remotes/origin/main",
            ],
            env=os.environ.copy(),
            capture_output=True,
            shell=False,
            check=False,
            timeout=10,
        )
        assert marker.exists()


@pytest.mark.parametrize("scope", ["system", "global", "repo"])
def test_real_fsmonitor_cannot_start_during_status(
    local_remote, tmp_path, monkeypatch, scope
):
    _, remote = local_remote
    marker = tmp_path / "fsmonitor-marker.txt"
    program = tmp_path / "fsmonitor-sentinel.sh"
    _write_git_program(program)
    monkeypatch.setenv("EXEC_MARKER", marker.as_posix())
    setup_client = GitClient(timeout_seconds=10, allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=731) as (_, _, access):
        setup_client.clone_branch(str(remote), "main", access)
        _set_scoped_config(
            tmp_path, access.path, monkeypatch, scope, "core.fsmonitor", program.as_posix()
        )
        client = GitClient(timeout_seconds=10, allow_local_file=True)
        if scope == "repo":
            with pytest.raises(GitClientError) as raised:
                client.assert_clean(access)
            assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
        else:
            client.assert_clean(access)
        assert not marker.exists()
        assert _read_credential_helpers(client, shutil.which("git"), access.path) == ["manager"]

        subprocess.run(
            [shutil.which("git"), "-C", str(access.path), "status", "--porcelain"],
            env=os.environ.copy(),
            capture_output=True,
            shell=False,
            check=False,
            timeout=10,
        )
        assert marker.exists()


@pytest.mark.parametrize("scope", ["system", "global", "repo"])
@pytest.mark.parametrize("include_key", ["include.path", "includeIf.gitdir:**/.path"])
def test_include_injected_execution_config_is_not_loaded(
    local_remote, tmp_path, monkeypatch, scope, include_key
):
    _, remote = local_remote
    marker = tmp_path / "include-marker.txt"
    program = tmp_path / "include-sentinel.sh"
    included = tmp_path / "included.gitconfig"
    _write_git_program(program)
    _git(tmp_path, "config", "--file", str(included), "core.fsmonitor", program.as_posix())
    monkeypatch.setenv("EXEC_MARKER", marker.as_posix())
    setup_client = GitClient(timeout_seconds=10, allow_local_file=True)
    with _reserved_access(tmp_path, monkeypatch, str(remote), project_id=732) as (_, _, access):
        setup_client.clone_branch(str(remote), "main", access)
        _set_scoped_config(
            tmp_path, access.path, monkeypatch, scope, include_key, str(included)
        )
        client = GitClient(timeout_seconds=10, allow_local_file=True)
        if scope == "repo":
            with pytest.raises(GitClientError):
                client.assert_clean(access)
        else:
            client.assert_clean(access)
        assert not marker.exists()

        subprocess.run(
            [shutil.which("git"), "-C", str(access.path), "status", "--porcelain"],
            env=os.environ.copy(),
            capture_output=True,
            shell=False,
            check=False,
            timeout=10,
        )
        assert marker.exists()


def _configure_malicious_hooks(
    tmp_path,
    repo_path,
    monkeypatch,
    *,
    scope,
    hooks_dir,
    marker,
):
    system_config = tmp_path / "hook-system.gitconfig"
    global_config = tmp_path / "hook-global.gitconfig"
    system_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    _git(
        tmp_path,
        "config",
        "--file",
        str(global_config),
        "credential.helper",
        "manager",
    )
    if scope == "system":
        _git(
            tmp_path,
            "config",
            "--file",
            str(system_config),
            "core.hooksPath",
            str(hooks_dir),
        )
    elif scope == "global":
        _git(
            tmp_path,
            "config",
            "--file",
            str(global_config),
            "core.hooksPath",
            str(hooks_dir),
        )
    else:
        _git(repo_path, "config", "core.hooksPath", str(hooks_dir))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("HOOK_MARKER", str(marker))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)


@pytest.mark.parametrize(
    ("operation", "malicious_scope"),
    [
        ("clone", "system"),
        ("clone", "global"),
        ("fetch", "system"),
        ("fetch", "global"),
        ("fetch", "repo"),
        ("checkout", "system"),
        ("checkout", "global"),
        ("checkout", "repo"),
    ],
)
def test_real_git_hooks_are_disabled_without_hiding_credential_helper(
    local_remote,
    tmp_path,
    monkeypatch,
    operation,
    malicious_scope,
):
    source, remote = local_remote
    git_path = shutil.which("git")
    hooks_dir = tmp_path / "malicious-hooks"
    hooks_dir.mkdir()
    _write_hook(hooks_dir, "post-checkout")
    _write_hook(hooks_dir, "reference-transaction")
    marker = tmp_path / "hook-started.txt"
    client = GitClient(git_path=git_path, timeout_seconds=10, allow_local_file=True)
    with _reserved_access(
        tmp_path, monkeypatch, str(remote), project_id=720
    ) as (_, _, access):
        if operation != "clone":
            client.clone_branch(str(remote), "main", access)
        _configure_malicious_hooks(
            tmp_path,
            access.path,
            monkeypatch,
            scope=malicious_scope,
            hooks_dir=hooks_dir,
            marker=marker,
        )
        protected_client = GitClient(
            git_path=git_path, timeout_seconds=10, allow_local_file=True
        )

        if operation == "clone":
            protected_client.clone_branch(str(remote), "main", access)
        elif operation == "fetch":
            (source / "tracked.txt").write_text("hook fetch\n", encoding="utf-8")
            _git(source, "add", "tracked.txt")
            _git(source, "commit", "-m", "hook fetch")
            _git(source, "push", "origin", "main")
            marker.unlink(missing_ok=True)
            if malicious_scope == "repo":
                with pytest.raises(GitClientError) as raised:
                    protected_client.fetch_branch(access, "main")
                assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
            else:
                protected_client.fetch_branch(access, "main")
        elif malicious_scope == "repo":
            with pytest.raises(GitClientError) as raised:
                protected_client.checkout_remote_head(access, "main")
            assert raised.value.code == "GIT_WORKSPACE_CONFLICT"
        else:
            protected_client.checkout_remote_head(access, "main")

        assert not marker.exists()
        assert _read_credential_helpers(protected_client, git_path, access.path) == ["manager"]


def test_malicious_hooks_positive_control_runs_without_command_policy(
    local_remote, tmp_path, monkeypatch
):
    source, remote = local_remote
    hooks_dir = tmp_path / "malicious-hooks"
    hooks_dir.mkdir()
    _write_hook(hooks_dir, "post-checkout")
    _write_hook(hooks_dir, "reference-transaction")
    marker = tmp_path / "hook-started.txt"
    control_checkout = tmp_path / "unprotected-clone"
    _configure_malicious_hooks(
        tmp_path,
        source,
        monkeypatch,
        scope="global",
        hooks_dir=hooks_dir,
        marker=marker,
    )

    _git(tmp_path, "clone", "--branch", "main", str(remote), str(control_checkout))
    assert marker.exists()

    marker.unlink()
    (source / "tracked.txt").write_text("positive control\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-m", "positive control")
    _git(source, "push", "origin", "main")
    marker.unlink(missing_ok=True)
    _git(control_checkout, "fetch", "origin", "main")
    assert marker.exists()


def test_git_client_contains_no_forbidden_git_action_or_shell_true():
    source = inspect.getsource(GitClient)
    for forbidden in ('"push"', '"merge"', '"rebase"', '"tag"', '"commit"', "shell=True"):
        assert forbidden not in source
