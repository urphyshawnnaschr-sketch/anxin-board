"""PRD 解析器单元测试：四类格式、编码、大小、字符上限、空内容、图片型 PDF、资源边界。"""

import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import main, prd, prd_parser  # noqa: E402
from app.prd_parser import (  # noqa: E402
    MAX_TEXT_CHARS,
    PrdParseError,
    parse_prd_bytes,
)
from prd_fixtures import (  # noqa: E402
    make_corrupt_docx,
    make_corrupt_pdf,
    make_docx,
    make_docx_bad_crc,
    make_docx_corrupt_body_stream,
    make_docx_custom,
    make_docx_many_entries,
    make_docx_path_traversal,
    make_docx_zip_bomb,
    make_image_only_pdf,
    make_md,
    make_text_pdf,
    make_txt,
)


def test_parse_markdown():
    text, version = parse_prd_bytes(make_md("你好，这是 PRD 正文"), ".md")
    assert "你好，这是 PRD 正文" in text
    assert version == "prd-parser-1.0"


def test_parse_txt_utf8():
    text, _ = parse_prd_bytes(make_txt("纯文本 PRD 内容"), ".txt")
    assert text == "纯文本 PRD 内容"


def test_parse_txt_gb18030():
    text, _ = parse_prd_bytes(make_txt("GB 编码的中文内容", encoding="gb18030"), ".txt")
    assert text == "GB 编码的中文内容"


def test_parse_txt_binary_is_rejected():
    """二进制内容（含 NUL）不得被强行当作文本：必须结构化拒绝，不产生伪预览。"""
    raw = bytes(range(0, 256))
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(raw, ".txt")
    assert "有效文本" in str(excinfo.value) or "NUL" in str(excinfo.value)


def test_parse_txt_undecodable_binary_is_rejected():
    raw = b"\x81\x82\x83 non-utf8 binary bytes"
    with pytest.raises(PrdParseError):
        parse_prd_bytes(raw, ".txt")


def test_parse_txt_high_control_ratio_rejected():
    raw = bytes([0x01, 0x02, 0x03]) * 100 + b"tail"
    with pytest.raises(PrdParseError):
        parse_prd_bytes(raw, ".txt")


def test_parse_txt_high_replacement_ratio_rejected():
    content = ("\ufffd" * 50) + "正常文本内容"
    with pytest.raises(PrdParseError):
        parse_prd_bytes(content.encode("utf-8"), ".txt")


def test_parse_docx():
    text, _ = parse_prd_bytes(make_docx(["第一段内容", "第二段内容"]), ".docx")
    assert "第一段内容" in text
    assert "第二段内容" in text


def test_parse_docx_missing_body_is_failure():
    with pytest.raises(PrdParseError):
        parse_prd_bytes(make_corrupt_docx(), ".docx")


def test_parse_text_pdf():
    text, _ = parse_prd_bytes(make_text_pdf("Hello PRD 123"), ".pdf")
    assert "Hello PRD" in text


def test_parse_image_only_pdf_is_rejected():
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_image_only_pdf(), ".pdf")
    assert "不支持 OCR" in str(excinfo.value)


def test_parse_corrupt_pdf_is_rejected():
    with pytest.raises(PrdParseError):
        parse_prd_bytes(make_corrupt_pdf(), ".pdf")


def test_parse_unsupported_extension():
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(b"hello", ".exe")
    assert "不支持" in str(excinfo.value)


def test_parse_empty_bytes():
    with pytest.raises(PrdParseError):
        parse_prd_bytes(b"", ".md")


def test_parse_whitespace_only_content():
    with pytest.raises(PrdParseError):
        parse_prd_bytes(b"   \n\t  ", ".txt")


def test_parse_extension_case_insensitive():
    text, _ = parse_prd_bytes(make_txt("大写扩展名"), ".TXT")
    assert text == "大写扩展名"


def test_parse_over_char_limit():
    content = "中" * (MAX_TEXT_CHARS + 1)
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_txt(content), ".txt")
    assert "超过上限" in str(excinfo.value)


def test_parse_at_char_limit_boundary():
    content = "中" * MAX_TEXT_CHARS
    text, _ = parse_prd_bytes(make_txt(content), ".txt")
    assert len(text) == MAX_TEXT_CHARS


# ---------- DOCX 资源边界 ----------


def test_parse_docx_at_char_limit_boundary(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 100)
    text, _ = parse_prd_bytes(make_docx(["a" * 100]), ".docx")
    assert len(text) == 100


