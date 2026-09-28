"""Deterministic credential-only Context Redaction Policy / Transform V1."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from fastapi import HTTPException

from app.context_redaction_inputs import resolve_context_redaction_input

SCHEMA_VERSION = "context_redaction_result_v1"
POLICY_SCHEMA_VERSION = "context_redaction_policy_v1"
POLICY_ID = "credential_redaction_v1"
POLICY_ALGORITHM_VERSION = "credential-redaction-algorithm-1.1"
INLINE_PLACEHOLDER = "[REDACTED:CREDENTIAL]"
PRIVATE_KEY_PLACEHOLDER = "[REDACTED:PRIVATE_KEY_MATERIAL]"
MODEL_SEND_STATE = "not_admitted"

SECRET_KEY_EXACT = (
    "password", "passwd", "pwd", "passphrase", "secret", "client_secret", "api_key",
    "apikey", "access_token", "refresh_token", "id_token", "auth_token", "token",
    "authorization", "proxy_authorization", "cookie", "set_cookie", "private_key",
    "private_key_password", "secret_access_key", "smtp_password",
)
SECRET_KEY_SUFFIXES = (
    "_password", "_passwd", "_passphrase", "_secret", "_client_secret", "_api_key",
    "_access_token", "_refresh_token", "_auth_token", "_private_key",
    "_private_key_password", "_secret_access_key",
)
HEADER_NAMES = ("Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie")
AUTH_SCHEMES = ("Bearer", "Basic")
PRIVATE_KEY_BLOCK_LABELS = (
    "PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY",
    "DSA PRIVATE KEY", "PGP PRIVATE KEY BLOCK",
)
RULE_ORDER = (
    "R1 private_key_block", "R2 sensitive_header_value", "R3 secret_key_scalar",
    "R4 url_userinfo", "R5 url_secret_query", "R6 auth_scheme_token",
)

POLICY_DESCRIPTOR: dict[str, object] = {
    "policy_schema_version": POLICY_SCHEMA_VERSION,
    "policy_id": POLICY_ID,
    "policy_algorithm_version": POLICY_ALGORITHM_VERSION,
    "inline_placeholder": INLINE_PLACEHOLDER,
    "private_key_placeholder": PRIVATE_KEY_PLACEHOLDER,
    "key_normalization_id": "ascii-secret-key-normalization-v1",
    "header_grammar_id": "single-line-sensitive-header-v1",
    "text_assignment_grammar_id": "single-line-secret-assignment-v1",
    "url_grammar_id": "explicit-scheme-url-token-v1",
    "url_query_grammar_id": "raw-query-no-percent-decode-v1",
    "auth_token_grammar_id": "bearer-basic-token-boundary-v1",
    "private_key_grammar_id": "exact-line-private-key-block-v1",
    "secret_key_exact": list(SECRET_KEY_EXACT),
    "secret_key_suffixes": list(SECRET_KEY_SUFFIXES),
    "header_names": list(HEADER_NAMES),
    "auth_schemes": list(AUTH_SCHEMES),
    "private_key_block_labels": list(PRIVATE_KEY_BLOCK_LABELS),
    "rule_order": list(RULE_ORDER),
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")
_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>[ \t]*)(?P<key>[A-Za-z0-9_.-]+|'[A-Za-z0-9_.-]+'|\"[A-Za-z0-9_.-]+\")"
    r"(?P<presep>[ \t]*)(?P<sep>[:=])(?P<postsep>[ \t]*)(?P<rest>.*)$"
)
_HEADER_RE = re.compile(
    r"^(?P<prefix>[ \t]*)(?P<name>[A-Za-z-]+)(?P<precolon>[ \t]*):"
    r"(?P<postcolon>[ \t]*)(?P<value>.*)$"
)
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_AUTH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?P<scheme>Bearer|Basic)(?P<ws>[ \t]+)"
    r"(?P<credential>[A-Za-z0-9._~+/=-]{8,})(?![A-Za-z0-9._~+/=-])"
)
_TOKEN_TERMINATORS = set(" \t\r\n\f\v<>\"'(){},;")
_FORBIDDEN_UNQUOTED = set(",;{}[]()")
_FIXED_LABEL_ALT = "|".join(re.escape(value) for value in PRIVATE_KEY_BLOCK_LABELS)
_FIXED_BEGIN_RE = re.compile(rf"^[ \t]*-----BEGIN (?P<label>{_FIXED_LABEL_ALT})-----[ \t]*$")
_FIXED_END_RE = re.compile(rf"^[ \t]*-----END (?P<label>{_FIXED_LABEL_ALT})-----[ \t]*$")
_ANY_MARKER_RE = re.compile(r"^[ \t]*-----(?:BEGIN|END) .+-----[ \t]*$")
_HEADER_CASEFOLDS = {value.casefold() for value in HEADER_NAMES}

_RESULT_KEYS = (
    "schema_version", "redaction_input_hash", "manifest_core_hash", "model_call_id",
    "call_identity_hash", "snapshot_id", "snapshot_hash", "project_id",
    "candidate_set_hash", "target", "target_type", "source_identity", "raw_body_kind",
    "redaction_policy_id", "redaction_policy_hash", "redaction_state",
    "redacted_body_state", "redacted_body", "redacted_body_hash", "redaction_stats",
    "redaction_match_count", "model_send_state", "unsupported_context_sources",
    "redaction_result_hash",
)
_RESULT_HASH_KEYS = tuple(
    key for key in _RESULT_KEYS if key not in {"redacted_body", "redaction_result_hash"}
)
_UPSTREAM_REQUIRED_FIELDS = (
    "schema_version", "redaction_input_hash", "manifest_core_hash", "model_call_id",
    "call_identity_hash", "snapshot_id", "snapshot_hash", "project_id",
    "candidate_set_hash", "target", "target_type", "source_identity", "raw_body_kind",
    "raw_body", "raw_body_state", "resolved_content_redaction_state", "model_send_state",
    "unsupported_context_sources",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


REDACTION_POLICY_HASH = _stable_hash(POLICY_DESCRIPTOR)


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _upstream_inconsistent() -> HTTPException:
    return _error(
        "CONTEXT_REDACTION_UPSTREAM_INCONSISTENT",
        "formal Context Redaction Input 的固定形状、状态或安全 identity 无法闭合。",
    )


def _unsupported_shape() -> HTTPException:
    return _error(
        "CONTEXT_REDACTION_UNSUPPORTED_SHAPE",
        "Context Redaction Input body 形状不属于 credential_redaction_v1 支持范围。",
    )


def _unsafe_ambiguity() -> HTTPException:
    return _error(
        "CONTEXT_REDACTION_UNSAFE_AMBIGUITY",
        "已命中 credential grammar，但无法唯一闭合待脱敏边界。",
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1:]
    return line, ""


def _merge_stats(target: dict[str, int], source: Mapping[str, int]) -> None:
    for rule_id, count in source.items():
        if count > 0:
            target[rule_id] = target.get(rule_id, 0) + count


def _normalize_key(value: str) -> str:
    return _KEY_COLLAPSE_RE.sub("_", value.casefold()).strip("_")


def _is_secret_key(value: str) -> bool:
    normalized = _normalize_key(value)
    return normalized in SECRET_KEY_EXACT or any(
        normalized.endswith(suffix) for suffix in SECRET_KEY_SUFFIXES
    )


def _opaque_markers(text: str) -> tuple[str, str]:
    """Return deterministic token-safe markers guaranteed absent from the raw input."""
    nonce = 0
    while True:
        credential = f"ANXINREDACTIONOPAQUECREDENTIAL{nonce}"
        private_key = f"ANXINREDACTIONOPAQUEPRIVATEKEY{nonce}"
        if credential not in text and private_key not in text:
            return credential, private_key
        nonce += 1


def _apply_r1_private_key_blocks(
    text: str, *, replacement: str = PRIVATE_KEY_PLACEHOLDER
) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text, 0
    output: list[str] = []
    active_label: str | None = None
    material_count = 0
    replacements = 0
    for line in lines:
        content, ending = _split_line_ending(line)
        begin = _FIXED_BEGIN_RE.fullmatch(content)
        end = _FIXED_END_RE.fullmatch(content)
        if active_label is None:
            if end is not None:
                raise _unsafe_ambiguity()
            if begin is not None:
                active_label = begin.group("label")
                material_count = 0
                output.append(line)
                continue
            output.append(line)
            continue
        if begin is not None:
            raise _unsafe_ambiguity()
        if end is not None:
            if end.group("label") != active_label or material_count == 0:
                raise _unsafe_ambiguity()
            active_label = None
            material_count = 0
            output.append(line)
            continue
        if _ANY_MARKER_RE.fullmatch(content) is not None:
            raise _unsafe_ambiguity()
        output.append(replacement + ending)
        material_count += 1
        replacements += 1
    if active_label is not None:
        raise _unsafe_ambiguity()
    return "".join(output), replacements


def _apply_r2_headers(
    text: str, *, replacement: str = INLINE_PLACEHOLDER
) -> tuple[str, int]:
    output: list[str] = []
    count = 0
    for line in text.splitlines(keepends=True):
        content, ending = _split_line_ending(line)
        match = _HEADER_RE.fullmatch(content)
        if match is None or match.group("name").casefold() not in _HEADER_CASEFOLDS:
            output.append(line)
            continue
        value = match.group("value")
        if not value or not value.strip(" \t"):
            output.append(line)
            continue
        output.append(
            match.group("prefix") + match.group("name") + match.group("precolon") + ":"
            + match.group("postcolon") + replacement + ending
        )
        count += 1
    return "".join(output), count


def _quoted_closing_index(value: str, quote: str) -> int | None:
    index = 1
    while index < len(value):
        char = value[index]
        if char == "\\":
            if index + 1 >= len(value):
                return None
            index += 2
            continue
        if char == quote:
            return index
        index += 1
    return None


def _finding(rule: str, start: int, end: int, *, isolated: bool = False) -> dict[str, object]:
    return {"rule_id": rule, "risk_level": "warning" if isolated else "high",
            "action": "isolated" if isolated else "redacted", "line_start": start,
            "line_end": end, "safe_snippet": INLINE_PLACEHOLDER}


def _has_unclosed_container(value: str) -> bool:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {"[": "]", "{": "}", "(": ")"}
    for char in value:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return True
    return bool(stack) or quote is not None


def _apply_r3_assignments(
    text: str, *, replacement: str = INLINE_PLACEHOLDER,
    opaque_values: frozenset[str] = frozenset(),
    diagnostics: list[dict[str, object]] | None = None,
) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    count = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        content, ending = _split_line_ending(line)
        match = _ASSIGNMENT_RE.fullmatch(content)
        if match is None:
            output.append(line); index += 1; continue
        raw_key = match.group("key")
        key = raw_key.strip("\"'")
        rest = match.group("rest")
        # A CSS class selector is not an assignment to a credential field.
        if (not _is_secret_key(key) or
                (key.startswith(".") and match.group("sep") == ":"
                 and re.fullmatch(r"[A-Za-z-]+[ \t]*\{[ \t]*", rest))):
            output.append(line); index += 1; continue
        if rest in opaque_values:
            output.append(line); index += 1; continue
        base = content[:match.start("rest")]
        end = index + 1
        isolated = False
        rendered = None
        if not rest.strip() or rest.startswith(("|", ">", "#")):
            indent = len(match.group("prefix"))
            while end < len(lines):
                nxt, _ = _split_line_ending(lines[end])
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" \t")) <= indent:
                    break
                end += 1
            if not rest.strip() and end == index + 1:
                output.append(line); index += 1; continue
            isolated = True
        elif rest[0] in {"'", '"'}:
            closing = _quoted_closing_index(rest, rest[0])
            if closing is None:
                # No trustworthy scalar boundary: withhold the remainder, never release it.
                end = len(lines)
                isolated = True
            elif re.fullmatch(r"[ \t]*(?:,[ \t]*)?", rest[closing + 1:]) is None:
                isolated = True
                if any(char in rest[closing + 1:] for char in ("+", "\\", "(", "[", "{")):
                    end = len(lines)
            else:
                rendered = base + rest[0] + replacement + rest[0] + rest[closing + 1:] + ending
        elif match.group("sep") == ":" and re.fullmatch(r"(?:string|number|boolean|str|int|bool)[ \t]*;", rest):
            output.append(line); index += 1; continue
        else:
            comment = re.search(r"[ \t]+#", rest)
            value = rest if comment is None else rest[:comment.start()]
            if any(char in _FORBIDDEN_UNQUOTED for char in value) or value.rstrip().endswith("\\"):
                isolated = True
                if (value.rstrip().endswith("\\") or _has_unclosed_container(value)):
                    end = len(lines)
            else:
                # Comments on credential lines can themselves contain credentials.
                rendered = base + replacement + ending
        if isolated:
            output.extend(replacement + _split_line_ending(item)[1] for item in lines[index:end])
        else:
            output.append(rendered)
        if diagnostics is not None:
            diagnostics.append(_finding("R3", index + 1, end, isolated=isolated))
        count += 1
        index = end
    return "".join(output), count


def _url_tokens(text: str) -> list[tuple[int, int, int]]:
    tokens: list[tuple[int, int, int]] = []
    position = 0
    while position < len(text):
        match = _SCHEME_RE.search(text, position)
        if match is None:
            break
        start = match.start()
        if start > 0 and text[start - 1] in (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-"
        ):
            position = start + 1
            continue
        index = match.end()
        while index < len(text) and text[index] not in _TOKEN_TERMINATORS:
            index += 1
        tokens.append((start, index, match.end()))
        position = max(index, match.end())
    return tokens


def _apply_r4_url_userinfo(
    text: str, *, replacement: str = INLINE_PLACEHOLDER,
    diagnostics: list[dict[str, object]] | None = None,
) -> tuple[str, int]:
    spans: list[tuple[int, int]] = []
    for _start, token_end, authority_start in _url_tokens(text):
        authority_end = token_end
        for marker in "/?#":
            found = text.find(marker, authority_start, token_end)
            if found != -1:
                authority_end = min(authority_end, found)
        authority = text[authority_start:authority_end]
        raw_at_count = authority.count("@")
        if raw_at_count > 1:
            spans.append((_start, token_end))
            if diagnostics is not None:
                line = len(text[:_start].splitlines()) + (not text[:_start].endswith("\n"))
                diagnostics.append(_finding("R4", max(1, line), max(1, line), isolated=True))
            continue
        if raw_at_count != 1:
            continue
        relative_at = authority.index("@")
        if relative_at == 0 or relative_at == len(authority) - 1:
            continue
        spans.append((authority_start, authority_start + relative_at))
    if not spans:
        return text, 0
    output = text
    for start, end in reversed(spans):
        output = output[:start] + replacement + output[end:]
    return output, len(spans)


def _apply_r5_url_secret_query(
    text: str, *, replacement: str = INLINE_PLACEHOLDER
) -> tuple[str, int]:
    spans: list[tuple[int, int]] = []
    for start, token_end, _authority_start in _url_tokens(text):
        query_marker = text.find("?", start, token_end)
        if query_marker == -1:
            continue
        fragment = text.find("#", query_marker + 1, token_end)
        query_end = token_end if fragment == -1 else fragment
        cursor = query_marker + 1
        while cursor <= query_end:
            amp = text.find("&", cursor, query_end)
            param_end = query_end if amp == -1 else amp
            eq = text.find("=", cursor, param_end)
            if eq != -1:
                raw_key = text[cursor:eq]
                value_start = eq + 1
                if _is_secret_key(raw_key) and value_start < param_end:
                    spans.append((value_start, param_end))
            if amp == -1:
                break
            cursor = amp + 1
    if not spans:
        return text, 0
    output = text
    for start, end in reversed(spans):
        output = output[:start] + replacement + output[end:]
    return output, len(spans)


def _apply_r6_auth_tokens(
    text: str, *, replacement: str = INLINE_PLACEHOLDER
) -> tuple[str, int]:
    matches = list(_AUTH_RE.finditer(text))
    if not matches:
        return text, 0
    output = text
    for match in reversed(matches):
        start, end = match.span("credential")
        output = output[:start] + replacement + output[end:]
    return output, len(matches)


def redact_with_diagnostics(text: str, *, include_assignments: bool = True) -> dict[str, object]:
    """Pure transform; findings contain fixed placeholders, never raw excerpts."""
    credential_marker, private_key_marker = _opaque_markers(text)
    output = text
    stats: dict[str, int] = {}
    diagnostics: list[dict[str, object]] = []
    rules = [("R1", _apply_r1_private_key_blocks, private_key_marker),
             ("R2", _apply_r2_headers, credential_marker)]
    if include_assignments:
        rules.append(("R3", _apply_r3_assignments, credential_marker))
    rules += [("R4", _apply_r4_url_userinfo, credential_marker),
              ("R5", _apply_r5_url_secret_query, credential_marker),
              ("R6", _apply_r6_auth_tokens, credential_marker)]
    for rule, transform, marker in rules:
        before = output
        kwargs = {"replacement": marker}
        if rule in {"R3", "R4"}:
            kwargs["diagnostics"] = diagnostics
        if rule == "R3":
            kwargs["opaque_values"] = frozenset({credential_marker, private_key_marker})
        previous = len(diagnostics)
        output, count = transform(output, **kwargs)
        if count:
            stats[rule] = count
            recorded = {f["line_start"] for f in diagnostics[previous:]}
            for line, (old, new) in enumerate(zip(before.splitlines(), output.splitlines()), 1):
                if old != new and line not in recorded and not any(
                    f["line_start"] <= line <= f["line_end"] for f in diagnostics[previous:]
                ):
                    diagnostics.append(_finding(rule, line, line))
    output = output.replace(private_key_marker, PRIVATE_KEY_PLACEHOLDER).replace(credential_marker, INLINE_PLACEHOLDER)
    return {"redacted_text": output, "redaction_stats": stats, "diagnostics": diagnostics}


def _redact_text(text: str, *, include_assignments: bool) -> tuple[str, dict[str, int]]:
    result = redact_with_diagnostics(text, include_assignments=include_assignments)
    return result["redacted_text"], result["redaction_stats"]


def _redact_profile_value(value: object) -> tuple[object, dict[str, int]]:
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        stats: dict[str, int] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise _unsupported_shape()
            if _is_secret_key(raw_key):
                if isinstance(raw_value, str):
                    output[raw_key] = INLINE_PLACEHOLDER
                    stats["R3"] = stats.get("R3", 0) + 1
                elif raw_value is None:
                    output[raw_key] = None
                else:
                    raise _unsupported_shape()
                continue
            redacted, nested_stats = _redact_profile_value(raw_value)
            output[raw_key] = redacted
            _merge_stats(stats, nested_stats)
        return output, stats
    if isinstance(value, list):
        output_list: list[object] = []
        stats: dict[str, int] = {}
        for item in value:
            redacted, nested_stats = _redact_profile_value(item)
            output_list.append(redacted)
            _merge_stats(stats, nested_stats)
        return output_list, stats
    if isinstance(value, str):
        return _redact_text(value, include_assignments=False)
    if value is None or type(value) in {bool, int, float}:
        return value, {}
    raise _unsupported_shape()


def _redact_prd_body(raw_body: Mapping[str, object]) -> tuple[dict[str, object], dict[str, int]]:
    required = {"kind", "text", "table_rows", "heading_level", "page_no"}
    if set(raw_body) != required:
        raise _unsupported_shape()
    text = raw_body["text"]
    table_rows = raw_body["table_rows"]
    if text is not None and not isinstance(text, str):
        raise _unsupported_shape()
    if table_rows is not None:
        if not isinstance(table_rows, list):
            raise _unsupported_shape()
        for row in table_rows:
            if not isinstance(row, list) or any(not isinstance(cell, str) for cell in row):
                raise _unsupported_shape()
    if raw_body["heading_level"] is not None and type(raw_body["heading_level"]) is not int:
        raise _unsupported_shape()
    if raw_body["page_no"] is not None and type(raw_body["page_no"]) is not int:
        raise _unsupported_shape()
    output = deepcopy(dict(raw_body))
    stats: dict[str, int] = {}
    if isinstance(text, str):
        redacted_text, text_stats = _redact_text(text, include_assignments=True)
        output["text"] = redacted_text
        _merge_stats(stats, text_stats)
    if isinstance(table_rows, list):
        redacted_rows: list[list[str]] = []
        for row in table_rows:
            redacted_row: list[str] = []
            for cell in row:
                redacted_cell, cell_stats = _redact_text(cell, include_assignments=True)
                redacted_row.append(redacted_cell)
                _merge_stats(stats, cell_stats)
            redacted_rows.append(redacted_row)
        output["table_rows"] = redacted_rows
    return output, stats


def _redact_git_diff(text: str) -> tuple[str, dict[str, int]]:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    segment_prefixes: list[str] = []
    segment_payloads: list[str] = []
    stats: dict[str, int] = {}
    inside_hunk = False

    def flush_segment() -> None:
        if not segment_payloads:
            return
        joined = "".join(segment_payloads)
        redacted, nested_stats = _redact_text(joined, include_assignments=True)
        redacted_lines = redacted.splitlines(keepends=True)
        if len(redacted_lines) != len(segment_payloads):
            raise _unsafe_ambiguity()
        output.extend(prefix + payload for prefix, payload in zip(segment_prefixes, redacted_lines))
        _merge_stats(stats, nested_stats)
        segment_prefixes.clear()
        segment_payloads.clear()

    for line in lines:
        if line.startswith("diff --git "):
            flush_segment()
            inside_hunk = False
            output.append(line)
            continue
        if line.startswith("@@"):
            flush_segment()
            inside_hunk = True
            output.append(line)
            continue
        if inside_hunk and line.startswith("\\ No newline at end of file"):
            flush_segment()
            output.append(line)
            continue
        if inside_hunk and line[:1] in {"+", "-", " "}:
            segment_prefixes.append(line[0])
            segment_payloads.append(line[1:])
            continue
        flush_segment()
        output.append(line)
    flush_segment()
    return "".join(output), stats


def _validate_upstream(
    value: object, *, model_call_id: int, target: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _upstream_inconsistent()
    if any(field not in value for field in _UPSTREAM_REQUIRED_FIELDS):
        raise _upstream_inconsistent()
    if (
        value["schema_version"] != "context_redaction_input_v1"
        or value["model_call_id"] != model_call_id
        or value["target"] != target
        or value["model_send_state"] != MODEL_SEND_STATE
        or type(value["model_call_id"]) is not int
        or value["model_call_id"] <= 0
        or type(value["snapshot_id"]) is not int
        or value["snapshot_id"] <= 0
        or type(value["project_id"]) is not int
        or value["project_id"] <= 0
        or not isinstance(value["target"], str)
        or not value["target"]
        or not isinstance(value["target_type"], str)
        or not isinstance(value["source_identity"], Mapping)
    ):
        raise _upstream_inconsistent()
    if any(
        not _is_sha256(value[field])
        for field in (
            "redaction_input_hash", "manifest_core_hash", "call_identity_hash",
            "snapshot_hash", "candidate_set_hash",
        )
    ):
        raise _upstream_inconsistent()
    unsupported = value["unsupported_context_sources"]
    if not isinstance(unsupported, list) or any(type(item) is not str for item in unsupported):
        raise _upstream_inconsistent()
    kind = value["raw_body_kind"]
    if kind == "profile_json":
        if (
            value["target_type"] != "profile" or target != "profile"
            or value["raw_body_state"] != "local_unredacted_ephemeral"
            or value["resolved_content_redaction_state"] != "pending"
            or not isinstance(value["raw_body"], Mapping)
        ):
            raise _upstream_inconsistent()
    elif kind == "prd_structured_block":
        if (
            value["target_type"] != "prd_block"
            or value["raw_body_state"] != "local_unredacted_ephemeral"
            or value["resolved_content_redaction_state"] != "pending"
            or not isinstance(value["raw_body"], Mapping)
        ):
            raise _upstream_inconsistent()
    elif kind == "text_unified_diff":
        if (
            value["target_type"] != "git_file_fact"
            or value["raw_body_state"] != "local_unredacted_ephemeral"
            or value["resolved_content_redaction_state"] != "pending"
            or not isinstance(value["raw_body"], str)
        ):
            raise _upstream_inconsistent()
    elif kind in {"binary_metadata_only", "sensitive_path_metadata_only"}:
        if (
            value["target_type"] != "git_file_fact" or value["raw_body"] is not None
            or value["raw_body_state"] != "not_applicable"
            or value["resolved_content_redaction_state"] != "not_applicable"
        ):
            raise _upstream_inconsistent()
    else:
        raise _upstream_inconsistent()
    safe_payload = {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in {"raw_body", "redaction_input_hash"}
    }
    try:
        computed = _stable_hash(safe_payload)
    except (TypeError, ValueError):
        raise _upstream_inconsistent() from None
    if computed != value["redaction_input_hash"]:
        raise _upstream_inconsistent()
    return value


def _redacted_body_hash(kind: str, redacted_body: object) -> str | None:
    if kind in {"profile_json", "prd_structured_block"}:
        return _stable_hash(redacted_body)
    if kind == "text_unified_diff":
        if not isinstance(redacted_body, str):
            raise _unsupported_shape()
        return hashlib.sha256(redacted_body.encode("utf-8")).hexdigest()
    if kind in {"binary_metadata_only", "sensitive_path_metadata_only"}:
        return None
    raise _unsupported_shape()


def _build_result(
    upstream: Mapping[str, object], *, redacted_body: object, redaction_state: str,
    redacted_body_state: str, redaction_stats: Mapping[str, int]
) -> dict[str, object]:
    stats = {
        rule_id: redaction_stats[rule_id]
        for rule_id in ("R1", "R2", "R3", "R4", "R5", "R6")
        if redaction_stats.get(rule_id, 0) > 0
    }
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "redaction_input_hash": upstream["redaction_input_hash"],
        "manifest_core_hash": upstream["manifest_core_hash"],
        "model_call_id": upstream["model_call_id"],
        "call_identity_hash": upstream["call_identity_hash"],
        "snapshot_id": upstream["snapshot_id"],
        "snapshot_hash": upstream["snapshot_hash"],
        "project_id": upstream["project_id"],
        "candidate_set_hash": upstream["candidate_set_hash"],
        "target": upstream["target"],
        "target_type": upstream["target_type"],
        "source_identity": deepcopy(dict(upstream["source_identity"])),
        "raw_body_kind": upstream["raw_body_kind"],
        "redaction_policy_id": POLICY_ID,
        "redaction_policy_hash": REDACTION_POLICY_HASH,
        "redaction_state": redaction_state,
        "redacted_body_state": redacted_body_state,
        "redacted_body": deepcopy(redacted_body),
        "redacted_body_hash": _redacted_body_hash(str(upstream["raw_body_kind"]), redacted_body),
        "redaction_stats": stats,
        "redaction_match_count": sum(stats.values()),
        "model_send_state": MODEL_SEND_STATE,
        "unsupported_context_sources": list(upstream["unsupported_context_sources"]),
    }
    hash_payload = {key: deepcopy(result[key]) for key in _RESULT_HASH_KEYS}
    result["redaction_result_hash"] = _stable_hash(hash_payload)
    if tuple(result) != _RESULT_KEYS:
        raise AssertionError("context_redaction_result_v1 key construction drift")
    return result


QUARANTINE_PLACEHOLDER = "[REDACTED:TARGET_QUARANTINED]"


def quarantine_body(kind: str) -> object:
    if kind == "text_unified_diff":
        return QUARANTINE_PLACEHOLDER
    if kind == "profile_json":
        return {"quarantined": QUARANTINE_PLACEHOLDER}
    if kind == "prd_structured_block":
        return {"kind": "paragraph", "text": QUARANTINE_PLACEHOLDER,
                "table_rows": None, "heading_level": None, "page_no": None}
    raise _unsupported_shape()


def is_quarantined_body(kind: str, body: object) -> bool:
    return kind in {"text_unified_diff", "profile_json", "prd_structured_block"} and body == quarantine_body(kind)


def build_context_redaction_result(
    *, model_call_id: int, budget_record: Mapping[str, object], target: str
) -> dict[str, object]:
    """Redact one formal historical target in-memory; never grant model-send admission."""
    upstream = _validate_upstream(
        resolve_context_redaction_input(
            model_call_id=model_call_id, budget_record=budget_record, target=target
        ),
        model_call_id=model_call_id,
        target=target,
    )
    kind = upstream["raw_body_kind"]
    if kind in {"binary_metadata_only", "sensitive_path_metadata_only"}:
        return _build_result(
            upstream, redacted_body=None, redaction_state="not_applicable",
            redacted_body_state="not_applicable", redaction_stats={}
        )
    raw_body = deepcopy(upstream["raw_body"])
    quarantined = False
    try:
        if kind == "profile_json":
            redacted_body, stats = _redact_profile_value(raw_body)
        elif kind == "prd_structured_block":
            if not isinstance(raw_body, Mapping):
                raise _unsupported_shape()
            redacted_body, stats = _redact_prd_body(raw_body)
        elif kind == "text_unified_diff":
            if not isinstance(raw_body, str):
                raise _unsupported_shape()
            redacted_body, stats = _redact_git_diff(raw_body)
        else:
            raise _upstream_inconsistent()
    except HTTPException as exc:
        if not isinstance(exc.detail, dict) or exc.detail.get("code") != "CONTEXT_REDACTION_UNSAFE_AMBIGUITY":
            raise
        redacted_body = quarantine_body(str(kind))
        stats = {}
        quarantined = True

    return _build_result(
        upstream, redacted_body=redacted_body,
        redaction_state="quarantined" if quarantined else "completed",
        redacted_body_state="local_redacted_ephemeral", redaction_stats=stats
    )


STRUCTURED_VALUE_REDACTION_SCHEMA_VERSION = "credential_safe_structured_value_v1"


def _redact_assignment_values(value: object) -> tuple[object, dict[str, int]]:
    """Second pure pass: add assignment grammar to already credential-redacted structure."""
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        stats: dict[str, int] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _unsupported_shape()
            redacted, nested = _redact_assignment_values(item)
            output[key] = redacted
            _merge_stats(stats, nested)
        return output, stats
    if isinstance(value, list):
        output_list: list[object] = []
        stats: dict[str, int] = {}
        for item in value:
            redacted, nested = _redact_assignment_values(item)
            output_list.append(redacted)
            _merge_stats(stats, nested)
        return output_list, stats
    if isinstance(value, str):
        return _redact_text(value, include_assignments=True)
    if value is None or type(value) in {bool, int, float}:
        return value, {}
    raise _unsupported_shape()


def redact_credential_safe_structured_value(*, value: object) -> dict[str, object]:
    """Pure reusable credential-redaction seam for non-Context structured business values.

    It reuses the exact credential_redaction_v1 grammar and returns no unredacted copy. It grants
    no Context/Evidence authority and performs no persistence, logging, credential read or I/O.
    """
    first_pass, stats = _redact_profile_value(deepcopy(value))
    redacted_value, assignment_stats = _redact_assignment_values(first_pass)
    _merge_stats(stats, assignment_stats)
    ordered_stats = {
        rule_id: stats[rule_id]
        for rule_id in ("R1", "R2", "R3", "R4", "R5", "R6")
        if stats.get(rule_id, 0) > 0
    }
    try:
        redacted_value_hash = _stable_hash(redacted_value)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _unsupported_shape() from exc
    return {
        "schema_version": STRUCTURED_VALUE_REDACTION_SCHEMA_VERSION,
        "redaction_policy_id": POLICY_ID,
        "redaction_policy_hash": REDACTION_POLICY_HASH,
        "redacted_value": deepcopy(redacted_value),
        "redacted_value_hash": redacted_value_hash,
        "redaction_stats": ordered_stats,
        "redaction_match_count": sum(ordered_stats.values()),
    }


def build_context_redaction_diagnostics(
    *, model_call_id: int, budget_record: Mapping[str, object], target: str
) -> list[dict[str, object]]:
    """Return safe locations from the same frozen input, without retaining raw content."""
    upstream = _validate_upstream(resolve_context_redaction_input(
        model_call_id=model_call_id, budget_record=budget_record, target=target
    ), model_call_id=model_call_id, target=target)
    source = upstream["source_identity"]
    location = {"target": target, "source_ref": source.get("source_ref", target)}
    if "path" in source:
        location["path"] = source["path"]
    findings: list[dict[str, object]] = []

    def scan(text: str, *, assignments: bool = True, offset: int = 0) -> None:
        try:
            detected = redact_with_diagnostics(text, include_assignments=assignments)["diagnostics"]
        except HTTPException as exc:
            if not isinstance(exc.detail, dict) or exc.detail.get("code") != "CONTEXT_REDACTION_UNSAFE_AMBIGUITY":
                raise
            detected = [{**_finding("R1", 1, max(1, len(text.splitlines()))),
                         "action": "isolated", "safe_snippet": QUARANTINE_PLACEHOLDER}]
        for finding in detected:
            findings.append({**location, **finding,
                             "line_start": finding["line_start"] + offset,
                             "line_end": finding["line_end"] + offset})

    def scan_profile(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if _is_secret_key(key) and isinstance(item, str):
                    findings.append({**location, **_finding("R3", 1, 1)})
                else:
                    scan_profile(item)
        elif isinstance(value, list):
            for item in value:
                scan_profile(item)
        elif isinstance(value, str):
            scan(value, assignments=False)

    kind, body = upstream["raw_body_kind"], upstream["raw_body"]
    if kind == "profile_json":
        scan_profile(body)
    elif kind == "prd_structured_block":
        if isinstance(body["text"], str):
            scan(body["text"])
        for row in body["table_rows"] or []:
            for cell in row:
                scan(cell)
    elif kind == "text_unified_diff":
        # Match the transform's hunk segments exactly. Locations are diff line ranges.
        segment: list[str] = []
        start = 0
        inside = False
        for number, line in enumerate(body.splitlines(keepends=True), 1):
            if inside and line[:1] in {"+", "-", " "} and not line.startswith("diff --git "):
                if not segment:
                    start = number
                segment.append(line[1:])
                continue
            if segment:
                scan("".join(segment), offset=start - 1)
                segment = []
            if line.startswith("diff --git "):
                inside = False
            elif line.startswith("@@"):
                inside = True
        if segment:
            scan("".join(segment), offset=start - 1)
        for finding in findings:
            finding["range_kind"] = "diff_lines"
    return findings
