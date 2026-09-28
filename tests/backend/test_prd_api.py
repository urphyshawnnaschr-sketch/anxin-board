"""PRD API 契约测试：上传、查询、确认、版本历史、错误码、写盘/数据库失败与隔离。

全部使用临时 SQLite 与临时 PRD 目录，不接触正式数据库与正式文件。
"""

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db, main, prd, prd_parser  # noqa: E402
from prd_fixtures import (  # noqa: E402
    make_corrupt_docx,
    make_docx,
    make_docx_bad_crc,
    make_docx_corrupt_body_stream,
    make_image_only_pdf,
    make_md,
    make_text_pdf,
    make_txt,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    prd_root = tmp_path / "prdroot"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(prd_root))
    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, tmp_path


def _create_project(client, name="PRD 项目"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def _upload(client, project_id, content, filename, content_type="application/octet-stream"):
    return client.post(
        f"/api/projects/{project_id}/prd-versions",
        files={"file": (filename, content, content_type)},
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _InsertFailConnection:
    """包一层真实连接：向 prd_versions 的 INSERT 一律抛错。"""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args, **kwargs):
        if str(sql).lstrip().upper().startswith("INSERT INTO PRD_VERSIONS"):
            raise sqlite3.OperationalError("simulated insert failure")
        return self._real.execute(sql, *args, **kwargs)


class _UpdateFailConnection:
    """包一层真实连接：对 prd_versions 的 UPDATE 一律抛错。"""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args, **kwargs):
        if str(sql).lstrip().upper().startswith("UPDATE PRD_VERSIONS"):
            raise sqlite3.OperationalError("simulated update failure")
        return self._real.execute(sql, *args, **kwargs)


# ---------- R-01：四类允许格式能导入并显示预览 ----------


@pytest.mark.parametrize(
    "content,filename",
    [
        (make_md("Markdown PRD 正文"), "需求文档.md"),
        (make_txt("纯文本 PRD 正文"), "说明.txt"),
        (make_docx(["Word 第一段", "Word 第二段"]), "方案.docx"),
        (make_text_pdf("Hello PDF PRD"), "手册.pdf"),
    ],
)
def test_upload_four_supported_types_show_preview(env, content, filename):
    client, tmp_path = env
    project_id = _create_project(client)
    response = _upload(client, project_id, content, filename)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "parsed"
    assert body["original_filename"] == filename
    assert body["version_no"] == 1
    assert body["size_bytes"] == len(content)
    assert body["preview"]
    assert "source_path" not in body
    assert "parsed_path" not in body
    assert "structured_path" not in body
    assert body["structured_schema_version"] == "prd_structured_evidence_v1"
    assert body["structured_parser_version"] == "prd-structured-parser-1.0"
    assert len(body["structured_hash"]) == 64
    assert len(body["document_fingerprint"]) == 64

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM prd_versions WHERE id = ?", (body["id"],)).fetchone()
    conn.close()
    structured_bytes = (tmp_path / "prdroot" / row["structured_path"]).read_bytes()
    structured = json.loads(structured_bytes.decode("utf-8"))
    assert structured_bytes == prd._canonical_json_bytes(structured)
    assert _sha256(structured_bytes) == body["structured_hash"]
    expected_format = Path(filename).suffix.lower().lstrip(".")
    assert structured["source_format"] == expected_format
    assert structured["source_hash"] == body["source_hash"]
    assert structured["schema_version"] == body["structured_schema_version"]
    assert structured["parser_version"] == body["structured_parser_version"]
    expected_fingerprint = prd._document_fingerprint(
        structured["parser_version"],
        structured["schema_version"],
        structured["source_format"],
        structured["source_hash"],
    )
    assert expected_fingerprint == structured["document_fingerprint"]
    assert expected_fingerprint == body["document_fingerprint"]


# ---------- R-02：超 20MB、非法扩展名、空文件、不可解析文件被阻断 ----------


def test_upload_oversize_rejected(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)
    monkeypatch.setattr(prd, "MAX_FILE_BYTES", 100)
    response = _upload(client, project_id, make_txt("x" * 101), "big.md")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PRD_FILE_TOO_LARGE"
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert history == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert not prd_dir.exists() or list(prd_dir.iterdir()) == []


class _CountingStream:
    """可计数的异步流：记录调用次数与剩余字节，用于验证分块读取即停。"""

    def __init__(self, size):
        self.remaining = size
        self.calls = 0

    async def read(self, n):
        self.calls += 1
        if self.remaining == 0:
            return b""
        take = min(n, self.remaining)
        self.remaining -= take
        return b"x" * take


def test_read_limited_stops_at_limit():
    import asyncio

    stream = _CountingStream(1024 * 1024)
    data = asyncio.run(prd._read_limited(stream, 100))
    assert len(data) == 100
    assert stream.remaining == 1024 * 1024 - 100
    assert stream.calls < 1024 * 1024


def test_read_limited_reads_exact_limit_plus_one():
    import asyncio

    stream = _CountingStream(1024 * 1024)
    data = asyncio.run(prd._read_limited(stream, 5))
    assert len(data) == 5


def test_upload_junction_escape_rejected(env, monkeypatch):
    import subprocess

    client, tmp_path = env
    project_id = _create_project(client)
    prd_root = tmp_path / "prdroot"
    project_dir = prd_root / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved"
    project_dir.rename(moved)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(project_dir), str(outside)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("无法创建 Windows junction（mklink /J 不可用）")
    response = _upload(client, project_id, make_md("逃逸测试"), "escape.md")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_ESCAPE"
    assert list(outside.iterdir()) == []
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []


def _make_link(link: Path, target: Path) -> bool:
    """创建目录联接（Windows junction）或目录符号链接（POSIX）。返回是否成功。"""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return result.returncode == 0
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


