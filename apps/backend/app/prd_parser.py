"""PRD 文件解析器：纯函数，无网络、无数据库、不做 OCR。

支持范围：.md / .txt（严格文本解码）、.docx（OpenXML 正文抽取）、
可提取文字的 .pdf。扫描件或图片型 PDF 明确提示无法提取文字，
不产生伪预览。文本型文件只接受 UTF-8、UTF-8 BOM、GB18030 中可被
严格解码且文本特征成立的内容；二进制、NUL、异常控制字符或过高
替换字符比例一律解析失败。解析结果只返回文本与警告，不接触文件系统。

资源边界：字符上限 ``MAX_TEXT_CHARS`` 在解析过程中生效，而不是
完整展开/解析后的结果校验：
- DOCX：打开 ZIP 后在读取正文前校验条目总数、单条目展开大小、
  总展开大小、压缩比、条目名路径穿越与加密/损坏容器；只读取固定
  正文条目 ``word/document.xml``；用 ``iterparse`` 逐段累计字符，
  一旦超过上限立即停止并抛 ``PrdParseError``，不先构建完整字符串。
- PDF：逐页提取文字并累计字符数，超过上限立即停止后续页面；对页数
  设置上限；加密、损坏、图片型 PDF 仍按既有规则结构化失败，不做 OCR。

结构化入口 ``parse_prd_structured_bytes`` 是 additive 能力：它不改变既有
``parse_prd_bytes`` 的签名、版本或行为，只按正式 PRD Structured Evidence
Contract V1 生成确定性的 heading / paragraph / table 证据块及 identity/hash。
"""

import hashlib
import io
import json
import re
import zipfile
import zlib
import xml.etree.ElementTree as ET

from pypdf import PdfReader

PARSER_VERSION = "prd-parser-1.0"
STRUCTURED_SCHEMA_VERSION = "prd_structured_evidence_v1"
STRUCTURED_PARSER_VERSION = "prd-structured-parser-1.0"
MAX_TEXT_CHARS = 200_000
MAX_STRUCTURED_BLOCKS = 10_000
PREVIEW_MAX_CHARS = 2000
IMAGE_ONLY_PDF_MIN_NON_WS = 3

# DOCX / ZIP 资源边界（读取正文前校验，防御解压炸弹与超大展开）
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_ENTRY_UNCOMPRESSED = 64 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 1000

# PDF 资源边界
MAX_PDF_PAGES = 2000

SUPPORTED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf"}
_STRUCTURED_FORMATS = {
    ".md": "md",
    ".txt": "txt",
    ".docx": "docx",
    ".pdf": "pdf",
}

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_BODY = f"{{{_W_NS}}}body"
_W_P = f"{{{_W_NS}}}p"
_W_T = f"{{{_W_NS}}}t"
_W_TBL = f"{{{_W_NS}}}tbl"
_W_TR = f"{{{_W_NS}}}tr"
_W_TC = f"{{{_W_NS}}}tc"
_W_PPR = f"{{{_W_NS}}}pPr"
_W_PSTYLE = f"{{{_W_NS}}}pStyle"
_W_SECTPR = f"{{{_W_NS}}}sectPr"
_W_VAL = f"{{{_W_NS}}}val"
_DOCX_CONTAINER_ERRORS = (
    zipfile.BadZipFile,
    RuntimeError,
    OSError,
    EOFError,
    zlib.error,
    NotImplementedError,
)
_DELIMITER_CELL_RE = re.compile(r":?-{3,}:?\Z")
_HEADING_STYLE_LEVELS = {f"Heading{i}": i for i in range(1, 7)}


class PrdParseError(Exception):
    """解析失败：无法提取文字、文件损坏、二进制内容或超出字符上限。"""


def _looks_like_text(text: str) -> bool:
    """启发式判定解码结果是否可合理视为文本，拒绝异常控制字符与替换字符。"""
    if not text:
        return False
    length = len(text)
    controls = sum(
        1
        for ch in text
        if (ord(ch) < 0x20 and ch not in "\t\n\r") or ord(ch) == 0x7F
    )
    if controls / length > 0.05:
        return False
    replacements = text.count("\ufffd")
    if replacements / length > 0.01:
        return False
    return True


