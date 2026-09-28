"""Formal packaged runtime entrypoint for AnxinBoard on Windows.

Normal mode owns only the process runtime: one loopback FastAPI process plus the
pre-built frontend assets.  The same packaged executable also exposes one explicit
offline restore sub-mode.  That sub-mode never mounts UI, probes Git, opens a browser,
or reads credentials; it is admitted only when the external Windows restore tool holds
the shared launcher lifecycle lock and supplies the fixed process authority marker.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from fastapi.staticfiles import StaticFiles
import uvicorn

from app.git_runtime_capability import (
    GitRuntimeCapabilityError,
    require_git_runtime_capability,
)
from app.main import app
from app.product_backup import ProductBackupError
from app.backup_restore import BackupRestoreError
from app.product_restore_guard import restore_product_backup_user_safe


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _frontend_root() -> Path:
    override = os.environ.get("ANXINBOARD_PRODUCT_UI_ROOT")
    if override:
        root = Path(override)
    elif getattr(sys, "frozen", False):
        root = _runtime_root() / "ui"
    else:
        root = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    root = root.resolve()
    if not root.is_dir() or not (root / "index.html").is_file():
        raise RuntimeError("PRODUCT_UI_NOT_BUILT")
    return root


def configure_product_ui() -> Path:
    """Mount immutable built UI after API routes, preserving same-origin /api."""
    root = _frontend_root()
    if not any(getattr(route, "name", None) == "product-ui" for route in app.routes):
        app.mount("/", StaticFiles(directory=str(root), html=True), name="product-ui")
    return root


def _port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _restore_path(value: str) -> str:
    if not value or "\x00" in value:
        raise argparse.ArgumentTypeError("restore backup path is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("restore backup path must be absolute")
    return str(path)


def _run_restore(package_path: str, *, confirmed: bool) -> int:
    if confirmed is not True:
        print("PRODUCT_RESTORE_CONFIRMATION_REQUIRED", file=sys.stderr, flush=True)
        return 64
    try:
        result = restore_product_backup_user_safe(package_path)
    except ProductBackupError as exc:
        print(f"{exc.code}: restore refused", file=sys.stderr, flush=True)
        return 79
    except BackupRestoreError as exc:
        allowed = {'BACKUP_RESTORE_ERROR', 'BACKUP_RESTORE_INVALID_PACKAGE', 'BACKUP_RESTORE_TOO_LARGE',
                   'BACKUP_RESTORE_TARGET_INVALID', 'BACKUP_RESTORE_TARGET_EXISTS'}
        code = exc.code if exc.code in allowed else 'PRODUCT_RESTORE_FAILED'
        print(f"{code}: restore refused", file=sys.stderr, flush=True)
        return 79
    except Exception:
        print("PRODUCT_RESTORE_FAILED: restore refused", file=sys.stderr, flush=True)
        return 79
    if result.get("product_schema_version") != "anxin_product_backup_restore_user_safe_v1":
        print("PRODUCT_RESTORE_RESULT_INVALID", file=sys.stderr, flush=True)
        return 79
    print("PRODUCT_RESTORE=SUCCESS", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="AnxinBoard.Runtime")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--port", type=_port)
    mode.add_argument("--restore-backup", type=_restore_path)
    parser.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()

    if args.restore_backup is not None:
        return _run_restore(args.restore_backup, confirmed=args.confirm_restore)
    if args.confirm_restore:
        parser.error("--confirm-restore is valid only with --restore-backup")

    # Formal Windows delivery is explicit about its core Git capability. Probe before
    # mounting UI or starting the Product server so a missing/old Git cannot surface
    # later as a generic historical-range failure. Offline restore deliberately skips
    # this unrelated prerequisite.
    try:
        require_git_runtime_capability()
    except GitRuntimeCapabilityError as exc:
        print(f"{exc.code}: {exc.user_message}", file=sys.stderr, flush=True)
        return 78

    configure_product_ui()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