def test_parse_docx_over_char_limit_stops_during_processing(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 100)
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_docx(["a" * 60, "b" * 60]), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_parse_docx_over_char_limit_counts_all_paragraphs(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 100)
    with pytest.raises(PrdParseError):
        parse_prd_bytes(make_docx(["a" * 40, "b" * 40, "c" * 40]), ".docx")


def test_parse_docx_entry_count_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_ZIP_ENTRIES", 3)
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_docx_many_entries(10), ".docx")
    assert "条目数量" in str(excinfo.value)


def test_parse_docx_entry_uncompressed_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_ZIP_ENTRY_UNCOMPRESSED", 1024)
    huge = b"x" * (16 * 1024)
    data = make_docx_custom({"word/document.xml": huge})
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(data, ".docx")
    assert "展开大小" in str(excinfo.value)


def test_parse_docx_total_uncompressed_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_ZIP_TOTAL_UNCOMPRESSED", 2048)
    data = make_docx_custom(
        {
            "word/document.xml": b"<w:document/>",
            "extra/1.bin": b"a" * 2048,
            "extra/2.bin": b"b" * 2048,
        }
    )
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(data, ".docx")
    assert "总展开大小" in str(excinfo.value)


def test_parse_docx_abnormal_compression_ratio(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_ZIP_COMPRESSION_RATIO", 10)
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_docx_zip_bomb(), ".docx")
    assert "压缩比" in str(excinfo.value)


def test_parse_docx_path_traversal_entry_rejected():
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_docx_path_traversal(), ".docx")
    assert "路径" in str(excinfo.value)


def test_parse_docx_does_not_read_arbitrary_entries():
    """只读取固定正文条目 word/document.xml：正文缺失时即使存在大量其他条目也失败。"""
    data = make_docx_custom({"random/file.xml": b"<x/>", "extra.bin": b"y" * 64})
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(data, ".docx")
    assert "正文" in str(excinfo.value)


def test_parse_docx_corrupt_container_fails():
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_corrupt_docx(), ".docx")
    assert "损坏" in str(excinfo.value)


def test_parse_docx_corrupt_body_stream_fails_structurally():
    data = make_docx_corrupt_body_stream()
    assert zipfile.is_zipfile(io.BytesIO(data))
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(data, ".docx")
    assert "docx 文件损坏" in str(excinfo.value)
    assert "压缩流" in str(excinfo.value) or "压缩容器" in str(excinfo.value)


def test_parse_docx_bad_crc_fails_structurally():
    """中央目录 CRC 被篡改的 docx：读流时 CRC 校验失败，必须收敛为 PrdParseError 且不泄漏内部异常类型。"""
    data = make_docx_bad_crc()
    assert zipfile.is_zipfile(io.BytesIO(data))
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(data, ".docx")
    message = str(excinfo.value)
    assert "损坏" in message
    for leaked in ("BadZipFile", "zlib", "CRC", "Traceback", "C:", "\\", "python", "line "):
        assert leaked.lower() not in message.lower(), message


class _OpenFailureArchive:
    def __init__(self, error):
        self.error = error
        self.info = zipfile.ZipInfo("word/document.xml")
        self.info.file_size = 100
        self.info.compress_size = 50

    def infolist(self):
        return [self.info]

    def getinfo(self, _name):
        return self.info

    def open(self, _name):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        zipfile.BadZipFile("crc"),
        RuntimeError("decompress"),
        OSError("io"),
        EOFError("short"),
        NotImplementedError("compression"),
    ],
)
def test_parse_docx_archive_open_failures_are_structured(error):
    with pytest.raises(PrdParseError) as excinfo:
        prd_parser._extract_docx_from_archive(_OpenFailureArchive(error))
    assert "正文压缩流无法读取" in str(excinfo.value)


def test_parse_docx_unexpected_programming_error_is_not_swallowed():
    with pytest.raises(ValueError):
        prd_parser._extract_docx_from_archive(_OpenFailureArchive(ValueError("bug")))


class _ReadFailureStream:
    def read(self, _size=-1):
        raise OSError("stream failed")


def test_parse_docx_body_stream_read_failure_is_structured():
    with pytest.raises(PrdParseError) as excinfo:
        prd_parser._extract_docx_paragraphs(_ReadFailureStream())
    assert "正文压缩流无法读取" in str(excinfo.value)


# ---------- PDF 资源边界 ----------


def test_parse_pdf_at_char_limit_boundary(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 100)
    text, _ = parse_prd_bytes(make_text_pdf("x" * 100), ".pdf")
    assert len(text) >= 100


def test_parse_pdf_over_char_limit_stops_remaining_pages(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 100)
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_text_pdf("x" * 150), ".pdf")
    assert "超过上限" in str(excinfo.value)