def _decode_text_bytes(data: bytes) -> str:
    """严格解码文本：UTF-8（含 BOM）→ GB18030（兼容中文）。

    任意失败都不使用 errors='replace' 强行解码；无法被任一编码严格
    解码、包含 NUL、或文本特征不成立时抛 PrdParseError，避免把二进制
    内容伪装成有效 PRD 预览。
    """
    if b"\x00" in data:
        raise PrdParseError("文件内容不是有效文本（包含 NUL 字节）")
    decoded = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            decoded = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise PrdParseError("无法识别文件编码，内容不是有效文本")
    if not _looks_like_text(decoded):
        raise PrdParseError("文件内容包含过多非文本字节，无法作为 PRD 导入")
    return decoded


def _count_non_whitespace(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _validate_docx_archive(infos) -> None:
    """读取正文前校验 ZIP 元数据：条目数、单条目与总展开大小、压缩比、路径穿越与加密。

    全部基于中央目录元数据判断，不实际解压任何条目。
    """
    if len(infos) > MAX_ZIP_ENTRIES:
        raise PrdParseError(f"docx 压缩包条目数量超过上限 {MAX_ZIP_ENTRIES}")
    total_uncompressed = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/") or ":" in name:
            raise PrdParseError("docx 压缩包条目名包含非法路径")
        if info.flag_bits & 0x1:
            raise PrdParseError("docx 压缩包包含加密条目，无法读取")
        if info.file_size > MAX_ZIP_ENTRY_UNCOMPRESSED:
            raise PrdParseError(f"docx 条目展开大小超过上限 {MAX_ZIP_ENTRY_UNCOMPRESSED}")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_ZIP_TOTAL_UNCOMPRESSED:
            raise PrdParseError(f"docx 压缩包总展开大小超过上限 {MAX_ZIP_TOTAL_UNCOMPRESSED}")
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_ZIP_COMPRESSION_RATIO:
                raise PrdParseError("docx 压缩包条目压缩比异常（疑似解压炸弹）")


def _extract_docx_paragraphs(stream) -> str:
    """用 iterparse 流式抽取 DOCX 正文：逐段累计字符，超过上限立即停止。

    不构建完整 XML 树，也不先拼接超大字符串；达到 ``MAX_TEXT_CHARS`` 即抛错。
    段落间以换行分隔；文本在 w:t 结束事件时立即取出，不依赖父节点子元素。
    """
    parts: list[str] = []
    char_count = 0
    pending_sep = False  # 前一个段落已结束，遇到下一段文字前需插入换行
    try:
        context = ET.iterparse(stream, events=("end",))
        for _event, elem in context:
            if elem.tag == _W_T:
                piece = elem.text or ""
                if pending_sep:
                    parts.append("\n")
                    char_count += 1
                    pending_sep = False
                char_count += len(piece)
                if char_count > MAX_TEXT_CHARS:
                    raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")
                parts.append(piece)
            elif elem.tag == _W_P:
                pending_sep = True
            elem.clear()
    except PrdParseError:
        raise
    except ET.ParseError as exc:
        raise PrdParseError("docx 正文 XML 无法解析") from exc
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：正文压缩流无法读取") from exc
    return "".join(parts)


def _extract_docx_from_archive(archive) -> str:
    """从已打开的 ZIP 容器读取固定正文条目，并收口容器、解压和流异常。"""
    try:
        infos = archive.infolist()
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：压缩容器无法读取") from exc
    _validate_docx_archive(infos)
    try:
        archive.getinfo("word/document.xml")
    except KeyError:
        raise PrdParseError("docx 文件缺少正文内容")
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：正文条目信息无法读取") from exc
    try:
        with archive.open("word/document.xml") as body:
            return _extract_docx_paragraphs(body)
    except PrdParseError:
        raise
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：正文压缩流无法读取") from exc


def _extract_docx_text(data: bytes) -> str:
    stream = io.BytesIO(data)
    try:
        if not zipfile.is_zipfile(stream):
            raise PrdParseError("docx 文件损坏：不是有效的 Office 文档")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            return _extract_docx_from_archive(archive)
    except PrdParseError:
        raise
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：压缩容器无法读取") from exc


def _extract_pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise PrdParseError(f"PDF 页数超过上限 {MAX_PDF_PAGES} 页")
    except PrdParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何解析器异常都收敛为可报告的解析失败
        raise PrdParseError("PDF 无法解析：文件损坏或已加密") from exc
    parts: list[str] = []
    char_count = 0
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            raise PrdParseError("PDF 无法解析：文件损坏或已加密") from exc
        if parts:
            char_count += 1  # 页间换行
        char_count += len(page_text)
        if char_count > MAX_TEXT_CHARS:
            raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")
        parts.append(page_text)
    text = "\n".join(parts)
    if _count_non_whitespace(text) < IMAGE_ONLY_PDF_MIN_NON_WS:
        raise PrdParseError("无法提取文字：该 PDF 为扫描件或图片型，不支持 OCR")
    return text


def parse_prd_bytes(data: bytes, extension: str) -> tuple[str, str]:
    """解析 PRD 原始字节，返回 (文本, parser_version)。失败抛 PrdParseError。

    `extension` 为小写扩展名（含点，例如 ".pdf"）。只处理内存中的字节，
    不写入任何文件。
    """
    ext = extension.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise PrdParseError(f"不支持的文件类型：{extension}")
    if len(data) == 0:
        raise PrdParseError("文件内容为空")
    if ext in (".md", ".txt"):
        text = _decode_text_bytes(data)
    elif ext == ".docx":
        text = _extract_docx_text(data)
    else:
        text = _extract_pdf_text(data)
    if not text.strip():
        raise PrdParseError("文件内容为空或仅包含空白字符")
    if len(text) > MAX_TEXT_CHARS:
        raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")
    return text, PARSER_VERSION


# ---------------------------------------------------------------------------
# PRD Structured Evidence Contract V1：additive structured parser
# ---------------------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_contract_blank_line(line: str) -> bool:
    return len(line.replace(" ", "").replace("\t", "")) == 0


def _normalize_block_text(text: str) -> str:
    lines = _normalize_newlines(text).split("\n")
    start = 0
    end = len(lines)
    while start < end and _is_contract_blank_line(lines[start]):
        start += 1
    while end > start and _is_contract_blank_line(lines[end - 1]):
        end -= 1
    return "\n".join(lines[start:end])


def _normalize_table_cell(text: str) -> str:
    return _normalize_newlines(text).strip(" \t\n")


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _structured_source_format(extension: str) -> str:
    ext = extension.lower()
    try:
        return _STRUCTURED_FORMATS[ext]
    except KeyError as exc:
        raise PrdParseError(f"不支持的文件类型：{extension}") from exc


def _append_structured_raw(blocks: list[dict], block: dict) -> None:
    if len(blocks) >= MAX_STRUCTURED_BLOCKS:
        raise PrdParseError(f"结构化证据块数量超过上限 {MAX_STRUCTURED_BLOCKS}")
    blocks.append(block)


def _raw_paragraph(text: str, *, page_no=None) -> dict:
    return {
        "kind": "paragraph",
        "text": text,
        "heading_level": None,
        "table_rows": None,
        "page_no": page_no,
    }


def _raw_heading(text: str, level: int) -> dict:
    return {
        "kind": "heading",
        "text": text,
        "heading_level": level,
        "table_rows": None,
        "page_no": None,
    }


def _raw_table(rows: list[list[str]]) -> dict:
    return {
        "kind": "table",
        "text": None,
        "heading_level": None,
        "table_rows": rows,
        "page_no": None,
    }


def _finalize_structured(data: bytes, source_format: str, raw_blocks: list[dict]) -> dict:
    source_hash = _sha256_hex(data)
    fingerprint_payload = {
        "parser_version": STRUCTURED_PARSER_VERSION,
        "schema_version": STRUCTURED_SCHEMA_VERSION,
        "source_format": source_format,
        "source_hash": source_hash,
    }
    document_fingerprint = _sha256_hex(_canonical_json_bytes(fingerprint_payload))
    blocks: list[dict] = []
    for ordinal, raw in enumerate(raw_blocks, start=1):
        kind = raw["kind"]
        if kind == "heading":
            content_payload = {
                "heading_level": raw["heading_level"],
                "kind": "heading",
                "text": raw["text"],
            }
        elif kind == "paragraph":
            content_payload = {"kind": "paragraph", "text": raw["text"]}
        elif kind == "table":
            content_payload = {"kind": "table", "table_rows": raw["table_rows"]}
        else:  # pragma: no cover - internal invariant
            raise PrdParseError("结构化证据内部类型不一致")
        content_hash = _sha256_hex(_canonical_json_bytes(content_payload))
        blocks.append(
            {
                "ordinal": ordinal,
                "kind": kind,
                "evidence_id": f"prd:block:{document_fingerprint}:{ordinal}",
                "content_hash": content_hash,
                "text": raw["text"],
                "heading_level": raw["heading_level"],
                "table_rows": raw["table_rows"],
                "page_no": raw["page_no"],
            }
        )
    return {
        "schema_version": STRUCTURED_SCHEMA_VERSION,
        "parser_version": STRUCTURED_PARSER_VERSION,
        "source_format": source_format,
        "source_hash": source_hash,
        "document_fingerprint": document_fingerprint,
        "blocks": blocks,
    }


def _leading_ascii_spaces(line: str) -> int:
    count = 0
    for ch in line:
        if ch != " ":
            break
        count += 1
    return count


def _md_fence_opener(line: str):
    indent = _leading_ascii_spaces(line)
    if indent > 3 or indent >= len(line):
        return None
    rest = line[indent:]
    fence_char = rest[0]
    if fence_char not in ("`", "~"):
        return None
    run = 0
    while run < len(rest) and rest[run] == fence_char:
        run += 1
    if run < 3:
        return None
    return fence_char, run


def _md_is_fence_closer(line: str, fence_char: str, opening_run: int) -> bool:
    indent = _leading_ascii_spaces(line)
    if indent > 3:
        return False
    rest = line[indent:]
    run = 0
    while run < len(rest) and rest[run] == fence_char:
        run += 1
    if run < opening_run:
        return False
    return all(ch in (" ", "\t") for ch in rest[run:])


def _md_atx_heading(line: str):
    indent = _leading_ascii_spaces(line)
    if indent > 3:
        return None
    rest = line[indent:]
    if not rest.startswith("#"):
        return None
    level = 0
    while level < len(rest) and rest[level] == "#":
        level += 1
    if level == 0 or level > 6:
        return None
    if level < len(rest) and rest[level] not in (" ", "\t"):
        return None
    pos = level
    while pos < len(rest) and rest[pos] in (" ", "\t"):
        pos += 1
    text = rest[pos:].rstrip(" \t")
    return level, text


def _md_setext_heading(lines: list[str], index: int):
    if index + 1 >= len(lines) or _is_contract_blank_line(lines[index]):
        return None
    underline = lines[index + 1]
    indent = _leading_ascii_spaces(underline)
    if indent > 3:
        return None
    rest = underline[indent:]
    stripped = rest.rstrip(" \t")
    if not stripped:
        return None
    marker = stripped[0]
    if marker not in ("=", "-") or any(ch != marker for ch in stripped):
        return None
    text = lines[index].strip(" \t")
    if not text:
        return None
    return (1 if marker == "=" else 2), text


def _pipe_is_separator(line: str, index: int) -> bool:
    slash_count = 0
    cursor = index - 1
    while cursor >= 0 and line[cursor] == "\\":
        slash_count += 1
        cursor -= 1
    return slash_count % 2 == 0


def _md_separator_indexes(line: str) -> list[int]:
    return [i for i, ch in enumerate(line) if ch == "|" and _pipe_is_separator(line, i)]


def _restore_literal_pipes(segment: str) -> str:
    output: list[str] = []
    for ch in segment:
        if ch == "|":
            slash_count = 0
            cursor = len(output) - 1
            while cursor >= 0 and output[cursor] == "\\":
                slash_count += 1
                cursor -= 1
            if slash_count % 2 == 1:
                output.pop()  # 最后一个反斜杠是 escape marker
            output.append("|")
        else:
            output.append(ch)
    return "".join(output)


def _split_md_pipe_row(line: str) -> list[str]:
    separators = _md_separator_indexes(line)
    if not separators:
        return [_normalize_table_cell(_restore_literal_pipes(line))]
    cells: list[str] = []
    start = 0
    for index in separators:
        cells.append(line[start:index])
        start = index + 1
    cells.append(line[start:])
    if separators[0] == 0:
        cells.pop(0)
    if separators[-1] == len(line) - 1:
        cells.pop()
    return [_normalize_table_cell(_restore_literal_pipes(cell)) for cell in cells]


def _md_table_match(lines: list[str], index: int):
    if index + 1 >= len(lines):
        return None
    header_line = lines[index]
    delimiter_line = lines[index + 1]
    header_has_separator = bool(_md_separator_indexes(header_line))
    delimiter_has_separator = bool(_md_separator_indexes(delimiter_line))
    if not (header_has_separator or delimiter_has_separator):
        return None
    header = _split_md_pipe_row(header_line)
    delimiter = _split_md_pipe_row(delimiter_line)
    if not header or len(header) != len(delimiter):
        return None
    if not all(_DELIMITER_CELL_RE.fullmatch(cell) for cell in delimiter):
        return None
    rows = [header]
    cursor = index + 2
    while cursor < len(lines):
        line = lines[cursor]
        if _is_contract_blank_line(line) or not _md_separator_indexes(line):
            break
        body = _split_md_pipe_row(line)
        if len(body) != len(header):
            break
        rows.append(body)
        cursor += 1
    return rows, cursor


def _md_high_priority_at(lines: list[str], index: int) -> bool:
    if index >= len(lines):
        return False
    line = lines[index]
    if _md_fence_opener(line) is not None:
        return True
    if _md_atx_heading(line) is not None:
        return True
    if _md_setext_heading(lines, index) is not None:
        return True
    if _md_table_match(lines, index) is not None:
        return True
    return False


def _parse_markdown_structured(text: str) -> list[dict]:
    lines = _normalize_newlines(text).split("\n")
    blocks: list[dict] = []
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor]
        if _is_contract_blank_line(line):
            cursor += 1
            continue

        opener = _md_fence_opener(line)
        if opener is not None:
            fence_char, opening_run = opener
            end = cursor + 1
            while end < len(lines) and not _md_is_fence_closer(
                lines[end], fence_char, opening_run
            ):
                end += 1
            if end < len(lines):
                end += 1
            paragraph = _normalize_block_text("\n".join(lines[cursor:end]))
            if paragraph:
                _append_structured_raw(blocks, _raw_paragraph(paragraph))
            cursor = end
            continue

        atx = _md_atx_heading(line)
        if atx is not None:
            level, heading_text = atx
            if heading_text:
                _append_structured_raw(blocks, _raw_heading(heading_text, level))
            cursor += 1
            continue

        setext = _md_setext_heading(lines, cursor)
        if setext is not None:
            level, heading_text = setext
            _append_structured_raw(blocks, _raw_heading(heading_text, level))
            cursor += 2
            continue

        table = _md_table_match(lines, cursor)
        if table is not None:
            rows, next_cursor = table
            if any(cell != "" for row in rows for cell in row):
                _append_structured_raw(blocks, _raw_table(rows))
            cursor = next_cursor
            continue

        end = cursor + 1
        while end < len(lines):
            if _is_contract_blank_line(lines[end]) or _md_high_priority_at(lines, end):
                break
            end += 1
        paragraph = _normalize_block_text("\n".join(lines[cursor:end]))
        if paragraph:
            _append_structured_raw(blocks, _raw_paragraph(paragraph))
        cursor = end
    return blocks


