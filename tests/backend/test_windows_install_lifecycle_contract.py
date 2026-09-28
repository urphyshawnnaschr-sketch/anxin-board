from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "installer" / "windows"
INSTALL = WINDOWS / "install-runtime.ps1"
ROLLBACK = WINDOWS / "rollback-runtime.ps1"
UNINSTALL = WINDOWS / "uninstall-product.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_install_is_user_scope_versioned_and_validates_payload_hashes() -> None:
    text = _text(INSTALL)
    assert "Programs\\AnxinBoard" in text
    assert "versions" in text
    assert "Get-FileHash" in text
    assert "anxin_windows_runtime_payload_v1" in text
    assert "current_payload_digest" in text
    assert ".staging-" in text
    assert "Move-Item -LiteralPath $staging -Destination $targetVersion" in text
    assert "ANXINBOARD_INSTALLER_TEST_MODE" in text
    assert "Credential" not in text


def test_install_rejects_reparse_unmanifested_and_unbound_payload_members() -> None:
    text = _text(INSTALL)
    assert "Assert-NoReparseRelativeChain" in text
    assert "Runtime payload contains a reparse point" in text
    assert "Runtime payload member set does not match manifest" in text
    assert "Runtime payload contains an unmanifested file" in text
    assert "Runtime manifest contains duplicate path identity" in text
    assert "Runtime manifest member digest is invalid" in text
    assert "foreach ($binding in @('runtime', 'frontend', 'diagnostics'))" in text
    assert text.count("Assert-RuntimeTree -Manifest $manifest") >= 3


def test_install_root_and_state_identity_are_reparse_fail_closed() -> None:
    text = _text(INSTALL)
    assert "Assert-NoReparseExistingAncestorChain -Path $installRoot" in text
    assert "Install path ancestor contains a reparse point" in text
    assert "Get-ChildItem -LiteralPath $installRoot -Force" in text
    assert "Where-Object { $_.Name -ieq 'install-state.json' }" in text
    assert "Existing install state is not a plain file" in text
    assert "Install state identity is ambiguous" in text
    assert "Installed current payload digest is invalid" in text
    assert "Installed previous payload digest is invalid" in text


def test_install_staging_and_state_temp_are_cleaned_fail_visible() -> None:
    text = _text(INSTALL)
    assert "if (Test-Path -LiteralPath $staging) { [System.IO.Directory]::Delete($staging, $true) }" in text
    assert "if (Test-Path -LiteralPath $tempState) { [System.IO.File]::Delete($tempState) }" in text
    assert "Move-Item -LiteralPath $tempState -Destination $statePath -Force" in text
    assert "Temporary install state is not a file" in text
    assert "Written install state is not a file" in text


def test_upgrade_preserves_previous_payload_for_pointer_rollback() -> None:
    install = _text(INSTALL)
    rollback = _text(ROLLBACK)
    assert "previous_payload_digest" in install
    assert "previous_payload_digest" in rollback
    assert "current_payload_digest = $previous" in rollback
    assert "previous_payload_digest = $current" in rollback
    assert "Directory]::Delete($previousDir" not in rollback


def test_rollback_revalidates_entire_previous_runtime_before_pointer_switch() -> None:
    text = _text(ROLLBACK)
    assert "Assert-RuntimeTree -Manifest $manifest -RuntimeRoot $previousRuntimeRoot" in text
    assert "Get-FileHash -LiteralPath $candidate -Algorithm SHA256" in text
    assert "Rollback runtime file size mismatch" in text
    assert "Rollback runtime file digest mismatch" in text
    assert "Rollback runtime member set does not match manifest" in text
    assert "Rollback runtime contains an unmanifested file" in text
    assert "Rollback target contains a reparse point" in text
    assert "Assert-NoReparseRelativeChain" in text
    assert text.index("Assert-RuntimeTree -Manifest $manifest -RuntimeRoot $previousRuntimeRoot") < text.index(
        "current_payload_digest = $previous"
    )


def test_rollback_rejects_reparse_ancestor_before_state_or_payload_read() -> None:
    text = _text(ROLLBACK)
    assert "Assert-NoReparseExistingAncestorChain -Path $installRoot" in text
    assert "Rollback path ancestor contains a reparse point" in text
    assert text.index("Assert-NoReparseExistingAncestorChain -Path $installRoot") < text.index(
        "Assert-PlainPath -Path $installRoot"
    )


def test_rollback_state_swap_uses_temp_then_replace_and_cleans_failed_temp() -> None:
    text = _text(ROLLBACK)
    assert ".install-state-" in text
    assert "Move-Item -LiteralPath $tempState -Destination $statePath -Force" in text
    assert "Temporary rollback state is not a file" in text
    assert "Written rollback state is not a file" in text
    assert "if (Test-Path -LiteralPath $tempState) { [System.IO.File]::Delete($tempState) }" in text


def test_uninstall_requires_explicit_confirmation_and_preserves_user_data_root() -> None:
    text = _text(UNINSTALL)
    assert "ConfirmProgramRemoval" in text
    assert "Programs\\AnxinBoard" in text
    assert "Join-Path $env:LOCALAPPDATA 'AnxinBoard'" in text
    assert "local-run.json" in text
    assert "Directory]::Delete($installRoot, $true)" in text
    assert "Directory]::Delete($dataRoot" not in text
    assert "Remove-Item" not in text
    assert "USER_DATA=PRESERVED" in text


def test_uninstall_runtime_ownership_check_is_object_based_and_reparse_fail_closed() -> None:
    text = _text(UNINSTALL)
    assert "Get-ChildItem -LiteralPath $dataRoot -Force" in text
    assert "Where-Object { $_.Name -ieq 'local-run.json' }" in text
    assert "runtime ownership object exists" in text
    assert "Product data root is a reparse point" in text
    assert "Product data root is not a directory" in text
    assert "Product data root cannot be enumerated" in text
    assert "Test-Path -LiteralPath $runtimeState -PathType Leaf" not in text


def test_uninstall_rejects_reparse_ancestors_and_tree_before_recursive_delete() -> None:
    text = _text(UNINSTALL)
    assert "Assert-NoReparseExistingAncestorChain -Path $installRoot -Label 'Install root'" in text
    assert "Assert-NoReparseExistingAncestorChain -Path $dataRoot -Label 'Product data root'" in text
    assert "path ancestor contains a reparse point; refusing removal" in text
    assert "Install root is a reparse point; refusing removal" in text
    assert "Install tree contains a reparse point; refusing removal" in text
    assert text.index("Install tree contains a reparse point; refusing removal") < text.index(
        "[System.IO.Directory]::Delete($installRoot, $true)"
    )


def test_lifecycle_test_roots_are_fail_closed_outside_explicit_ci_mode() -> None:
    for path in (INSTALL, ROLLBACK, UNINSTALL):
        text = _text(path)
        assert "ANXINBOARD_INSTALLER_TEST_MODE" in text
        assert "TestInstallRoot" in text