def test_parse_pdf_page_count_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_PDF_PAGES", 2)
    pages = [_CountingPage("hi") for _ in range(3)]
    monkeypatch.setattr(prd_parser, "PdfReader", lambda _data: _FakeReader(pages))
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(b"x", ".pdf")
    assert "页数" in str(excinfo.value)


class _CountingPage:
    """可计数的 PDF 页：记录 extract_text 调用次数，用于验证提前停止。"""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def extract_text(self):
        self.calls += 1
        return self.text


class _FakeReader:
    """可控的假 PdfReader：页数可控，页对象计数。"""

    def __init__(self, pages):
        self.pages = pages


def test_parse_pdf_stops_extracting_after_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 50)
    pages = [_CountingPage("x" * 40) for _ in range(4)]
    monkeypatch.setattr(prd_parser, "PdfReader", lambda _data: _FakeReader(pages))
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(b"x", ".pdf")
    assert "超过上限" in str(excinfo.value)
    called = sum(1 for p in pages if p.calls)
    assert called == 2  # 第 3 页累计超过上限，剩余页面不再提取


# ---------- 原行为不回归 ----------


def test_parse_docx_normal_still_works():
    text, _ = parse_prd_bytes(make_docx(["第一段内容", "第二段内容"]), ".docx")
    assert "第一段内容" in text
    assert "第二段内容" in text


def test_parse_pdf_normal_still_works():
    text, _ = parse_prd_bytes(make_text_pdf("Hello PDF PRD"), ".pdf")
    assert "Hello PDF PRD" in text


def test_parse_image_only_pdf_still_rejected():
    with pytest.raises(PrdParseError) as excinfo:
        parse_prd_bytes(make_image_only_pdf(), ".pdf")
    assert "不支持 OCR" in str(excinfo.value)


def test_parse_corrupt_pdf_still_rejected():
    with pytest.raises(PrdParseError):
        parse_prd_bytes(make_corrupt_pdf(), ".pdf")


# ---------- API 与公共路径安全机制专项 ----------


def test_api_corrupt_docx_body_stream_is_controlled_parse_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    prd_root = tmp_path / "prdroot"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(prd_root))
    with TestClient(main.app) as client:
        project = client.post("/api/projects", json={"name": "损坏流测试"}).json()
        response = client.post(
            f"/api/projects/{project['id']}/prd-versions",
            files={"file": ("broken.docx", make_docx_corrupt_body_stream(), "application/octet-stream")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "parse_failed"
        assert body["preview"] == ""
        assert body.get("parsed_hash") is None
        assert body["warnings"]
        warning = body["warnings"][0]["message"]
        assert "docx 文件损坏" in warning
        assert str(tmp_path) not in warning
        assert "Traceback" not in warning
        confirm = client.post(f"/api/prd-versions/{body['id']}/confirm", json={})
        assert confirm.status_code == 400
        assert confirm.json()["detail"]["code"] == "PRD_CONFIRM_INVALID_STATE"
        history = client.get(f"/api/projects/{project['id']}/prd-versions").json()
        assert history[0]["active"] is False
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source_path, parsed_path, parsed_hash, status FROM prd_versions WHERE id = ?",
        (body["id"],),
    ).fetchone()
    conn.close()
    assert row["status"] == "parse_failed"
    assert row["parsed_path"] is None
    assert row["parsed_hash"] is None
    assert (prd_root / row["source_path"]).exists()
    assert len(list((prd_root / str(project["id"]) / "prd").iterdir())) == 1


def test_write_read_cleanup_use_same_storage_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(tmp_path / "root"))
    calls = []
    original = prd._resolve_storage_target

    def spy(rel):
        calls.append(rel)
        return original(rel)

    monkeypatch.setattr(prd, "_resolve_storage_target", spy)
    rel = "1/prd/shared.txt"
    prd._secure_write(rel, "共享机制".encode("utf-8"))
    text, truncated = prd._read_preview(rel)
    assert text == "共享机制"
    assert truncated is False
    prd._remove_files([rel])
    assert not (tmp_path / "root" / rel).exists()
    assert calls.count(rel) >= 5


# ---------- PRD Structured Evidence Contract V1 ----------


def _structured(data: bytes, extension: str):
    return prd_parser.parse_prd_structured_bytes(data, extension)


def _structured_docx_xml(body_xml: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{prd_parser._W_NS}"><w:body>{body_xml}</w:body></w:document>'
    ).encode("utf-8")
    return make_docx_custom({"word/document.xml": document})


