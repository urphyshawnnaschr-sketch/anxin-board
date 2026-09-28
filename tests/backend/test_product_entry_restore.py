from __future__ import annotations

import sys
from pathlib import Path

import pytest

import product_entry
from app.product_restore_guard import ProductRestoreLifecycleAuthorityError


def _backup_path(tmp_path: Path) -> str:
    return str((tmp_path / "backup.zip").resolve())


def test_restore_mode_requires_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _backup_path(tmp_path)
    monkeypatch.setattr(sys, "argv", ["AnxinBoard.Runtime", "--restore-backup", backup])
    assert product_entry.main() == 64


def test_restore_mode_never_runs_git_or_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"git": 0, "ui": 0, "restore": 0}
    backup = _backup_path(tmp_path)

    monkeypatch.setattr(sys, "argv", [
        "AnxinBoard.Runtime",
        "--restore-backup",
        backup,
        "--confirm-restore",
    ])
    monkeypatch.setattr(product_entry, "require_git_runtime_capability", lambda: calls.__setitem__("git", calls["git"] + 1))
    monkeypatch.setattr(product_entry, "configure_product_ui", lambda: calls.__setitem__("ui", calls["ui"] + 1))

    def restore(path: str):
        calls["restore"] += 1
        assert path == backup
        return {"product_schema_version": "anxin_product_backup_restore_user_safe_v1"}

    monkeypatch.setattr(product_entry, "restore_product_backup_user_safe", restore)
    assert product_entry.main() == 0
    assert calls == {"git": 0, "ui": 0, "restore": 1}


def test_restore_mode_maps_safety_refusal_to_stable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _backup_path(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "AnxinBoard.Runtime",
        "--restore-backup",
        backup,
        "--confirm-restore",
    ])

    def refuse(_path: str):
        raise ProductRestoreLifecycleAuthorityError("missing lifecycle authority")

    monkeypatch.setattr(product_entry, "restore_product_backup_user_safe", refuse)
    assert product_entry.main() == 79
