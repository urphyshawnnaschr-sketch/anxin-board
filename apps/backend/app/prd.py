"""PRD 导入业务：文件安全接收、仓库外存储、解析、预览、人工确认与版本历史。

安全约束：
- 只接收用户通过 multipart 上传的单个文件；
- 文件名只做展示，实际落盘名使用随机 UUID，杜绝路径穿越与同名覆盖；
- 原始文件、解析文件和结构化结果一律写入仓库外项目目录（可被 ANXINBOARD_PRD_ROOT 覆盖用于测试）；
- API 响应不回显本机绝对路径，不返回完整 PRD 正文或 structured artifact，只返回必要预览与追溯 metadata；
- 写盘或数据库失败时不留下可误用的半条记录：先完成内存解析，再写文件，最后入库；入库失败时清理本次已写文件。
"""

import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.db import get_connection
from app.prd_parser import (
    PARSER_VERSION,
    STRUCTURED_PARSER_VERSION,
    STRUCTURED_SCHEMA_VERSION,
    SUPPORTED_EXTENSIONS,
    PrdParseError,
    parse_prd_bytes,
    parse_prd_structured_bytes,
)

MAX_FILE_BYTES = 20 * 1024 * 1024
PREVIEW_MAX_CHARS = 2000

_STRUCTURED_FORMATS = {
    ".md": "md",
    ".txt": "txt",
    ".docx": "docx",
    ".pdf": "pdf",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_PRD_COLUMNS = (
    "id, project_id, version_no, original_filename, source_path, source_hash, "
    "size_bytes, parsed_path, parsed_hash, parser_version, structured_path, "
    "structured_hash, structured_schema_version, structured_parser_version, "
    "document_fingerprint, status, warnings_json, created_at, confirmed_by, confirmed_at"
)


class PrdConfirmPayload(BaseModel):
    confirmed_by: str | None = None


def get_prd_root() -> Path:
    """PRD 文件根目录：默认 %LOCALAPPDATA%\\AnxinBoard\\projects，可用环境变量覆盖（测试用）。"""
    override = os.environ.get("ANXINBOARD_PRD_ROOT")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; cannot locate the PRD directory")
    return Path(local_app_data) / "AnxinBoard" / "projects"


def get_project_prd_dir(project_id: int) -> Path:
    """单项目 PRD 目录：<root>/<project_id>/prd/，位于仓库外。"""
    return get_prd_root() / str(project_id) / "prd"


def _bad_input(message: str, code: str = "INVALID_PRD_INPUT") -> HTTPException:
    return HTTPException(status_code=400, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "PRD_VERSION_NOT_FOUND", "message": "PRD 版本不存在或已被删除"},
    )


def _project_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "PROJECT_NOT_FOUND", "message": "项目不存在或已被删除"},
    )


def _read_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PRD_READ_FAILED", "message": "读取 PRD 数据失败"},
    )


def _save_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PRD_SAVE_FAILED", "message": "保存失败，原数据未改变"},
    )


def _storage_failed() -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PRD_STORAGE_FAILED", "message": "PRD 文件存储失败"},
    )


def _storage_escape(message: str) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"code": "PRD_STORAGE_ESCAPE", "message": message},
    )


def _structured_required() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "PRD_STRUCTURED_RESULT_REQUIRED",
            "message": "该 PRD 版本缺少可验证的结构化解析结果，请重新导入后确认。",
        },
    )


def _structured_invalid() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "PRD_STRUCTURED_RESULT_INVALID",
            "message": "该 PRD 版本的冻结解析结果无法验证，请重新导入后确认。",
        },
    )


def _require_project(project_id: int) -> None:
    if project_id <= 0:
        raise _project_not_found()
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    if row is None:
        raise _project_not_found()