def test_structured_markdown_cursor_stops_before_atx_setext_and_table():
    raw = (
        "alpha\n"
        "# Beta\n"
        "gamma\n"
        "Delta\n"
        "----\n"
        "epsilon\n"
        "a | b\n"
        "--- | ---\n"
        "1 | 2\n"
        "omega\n"
    ).encode("utf-8")
    result = _structured(raw, ".md")
    blocks = result["blocks"]
    assert [(b["kind"], b["text"]) for b in blocks[:5]] == [
        ("paragraph", "alpha"),
        ("heading", "Beta"),
        ("paragraph", "gamma"),
        ("heading", "Delta"),
        ("paragraph", "epsilon"),
    ]
    assert blocks[5]["kind"] == "table"
    assert blocks[5]["table_rows"] == [["a", "b"], ["1", "2"]]
    assert blocks[6]["kind"] == "paragraph"
    assert blocks[6]["text"] == "omega"
    assert [b["ordinal"] for b in blocks] == list(range(1, len(blocks) + 1))


def test_structured_markdown_fence_three_spaces_closes_four_spaces_does_not():
    three = b"```\ninside\n   ```\nafter\n"
    result = _structured(three, ".md")
    assert [b["kind"] for b in result["blocks"]] == ["paragraph", "paragraph"]
    assert result["blocks"][0]["text"] == "```\ninside\n   ```"
    assert result["blocks"][1]["text"] == "after"

    four = b"```\ninside\n    ```\nafter\n"
    result = _structured(four, ".md")
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["text"] == "```\ninside\n    ```\nafter"


def test_structured_markdown_fenced_content_does_not_create_heading_or_table():
    raw = b"```\n# not heading\na | b\n--- | ---\n```\n"
    result = _structured(raw, ".md")
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["kind"] == "paragraph"


def test_structured_markdown_pipe_escape_uses_odd_even_backslash_rule():
    one = "a\\|b | c\n--- | ---\n1 | 2\n".encode("utf-8")
    result = _structured(one, ".md")
    assert result["blocks"][0]["table_rows"][0] == ["a|b", "c"]

    two = "a\\\\|b | c\n--- | --- | ---\n1 | 2 | 3\n".encode("utf-8")
    result = _structured(two, ".md")
    assert result["blocks"][0]["table_rows"][0] == ["a\\\\", "b", "c"]

    three = "a\\\\\\|b | c\n--- | ---\n1 | 2\n".encode("utf-8")
    result = _structured(three, ".md")
    assert result["blocks"][0]["table_rows"][0] == ["a\\\\|b", "c"]


def test_structured_markdown_empty_atx_is_consumed_without_fallback_block():
    result = _structured(b"#   \nbody\n", ".md")
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["kind"] == "paragraph"
    assert result["blocks"][0]["text"] == "body"


def test_structured_markdown_non_structural_constructs_use_paragraph_fallback():
    raw = (
        "- item one\n- item two\n\n"
        "> quote\n\n"
        "    indented code\n\n"
        "***\n\n"
        "<div>html</div>\n"
    ).encode("utf-8")
    result = _structured(raw, ".md")
    assert [b["kind"] for b in result["blocks"]] == ["paragraph"] * 5


def test_structured_txt_only_splits_on_blank_lines_and_does_not_guess_structure():
    raw = b"# not heading\nsecond line\n\n| not | table |\n"
    result = _structured(raw, ".txt")
    assert [b["kind"] for b in result["blocks"]] == ["paragraph", "paragraph"]
    assert result["blocks"][0]["text"] == "# not heading\nsecond line"


def test_structured_same_bytes_different_source_format_changes_identity():
    raw = b"# Title\n\nbody\n"
    md = _structured(raw, ".md")
    txt = _structured(raw, ".txt")
    assert md["source_hash"] == txt["source_hash"]
    assert md["source_format"] == "md"
    assert txt["source_format"] == "txt"
    assert md["document_fingerprint"] != txt["document_fingerprint"]
    assert md["blocks"][0]["evidence_id"] != txt["blocks"][0]["evidence_id"]


def test_structured_repeat_is_byte_identity_stable():
    raw = "# 标题\n\n正文\n".encode("utf-8")
    assert _structured(raw, ".md") == _structured(raw, ".md")


def test_structured_parser_or_schema_version_change_changes_document_identity(monkeypatch):
    raw = b"body"
    baseline = _structured(raw, ".txt")
    monkeypatch.setattr(prd_parser, "STRUCTURED_PARSER_VERSION", "prd-structured-parser-1.1")
    parser_changed = _structured(raw, ".txt")
    assert parser_changed["document_fingerprint"] != baseline["document_fingerprint"]
    monkeypatch.setattr(prd_parser, "STRUCTURED_PARSER_VERSION", "prd-structured-parser-1.0")
    monkeypatch.setattr(prd_parser, "STRUCTURED_SCHEMA_VERSION", "prd_structured_evidence_v2")
    schema_changed = _structured(raw, ".txt")
    assert schema_changed["document_fingerprint"] != baseline["document_fingerprint"]


