from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "windows" / "build-distribution.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_candidate_bundle_is_explicitly_not_a_formal_release() -> None:
    text = _text()
    assert "anxin_windows_delivery_candidate_v1" in text
    assert "formal_release = $false" in text
    assert "formal_release = $false" in text
    assert "clean Windows evidence and formal signing/release remain pending" in text


def test_candidate_bundle_contains_runtime_and_lifecycle_tools_without_dev_runtime() -> None:
    text = _text()
    assert "payload/AnxinBoard.Runtime/AnxinBoard.Runtime.exe" in text
    assert "tools/install-runtime.ps1" in text
    assert "tools/rollback-runtime.ps1" in text
    assert "tools/uninstall-product.ps1" in text
    assert "payload/AnxinBoard.Runtime/tools/collect-diagnostics.ps1" in text
    assert "tools/resolve-installed-runtime.ps1" in text
    assert "diagnostics_tool = 'payload/AnxinBoard.Runtime/tools/collect-diagnostics.ps1'" in text
    assert "runtime_resolver_tool = 'tools/resolve-installed-runtime.ps1'" in text
    assert "'tools/collect-diagnostics.ps1' =" not in text
    assert "$gitRuntimePrerequisite = 'Git for Windows 2.45+ with --no-lazy-fetch capability'" in text
    assert "normal_user_machine_prerequisites = @($gitRuntimePrerequisite)" in text
    assert "npm run dev" not in text


def test_candidate_bundle_declares_digest_and_reproducibility_scope() -> None:
    text = _text()
    assert "payload_manifest_sha256" in text
    assert "delivery-manifest.json" in text
    assert "fixed ZIP timestamps" in text
    assert "LastWriteTime = $fixedTime" in text
    assert "Get-FileHash" in text
    assert "DELIVERY_ZIP_SHA256" in text


def test_distribution_revalidates_exact_payload_before_bundle_creation() -> None:
    text = _text()
    assert "Runtime payload member set does not match manifest" in text
    assert "Runtime payload contains an unmanifested file" in text
    assert "Payload root contains files outside the runtime manifest contract" in text
    assert "Payload root contains an unmanifested delivery file" in text
    assert "Delivery payload contains a reparse point" in text
    assert "Assert-NoReparseRelativeChain" in text
    assert "Runtime manifest contains duplicate path identity" in text
    assert "Runtime manifest member digest is invalid" in text
    assert "foreach ($binding in @('runtime', 'frontend', 'diagnostics'))" in text
    assert text.index("Runtime payload member set does not match manifest") < text.index(
        "anxin_windows_delivery_candidate_v1"
    )


def test_failed_distribution_removes_partial_candidate_zip() -> None:
    text = _text()
    assert "if (Test-Path -LiteralPath $zipPath) { [System.IO.File]::Delete($zipPath) }" in text
    assert "[System.IO.FileMode]::CreateNew" in text


def test_candidate_bundle_never_reads_credentials_or_real_user_data() -> None:
    lowered = _text().casefold()
    for token in ("cmdkey", "get-storedcredential", "local-run.json", "anxinboard.db"):
        assert token not in lowered
