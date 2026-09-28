from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "apps" / "backend" / "product_entry.py"
BUILD = ROOT / "installer" / "windows" / "build-runtime.ps1"
SMOKE = ROOT / "installer" / "windows" / "test-runtime.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-product-delivery.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_formal_runtime_is_single_loopback_process_with_built_ui() -> None:
    text = _text(ENTRY)
    assert 'host="127.0.0.1"' in text
    assert 'app.mount("/", StaticFiles' in text
    assert 'ANXINBOARD_PRODUCT_UI_ROOT' in text
    assert "vite" not in text.casefold()
    assert "node" not in text.casefold()
    for token in ("windows_credential_store", "cmdkey", "get-storedcredential", "deepseek-api-key"):
        assert token not in text.casefold()
    assert "Start-Process" not in text


def test_packaging_dependencies_are_build_time_only() -> None:
    build = _text(BUILD)
    assert "npm.Source run build" in build
    assert "PyInstaller" in build
    assert "build-venv" in build
    assert "runtime-manifest.json" in build
    assert "Get-FileHash" in build
    assert "ANXINBOARD_LOCAL_BOOTSTRAP_SECRET" not in build
    assert "Credential" not in build


def test_runtime_provenance_requires_explicit_exact_product_head() -> None:
    build = _text(BUILD)
    workflow = _text(WORKFLOW)
    assert "[string]$SourceCommit" in build
    assert "SourceCommit must be an exact 40-character Git commit SHA" in build
    assert "SourceCommit must equal exact Product repository HEAD" in build
    assert "$env:GITHUB_SHA" not in build
    assert "PRODUCT_SOURCE_COMMIT: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}" in workflow
    assert "ref: ${{ env.PRODUCT_SOURCE_COMMIT }}" in workflow
    assert "-SourceCommit $env:PRODUCT_SOURCE_COMMIT" in workflow
    assert "delivery candidate source commit mismatch" in workflow


def test_disposable_smoke_uses_fake_session_material_and_own_process_only() -> None:
    smoke = _text(SMOKE)
    assert "lane-c-ci-bootstrap-not-a-real-secret-2026" in smoke
    assert "/api/health" in smoke
    assert "/api/local-session/exchange" in smoke
    assert "Stop-Process -Id $process.Id" in smoke
    assert "cmdkey" not in smoke.casefold()
    for token in ("windows_credential_store", "cmdkey", "get-storedcredential", "deepseek-api-key"):
        assert token not in smoke.casefold()


def test_disposable_smoke_binds_and_restores_an_explicit_isolated_database() -> None:
    smoke = _text(SMOKE)
    assert "$oldDbPath = [Environment]::GetEnvironmentVariable('ANXINBOARD_DB_PATH', 'Process')" in smoke
    assert "$testDataRoot = Join-Path $testLocalApp 'AnxinBoard'" in smoke
    assert "$testDbPath = Join-Path $testDataRoot 'anxinboard.db'" in smoke
    assert "[Environment]::SetEnvironmentVariable('ANXINBOARD_DB_PATH', $testDbPath, 'Process')" in smoke
    assert "packaged runtime did not initialize the explicit isolated product database" in smoke
    assert "[Environment]::SetEnvironmentVariable('ANXINBOARD_DB_PATH', $oldDbPath, 'Process')" in smoke


def test_disposable_smoke_web_requests_are_windows_powershell_51_compatible() -> None:
    smoke = _text(SMOKE)
    web_requests = [line.strip() for line in smoke.splitlines() if "Invoke-WebRequest" in line]
    assert len(web_requests) == 2
    assert all("-UseBasicParsing" in line for line in web_requests)


def test_hosted_jobs_move_temp_data_and_pytest_outside_git_ancestor() -> None:
    for path in (WORKFLOW, CI_WORKFLOW):
        workflow = _text(path)
        assert "ANXINBOARD_CI_ROOT" in workflow
        assert "Test-Path -LiteralPath (Join-Path $ancestor.FullName '.git')" in workflow
        assert '"TEMP=$temp"' in workflow
        assert '"TMP=$temp"' in workflow
        assert '"TMPDIR=$temp"' in workflow
        assert '"LOCALAPPDATA=$localApp"' in workflow
        # Product overrides are accepted only below tempfile.gettempdir().
        assert "ANXINBOARD_PROJECTS_ROOT=$(Join-Path $temp 'projects')" in workflow
        assert "ANXINBOARD_PRD_ROOT=$(Join-Path $temp 'prd')" in workflow
        assert '--basetemp="$baseTemp"' in workflow


def test_public_workflows_have_no_live_credentials_or_physical_runner_authority() -> None:
    for path in (WORKFLOW, CI_WORKFLOW):
        workflow = _text(path)
        assert "permissions:\n  contents: read" in workflow.replace("\r\n", "\n")
        assert "self-hosted" not in workflow
        assert "secrets." not in workflow
        assert "run_brownfield_baseline_live.py" not in workflow
        assert "run_brownfield_atlas_live_spike.py" not in workflow
        assert "workflow_dispatch:" in workflow
        assert "pytest" in workflow


def test_windows_harness_does_not_claim_win10_or_win11_clean_machine_proof() -> None:
    workflow = _text(WORKFLOW)
    assert "windows-2022" in workflow
    assert "windows-2025" in workflow
    assert "windows-10" not in workflow.casefold()
    assert "windows-11" not in workflow.casefold()
    assert "clean-machine-pass" not in workflow.casefold()
