from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "installer" / "windows" / "resolve-installed-runtime.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_resolver_is_integrity_only_and_does_not_duplicate_launcher_authority() -> None:
    text = _text()
    assert "Installed runtime manifest digest does not match active pointer" in text
    assert "Installed runtime member set does not match manifest" in text
    assert "Installed runtime contains an unmanifested file" in text
    assert "Installed runtime contains a reparse point" in text
    assert "foreach ($binding in @('runtime', 'frontend', 'diagnostics'))" in text
    assert "launcher_authority = 'not_performed'" in text
    assert "credential_access = 'not_performed'" in text
    lowered = text.casefold()
    for forbidden in (
        "start-process",
        "stop-process",
        "secure-launch.lock",
        "anxinboard_local_bootstrap_secret",
        "cmdkey",
        "get-storedcredential",
    ):
        assert forbidden not in lowered


def test_resolver_rejects_reparse_in_existing_install_ancestor_chain() -> None:
    text = _text()
    assert "Assert-NoReparseExistingAncestorChain -Path $installRoot" in text
    assert "Installed runtime path ancestor contains a reparse point" in text
    assert text.index("Assert-NoReparseExistingAncestorChain -Path $installRoot") < text.index(
        "$rootItem = Assert-PlainPath -Path $installRoot"
    )


def test_resolver_output_is_relative_and_does_not_emit_user_absolute_path() -> None:
    text = _text()
    assert "runtime_executable_relative" in text
    assert "runtime_executable =" not in text
    assert '"versions/$digest/AnxinBoard.Runtime/$runtimeRelative"' in text


def test_resolver_test_root_override_is_explicitly_gated() -> None:
    text = _text()
    assert "ANXINBOARD_INSTALLER_TEST_MODE" in text
    assert "TestInstallRoot is available only in explicit installer test mode" in text
