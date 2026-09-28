"""Guarded local-browser export for the product-owned SQLite backup package.

Export is intentionally the only browser route in this slice. Destructive restore is
not exposed here: the offline restore primitive must first share the independently
accepted launcher lifecycle authority so a second runtime cannot race the replacement.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.local_session_api import require_local_write_request
from app.product_backup import ProductBackupError, create_product_backup


router = APIRouter()


def _cleanup_export_root(root: Path) -> None:
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        pass


@router.post("/api/product-backup/export")
def export_product_backup(request: Request) -> FileResponse:
    """Return one DB-only backup to the authenticated same-origin local browser."""

    # Although export is not a database mutation, it is a sensitive data-release action.
    # Reuse the established local browser/session/origin/request-id gate rather than
    # creating a weaker parallel authorization concept.
    require_local_write_request(request, require_idempotency_key=False)

    root = Path(tempfile.mkdtemp(prefix="anxinboard-backup-export-"))
    package = root / "AnxinBoard-backup.zip"
    try:
        create_product_backup(package)
    except ProductBackupError as exc:
        _cleanup_export_root(root)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": exc.code,
                "message": "当前本地项目数据无法生成备份包；未读取或导出任何凭据。",
            },
        ) from exc
    except Exception:
        _cleanup_export_root(root)
        raise

    return FileResponse(
        path=package,
        media_type="application/zip",
        filename="AnxinBoard-backup.zip",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(_cleanup_export_root, root),
    )
