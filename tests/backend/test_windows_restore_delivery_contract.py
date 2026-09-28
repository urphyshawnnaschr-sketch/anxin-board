from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def test_windows_restore_tool_shares_launcher_lock_and_never_reads_credentials() -> None:
    source = _text("installer/windows/restore-product-backup.ps1")
    folded = source.casefold()
    assert "secure-launch.lock" in folded
    assert "[system.io.fileshare]::none" in folded
    assert "local-run.json" in folded
    assert "anxinboard_offline_restore_authority" in folded
    assert "launcher-lock-held-v1" in folded
    assert "resolve-installed-runtime.ps1" in folded
    assert "--restore-backup" in folded
    assert "--confirm-restore" in folded
    assert "credential manager" not in folded
    assert "cmdkey" not in folded
    assert "get-storedcredential" not in folded


def test_windows_candidate_manifest_binds_restore_tool() -> None:
    source = _text("installer/windows/build-distribution.ps1")
    assert "'tools/restore-product-backup.ps1'" in source
    assert "restore_tool = 'tools/restore-product-backup.ps1'" in source


def test_inno_installs_restore_tool_and_nontechnical_start_menu_entry() -> None:
    source = _text("installer/windows/AnxinBoard.iss")
    assert 'Source: "restore-product-backup.ps1"; DestDir: "{app}\\tools"' in source
    assert '[Icons]' in source
    assert '安心看板 - 恢复备份' in source
    assert 'restore-product-backup.ps1' in source


def test_settings_no_longer_claims_backup_restore_is_unavailable() -> None:
    source = _text("apps/frontend/src/views/SettingsBackupView.vue")
    assert "导出备份 ZIP" in source
    assert "安心看板 - 恢复备份" in source
    assert "历史模型/邮件发送记录" in source
    assert "备份包底层能力存在，但当前界面尚未开放安全操作入口" not in source