def _parse_txt_structured(text: str) -> list[dict]:
    lines = _normalize_newlines(text).split("\n")
    blocks: list[dict] = []
    cursor = 0
    while cursor < len(lines):
        while cursor < len(lines) and _is_contract_blank_line(lines[cursor]):
            cursor += 1
        if cursor >= len(lines):
            break
        end = cursor + 1
        while end < len(lines) and not _is_contract_blank_line(lines[end]):
            end += 1
        paragraph = _normalize_block_text("\n".join(lines[cursor:end]))
        if paragraph:
            _append_structured_raw(blocks, _raw_paragraph(paragraph))
        cursor = end
    return blocks


def _docx_paragraph_text(element) -> str:
    return "".join((node.text or "") for node in element.iter(_W_T))


def _docx_paragraph_block(element):
    text = _normalize_block_text(_docx_paragraph_text(element))
    if not text:
        return None
    style = None
    ppr = element.find(_W_PPR)
    if ppr is not None:
        style_element = ppr.find(_W_PSTYLE)
        if style_element is not None:
            style = style_element.attrib.get(_W_VAL)
    level = _HEADING_STYLE_LEVELS.get(style)
    return _raw_heading(text, level) if level is not None else _raw_paragraph(text)


def _docx_table_block(element):
    rows: list[list[str]] = []
    for row_element in element:
        if row_element.tag != _W_TR:
            continue
        row: list[str] = []
        for cell_element in row_element:
            if cell_element.tag != _W_TC:
                continue
            paragraphs: list[str] = []
            for paragraph in cell_element.iter(_W_P):
                paragraphs.append(_docx_paragraph_text(paragraph))
            cell_text = _normalize_table_cell("\n".join(paragraphs))
            row.append(cell_text)
        rows.append(row)
    if not any(cell != "" for row in rows for cell in row):
        return None
    return _raw_table(rows)


