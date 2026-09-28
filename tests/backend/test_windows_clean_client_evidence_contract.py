from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "windows" / "test-clean-client-evidence.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_clean_client_harness_requires_explicit_disposable_vm_and_workstation() -> None:
    text = _text()
    assert "ANXINBOARD_DISPOSABLE_VM_EVIDENCE" in text
    assert "-AllowDefaultUserInstall" in text or "AllowDefaultUserInstall" in text
    assert "ProductType" in text
    assert "Windows workstation ProductType=1" in text
    assert "not recognizably virtual/disposable" in text
    assert "virtual|vmware|virtualbox|kvm|qemu|hyper-v|parallels|ec2|google compute" in text


def test_clean_client_harness_distinguishes_windows_10_and_11_by_client_build() -> None:
    text = _text()
    assert "ValidateSet('windows10', 'windows11')" in text
    assert "$build -ge 22000" in text
    assert "Expected $ExpectedClient but detected $actualClient" in text
    assert "os_build" in text
    assert "product_type" in text


def test_clean_client_harness_attests_candidate_before_install() -> None:
    text = _text()
    assert "Candidate ZIP digest mismatch" in text
    assert "Assert-DeliveryBundle -Root $extractRoot" in text
    assert "Delivery bundle member set mismatch" in text
    assert "Delivery bundle contains an unmanifested member" in text
    assert "Delivery member digest mismatch" in text
    assert text.index("Assert-DeliveryBundle -Root $extractRoot") < text.index(
        "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installTool"
    )


def test_clean_client_harness_refuses_to_overclaim_launcher_or_credentials() -> None:
    text = _text()
    assert "launcher_130_integration = 'not_exercised'" in text
    assert "credential_secret_access = 'not_performed'" in text
    assert "CLEAN_WINDOWS_CLIENT_EVIDENCE=SUCCESS_PRE_LAUNCHER" in text
    assert "LAUNCHER_130_INTEGRATION=NOT_EXERCISED" in text
    assert "CREDENTIAL_SECRET_ACCESS=NOT_PERFORMED" in text
    assert "launcher_authority -ne 'not_performed'" in text
    assert "credential_access -ne 'not_performed'" in text
    lowered = text.casefold()
    assert "cmdkey" not in lowered
    assert "get-storedcredential" not in lowered


def test_clean_client_harness_exercises_full_prelauncher_lifecycle_and_preservation() -> None:
    text = _text()
    for marker in (
        "result.install = 'passed'",
        "result.runtime_resolver = 'passed'",
        "result.runtime_smoke = 'passed'",
        "result.idempotent_reinstall = 'passed'",
        "result.synthetic_upgrade = 'passed'",
        "result.rollback = 'passed'",
        "result.diagnostics = 'passed'",
        "result.uninstall = 'passed'",
        "result.user_data_preserved = 'passed'",
    ):
        assert marker in text
    assert "rollback_tool" in text
    assert "runtime_resolver_tool" in text
    assert "Synthetic upgrade did not preserve rollback identity" in text
    assert "Rollback did not restore the original payload identity" in text
    assert "Resolver did not follow upgraded active payload" in text
    assert "Resolver did not follow rolled-back active payload" in text
    assert "Clean-client program root survived uninstall" in text
    assert "Clean-client user data root was not preserved" in text


def test_clean_client_diagnostics_uses_installed_manifest_bound_copy() -> None:
    text = _text()
    assert "Resolve-InstalledDiagnosticsTool" in text
    assert "Installed diagnostics manifest schema mismatch" in text
    assert "Installed diagnostics binding is invalid" in text
    assert "Installed diagnostics tool is a reparse point" in text
    assert "$activePayload = Join-Path (Join-Path $programRoot 'versions') $firstDigest" in text
    assert "$installedDiagnosticsTool = Resolve-InstalledDiagnosticsTool -InstalledPayload $activePayload" in text
    assert "-File $installedDiagnosticsTool" in text
    assert "-File $diagnosticsTool" not in text
