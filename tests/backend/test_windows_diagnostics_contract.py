from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "windows" / "collect-diagnostics.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_diagnostics_has_fixed_product_owned_default_root_and_test_gate() -> None:
    text = _text()
    assert "Join-Path $env:LOCALAPPDATA 'AnxinBoard'" in text
    assert "ANXINBOARD_DIAGNOSTICS_TEST_MODE" in text
    assert "TestDataRoot is available only in explicit diagnostics test mode" in text
    assert "Diagnostics data root must be local" in text


def test_runtime_manifest_override_is_test_only_and_production_is_install_relative() -> None:
    text = _text()
    assert "RuntimeManifest override is available only in explicit diagnostics test mode" in text
    assert "$runtimeRoot = Split-Path -Parent $PSScriptRoot" in text
    assert "$versionRoot = Split-Path -Parent $runtimeRoot" in text
    assert "Join-Path $versionRoot 'runtime-manifest.json'" in text
    assert "runtime_payload_invalid" in text
    assert "parse_status = 'unsafe_path'" in text


def test_diagnostics_refuses_reparse_output_root_and_never_follows_data_reparse_files() -> None:
    text = _text()
    assert "Diagnostics data root is a reparse point; refusing collection" in text
    assert "Diagnostics output directory is a reparse point; refusing collection" in text
    assert "Test-IsReparsePoint -Item $stateItem" in text
    assert "Test-IsReparsePoint -Item $dbItem" in text
    assert "state_unsafe_path" in text
    assert "database_unsafe_path" in text


def test_diagnostics_whitelists_runtime_state_instead_of_copying_raw_state() -> None:
    text = _text()
    assert "Get-Content -LiteralPath $StateFile" in text
    assert "role =" in text
    assert "pid =" in text
    assert "name =" in text
    assert "observed =" in text
    assert "repoRoot" not in text
    assert "startTime" not in text
    assert "ConvertTo-Json -Depth 8 -Compress" in text


def test_diagnostics_classifies_required_failure_domains_without_auto_repair() -> None:
    text = _text()
    assert "failure_domains" in text
    for domain in ("install =", "runtime =", "port =", "session =", "database =", "config ="):
        assert domain in text
    assert "runtime_payload_missing" in text
    assert "runtime_payload_invalid" in text
    assert "state_invalid" in text
    assert "state_unsafe_path" in text
    assert "process_identity_mismatch" in text
    assert "recorded_process_not_running" in text
    assert "database_missing" in text
    assert "database_unsafe_path" in text
    assert "database_empty_or_unreadable_metadata" in text
    assert "browser_session_is_memory_only" in text
    assert "credential_values_are_outside_diagnostics" in text
    assert "automatic_repair = [ordered]@{ performed = $false }" in text


def test_port_diagnostic_is_bounded_to_loopback_product_health() -> None:
    text = _text()
    assert 'http://127.0.0.1:$Port/api/health' in text
    assert "$request.Proxy = $null" in text
    assert "$request.Timeout = 1500" in text
    assert "external_network = 'not_used'" in text


def test_diagnostics_never_queries_credentials_environment_or_raw_logs() -> None:
    lowered = _text().casefold()
    forbidden = [
        "cmdkey",
        "get-storedcredential",
        "windows credential manager",
        "get-childitem env:",
        "authorization",
        "cookie",
        "*.log",
    ]
    for token in forbidden:
        assert token not in lowered
    assert "secret_values = 'never_collected'" in lowered
    assert "enumeration = 'not_performed'" in lowered
    assert "raw_logs" in lowered


def test_diagnostics_package_contains_only_generated_summary_and_cleans_partial_zip() -> None:
    text = _text()
    assert "CreateEntry('summary.json'" in text
    assert "ZipArchiveMode]::Create" in text
    assert "Get-ChildItem" not in text
    assert "[System.IO.File]::Delete($packagePath)" in text