def test_structured_hashes_are_lowercase_sha256_and_content_hash_excludes_locator(monkeypatch):
    pages = [_CountingPage("same text"), _CountingPage(""), _CountingPage("same text")]
    monkeypatch.setattr(prd_parser, "PdfReader", lambda _data: _FakeReader(pages))
    result = _structured(b"pdf-source", ".pdf")
    assert [b["page_no"] for b in result["blocks"]] == [1, 3]
    first, second = result["blocks"]
    assert first["content_hash"] == second["content_hash"]
    assert first["ordinal"] != second["ordinal"]
    assert first["evidence_id"] != second["evidence_id"]
    for value in (result["source_hash"], result["document_fingerprint"], first["content_hash"]):
        assert len(value) == 64
        assert value == value.lower()
        int(value, 16)


def test_structured_heading_level_is_part_of_content_hash():
    h1 = _structured(b"# Same\n", ".md")["blocks"][0]
    h2 = _structured(b"## Same\n", ".md")["blocks"][0]
    assert h1["text"] == h2["text"] == "Same"
    assert h1["heading_level"] == 1
    assert h2["heading_level"] == 2
    assert h1["content_hash"] != h2["content_hash"]


def test_structured_docx_preserves_body_order_and_physical_table_cells():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Section</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Body</w:t></w:r></w:p>'
        '<w:tbl>'
        '<w:tr>'
        '<w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc>'
        '</w:tr>'
        '<w:tr>'
        '<w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
        '<w:tc><w:p><w:r><w:t>C</w:t></w:r></w:p></w:tc>'
        '</w:tr>'
        '</w:tbl>'
    )
    result = _structured(_structured_docx_xml(body), ".docx")
    blocks = result["blocks"]
    assert [b["kind"] for b in blocks] == ["heading", "paragraph", "table"]
    assert blocks[0]["heading_level"] == 2
    assert blocks[0]["text"] == "Section"
    assert blocks[1]["text"] == "Body"
    assert blocks[2]["table_rows"] == [["A", "B"], ["", "C"]]
    assert all(b["page_no"] is None for b in blocks)


def test_structured_docx_custom_heading_style_is_paragraph_not_guess():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="MyHeading"/></w:pPr>'
        '<w:r><w:t>Looks like heading</w:t></w:r></w:p>'
    )
    result = _structured(_structured_docx_xml(body), ".docx")
    assert result["blocks"][0]["kind"] == "paragraph"
    assert result["blocks"][0]["heading_level"] is None


def test_structured_docx_unsupported_body_child_with_text_fails_closed():
    body = '<w:sdt><w:sdtContent><w:p><w:r><w:t>Hidden business text</w:t></w:r></w:p></w:sdtContent></w:sdt>'
    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "不支持" in str(excinfo.value)


def test_structured_docx_table_paragraphs_are_not_duplicated_as_blocks():
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p><w:r><w:t>p1</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>p2</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )
    result = _structured(_structured_docx_xml(body), ".docx")
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["kind"] == "table"
    assert result["blocks"][0]["table_rows"] == [["p1\np2"]]


def test_structured_pdf_one_nonempty_block_per_physical_page(monkeypatch):
    pages = [
        _CountingPage("first\n\nparagraph-looking-gap"),
        _CountingPage("   \n\t"),
        _CountingPage("third"),
    ]
    monkeypatch.setattr(prd_parser, "PdfReader", lambda _data: _FakeReader(pages))
    result = _structured(b"pdf-source", ".pdf")
    assert len(result["blocks"]) == 2
    assert [b["page_no"] for b in result["blocks"]] == [1, 3]
    assert result["blocks"][0]["text"] == "first\n\nparagraph-looking-gap"
    assert all(b["kind"] == "paragraph" for b in result["blocks"])


def test_structured_pdf_image_only_rule_and_page_limit_still_apply(monkeypatch):
    pages = [_CountingPage("a"), _CountingPage("b")]
    monkeypatch.setattr(prd_parser, "PdfReader", lambda _data: _FakeReader(pages))
    with pytest.raises(PrdParseError) as excinfo:
        _structured(b"pdf-source", ".pdf")
    assert "不支持 OCR" in str(excinfo.value)

    monkeypatch.setattr(prd_parser, "MAX_PDF_PAGES", 1)
    pages = [_CountingPage("abc"), _CountingPage("def")]
    monkeypatch.setattr(prd_parser, "PdfReader", lambda _data: _FakeReader(pages))
    with pytest.raises(PrdParseError) as excinfo:
        _structured(b"pdf-source", ".pdf")
    assert "页数" in str(excinfo.value)