def _structured_content_char_count(blocks: list[dict]) -> int:
    total = 0
    for block in blocks:
        if block["kind"] in ("heading", "paragraph"):
            total += len(block["text"])
        elif block["kind"] == "table":
            total += sum(len(cell) for row in block["table_rows"] for cell in row)
    return total


def _process_docx_body_child(element, blocks: list[dict]) -> None:
    if element.tag == _W_P:
        block = _docx_paragraph_block(element)
        if block is not None:
            _append_structured_raw(blocks, block)
        return
    if element.tag == _W_TBL:
        block = _docx_table_block(element)
        if block is not None:
            _append_structured_raw(blocks, block)
        return
    if element.tag == _W_SECTPR:
        return
    text = _normalize_block_text(_docx_paragraph_text(element))
    if text:
        raise PrdParseError("docx 正文包含 V1 不支持且带业务文本的 body 元素")


def _docx_outer_physical_cell(stack: list):
    return next((element for element in stack if element.tag == _W_TC), None)


def _docx_direct_body_child(stack: list):
    for index, element in enumerate(stack[1:], start=1):
        if stack[index - 1].tag == _W_BODY:
            return element
    return None


class _DocxNewlineNormalizer:
    """Incrementally apply CRLF/CR -> LF with one bit of carry state."""

    __slots__ = ("pending_cr",)

    def __init__(self) -> None:
        self.pending_cr = False

    def feed(self, text: str):
        for char in text:
            if self.pending_cr:
                self.pending_cr = False
                yield "\n"
                if char == "\n":
                    continue
            if char == "\r":
                self.pending_cr = True
            else:
                yield char

    def finish(self):
        if not self.pending_cr:
            return None
        self.pending_cr = False
        return "\n"

    def reset(self) -> None:
        self.pending_cr = False