def _validate_filename(raw: str) -> str:
    """严格校验原始上传文件名，禁止路径穿越、绝对路径、盘符、隐藏文件与控制字符。

    先做百分号解码（拦截 multipart 传输层编码后的路径分隔符、`..` 与控制字符），
    再对解码后的名字执行同一套校验；任何非法形式直接 400 拒绝，不静默改名后继续。
    """
    if raw is None or raw == "":
        raise _bad_input("文件名不能为空", code="PRD_FILE_NAME_INVALID")
    name = urllib.parse.unquote(raw.strip())
    if name == "":
        raise _bad_input("文件名不能为空", code="PRD_FILE_NAME_INVALID")
    if "/" in name or "\\" in name:
        raise _bad_input("文件名不能包含路径分隔符", code="PRD_FILE_NAME_INVALID")
    if re.match(r"^[A-Za-z]:", name):
        raise _bad_input("文件名不能包含盘符或绝对路径", code="PRD_FILE_NAME_INVALID")
    if name in (".", ".."):
        raise _bad_input("文件名不能是 . 或 .. 路径段", code="PRD_FILE_NAME_INVALID")
    if name.startswith("."):
        raise _bad_input("不允许导入隐藏文件", code="PRD_FILE_NAME_INVALID")
    if any(ord(ch) < 32 or ord(ch) == 0x7F for ch in name):
        raise _bad_input("文件名不能包含控制字符", code="PRD_FILE_NAME_INVALID")
    if len(name) > 200:
        raise _bad_input("文件名不能超过 200 个字符", code="PRD_FILE_NAME_INVALID")
    return name


def _check_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise _bad_input("不支持的文件类型，仅支持 .md / .txt / .docx / .pdf", code="UNSUPPORTED_PRD_TYPE")
    return ext


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256_hex(value) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _document_fingerprint(
    parser_version: str,
    schema_version: str,
    source_format: str,
    source_hash: str,
) -> str:
    payload = {
        "parser_version": parser_version,
        "schema_version": schema_version,
        "source_format": source_format,
        "source_hash": source_hash,
    }
    return _sha256_hex(_canonical_json_bytes(payload))


def _validate_structured_result(result, ext: str, source_hash: str) -> bytes:
    """上传时只验证 structured identity 自一致性，不重新实现 block/parser 语义。"""
    if not isinstance(result, dict):
        raise PrdParseError("结构化解析结果无效")
    expected_format = _STRUCTURED_FORMATS[ext]
    if result.get("source_hash") != source_hash:
        raise PrdParseError("结构化解析结果与原文件指纹不一致")
    if result.get("source_format") != expected_format:
        raise PrdParseError("结构化解析结果与文件格式不一致")
    if result.get("schema_version") != STRUCTURED_SCHEMA_VERSION:
        raise PrdParseError("结构化解析结果 schema 版本不一致")
    if result.get("parser_version") != STRUCTURED_PARSER_VERSION:
        raise PrdParseError("结构化解析结果 parser 版本不一致")
    fingerprint = result.get("document_fingerprint")
    if not _is_sha256_hex(fingerprint):
        raise PrdParseError("结构化解析结果文档指纹无效")
    if not isinstance(result.get("blocks"), list):
        raise PrdParseError("结构化解析结果缺少 blocks")
    expected_fingerprint = _document_fingerprint(
        result["parser_version"],
        result["schema_version"],
        result["source_format"],
        result["source_hash"],
    )
    if fingerprint != expected_fingerprint:
        raise PrdParseError("结构化解析结果文档指纹不一致")
    try:
        return _canonical_json_bytes(result)
    except (TypeError, ValueError) as exc:
        raise PrdParseError("结构化解析结果无法确定性序列化") from exc


def _safe_rel_name(ext: str) -> str:
    return f"{uuid.uuid4().hex}{ext}"


def _is_reparse_point(path: Path) -> bool:
    """Windows 符号链接、目录联接或其他 reparse point 检测。"""
    try:
        if path.is_symlink():
            return True
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == -1:  # INVALID_FILE_ATTRIBUTES：路径不存在
                return False
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except (AttributeError, OSError):
            pass
    return False


def _secure_root() -> Path:
    """批准根目录：拒绝根目录本身为符号链接/联接，返回 resolve 后的规范路径。"""
    root = Path(get_prd_root())
    if _is_reparse_point(root):
        raise _storage_escape("PRD 根目录不允许是符号链接或目录联接")
    resolved = root.resolve(strict=False)
    if _is_reparse_point(resolved):
        raise _storage_escape("PRD 根目录解析后不允许是符号链接或目录联接")
    return resolved


def _secure_relative(rel: str) -> Path:
    """跨平台拒绝绝对、根相对、盘符、空路径、反斜杠及 `.` / `..` 路径段。"""
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise _storage_escape("存储相对路径不合法")
    posix_path = PurePosixPath(rel)
    windows_path = PureWindowsPath(rel)
    raw_parts = rel.split("/")
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise _storage_escape("存储相对路径不合法")
    return Path(*raw_parts)


