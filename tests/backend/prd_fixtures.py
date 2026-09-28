"""PRD 测试夹具：在内存中构造小体积 .md/.txt/.docx/.pdf 文件，全部为合成数据。"""

import io
import struct
import zipfile

from xml.sax.saxutils import escape

_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_md(text: str) -> bytes:
    return f"# 标题\n\n{text}\n".encode("utf-8")


def make_txt(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_DOCX_NS}"><w:body>{body}</w:body></w:document>'
    ).encode("utf-8")
    files = {
        "[Content_Types].xml": (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Override PartName="/word/document.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            b"</Types>"
        ),
        "_rels/.rels": (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            b'Target="word/document.xml"/>'
            b"</Relationships>"
        ),
        "word/document.xml": document,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_docx_corrupt_body_stream() -> bytes:
    """保持 ZIP 外壳和中央目录可读，只破坏正文压缩数据以触发解压或 CRC 失败。"""
    raw = bytearray(make_docx(["用于破坏压缩流的正文，长度足够形成稳定压缩数据。" * 8]))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("word/document.xml")
    name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
    data_start = info.header_offset + 30 + name_len + extra_len
    if info.compress_size <= 0:
        raise AssertionError("fixture expected compressed document.xml")
    raw[data_start + info.compress_size // 2] ^= 0xFF
    result = bytes(raw)
    assert zipfile.is_zipfile(io.BytesIO(result))
    return result


def _docx_central_crc_offset(data: bytes) -> int:
    """返回中央目录中 word/document.xml 条目 CRC 字段偏移（条目内偏移 16）。"""
    j = data.find(b"PK\x01\x02")
    while j != -1:
        fname_len = struct.unpack("<H", data[j + 28 : j + 30])[0]
        extra_len = struct.unpack("<H", data[j + 30 : j + 32])[0]
        name = data[j + 46 : j + 46 + fname_len].decode("utf-8", errors="replace")
        if name == "word/document.xml":
            return j + 16
        j = data.find(b"PK\x01\x02", j + 4)
    raise AssertionError("中央目录中 word/document.xml 条目未找到")


def make_docx_bad_crc() -> bytes:
    """有效 ZIP 容器 + 中央目录中 word/document.xml 的 CRC 被篡改（读流时 CRC 校验失败）。"""
    data = bytearray(make_docx(["正常正文内容"]))
    crc_off = _docx_central_crc_offset(bytes(data))
    data[crc_off : crc_off + 4] = b"\xff\xff\xff\xff"
    result = bytes(data)
    assert zipfile.is_zipfile(io.BytesIO(result))
    return result


def _pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("ascii") + b")"


def _build_pdf(objects: list[bytes]) -> bytes:
    """按对象序号生成带正确 xref 偏移的最小 PDF。"""
    header = b"%PDF-1.4\n"
    parts = []
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offset = len(header) + sum(len(part) for part in parts)
        offsets.append(offset)
        parts.append(f"{number} 0 obj\n".encode() + body + b"endobj\n")
    body = b"".join(parts)
    xref_offset = len(header) + len(body)
    xref = f"xref\n0 {len(objects) + 1}\n".encode()
    xref += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        xref += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return header + body + xref + trailer


def make_text_pdf(text: str) -> bytes:
    content = b"BT /F1 12 Tf 72 720 Td " + _pdf_string(text) + b" Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return _build_pdf(objects)


def make_image_only_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    return _build_pdf(objects)


def make_corrupt_pdf() -> bytes:
    return b"%PDF-1.4\nthis is not a real pdf\n"


def make_corrupt_docx() -> bytes:
    return b"this is definitely not a zip archive"


def make_docx_custom(entries: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """构造带自定义 ZIP 条目的 docx，用于资源边界测试（条目数/展开大小/压缩比/路径穿越）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_docx_zip_bomb() -> bytes:
    """构造压缩比异常（高可压缩、展开远大于压缩）的 docx 容器。"""
    huge = b"\x00" * (8 * 1024 * 1024)
    return make_docx_custom(
        {"word/document.xml": huge, "[Content_Types].xml": b"x", "_rels/.rels": b"y"}
    )


def make_docx_many_entries(count: int = 50) -> bytes:
    """构造条目数量可控的 docx 容器（含大量无关条目）。"""
    entries = {
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{_DOCX_NS}"><w:body>'
            "<w:p><w:r><w:t>body</w:t></w:r></w:p>"
            "</w:body></w:document>"
        ).encode("utf-8")
    }
    for i in range(count):
        entries[f"extra/{i}.xml"] = b"<x/>"
    return make_docx_custom(entries)


def make_docx_path_traversal() -> bytes:
    """构造含路径穿越条目名的 docx 容器。"""
    return make_docx_custom(
        {
            "word/document.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{_DOCX_NS}"><w:body/></w:document>'
            ).encode("utf-8"),
            "../evil.xml": b"<x/>",
        }
    )