def _account_docx_cell_chars(
    chars,
    has_retained_content: bool,
    pending_trim_count: int,
) -> tuple[int, bool, int]:
    committed = 0
    for char in chars:
        if char in " \t\n":
            if has_retained_content:
                pending_trim_count += 1
            continue
        if has_retained_content:
            committed += pending_trim_count
        pending_trim_count = 0
        committed += 1
        has_retained_content = True
    return committed, has_retained_content, pending_trim_count


def _account_docx_cell_text(
    text: str,
    newlines: _DocxNewlineNormalizer,
    has_retained_content: bool,
    pending_trim_count: int,
) -> tuple[int, bool, int]:
    return _account_docx_cell_chars(
        newlines.feed(text),
        has_retained_content,
        pending_trim_count,
    )


def _account_docx_block_chars(
    chars,
    has_retained_line: bool,
    pending_blank_count: int,
    current_line_pending_count: int,
    current_line_has_content: bool,
) -> tuple[int, bool, int, int, bool]:
    committed = 0
    for char in chars:
        if char == "\n":
            if current_line_has_content:
                pending_blank_count = 1
            elif has_retained_line:
                pending_blank_count += current_line_pending_count + 1
            current_line_pending_count = 0
            current_line_has_content = False
            continue
        if current_line_has_content:
            committed += 1
            continue
        if char in " \t":
            current_line_pending_count += 1
            continue
        if has_retained_line:
            committed += pending_blank_count
        committed += current_line_pending_count + 1
        has_retained_line = True
        pending_blank_count = 0
        current_line_pending_count = 0
        current_line_has_content = True
    return (
        committed,
        has_retained_line,
        pending_blank_count,
        current_line_pending_count,
        current_line_has_content,
    )


