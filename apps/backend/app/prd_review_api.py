"""Protected full-text review channel for PRD Human confirmation.

The ordinary PRD version API intentionally exposes only a small preview.  When that
preview is truncated, the launcher-established local browser session may page through
all parsed text before the UI enables the Human confirmation action.  Every chunk is
bound to the stored parsed hash and no storage path is returned.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.db import get_connection
from app.local_session_api import require_local_read_request
from app.prd import _read_failed, _resolve_storage_target, _sha256_hex


router = APIRouter()
REVIEW_CHUNK_DEFAULT = 50_000
REVIEW_CHUNK_MAX = 100_000


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "PRD_VERSION_NOT_FOUND", "message": "PRD 版本不存在或已被删除"},
    )


def _not_reviewable() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "PRD_REVIEW_NOT_AVAILABLE", "message": "该 PRD 版本没有可审阅的完整解析文本。"},
    )


@router.get("/api/prd-versions/{version_id}/review")
def review_prd_version(
    version_id: int,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=REVIEW_CHUNK_DEFAULT, ge=1, le=REVIEW_CHUNK_MAX),
) -> JSONResponse:
    """Return one integrity-bound chunk of parsed PRD text to the live local browser."""

    require_local_read_request(request)
    if version_id <= 0:
        raise _not_found()

    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, status, parsed_path, parsed_hash FROM prd_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise _read_failed() from exc

    if row is None:
        raise _not_found()
    if row["status"] not in ("parsed", "parse_confirmed", "superseded"):
        raise _not_reviewable()
    parsed_path = row["parsed_path"]
    parsed_hash = row["parsed_hash"]
    if not isinstance(parsed_path, str) or not isinstance(parsed_hash, str):
        raise _not_reviewable()

    resolved = _resolve_storage_target(parsed_path)
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise _read_failed() from exc
    if _sha256_hex(raw) != parsed_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRD_REVIEW_INTEGRITY_MISMATCH",
                "message": "完整解析文本与已记录版本指纹不一致，确认操作保持关闭。",
            },
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "PRD_REVIEW_TEXT_INVALID", "message": "完整解析文本不是有效 UTF-8。"},
        ) from exc

    total_chars = len(text)
    if offset > total_chars:
        raise HTTPException(
            status_code=416,
            detail={"code": "PRD_REVIEW_OFFSET_INVALID", "message": "审阅位置超出当前解析文本范围。"},
        )

    end = min(total_chars, offset + limit)
    body = {
        "schema_version": "prd_parsed_review_v1",
        "version_id": version_id,
        "parsed_hash": parsed_hash,
        "offset": offset,
        "content": text[offset:end],
        "next_offset": end,
        "complete": end >= total_chars,
        "total_chars": total_chars,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