def _resolve_storage_target(rel: str) -> Path:
    """写入、读取与清理共用的批准根、路径链和最终规范包含校验。"""
    root = _secure_root()
    rel_path = _secure_relative(rel)
    target = root
    for part in rel_path.parts:
        target = target / part
        if _is_reparse_point(target):
            raise _storage_escape("目标路径链包含符号链接或目录联接")
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _storage_escape("目标路径无法安全解析") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _storage_escape("目标路径逃逸批准根目录") from exc
    if _is_reparse_point(resolved):
        raise _storage_escape("目标路径解析后仍为符号链接或目录联接")
    return resolved


def _secure_target(rel: str) -> Path:
    """兼容旧调用名；全部安全判定委托给共享底层解析机制。"""
    return _resolve_storage_target(rel)


def _secure_write(rel: str, data: bytes) -> None:
    """创建前、创建父目录后和写入后均使用共享安全目标解析。"""
    target = _resolve_storage_target(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _resolve_storage_target(rel)
    try:
        target.write_bytes(data)
        _resolve_storage_target(rel)
    except (HTTPException, OSError):
        try:
            _resolve_storage_target(rel).unlink(missing_ok=True)
        except (HTTPException, OSError):
            pass
        raise


def _write_source_file(project_id: int, ext: str, data: bytes) -> str:
    rel = f"{project_id}/prd/{_safe_rel_name(ext)}"
    _secure_write(rel, data)
    return rel


def _write_parsed_file(project_id: int, text: str) -> str:
    rel = f"{project_id}/prd/{_safe_rel_name('.txt')}"
    _secure_write(rel, text.encode("utf-8"))
    return rel


def _write_structured_file(project_id: int, data: bytes) -> str:
    rel = f"{project_id}/prd/{_safe_rel_name('.json')}"
    _secure_write(rel, data)
    return rel


def _remove_files(rel_paths) -> None:
    """清理：对每条相对路径先做共享安全解析；安全失败只跳过，不覆盖原始业务异常。"""
    for rel in rel_paths:
        if not rel:
            continue
        try:
            resolved = _resolve_storage_target(rel)
        except HTTPException:
            continue
        try:
            resolved.unlink(missing_ok=True)
        except OSError:
            pass


def _read_preview(parsed_path: str | None) -> tuple[str, bool]:
    if not parsed_path:
        return "", False
    resolved = _resolve_storage_target(parsed_path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise _read_failed() from exc
    truncated = len(text) > PREVIEW_MAX_CHARS
    return text[:PREVIEW_MAX_CHARS], truncated


def _read_confirm_artifact(rel: str) -> bytes:
    resolved = _resolve_storage_target(rel)
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise _structured_invalid() from exc


def _to_dict(row) -> dict:
    data = dict(row)
    raw = data.get("warnings_json") or "[]"
    try:
        data["warnings"] = json.loads(raw)
    except json.JSONDecodeError:
        data["warnings"] = []
    data.pop("warnings_json", None)
    return data


def _public_dict(data: dict) -> dict:
    """剔除内部路径字段，API 不回显本机绝对路径。"""
    return {
        key: value
        for key, value in data.items()
        if key not in ("source_path", "parsed_path", "structured_path")
    }


def _version_response(row) -> dict:
    data = _to_dict(row)
    preview, truncated = _read_preview(data.get("parsed_path"))
    public = _public_dict(data)
    public["preview"] = preview
    public["preview_truncated"] = truncated
    return public


def _invalid_confirm_state(status: str) -> HTTPException:
    messages = {
        "uploaded": "PRD 尚未完成解析，无法确认",
        "parse_failed": "PRD 解析失败，无法确认",
        "parse_confirmed": "PRD 已是当前有效版本",
        "superseded": "PRD 已被更新版本替换，无法确认",
    }
    return HTTPException(
        status_code=400,
        detail={
            "code": "PRD_CONFIRM_INVALID_STATE",
            "message": messages.get(status, "该版本当前状态无法确认"),
        },
    )


def _validate_confirm_integrity(current: dict) -> None:
    structured_fields = (
        "structured_path",
        "structured_hash",
        "structured_schema_version",
        "structured_parser_version",
        "document_fingerprint",
    )
    if any(not current.get(field) for field in structured_fields):
        raise _structured_required()

    if not current.get("source_path") or not current.get("parsed_path"):
        raise _structured_invalid()
    if not _is_sha256_hex(current.get("source_hash")):
        raise _structured_invalid()
    if not _is_sha256_hex(current.get("parsed_hash")):
        raise _structured_invalid()
    if not _is_sha256_hex(current.get("structured_hash")):
        raise _structured_invalid()
    if not _is_sha256_hex(current.get("document_fingerprint")):
        raise _structured_invalid()
    if not isinstance(current.get("structured_schema_version"), str):
        raise _structured_invalid()
    if not isinstance(current.get("structured_parser_version"), str):
        raise _structured_invalid()

    source_bytes = _read_confirm_artifact(current["source_path"])
    parsed_bytes = _read_confirm_artifact(current["parsed_path"])
    structured_bytes = _read_confirm_artifact(current["structured_path"])

    if _sha256_hex(source_bytes) != current["source_hash"]:
        raise _structured_invalid()
    if _sha256_hex(parsed_bytes) != current["parsed_hash"]:
        raise _structured_invalid()
    if _sha256_hex(structured_bytes) != current["structured_hash"]:
        raise _structured_invalid()

    try:
        structured = json.loads(structured_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _structured_invalid() from exc
    if not isinstance(structured, dict):
        raise _structured_invalid()
    try:
        if _canonical_json_bytes(structured) != structured_bytes:
            raise _structured_invalid()
    except (TypeError, ValueError) as exc:
        raise _structured_invalid() from exc

    source_format = structured.get("source_format")
    expected_source_format = _STRUCTURED_FORMATS.get(
        Path(str(current.get("original_filename") or "")).suffix.lower()
    )
    if source_format not in _STRUCTURED_FORMATS.values() or source_format != expected_source_format:
        raise _structured_invalid()
    if structured.get("source_hash") != current["source_hash"]:
        raise _structured_invalid()
    if structured.get("schema_version") != current["structured_schema_version"]:
        raise _structured_invalid()
    if structured.get("parser_version") != current["structured_parser_version"]:
        raise _structured_invalid()
    if structured.get("document_fingerprint") != current["document_fingerprint"]:
        raise _structured_invalid()

    artifact_fingerprint = structured.get("document_fingerprint")
    if not _is_sha256_hex(artifact_fingerprint):
        raise _structured_invalid()
    try:
        expected_fingerprint = _document_fingerprint(
            structured["parser_version"],
            structured["schema_version"],
            source_format,
            structured["source_hash"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _structured_invalid() from exc
    if expected_fingerprint != artifact_fingerprint:
        raise _structured_invalid()
    if expected_fingerprint != current["document_fingerprint"]:
        raise _structured_invalid()


router = APIRouter()


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """分块读取，最多读取 limit 字节，超过立即停止，避免整文件读入内存。"""
    buffer = bytearray()
    while len(buffer) < limit:
        chunk = await file.read(limit - len(buffer))
        if not chunk:
            break
        buffer += chunk
    return bytes(buffer)


@router.post("/api/projects/{project_id}/prd-versions", status_code=201)
async def upload_prd(project_id: int, file: UploadFile = File(...)) -> dict:
    _require_project(project_id)
    filename = _validate_filename(file.filename or "")
    ext = _check_extension(filename)
    try:
        data = await _read_limited(file, MAX_FILE_BYTES + 1)
    finally:
        await file.close()
    if len(data) > MAX_FILE_BYTES:
        raise _bad_input("文件大小超过 20MB 上限", code="PRD_FILE_TOO_LARGE")
    if len(data) == 0:
        raise _bad_input("文件内容为空", code="PRD_FILE_EMPTY")
    source_hash = _sha256_hex(data)

    text = None
    parsed_rel = None
    parsed_hash = None
    structured_rel = None
    structured_hash = None
    structured_schema_version = None
    structured_parser_version = None
    document_fingerprint = None
    structured_bytes = None
    parser_version = PARSER_VERSION
    warnings = []

    legacy_error = None
    structured_error = None
    try:
        text, parser_version = parse_prd_bytes(data, ext)
    except PrdParseError as exc:
        legacy_error = exc
        parser_version = PARSER_VERSION

    try:
        structured_result = parse_prd_structured_bytes(data, ext)
        structured_bytes = _validate_structured_result(structured_result, ext, source_hash)
        structured_hash = _sha256_hex(structured_bytes)
        structured_schema_version = structured_result["schema_version"]
        structured_parser_version = structured_result["parser_version"]
        document_fingerprint = structured_result["document_fingerprint"]
    except PrdParseError as exc:
        structured_error = exc
        structured_bytes = None
        structured_hash = None
        structured_schema_version = None
        structured_parser_version = None
        document_fingerprint = None

    if legacy_error is not None or structured_error is not None:
        chosen_error = legacy_error or structured_error
        warnings = [{"type": "parse_failed", "message": str(chosen_error)}]
        text = None
        structured_bytes = None
        structured_hash = None
        structured_schema_version = None
        structured_parser_version = None
        document_fingerprint = None

    try:
        source_rel = _write_source_file(project_id, ext, data)
    except OSError as exc:
        raise _storage_failed() from exc

    if text is not None and structured_bytes is not None:
        try:
            parsed_rel = _write_parsed_file(project_id, text)
            parsed_hash = _sha256_hex(text.encode("utf-8"))
            structured_rel = _write_structured_file(project_id, structured_bytes)
            status = "parsed"
        except HTTPException:
            _remove_files([source_rel, parsed_rel, structured_rel])
            raise
        except OSError as exc:
            _remove_files([source_rel, parsed_rel, structured_rel])
            raise _storage_failed() from exc
    else:
        status = "parse_failed"
        parsed_hash = None
        structured_rel = None

    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_max = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) FROM prd_versions WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            version_no = int(current_max[0]) + 1
            cursor = conn.execute(
                f"""
                INSERT INTO prd_versions (
                    project_id, version_no, original_filename, source_path, source_hash,
                    size_bytes, parsed_path, parsed_hash, parser_version,
                    structured_path, structured_hash, structured_schema_version,
                    structured_parser_version, document_fingerprint, status,
                    warnings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    version_no,
                    filename,
                    source_rel,
                    source_hash,
                    len(data),
                    parsed_rel,
                    parsed_hash,
                    parser_version,
                    structured_rel,
                    structured_hash,
                    structured_schema_version,
                    structured_parser_version,
                    document_fingerprint,
                    status,
                    json.dumps(warnings, ensure_ascii=False),
                    created_at,
                ),
            )
            row = conn.execute(
                f"SELECT {_PRD_COLUMNS} FROM prd_versions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            conn.commit()
    except sqlite3.Error as exc:
        _remove_files([source_rel, parsed_rel, structured_rel])
        raise _save_failed() from exc

    return _version_response(row)


@router.get("/api/prd-versions/{version_id}")
def get_prd_version(version_id: int) -> dict:
    if version_id <= 0:
        raise _not_found()
    try:
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_PRD_COLUMNS} FROM prd_versions WHERE id = ?", (version_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    if row is None:
        raise _not_found()
    return _version_response(row)


@router.post("/api/prd-versions/{version_id}/confirm")
def confirm_prd(version_id: int, payload: PrdConfirmPayload) -> dict:
    if version_id <= 0:
        raise _not_found()
    confirmed_by = (payload.confirmed_by or "").strip() or "local"
    if len(confirmed_by) > 100 or any(ord(ch) < 32 for ch in confirmed_by):
        raise _bad_input("确认人信息不合法")
    confirmed_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_PRD_COLUMNS} FROM prd_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise _not_found()
            current = dict(row)
            if current["status"] != "parsed":
                raise _invalid_confirm_state(current["status"])
            _validate_confirm_integrity(current)
            conn.execute(
                """
                UPDATE prd_versions SET status = 'superseded'
                WHERE project_id = ? AND status = 'parse_confirmed'
                """,
                (current["project_id"],),
            )
            conn.execute(
                "UPDATE prd_versions SET status = 'parse_confirmed', confirmed_by = ?, confirmed_at = ? "
                "WHERE id = ?",
                (confirmed_by, confirmed_at, version_id),
            )
            updated = conn.execute(
                f"SELECT {_PRD_COLUMNS} FROM prd_versions WHERE id = ?", (version_id,)
            ).fetchone()
            conn.commit()
    except sqlite3.Error as exc:
        raise _save_failed() from exc
    return _version_response(updated)


@router.get("/api/projects/{project_id}/prd-versions")
def list_prd_versions(project_id: int) -> list[dict]:
    _require_project(project_id)
    try:
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_PRD_COLUMNS} FROM prd_versions
                WHERE project_id = ? ORDER BY version_no DESC
                """,
                (project_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise _read_failed() from exc
    versions = []
    for row in rows:
        data = _to_dict(row)
        data["active"] = data["status"] == "parse_confirmed"
        versions.append(_public_dict(data))
    return versions