def _account_docx_block_text(
    text: str,
    newlines: _DocxNewlineNormalizer,
    has_retained_line: bool,
    pending_blank_count: int,
    current_line_pending_count: int,
    current_line_has_content: bool,
) -> tuple[int, bool, int, int, bool]:
    return _account_docx_block_chars(
        newlines.feed(text),
        has_retained_line,
        pending_blank_count,
        current_line_pending_count,
        current_line_has_content,
    )


def _count_docx_resource_text(text: str, newlines: _DocxNewlineNormalizer) -> int:
    return sum(1 for _char in newlines.feed(text))


class _DocxStreamingAccounting:
    """Bounded DOCX canonical state plus one cumulative parsing budget."""

    def __init__(self) -> None:
        self.supported_canonical_count = 0
        self.unsupported_resource_count = 0

        self.active_cell = None
        self.cell_newlines = _DocxNewlineNormalizer()
        self.cell_has_retained_content = False
        self.pending_cell_trim_count = 0

        self.active_paragraph = None
        self.paragraph_newlines = _DocxNewlineNormalizer()
        self.paragraph_has_retained_line = False
        self.paragraph_pending_blank_count = 0
        self.paragraph_line_pending_count = 0
        self.paragraph_line_has_content = False

        self.active_unsupported_child = None
        self.unsupported_newlines = _DocxNewlineNormalizer()

    def _check_limit(self) -> None:
        if (
            self.supported_canonical_count + self.unsupported_resource_count
            > MAX_TEXT_CHARS
        ):
            raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")

    def _commit_supported(self, added: int) -> None:
        self.supported_canonical_count += added
        self._check_limit()

    def _ensure_cell(self, cell) -> None:
        if cell is self.active_cell:
            return
        self.active_cell = cell
        self.cell_newlines.reset()
        self.cell_has_retained_content = False
        self.pending_cell_trim_count = 0

    def consume_cell_text(self, cell, text: str) -> None:
        self._ensure_cell(cell)
        (
            added,
            self.cell_has_retained_content,
            self.pending_cell_trim_count,
        ) = _account_docx_cell_text(
            text,
            self.cell_newlines,
            self.cell_has_retained_content,
            self.pending_cell_trim_count,
        )
        self._commit_supported(added)

    def end_cell_paragraph(self, cell) -> None:
        self.consume_cell_text(cell, "\n")

    def finish_cell(self, cell) -> None:
        if cell is not self.active_cell:
            return
        trailing = self.cell_newlines.finish()
        if trailing is not None:
            (
                added,
                self.cell_has_retained_content,
                self.pending_cell_trim_count,
            ) = _account_docx_cell_chars(
                (trailing,),
                self.cell_has_retained_content,
                self.pending_cell_trim_count,
            )
            self._commit_supported(added)
        self.active_cell = None
        self.cell_has_retained_content = False
        self.pending_cell_trim_count = 0

    def _ensure_paragraph(self, paragraph) -> None:
        if paragraph is self.active_paragraph:
            return
        self.active_paragraph = paragraph
        self.paragraph_newlines.reset()
        self.paragraph_has_retained_line = False
        self.paragraph_pending_blank_count = 0
        self.paragraph_line_pending_count = 0
        self.paragraph_line_has_content = False

    def consume_paragraph_text(self, paragraph, text: str) -> None:
        self._ensure_paragraph(paragraph)
        (
            added,
            self.paragraph_has_retained_line,
            self.paragraph_pending_blank_count,
            self.paragraph_line_pending_count,
            self.paragraph_line_has_content,
        ) = _account_docx_block_text(
            text,
            self.paragraph_newlines,
            self.paragraph_has_retained_line,
            self.paragraph_pending_blank_count,
            self.paragraph_line_pending_count,
            self.paragraph_line_has_content,
        )
        self._commit_supported(added)

    def finish_paragraph(self, paragraph) -> None:
        if paragraph is not self.active_paragraph:
            return
        trailing = self.paragraph_newlines.finish()
        if trailing is not None:
            (
                added,
                self.paragraph_has_retained_line,
                self.paragraph_pending_blank_count,
                self.paragraph_line_pending_count,
                self.paragraph_line_has_content,
            ) = _account_docx_block_chars(
                (trailing,),
                self.paragraph_has_retained_line,
                self.paragraph_pending_blank_count,
                self.paragraph_line_pending_count,
                self.paragraph_line_has_content,
            )
            self._commit_supported(added)
        self.active_paragraph = None
        self.paragraph_has_retained_line = False
        self.paragraph_pending_blank_count = 0
        self.paragraph_line_pending_count = 0
        self.paragraph_line_has_content = False

    def consume_unsupported_text(self, body_child, text: str) -> None:
        if body_child is not self.active_unsupported_child:
            self.active_unsupported_child = body_child
            self.unsupported_newlines.reset()
        self.unsupported_resource_count += _count_docx_resource_text(
            text,
            self.unsupported_newlines,
        )
        self._check_limit()

    def finish_unsupported_child(self, body_child) -> None:
        if body_child is not self.active_unsupported_child:
            return
        if self.unsupported_newlines.finish() is not None:
            self.unsupported_resource_count += 1
        self.active_unsupported_child = None
        self._check_limit()