def test_structured_block_limit_fails_closed_without_truncation(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_STRUCTURED_BLOCKS", 1)
    with pytest.raises(PrdParseError) as excinfo:
        _structured(b"first\n\nsecond\n", ".txt")
    assert "证据块数量超过上限" in str(excinfo.value)


def test_structured_legacy_parse_prd_bytes_remains_unchanged():
    raw = make_md("兼容正文")
    text, version = parse_prd_bytes(raw, ".md")
    assert text == "# 标题\n\n兼容正文\n"
    assert version == "prd-parser-1.0"
    structured = _structured(raw, ".md")
    assert structured["parser_version"] == "prd-structured-parser-1.0"
    assert structured["schema_version"] == "prd_structured_evidence_v1"


def test_structured_markdown_single_column_table_allows_separator_only_in_delimiter():
    raw = b"alpha\n| --- |\n| beta |\n"
    result = _structured(raw, ".md")
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["kind"] == "table"
    assert result["blocks"][0]["table_rows"] == [["alpha"], ["beta"]]


def test_structured_markdown_single_column_table_allows_separator_only_in_header():
    raw = b"| alpha |\n:---:\n| beta |\n"
    result = _structured(raw, ".md")
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["kind"] == "table"
    assert result["blocks"][0]["table_rows"] == [["alpha"], ["beta"]]


def test_structured_markdown_single_column_body_still_requires_separator():
    raw = b"alpha\n| --- |\nbeta\n"
    result = _structured(raw, ".md")
    assert [block["kind"] for block in result["blocks"]] == ["table", "paragraph"]
    assert result["blocks"][0]["table_rows"] == [["alpha"]]
    assert result["blocks"][1]["text"] == "beta"


def test_structured_markdown_table_requires_separator_in_header_or_delimiter():
    raw = b"alpha\n:---:\nbeta\n"
    result = _structured(raw, ".md")
    assert all(block["kind"] != "table" for block in result["blocks"])


def test_structured_docx_cross_cell_boundary_counts_only_canonical_text(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 2)
    body = (
        '<w:tbl><w:tr>'
        '<w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc>'
        '</w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A", "B"]]


def test_structured_docx_cross_cell_boundary_still_fails_closed(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:tbl><w:tr>'
        '<w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc>'
        '</w:tr></w:tbl>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_direct_body_blocks_count_only_canonical_text(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 2)
    body = (
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert [(block["kind"], block["text"]) for block in result["blocks"]] == [
        ("paragraph", "A"),
        ("paragraph", "B"),
    ]


def test_structured_docx_direct_body_blocks_still_fail_closed(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_leading_empty_paragraph_is_not_counted(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p/>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A"]]


def test_structured_docx_leading_empty_paragraph_still_fails_during_streaming(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 0)
    monkeypatch.setattr(
        prd_parser,
        "_structured_content_char_count",
        lambda _blocks: pytest.fail("final structured-content guard was reached"),
    )
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p/>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


@pytest.mark.parametrize("cell_text", [" A ", " A", "\tA", "A ", "A\t"])
def test_structured_docx_cell_edge_whitespace_is_not_counted(monkeypatch, cell_text):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:tbl><w:tr><w:tc><w:p><w:r>'
        f'<w:t xml:space="preserve">{cell_text}</w:t>'
        '</w:r></w:p></w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A"]]


def test_structured_docx_leading_whitespace_only_paragraph_is_not_counted(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p><w:r><w:t xml:space="preserve"> \t</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A"]]


def test_structured_docx_trailing_whitespace_only_paragraph_is_not_counted(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve"> \t</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A"]]


def test_structured_docx_retained_internal_whitespace_matches_canonical_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 5)
    body = (
        '<w:tbl><w:tr><w:tc><w:p><w:r>'
        '<w:t xml:space="preserve">A \t B</w:t>'
        '</w:r></w:p></w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A \t B"]]


def test_structured_docx_retained_internal_whitespace_fails_during_streaming(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 4)
    monkeypatch.setattr(
        prd_parser,
        "_structured_content_char_count",
        lambda _blocks: pytest.fail("final structured-content guard was reached"),
    )
    body = (
        '<w:tbl><w:tr><w:tc><w:p><w:r>'
        '<w:t xml:space="preserve">A \t B</w:t>'
        '</w:r></w:p></w:tc></w:tr></w:tbl>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_whitespace_only_direct_body_block_is_not_counted(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 1)
    body = (
        '<w:p><w:r><w:t xml:space="preserve"> \t </w:t></w:r></w:p>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert [(block["kind"], block["text"]) for block in result["blocks"]] == [
        ("paragraph", "A")
    ]


def test_structured_docx_empty_physical_paragraphs_count_toward_char_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 5)
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p/><w:p/><w:p/>'
        '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )
    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_empty_physical_paragraphs_boundary_matches_final_cell(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 6)
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p/><w:p/><w:p/>'
        '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )
    result = _structured(_structured_docx_xml(body), ".docx")
    assert result["blocks"][0]["table_rows"] == [["A\n\n\n\nB"]]
    assert len(result["blocks"][0]["table_rows"][0][0]) == 6


def test_structured_docx_split_crlf_across_cell_runs_matches_canonical_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    body = (
        '<w:tbl><w:tr><w:tc><w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r>'
        '<w:r><w:t>&#10;B</w:t></w:r>'
        '</w:p></w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A\nB"]]


def test_structured_docx_split_crlf_across_direct_body_runs_matches_canonical_limit(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    body = (
        '<w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r>'
        '<w:r><w:t>&#10;B</w:t></w:r>'
        '</w:p>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["text"] == "A\nB"


def test_structured_docx_split_cr_before_non_lf_matches_canonical_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    body = (
        '<w:tbl><w:tr><w:tc><w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r>'
        '<w:r><w:t>B</w:t></w:r>'
        '</w:p></w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A\nB"]]


def test_structured_docx_split_crlf_with_pending_trim_matches_canonical_limit(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 5)
    body = (
        '<w:tbl><w:tr><w:tc><w:p>'
        '<w:r><w:t xml:space="preserve">A &#13;</w:t></w:r>'
        '<w:r><w:t xml:space="preserve">&#10; B</w:t></w:r>'
        '</w:p></w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A \n B"]]


def test_structured_docx_trailing_cr_is_flushed_and_reset_at_block_boundary(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 2)
    body = (
        '<w:p><w:r><w:t>A&#13;</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert [block["text"] for block in result["blocks"]] == ["A", "B"]


def test_structured_docx_split_crlf_over_limit_fails_during_streaming(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 2)
    monkeypatch.setattr(
        prd_parser,
        "_structured_content_char_count",
        lambda _blocks: pytest.fail("final structured-content guard was reached"),
    )
    body = (
        '<w:tbl><w:tr><w:tc><w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r>'
        '<w:r><w:t>&#10;B</w:t></w:r>'
        '</w:p></w:tc></w:tr></w:tbl>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_unsupported_body_descendant_over_limit_fails_during_streaming(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    monkeypatch.setattr(
        prd_parser,
        "_process_docx_body_child",
        lambda _element, _blocks: pytest.fail("end-of-child semantic guard was reached"),
    )
    body = (
        '<w:sdt><w:sdtContent><w:p><w:r><w:t>ABCD</w:t></w:r></w:p>'
        '</w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_unsupported_body_descendant_within_limit_stays_unsupported(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    body = (
        '<w:sdt><w:sdtContent><w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r>'
        '<w:r><w:t>&#10;B</w:t></w:r>'
        '</w:p></w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "不支持" in str(excinfo.value)


def test_structured_docx_trailing_cr_coalesces_with_cell_paragraph_separator(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    body = (
        '<w:tbl><w:tr><w:tc>'
        '<w:p><w:r><w:t>A&#13;</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )

    result = _structured(_structured_docx_xml(body), ".docx")

    assert result["blocks"][0]["table_rows"] == [["A\nB"]]


def test_structured_docx_combines_supported_limit_with_unsupported_usage(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    original_process = prd_parser._process_docx_body_child
    unsupported_tag = f"{{{prd_parser._W_NS}}}sdt"

    def fail_if_unsupported_semantic_guard_is_reached(element, blocks):
        if element.tag == unsupported_tag:
            pytest.fail("end-of-child semantic guard was reached")
        return original_process(element, blocks)

    monkeypatch.setattr(
        prd_parser,
        "_process_docx_body_child",
        fail_if_unsupported_semantic_guard_is_reached,
    )
    body = (
        '<w:p><w:r><w:t>ABC</w:t></w:r></w:p>'
        '<w:sdt><w:sdtContent><w:p><w:r><w:t>D</w:t></w:r></w:p>'
        '</w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_combined_budget_counts_normalized_unsupported_text(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 5)
    original_process = prd_parser._process_docx_body_child
    unsupported_tag = f"{{{prd_parser._W_NS}}}sdt"

    def fail_if_unsupported_semantic_guard_is_reached(element, blocks):
        if element.tag == unsupported_tag:
            pytest.fail("end-of-child semantic guard was reached")
        return original_process(element, blocks)

    monkeypatch.setattr(
        prd_parser,
        "_process_docx_body_child",
        fail_if_unsupported_semantic_guard_is_reached,
    )
    body = (
        '<w:p><w:r><w:t>ABCD</w:t></w:r></w:p>'
        '<w:sdt><w:sdtContent><w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r><w:r><w:t>&#10;</w:t></w:r>'
        '</w:p></w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_unsupported_usage_before_supported_uses_same_budget(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    monkeypatch.setattr(
        prd_parser,
        "_structured_content_char_count",
        lambda _blocks: pytest.fail("final structured-content guard was reached"),
    )
    body = (
        '<w:sdt><w:sdtContent><w:p><w:r>'
        '<w:t xml:space="preserve">  </w:t>'
        '</w:r></w:p></w:sdtContent></w:sdt>'
        '<w:p><w:r><w:t>AB</w:t></w:r></w:p>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_multiple_unsupported_children_share_cumulative_budget(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    original_process = prd_parser._process_docx_body_child
    unsupported_tag = f"{{{prd_parser._W_NS}}}sdt"
    processed_unsupported_children = 0

    def track_unsupported_semantic_guards(element, blocks):
        nonlocal processed_unsupported_children
        if element.tag == unsupported_tag:
            processed_unsupported_children += 1
        return original_process(element, blocks)

    monkeypatch.setattr(
        prd_parser,
        "_process_docx_body_child",
        track_unsupported_semantic_guards,
    )
    child = (
        '<w:sdt><w:sdtContent><w:p><w:r>'
        '<w:t xml:space="preserve">  </w:t>'
        '</w:r></w:p></w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(child + child), ".docx")
    assert "超过上限" in str(excinfo.value)
    assert processed_unsupported_children == 1


def test_structured_docx_unsupported_nested_cell_text_uses_resource_scope(monkeypatch):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    original_process = prd_parser._process_docx_body_child
    unsupported_tag = f"{{{prd_parser._W_NS}}}sdt"

    def fail_if_unsupported_semantic_guard_is_reached(element, blocks):
        if element.tag == unsupported_tag:
            pytest.fail("end-of-child semantic guard was reached")
        return original_process(element, blocks)

    monkeypatch.setattr(
        prd_parser,
        "_process_docx_body_child",
        fail_if_unsupported_semantic_guard_is_reached,
    )
    body = (
        '<w:sdt><w:sdtContent><w:tbl><w:tr><w:tc><w:p><w:r>'
        '<w:t xml:space="preserve">    </w:t>'
        '</w:r></w:p></w:tc></w:tr></w:tbl></w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)


def test_structured_docx_unsupported_nested_cell_split_crlf_stays_semantic(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    body = (
        '<w:sdt><w:sdtContent><w:tbl><w:tr><w:tc><w:p>'
        '<w:r><w:t>A&#13;</w:t></w:r>'
        '<w:r><w:t>&#10;B</w:t></w:r>'
        '</w:p></w:tc></w:tr></w:tbl></w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "不支持" in str(excinfo.value)


def test_structured_docx_unsupported_mixed_descendants_keep_one_ownership(
    monkeypatch,
):
    monkeypatch.setattr(prd_parser, "MAX_TEXT_CHARS", 3)
    original_process = prd_parser._process_docx_body_child
    unsupported_tag = f"{{{prd_parser._W_NS}}}sdt"

    def fail_if_unsupported_semantic_guard_is_reached(element, blocks):
        if element.tag == unsupported_tag:
            pytest.fail("end-of-child semantic guard was reached")
        return original_process(element, blocks)

    monkeypatch.setattr(
        prd_parser,
        "_process_docx_body_child",
        fail_if_unsupported_semantic_guard_is_reached,
    )
    body = (
        '<w:sdt><w:sdtContent>'
        '<w:p><w:r><w:t xml:space="preserve">  </w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r>'
        '<w:t xml:space="preserve">  </w:t>'
        '</w:r></w:p></w:tc></w:tr></w:tbl>'
        '</w:sdtContent></w:sdt>'
    )

    with pytest.raises(PrdParseError) as excinfo:
        _structured(_structured_docx_xml(body), ".docx")
    assert "超过上限" in str(excinfo.value)