def _db_row_rels(tmp_path, version_id):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source_path, parsed_path FROM prd_versions WHERE id = ?", (version_id,)
    ).fetchone()
    conn.close()
    return row["source_path"], row["parsed_path"]


def _db_artifact_row(tmp_path, version_id):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM prd_versions WHERE id = ?", (version_id,)).fetchone()
    conn.close()
    return row


def test_read_preview_after_project_dir_replaced_with_junction(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("正常正文"), "ok.md").json()
    source_rel, parsed_rel = _db_row_rels(tmp_path, version["id"])
    prd_root = tmp_path / "prdroot"
    project_dir = prd_root / str(project_id)
    moved = tmp_path / "moved_project"
    project_dir.rename(moved)
    outside = tmp_path / "outside_project"
    outside.mkdir()
    outside_prd = outside / "prd"
    outside_prd.mkdir(parents=True)
    outside_file = outside_prd / Path(parsed_rel).name
    outside_file.write_text("根外被读取的解析内容", encoding="utf-8")
    if not _make_link(project_dir, outside):
        pytest.skip("无法创建 junction/符号链接")
    response = client.get(f"/api/prd-versions/{version['id']}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_ESCAPE"
    assert outside_file.read_text(encoding="utf-8") == "根外被读取的解析内容"


def test_read_preview_after_prd_dir_replaced_with_junction(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("正常正文"), "ok.md").json()
    _, parsed_rel = _db_row_rels(tmp_path, version["id"])
    prd_root = tmp_path / "prdroot"
    prd_dir = prd_root / str(project_id) / "prd"
    moved = tmp_path / "moved_prd"
    prd_dir.rename(moved)
    outside = tmp_path / "outside_prd"
    outside.mkdir()
    outside_file = outside / Path(parsed_rel).name
    outside_file.write_text("根外被读取的解析内容", encoding="utf-8")
    if not _make_link(prd_dir, outside):
        pytest.skip("无法创建 junction/符号链接")
    response = client.get(f"/api/prd-versions/{version['id']}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_ESCAPE"
    assert outside_file.read_text(encoding="utf-8") == "根外被读取的解析内容"


def test_read_preview_symlink_after_write_blocked(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("正常正文"), "ok.md").json()
    _, parsed_rel = _db_row_rels(tmp_path, version["id"])
    prd_root = tmp_path / "prdroot"
    prd_dir = prd_root / str(project_id) / "prd"
    moved = tmp_path / "moved_prd_sym"
    prd_dir.rename(moved)
    outside = tmp_path / "outside_prd_sym"
    outside.mkdir()
    outside_file = outside / Path(parsed_rel).name
    outside_file.write_text("根外被读取的解析内容", encoding="utf-8")
    if os.name == "nt":
        pytest.skip("Windows 上已有 junction 等价测试")
    if not _make_link(prd_dir, outside):
        pytest.skip("无法创建目录符号链接")
    response = client.get(f"/api/prd-versions/{version['id']}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_ESCAPE"
    assert outside_file.read_text(encoding="utf-8") == "根外被读取的解析内容"


def test_remove_files_after_prd_dir_replaced_with_junction(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("正常正文"), "ok.md").json()
    source_rel, parsed_rel = _db_row_rels(tmp_path, version["id"])
    prd_root = tmp_path / "prdroot"
    prd_dir = prd_root / str(project_id) / "prd"
    moved = tmp_path / "moved_prd"
    prd_dir.rename(moved)
    outside = tmp_path / "outside_prd"
    outside.mkdir()
    outside_source = outside / Path(source_rel).name
    outside_parsed = outside / Path(parsed_rel).name
    outside_source.write_text("根外原文件A", encoding="utf-8")
    outside_parsed.write_text("根外原文件B", encoding="utf-8")
    if not _make_link(prd_dir, outside):
        pytest.skip("无法创建 junction/符号链接")
    prd._remove_files([source_rel, parsed_rel])
    assert outside_source.read_text(encoding="utf-8") == "根外原文件A"
    assert outside_parsed.read_text(encoding="utf-8") == "根外原文件B"
    assert (moved / Path(source_rel).name).exists()
    assert (moved / Path(parsed_rel).name).exists()


def test_remove_files_after_project_dir_replaced_with_junction(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("正常正文"), "ok.md").json()
    source_rel, parsed_rel = _db_row_rels(tmp_path, version["id"])
    prd_root = tmp_path / "prdroot"
    project_dir = prd_root / str(project_id)
    moved = tmp_path / "moved_project"
    project_dir.rename(moved)
    outside = tmp_path / "outside_project"
    outside.mkdir()
    outside_prd = outside / "prd"
    outside_prd.mkdir(parents=True)
    outside_source = outside_prd / Path(source_rel).name
    outside_parsed = outside_prd / Path(parsed_rel).name
    outside_source.write_text("根外原文件A", encoding="utf-8")
    outside_parsed.write_text("根外原文件B", encoding="utf-8")
    if not _make_link(project_dir, outside):
        pytest.skip("无法创建 junction/符号链接")
    prd._remove_files([source_rel, parsed_rel])
    assert outside_source.read_text(encoding="utf-8") == "根外原文件A"
    assert outside_parsed.read_text(encoding="utf-8") == "根外原文件B"
    assert (moved / "prd" / Path(source_rel).name).exists()
    assert (moved / "prd" / Path(parsed_rel).name).exists()


def test_remove_files_normal_files_removed(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("正常正文"), "ok.md").json()
    source_rel, parsed_rel = _db_row_rels(tmp_path, version["id"])
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert (prd_dir / Path(source_rel).name).exists()
    assert (prd_dir / Path(parsed_rel).name).exists()
    prd._remove_files([source_rel, parsed_rel])
    assert not (prd_dir / Path(source_rel).name).exists()
    assert not (prd_dir / Path(parsed_rel).name).exists()


def test_read_preview_rejects_absolute_and_dotdot(env):
    client, _ = env
    project_id = _create_project(client)
    _upload(client, project_id, make_md("x"), "ok.md")
    bad_paths = [
        "C:/evil.txt",
        "C:\\evil.txt",
        "..\\evil.txt",
        "../evil.txt",
        "/abs/evil.txt",
        "\\abs\\evil.txt",
        "1/../evil.txt",
    ]
    for bad in bad_paths:
        with pytest.raises(Exception) as excinfo:
            prd._read_preview(bad)
        assert excinfo.value.status_code == 500, repr(bad)
        assert excinfo.value.detail["code"] == "PRD_STORAGE_ESCAPE", repr(bad)


def test_remove_files_rejects_absolute_and_dotdot(env):
    client, tmp_path = env
    project_id = _create_project(client)
    _upload(client, project_id, make_md("x"), "ok.md")
    outside_decoy = tmp_path / "outside_decoy.txt"
    outside_decoy.write_text("根外诱饵", encoding="utf-8")
    bad_paths = [
        "C:/evil.txt",
        "C:\\evil.txt",
        "..\\evil.txt",
        "../evil.txt",
        "/abs/evil.txt",
        "\\abs\\evil.txt",
        "1/../evil.txt",
    ]
    prd._remove_files(bad_paths)
    assert outside_decoy.read_text(encoding="utf-8") == "根外诱饵"


def test_upload_unsupported_extension_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    for filename in ["script.exe", "config.json", "note.rtf", "no_extension"]:
        response = _upload(client, project_id, b"hello", filename)
        assert response.status_code == 400, filename
        assert response.json()["detail"]["code"] == "UNSUPPORTED_PRD_TYPE"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []


def test_upload_empty_file_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    response = _upload(client, project_id, b"", "empty.md")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PRD_FILE_EMPTY"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []


def test_upload_corrupt_docx_marked_parse_failed(env):
    client, _ = env
    project_id = _create_project(client)
    response = _upload(client, project_id, make_corrupt_docx(), "broken.docx")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parse_failed"
    assert body["warnings"]
    assert any("损坏" in w["message"] for w in body["warnings"])


@pytest.mark.parametrize(
    "content_factory",
    [make_docx_corrupt_body_stream, make_docx_bad_crc],
)
def test_upload_corrupt_docx_stream_is_stable_parse_failure(env, content_factory):
    client, tmp_path = env
    project_id = _create_project(client)
    response = _upload(client, project_id, content_factory(), "broken_stream.docx")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "parse_failed"
    assert body["preview"] == ""
    assert not body.get("parsed_hash")
    assert not body.get("structured_hash")
    assert body["warnings"]
    warning = body["warnings"][0]
    assert warning["type"] == "parse_failed"
    message = warning["message"]
    assert "损坏" in message
    for leaked in ("BadZipFile", "zlib", "CRC", "Traceback", "C:", "\\", "python", "line "):
        assert leaked.lower() not in message.lower(), message

    confirm = client.post(f"/api/prd-versions/{body['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert confirm.status_code == 400
    assert confirm.json()["detail"]["code"] == "PRD_CONFIRM_INVALID_STATE"

    source_rel, parsed_rel = _db_row_rels(tmp_path, body["id"])
    source_path = tmp_path / "prdroot" / source_rel
    assert source_path.exists()
    if parsed_rel:
        assert not (tmp_path / "prdroot" / parsed_rel).exists()

    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert history[0]["status"] == "parse_failed"


def test_secure_write_junction_parent_rejected(env):
    _, tmp_path = env
    prd_root = tmp_path / "prdroot"
    project_dir = prd_root / "7"
    project_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside_parent"
    outside.mkdir()
    moved = tmp_path / "moved_parent"
    project_dir.rename(moved)
    if not _make_link(project_dir, outside):
        pytest.skip("无法创建 junction/符号链接")
    with pytest.raises(Exception) as excinfo:
        prd._secure_write("7/prd/evil.txt", b"x")
    assert excinfo.value.status_code == 500
    assert excinfo.value.detail["code"] == "PRD_STORAGE_ESCAPE"
    assert list(outside.iterdir()) == []


def test_secure_write_junction_root_rejected(env):
    _, tmp_path = env
    prd_root = tmp_path / "prdroot"
    outside = tmp_path / "outside_root"
    outside.mkdir()
    moved = tmp_path / "moved_root"
    prd_root.mkdir()
    prd_root.rename(moved)
    if not _make_link(prd_root, outside):
        pytest.skip("无法创建 junction/符号链接")
    with pytest.raises(Exception) as excinfo:
        prd._secure_write("1/prd/evil.txt", b"x")
    assert excinfo.value.status_code == 500
    assert excinfo.value.detail["code"] == "PRD_STORAGE_ESCAPE"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "bad",
    [
        "C:/evil.txt",
        "C:\\evil.txt",
        "..\\evil.txt",
        "../evil.txt",
        "/abs/evil.txt",
        "\\abs\\evil.txt",
        "1/../evil.txt",
    ],
)
def test_secure_write_rejects_absolute_and_dotdot(env, bad):
    _, tmp_path = env
    outside_decoy = tmp_path / "outside_decoy.txt"
    outside_decoy.write_text("根外诱饵", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        prd._secure_write(bad, b"x")
    assert excinfo.value.status_code == 500, repr(bad)
    assert excinfo.value.detail["code"] == "PRD_STORAGE_ESCAPE", repr(bad)
    assert outside_decoy.read_text(encoding="utf-8") == "根外诱饵"


# ---------- R-03：图片型 PDF 不做 OCR ----------


def test_upload_image_pdf_not_supported_no_fake_preview(env):
    client, _ = env
    project_id = _create_project(client)
    response = _upload(client, project_id, make_image_only_pdf(), "scan.pdf")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parse_failed"
    assert any("不支持 OCR" in w["message"] for w in body["warnings"])
    assert body["preview"] == ""
    assert not body.get("parsed_hash")
    assert not body.get("structured_hash")
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert history[0]["status"] == "parse_failed"


# ---------- R-04：原文件 hash 与解析 hash 可追溯 ----------


def test_upload_hashes_match_files_on_disk(env):
    client, tmp_path = env
    project_id = _create_project(client)
    content = make_md("可追溯的 PRD 内容")
    response = _upload(client, project_id, content, "trace.md")
    assert response.status_code == 201
    body = response.json()
    assert body["source_hash"] == _sha256(content)

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM prd_versions WHERE id = ?", (body["id"],)).fetchone()
    conn.close()
    source_path = tmp_path / "prdroot" / row["source_path"]
    parsed_path = tmp_path / "prdroot" / row["parsed_path"]
    structured_path = tmp_path / "prdroot" / row["structured_path"]
    assert source_path.exists()
    assert _sha256(source_path.read_bytes()) == body["source_hash"]
    assert parsed_path.exists()
    parsed_text = parsed_path.read_text(encoding="utf-8")
    assert _sha256(parsed_text.encode("utf-8")) == body["parsed_hash"]
    assert structured_path.exists()
    structured_bytes = structured_path.read_bytes()
    assert _sha256(structured_bytes) == body["structured_hash"]
    assert body["parser_version"] == "prd-parser-1.0"
    assert body["structured_parser_version"] == "prd-structured-parser-1.0"


# ---------- R-05：确认前不得成为当前有效 PRD ----------


def test_upload_does_not_become_active_before_confirm(env):
    client, _ = env
    project_id = _create_project(client)
    response = _upload(client, project_id, make_md("待确认内容"), "pending.md")
    assert response.status_code == 201
    assert response.json()["status"] == "parsed"
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert len(history) == 1
    assert history[0]["active"] is False
    assert history[0]["status"] == "parsed"


# ---------- R-06：确认后同项目只有一个有效版本 ----------


def test_confirm_second_version_supersedes_first(env):
    client, _ = env
    project_id = _create_project(client)
    first = _upload(client, project_id, make_md("第一版"), "v1.md").json()
    first_confirm = client.post(f"/api/prd-versions/{first['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert first_confirm.status_code == 200
    assert first_confirm.json()["status"] == "parse_confirmed"
    assert first_confirm.json()["confirmed_by"] == "项目经理"
    assert first_confirm.json()["confirmed_at"]

    second = _upload(client, project_id, make_md("第二版"), "v2.md").json()
    assert second["status"] == "parsed"
    second_confirm = client.post(f"/api/prd-versions/{second['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert second_confirm.status_code == 200

    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    active = [v for v in history if v["active"]]
    assert len(active) == 1
    assert active[0]["id"] == second["id"]
    by_id = {v["id"]: v for v in history}
    assert by_id[first["id"]]["status"] == "superseded"
    assert len(history) == 2


def test_get_superseded_version_still_viewable(env):
    client, _ = env
    project_id = _create_project(client)
    first = _upload(client, project_id, make_md("旧版正文"), "old.md").json()
    client.post(f"/api/prd-versions/{first['id']}/confirm", json={"confirmed_by": "项目经理"})
    second = _upload(client, project_id, make_md("新版正文"), "new.md").json()
    client.post(f"/api/prd-versions/{second['id']}/confirm", json={"confirmed_by": "项目经理"})

    response = client.get(f"/api/prd-versions/{first['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "superseded"
    assert "旧版正文" in body["preview"]


# ---------- R-07：文件或解析内容变化后旧确认不能复用 ----------


def test_new_version_requires_new_confirm(env):
    client, _ = env
    project_id = _create_project(client)
    first = _upload(client, project_id, make_md("初始内容"), "base.md").json()
    client.post(f"/api/prd-versions/{first['id']}/confirm", json={"confirmed_by": "项目经理"})
    second = _upload(client, project_id, make_md("变化后的内容"), "base.md").json()
    assert second["id"] != first["id"]
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    active = [v for v in history if v["active"]]
    assert len(active) == 1 and active[0]["id"] == first["id"]
    assert second["status"] == "parsed"


# ---------- R-08：写盘或数据库失败不留下可误用半条记录 ----------


def test_db_insert_failure_cleans_files_no_record(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)
    real_get_connection = prd.get_connection

    def failing_get_connection():
        return _InsertFailConnection(real_get_connection())

    monkeypatch.setattr(prd, "get_connection", failing_get_connection)
    response = _upload(client, project_id, make_md("写入会失败"), "fail.md")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_SAVE_FAILED"

    monkeypatch.setattr(prd, "get_connection", real_get_connection)
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert history == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    if prd_dir.exists():
        assert list(prd_dir.iterdir()) == []


def test_storage_write_failure_leaves_no_record(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)
    real_write = prd._write_source_file

    def failing_write(project_id_value, ext, data):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(prd, "_write_source_file", failing_write)
    response = _upload(client, project_id, make_md("写盘会失败"), "disk.md")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_FAILED"

    monkeypatch.setattr(prd, "_write_source_file", real_write)
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert history == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    if prd_dir.exists():
        assert list(prd_dir.iterdir()) == []


def test_db_write_failure_does_not_change_active_version(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    first = _upload(client, project_id, make_md("活动版本"), "active.md").json()
    client.post(f"/api/prd-versions/{first['id']}/confirm", json={"confirmed_by": "项目经理"})
    second = _upload(client, project_id, make_md("准备确认但数据库失败"), "pending.md").json()

    real_get_connection = prd.get_connection

    def failing_get_connection():
        return _UpdateFailConnection(real_get_connection())

    monkeypatch.setattr(prd, "get_connection", failing_get_connection)
    response = client.post(f"/api/prd-versions/{second['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert response.status_code == 500

    monkeypatch.setattr(prd, "get_connection", real_get_connection)
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    active = [v for v in history if v["active"]]
    assert len(active) == 1
    assert active[0]["id"] == first["id"]
    pending = next(v for v in history if v["id"] == second["id"])
    assert pending["status"] == "parsed"


# ---------- R-09：重启后预览、状态和活动版本仍在 ----------


def test_restart_preserves_versions_and_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "restart.db"))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(tmp_path / "prdroot"))
    importlib.reload(main)
    with TestClient(main.app) as first_client:
        project_id = _create_project(first_client, name="重启验证")
        version = _upload(first_client, project_id, make_md("重启后仍在的 PRD"), "persist.md").json()
        first_client.post(f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"})

    importlib.reload(main)
    with TestClient(main.app) as second_client:
        detail = second_client.get(f"/api/prd-versions/{version['id']}")
        history = second_client.get(f"/api/projects/{project_id}/prd-versions")
    assert detail.status_code == 200
    assert detail.json()["status"] == "parse_confirmed"
    assert "重启后仍在的 PRD" in detail.json()["preview"]
    active = [v for v in history.json() if v["active"]]
    assert len(active) == 1 and active[0]["id"] == version["id"]


# ---------- 上传查询与确认的边界 ----------


def test_upload_to_missing_project_404(env):
    client, _ = env
    response = _upload(client, 99999, make_md("无"), "x.md")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_get_version_not_found(env):
    client, _ = env
    response = client.get("/api/prd-versions/99999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PRD_VERSION_NOT_FOUND"


def test_confirm_parse_failed_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_image_only_pdf(), "scan.pdf").json()
    response = client.post(f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PRD_CONFIRM_INVALID_STATE"


def test_confirm_again_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("内容"), "v.md").json()
    client.post(f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"})
    response = client.post(f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PRD_CONFIRM_INVALID_STATE"


def test_confirm_superseded_rejected(env):
    client, _ = env
    project_id = _create_project(client)
    first = _upload(client, project_id, make_md("一"), "1.md").json()
    client.post(f"/api/prd-versions/{first['id']}/confirm", json={"confirmed_by": "项目经理"})
    second = _upload(client, project_id, make_md("二"), "2.md").json()
    client.post(f"/api/prd-versions/{second['id']}/confirm", json={"confirmed_by": "项目经理"})
    response = client.post(f"/api/prd-versions/{first['id']}/confirm", json={"confirmed_by": "项目经理"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PRD_CONFIRM_INVALID_STATE"


def test_confirm_not_found(env):
    client, _ = env
    response = client.post("/api/prd-versions/99999/confirm", json={"confirmed_by": "项目经理"})
    assert response.status_code == 404


def test_confirm_invalid_confirmed_by(env):
    client, _ = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("内容"), "v.md").json()
    response = client.post(
        f"/api/prd-versions/{version['id']}/confirm",
        json={"confirmed_by": "x" * 101},
    )
    assert response.status_code == 400


def test_confirm_default_confirmed_by(env):
    client, _ = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("内容"), "v.md").json()
    response = client.post(f"/api/prd-versions/{version['id']}/confirm", json={})
    assert response.status_code == 200
    assert response.json()["confirmed_by"] == "local"


def test_validate_filename_rejects_dangerous_forms():
    bad_names = [
        "a/b.md",
        "a\\b.md",
        "C:evil.md",
        "C:\\evil.md",
        "C:/evil.md",
        "/abs/path.md",
        "\\abs\\path.md",
        ".",
        "..",
        ".hidden.md",
        "..md",
        "a%2Fb.md",
        "a%5Cb.md",
        "a%01b.md",
        "bad\x00name.md",
        "bad\x1fname.md",
        "",
        "   ",
    ]
    for bad in bad_names:
        with pytest.raises(Exception) as excinfo:
            prd._validate_filename(bad)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail["code"] == "PRD_FILE_NAME_INVALID", repr(bad)


def test_validate_filename_accepts_normal_names():
    good_names = ["需求文档.md", "spec V1.0.txt", "readme.md", "a b.txt", "100%.md", "方案.docx"]
    for good in good_names:
        result = prd._validate_filename(good)
        assert result == good, repr(good)


def test_upload_path_style_filename_rejected(env):
    client, tmp_path = env
    project_id = _create_project(client)
    bad_names = [
        "subdir/evil.md",
        "..\\evil.md",
        "C:/evil.md",
        "/abs/path.md",
        ".hidden.md",
        "..md",
    ]
    for bad in bad_names:
        response = _upload(client, project_id, make_md("x"), bad)
        assert response.status_code == 400, repr(bad)
        assert response.json()["detail"]["code"] == "PRD_FILE_NAME_INVALID"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert not prd_dir.exists() or list(prd_dir.iterdir()) == []


def test_upload_control_char_filename_rejected(env):
    client, tmp_path = env
    project_id = _create_project(client)
    response = _upload(client, project_id, make_md("x"), "bad\x01name.md")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PRD_FILE_NAME_INVALID"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert not prd_dir.exists() or list(prd_dir.iterdir()) == []


def test_version_history_ordered_desc(env):
    client, _ = env
    project_id = _create_project(client)
    v1 = _upload(client, project_id, make_md("一"), "a.md").json()
    v2 = _upload(client, project_id, make_md("二"), "b.md").json()
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert [v["version_no"] for v in history] == [2, 1]
    assert history[0]["id"] == v2["id"]
    assert history[1]["id"] == v1["id"]


def test_reupload_same_name_preserves_old_version(env):
    client, _ = env
    project_id = _create_project(client)
    v1 = _upload(client, project_id, make_md("第一份"), "same.md").json()
    v2 = _upload(client, project_id, make_md("第二份"), "same.md").json()
    assert v2["version_no"] == 2
    assert v1["id"] != v2["id"]
    assert "第一份" in client.get(f"/api/prd-versions/{v1['id']}").json()["preview"]
    assert client.get(f"/api/projects/{project_id}/prd-versions").json().__len__() == 2


def test_missing_project_for_history_404(env):
    client, _ = env
    response = client.get("/api/projects/99999/prd-versions")
    assert response.status_code == 404


# ---------- 字符上限阻断 ----------


def test_upload_over_char_limit_blocked(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 100)
    response = _upload(client, project_id, make_txt("中" * 101), "long.txt")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parse_failed"
    assert any("超过上限" in w["message"] for w in body["warnings"])
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    assert history[0]["active"] is False


# ---------- 数据与安全隔离 ----------


def test_storage_only_within_temp_root(env):
    client, tmp_path = env
    project_id = _create_project(client)
    _upload(client, project_id, make_md("隔离验证"), "iso.md")
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert prd_dir.is_dir()
    files = list(prd_dir.iterdir())
    assert len(files) == 3
    for file in files:
        assert file.is_file()


def test_operations_only_inside_test_root(tmp_path, monkeypatch):
    formal_dir = tmp_path / "formal"
    formal_dir.mkdir()
    formal_db = formal_dir / "anxinboard.db"
    formal_db.write_text("FORMAL DB CONTENT")
    formal_root = formal_dir / "projects"
    formal_root.mkdir()
    formal_snapshot = {
        str(formal_db): (formal_db.stat().st_size, formal_db.stat().st_mtime_ns),
        str(formal_root): (formal_root.stat().st_size, formal_root.stat().st_mtime_ns),
    }

    test_root = tmp_path / "test"
    test_root.mkdir()
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(test_root / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(test_root / "prdroot"))
    importlib.reload(main)
    with TestClient(main.app) as client:
        assert str(db.get_db_path()).startswith(str(test_root))
        assert str(prd.get_prd_root()).startswith(str(test_root))
        project_id = _create_project(client)
        version = _upload(client, project_id, make_md("隔离验证"), "formal.md").json()
        client.post(f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"})

    assert (test_root / "test.db").is_file()
    assert (test_root / "prdroot" / str(project_id) / "prd").is_dir()
    assert len(list((test_root / "prdroot").rglob("*.txt"))) >= 1
    assert len(list((test_root / "prdroot").rglob("*.json"))) >= 1
    assert len(list((test_root / "prdroot").rglob("*"))) >= 3
    for path, before in formal_snapshot.items():
        after = (Path(path).stat().st_size, Path(path).stat().st_mtime_ns)
        assert after == before, f"模拟正式路径被改动：{path}"
    assert sorted(p.name for p in formal_dir.iterdir()) == ["anxinboard.db", "projects"]


def test_db_migration_adds_prd_versions_table(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    importlib.reload(main)
    with TestClient(main.app):
        pass
    with TestClient(main.app):
        pass

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "prd_versions" in tables


# ---------- Structured Persistence V1 ----------


def _assert_candidate_and_active_unchanged(client, project_id, active_id, candidate_id):
    history = client.get(f"/api/projects/{project_id}/prd-versions").json()
    by_id = {item["id"]: item for item in history}
    assert by_id[active_id]["status"] == "parse_confirmed"
    assert by_id[active_id]["active"] is True
    assert by_id[candidate_id]["status"] == "parsed"
    assert by_id[candidate_id]["active"] is False


def _make_active_and_candidate(client, project_id):
    active = _upload(client, project_id, make_md("已确认版本"), "active.md").json()
    confirmed = client.post(
        f"/api/prd-versions/{active['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert confirmed.status_code == 200
    candidate = _upload(client, project_id, make_md("待确认版本"), "candidate.md").json()
    return active, candidate


def test_db_migration_adds_structured_nullable_columns_without_backfill(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE prd_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            version_no INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            parsed_path TEXT NULL,
            parsed_hash TEXT NULL,
            parser_version TEXT NULL,
            status TEXT NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            confirmed_by TEXT NULL,
            confirmed_at TEXT NULL,
            UNIQUE (project_id, version_no)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO prd_versions (
            project_id, version_no, original_filename, source_path, source_hash,
            size_bytes, parsed_path, parsed_hash, parser_version, status,
            warnings_json, created_at, confirmed_by, confirmed_at
        ) VALUES (1, 1, 'old.md', '1/prd/source.md', ?, 3, '1/prd/parsed.txt', ?, ?,
                  'parse_confirmed', '[]', '2026-01-01T00:00:00+00:00', 'old', '2026-01-01T00:01:00+00:00')
        """,
        (_sha256(b"old"), _sha256(b"old"), "prd-parser-1.0"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(tmp_path / "prdroot"))
    importlib.reload(main)
    with TestClient(main.app):
        pass
    with TestClient(main.app):
        pass

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prd_versions)").fetchall()}
    row = conn.execute("SELECT * FROM prd_versions WHERE id = 1").fetchone()
    conn.close()
    assert {
        "structured_path",
        "structured_hash",
        "structured_schema_version",
        "structured_parser_version",
        "document_fingerprint",
    }.issubset(columns)
    assert row["status"] == "parse_confirmed"
    assert row["source_hash"] == _sha256(b"old")
    assert row["parsed_hash"] == _sha256(b"old")
    assert row["parser_version"] == "prd-parser-1.0"
    for field in (
        "structured_path",
        "structured_hash",
        "structured_schema_version",
        "structured_parser_version",
        "document_fingerprint",
    ):
        assert row[field] is None


def test_upload_structured_failure_is_parse_failed_without_partial_artifacts(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)

    def fail_structured(*args, **kwargs):
        raise prd_parser.PrdParseError("structured failed")

    monkeypatch.setattr(prd, "parse_prd_structured_bytes", fail_structured)
    response = _upload(client, project_id, make_md("legacy succeeds"), "only-legacy.md")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parse_failed"
    assert not body.get("parsed_hash")
    assert not body.get("structured_hash")
    row = _db_artifact_row(tmp_path, body["id"])
    assert row["parsed_path"] is None
    assert row["structured_path"] is None
    files = list((tmp_path / "prdroot" / str(project_id) / "prd").iterdir())
    assert len(files) == 1


def test_upload_legacy_failure_is_parse_failed_without_partial_artifacts(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)

    def fail_legacy(*args, **kwargs):
        raise prd_parser.PrdParseError("legacy failed")

    monkeypatch.setattr(prd, "parse_prd_bytes", fail_legacy)
    response = _upload(client, project_id, make_md("structured succeeds"), "only-structured.md")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "parse_failed"
    assert not body.get("parsed_hash")
    assert not body.get("structured_hash")
    row = _db_artifact_row(tmp_path, body["id"])
    assert row["parsed_path"] is None
    assert row["structured_path"] is None
    files = list((tmp_path / "prdroot" / str(project_id) / "prd").iterdir())
    assert len(files) == 1


def test_upload_source_format_mismatch_fails_closed(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    real = prd.parse_prd_structured_bytes

    def mismatched(data, ext):
        result = real(data, ext)
        result["source_format"] = "txt"
        return result

    monkeypatch.setattr(prd, "parse_prd_structured_bytes", mismatched)
    response = _upload(client, project_id, make_md("format mismatch"), "mismatch.md")
    assert response.status_code == 201
    assert response.json()["status"] == "parse_failed"


def test_upload_fingerprint_self_inconsistency_fails_closed(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    real = prd.parse_prd_structured_bytes

    def mismatched(data, ext):
        result = real(data, ext)
        result["document_fingerprint"] = "0" * 64
        return result

    monkeypatch.setattr(prd, "parse_prd_structured_bytes", mismatched)
    response = _upload(client, project_id, make_md("fingerprint mismatch"), "mismatch.md")
    assert response.status_code == 201
    assert response.json()["status"] == "parse_failed"


def test_parsed_write_failure_cleans_source(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)

    def fail_parsed(*args, **kwargs):
        raise OSError("parsed write failed")

    monkeypatch.setattr(prd, "_write_parsed_file", fail_parsed)
    response = _upload(client, project_id, make_md("write fail"), "parsed-fail.md")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_FAILED"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert not prd_dir.exists() or list(prd_dir.iterdir()) == []


def test_structured_write_failure_cleans_source_and_parsed(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)

    def fail_structured(*args, **kwargs):
        raise OSError("structured write failed")

    monkeypatch.setattr(prd, "_write_structured_file", fail_structured)
    response = _upload(client, project_id, make_md("write fail"), "structured-fail.md")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_FAILED"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert not prd_dir.exists() or list(prd_dir.iterdir()) == []


def test_structured_post_write_storage_escape_cleans_orphan(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)
    real_resolve = prd._resolve_storage_target
    structured_calls = {}

    def fail_once_after_structured_write(rel):
        target = real_resolve(rel)
        if rel.endswith(".json"):
            structured_calls[rel] = structured_calls.get(rel, 0) + 1
            if structured_calls[rel] == 3:
                assert target.exists()
                assert target.read_bytes()
                raise prd._storage_escape("simulated post-write verification failure")
        return target

    monkeypatch.setattr(prd, "_resolve_storage_target", fail_once_after_structured_write)
    response = _upload(client, project_id, make_md("post-write escape"), "post-write.md")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_ESCAPE"
    assert client.get(f"/api/projects/{project_id}/prd-versions").json() == []
    assert len(structured_calls) == 1
    structured_rel = next(iter(structured_calls))
    assert structured_calls[structured_rel] >= 4
    assert not (tmp_path / "prdroot" / structured_rel).exists()
    prd_dir = tmp_path / "prdroot" / str(project_id) / "prd"
    assert not prd_dir.exists() or list(prd_dir.iterdir()) == []


def test_confirm_uses_frozen_artifacts_without_reparse(env, monkeypatch):
    client, _ = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("frozen"), "frozen.md").json()

    def forbidden(*args, **kwargs):
        raise AssertionError("confirm must not call parser")

    monkeypatch.setattr(prd, "parse_prd_bytes", forbidden)
    monkeypatch.setattr(prd, "parse_prd_structured_bytes", forbidden)
    response = client.post(
        f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "parse_confirmed"


@pytest.mark.parametrize(
    "artifact_field,mode",
    [
        ("source_path", "missing"),
        ("source_path", "tamper"),
        ("parsed_path", "missing"),
        ("parsed_path", "tamper"),
        ("structured_path", "missing"),
        ("structured_path", "tamper"),
    ],
)
def test_confirm_artifact_missing_or_tampered_fails_closed(env, monkeypatch, artifact_field, mode):
    client, tmp_path = env
    project_id = _create_project(client)
    active, candidate = _make_active_and_candidate(client, project_id)
    row = _db_artifact_row(tmp_path, candidate["id"])
    target = tmp_path / "prdroot" / row[artifact_field]
    if mode == "missing":
        target.unlink()
    else:
        target.write_bytes(target.read_bytes() + b"tampered")

    def forbidden(*args, **kwargs):
        raise AssertionError("confirm must not call parser")

    monkeypatch.setattr(prd, "parse_prd_bytes", forbidden)
    monkeypatch.setattr(prd, "parse_prd_structured_bytes", forbidden)
    response = client.post(
        f"/api/prd-versions/{candidate['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_STRUCTURED_RESULT_INVALID"
    _assert_candidate_and_active_unchanged(client, project_id, active["id"], candidate["id"])


def _rewrite_structured_and_db(tmp_path, version_id, mutator, *, update_fingerprint=False):
    row = _db_artifact_row(tmp_path, version_id)
    path = tmp_path / "prdroot" / row["structured_path"]
    structured = json.loads(path.read_text(encoding="utf-8"))
    mutator(structured)
    data = prd._canonical_json_bytes(structured)
    path.write_bytes(data)
    conn = sqlite3.connect(tmp_path / "test.db")
    if update_fingerprint:
        conn.execute(
            "UPDATE prd_versions SET structured_hash = ?, document_fingerprint = ? WHERE id = ?",
            (_sha256(data), structured["document_fingerprint"], version_id),
        )
    else:
        conn.execute(
            "UPDATE prd_versions SET structured_hash = ? WHERE id = ?",
            (_sha256(data), version_id),
        )
    conn.commit()
    conn.close()


def test_confirm_corrupt_structured_json_with_matching_db_hash_fails_closed(env):
    client, tmp_path = env
    project_id = _create_project(client)
    active, candidate = _make_active_and_candidate(client, project_id)
    row = _db_artifact_row(tmp_path, candidate["id"])
    path = tmp_path / "prdroot" / row["structured_path"]
    corrupt = b"{not-json"
    path.write_bytes(corrupt)
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        "UPDATE prd_versions SET structured_hash = ? WHERE id = ?",
        (_sha256(corrupt), candidate["id"]),
    )
    conn.commit()
    conn.close()
    response = client.post(
        f"/api/prd-versions/{candidate['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_STRUCTURED_RESULT_INVALID"
    _assert_candidate_and_active_unchanged(client, project_id, active["id"], candidate["id"])


def test_confirm_source_format_identity_mismatch_fails_closed(env):
    client, tmp_path = env
    project_id = _create_project(client)
    active, candidate = _make_active_and_candidate(client, project_id)

    def mutate(structured):
        structured["source_format"] = "txt"

    _rewrite_structured_and_db(tmp_path, candidate["id"], mutate)
    response = client.post(
        f"/api/prd-versions/{candidate['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_STRUCTURED_RESULT_INVALID"
    _assert_candidate_and_active_unchanged(client, project_id, active["id"], candidate["id"])


def test_confirm_fingerprint_self_inconsistency_fails_closed(env):
    client, tmp_path = env
    project_id = _create_project(client)
    active, candidate = _make_active_and_candidate(client, project_id)

    def mutate(structured):
        structured["document_fingerprint"] = "0" * 64

    _rewrite_structured_and_db(
        tmp_path,
        candidate["id"],
        mutate,
        update_fingerprint=True,
    )
    response = client.post(
        f"/api/prd-versions/{candidate['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_STRUCTURED_RESULT_INVALID"
    _assert_candidate_and_active_unchanged(client, project_id, active["id"], candidate["id"])


def test_historical_parsed_without_structured_requires_reimport_no_reparse(env, monkeypatch):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("legacy parsed"), "legacy.md").json()
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        """
        UPDATE prd_versions SET
            structured_path = NULL,
            structured_hash = NULL,
            structured_schema_version = NULL,
            structured_parser_version = NULL,
            document_fingerprint = NULL
        WHERE id = ?
        """,
        (version["id"],),
    )
    conn.commit()
    conn.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("historical confirm must not backfill/reparse")

    monkeypatch.setattr(prd, "parse_prd_bytes", forbidden)
    monkeypatch.setattr(prd, "parse_prd_structured_bytes", forbidden)
    response = client.post(
        f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRD_STRUCTURED_RESULT_REQUIRED"
    detail = client.get(f"/api/prd-versions/{version['id']}").json()
    assert detail["status"] == "parsed"


def test_historical_confirmed_without_structured_still_readable(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("historical confirmed"), "historical.md").json()
    assert client.post(
        f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"}
    ).status_code == 200
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        """
        UPDATE prd_versions SET
            structured_path = NULL,
            structured_hash = NULL,
            structured_schema_version = NULL,
            structured_parser_version = NULL,
            document_fingerprint = NULL
        WHERE id = ?
        """,
        (version["id"],),
    )
    conn.commit()
    conn.close()
    detail = client.get(f"/api/prd-versions/{version['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "parse_confirmed"
    assert "historical confirmed" in detail.json()["preview"]


def test_prd_structured_persistence_does_not_write_evidence_tables(env):
    client, tmp_path = env
    project_id = _create_project(client)
    version = _upload(client, project_id, make_md("no evidence yet"), "no-evidence.md").json()
    assert client.post(
        f"/api/prd-versions/{version['id']}/confirm", json={"confirmed_by": "项目经理"}
    ).status_code == 200
    conn = sqlite3.connect(tmp_path / "test.db")
    snapshot_count = conn.execute("SELECT COUNT(*) FROM evidence_snapshots").fetchone()[0]
    item_count = conn.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0]
    conn.close()
    assert snapshot_count == 0
    assert item_count == 0