def _extract_docx_structured_from_archive(archive) -> list[dict]:
    try:
        infos = archive.infolist()
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：压缩容器无法读取") from exc
    _validate_docx_archive(infos)
    try:
        archive.getinfo("word/document.xml")
    except KeyError:
        raise PrdParseError("docx 文件缺少正文内容")
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：正文条目信息无法读取") from exc

    blocks: list[dict] = []
    stack: list = []
    accounting = _DocxStreamingAccounting()
    non_ws = 0
    try:
        with archive.open("word/document.xml") as body:
            context = ET.iterparse(body, events=("start", "end"))
            for event, elem in context:
                if event == "start":
                    stack.append(elem)
                    continue

                body_child = _docx_direct_body_child(stack)
                body_child_tag = body_child.tag if body_child is not None else None
                if elem.tag == _W_T:
                    piece = elem.text or ""
                    if body_child_tag == _W_P:
                        accounting.consume_paragraph_text(body_child, piece)
                    elif body_child_tag == _W_TBL:
                        cell = _docx_outer_physical_cell(stack)
                        if cell is not None:
                            accounting.consume_cell_text(cell, piece)
                    elif body_child is not None and body_child_tag != _W_SECTPR:
                        accounting.consume_unsupported_text(body_child, piece)
                    non_ws += _count_non_whitespace(piece)
                elif body_child_tag == _W_TBL:
                    cell = _docx_outer_physical_cell(stack)
                    if elem.tag == _W_P and cell is not None:
                        accounting.end_cell_paragraph(cell)
                    elif elem.tag == _W_TC and elem is cell:
                        accounting.finish_cell(cell)
                elif body_child_tag == _W_P and elem is body_child:
                    accounting.finish_paragraph(body_child)
                elif (
                    body_child is not None
                    and body_child_tag != _W_SECTPR
                    and elem is body_child
                ):
                    accounting.finish_unsupported_child(body_child)

                parent = stack[-2] if len(stack) >= 2 else None
                if parent is not None and parent.tag == _W_BODY:
                    _process_docx_body_child(elem, blocks)
                    elem.clear()
                stack.pop()
    except PrdParseError:
        raise
    except ET.ParseError as exc:
        raise PrdParseError("docx 正文 XML 无法解析") from exc
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：正文压缩流无法读取") from exc
    if non_ws == 0:
        raise PrdParseError("文件内容为空或仅包含空白字符")
    if _structured_content_char_count(blocks) > MAX_TEXT_CHARS:
        raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")
    return blocks


def _parse_docx_structured(data: bytes) -> list[dict]:
    stream = io.BytesIO(data)
    try:
        if not zipfile.is_zipfile(stream):
            raise PrdParseError("docx 文件损坏：不是有效的 Office 文档")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            return _extract_docx_structured_from_archive(archive)
    except PrdParseError:
        raise
    except _DOCX_CONTAINER_ERRORS as exc:
        raise PrdParseError("docx 文件损坏：压缩容器无法读取") from exc


def _parse_pdf_structured(data: bytes) -> list[dict]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise PrdParseError(f"PDF 页数超过上限 {MAX_PDF_PAGES} 页")
    except PrdParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PrdParseError("PDF 无法解析：文件损坏或已加密") from exc

    blocks: list[dict] = []
    char_count = 0
    non_ws = 0
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            raise PrdParseError("PDF 无法解析：文件损坏或已加密") from exc
        if page_index > 1:
            char_count += 1
        char_count += len(page_text)
        if char_count > MAX_TEXT_CHARS:
            raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")
        non_ws += _count_non_whitespace(page_text)
        paragraph = _normalize_block_text(page_text)
        if paragraph and paragraph.strip():
            _append_structured_raw(blocks, _raw_paragraph(paragraph, page_no=page_index))
    if non_ws < IMAGE_ONLY_PDF_MIN_NON_WS:
        raise PrdParseError("无法提取文字：该 PDF 为扫描件或图片型，不支持 OCR")
    return blocks


def parse_prd_structured_bytes(data: bytes, extension: str) -> dict:
    """按 PRD Structured Evidence Contract V1 返回确定性结构化证据结果。

    本入口只处理内存字节，不写文件、不访问网络/数据库，也不改变旧
    ``parse_prd_bytes`` 的 API。失败统一抛 ``PrdParseError``。
    """
    source_format = _structured_source_format(extension)
    if len(data) == 0:
        raise PrdParseError("文件内容为空")

    if source_format in ("md", "txt"):
        text = _decode_text_bytes(data)
        if not text.strip():
            raise PrdParseError("文件内容为空或仅包含空白字符")
        if len(text) > MAX_TEXT_CHARS:
            raise PrdParseError(f"解析文本超过上限 {MAX_TEXT_CHARS} 个字符")
        raw_blocks = (
            _parse_markdown_structured(text)
            if source_format == "md"
            else _parse_txt_structured(text)
        )
    elif source_format == "docx":
        raw_blocks = _parse_docx_structured(data)
    else:
        raw_blocks = _parse_pdf_structured(data)

    return _finalize_structured(data, source_format, raw_blocks)
