"""DeepSeek V4 Flash send-time current authority; never sends business payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
import re

import httpx
from fastapi import HTTPException

from app.deepseek_credential import ReportCredentialError, read_report_credential

from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION
from app import model_qualification_registry
from app.deepseek_model_catalog import (
    REPORT_CONTEXT_WINDOW_TOKENS,
    REPORT_MAX_OUTPUT_TOKENS,
    REPORT_MODEL_ID,
    REPORT_MODEL_VERSION,
)
from app.model_call_ledger import get_model_call


SCHEMA_VERSION = "deepseek_current_authority_v1"
QUALIFICATION_SCHEMA_VERSION = "deepseek_current_model_qualification_v1"
REGISTRY_QUALIFICATION_SCHEMA_VERSION = "deepseek_current_registry_qualification_v1"
AUTHORIZATION_SCHEMA_VERSION = "deepseek_current_data_authorization_v1"
PROVIDER = "deepseek"
MODEL_ID = REPORT_MODEL_ID
MODEL_VERSION = REPORT_MODEL_VERSION
CONTEXT_WINDOW_TOKENS = REPORT_CONTEXT_WINDOW_TOKENS
MAX_OUTPUT_TOKENS = REPORT_MAX_OUTPUT_TOKENS
PURPOSE_ID = "anxin_board_daily_report_v1"
REGENERATE_TASK_TYPE = "daily_report_regenerate"
REGENERATE_OUTPUT_SCHEMA_VERSION = "daily-report-regenerate/1.0"
REGENERATE_PROMPT_VERSION = "page07-regenerate-prompt/1.0"
REGENERATE_RULE_VERSION = "page07-regenerate-rules/2.0"
REGENERATE_SAMPLE_PACK_VERSION = "page07-regenerate-qualification-pack/2.0"
REGENERATE_PURPOSE_ID = "anxin_board_daily_report_regenerate_v1"
MODELS_URL = "https://api.deepseek.com/models"
METADATA_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
_MODELS_RESPONSE_LIMIT = 1_048_576
_METADATA_RESPONSE_LIMIT = 2_097_152
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _input_invalid(message: str = "DeepSeek current authority 输入无效。") -> HTTPException:
    return _error(400, "DEEPSEEK_AUTHORITY_INPUT_INVALID", message)


def _call_invalid(message: str = "Formal Model Call identity 无效。") -> HTTPException:
    return _error(409, "DEEPSEEK_AUTHORITY_CALL_IDENTITY_INVALID", message)


def _credential_unavailable(reason: str | None = None) -> HTTPException:
    error = _error(
        503,
        "DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE",
        "DeepSeek 凭据不可用，请检查设置中已保存的凭据及当前 Windows 用户权限。",
    )
    if reason is not None:
        error.detail["credential_error_code"] = reason
    return error


def _qualification_unavailable() -> HTTPException:
    return _error(
        503,
        "DEEPSEEK_AUTHORITY_QUALIFICATION_UNAVAILABLE",
        "DeepSeek current model qualification source 不可用。",
    )


def _qualification_not_admitted(message: str) -> HTTPException:
    return _error(409, "DEEPSEEK_AUTHORITY_QUALIFICATION_NOT_ADMITTED", message)


def _qualification_registry_invalid(message: str) -> HTTPException:
    return _error(409, "DEEPSEEK_AUTHORITY_QUALIFICATION_REGISTRY_INVALID", message)


def _model_not_listed() -> HTTPException:
    return _error(
        409,
        "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED",
        f"{MODEL_ID} 当前未被官方 models source exact 列出。",
    )


def _metadata_unavailable() -> HTTPException:
    return _error(
        503,
        "DEEPSEEK_AUTHORITY_MODEL_METADATA_UNAVAILABLE",
        "DeepSeek current model metadata source 不可用。",
    )


def _version_changed() -> HTTPException:
    return _error(
        409,
        "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED",
        "DeepSeek V4 Flash current semantic model version 已变化。",
    )


def _constraint_changed() -> HTTPException:
    return _error(
        409,
        "DEEPSEEK_AUTHORITY_PROVIDER_CONSTRAINT_CHANGED",
        "DeepSeek V4 Flash current provider constraints 已变化。",
    )


def _not_authorized() -> HTTPException:
    return _error(
        403,
        "DEEPSEEK_AUTHORITY_DATA_SEND_NOT_AUTHORIZED",
        "当前 exact data scope 未获得 Human send authorization。",
    )


def _source_invalid(
    message: str = "DeepSeek current authority source 无法完成结构化自校验。",
) -> HTTPException:
    return _error(502, "DEEPSEEK_AUTHORITY_SOURCE_INVALID", message)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _source_invalid() from exc


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_hash(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _validate_direct_inputs(
    model_call_id: object,
    final_context_manifest_hash: object,
    framed_payload_hash: object,
) -> tuple[int, str, str]:
    if type(model_call_id) is not int or model_call_id <= 0:
        raise _input_invalid("model_call_id 必须是正整数。")
    if not _is_hash(final_context_manifest_hash):
        raise _input_invalid("final_context_manifest_hash 必须是 lowercase SHA-256。")
    if not _is_hash(framed_payload_hash):
        raise _input_invalid("framed_payload_hash 必须是 lowercase SHA-256。")
    return model_call_id, final_context_manifest_hash, framed_payload_hash


def _validate_regenerate_hash_inputs(
    *,
    request_envelope_hash: object,
    prompt_contract_hash: object,
    sampling_parameters_hash: object,
) -> tuple[str, str, str]:
    for field_name, value in (
        ("request_envelope_hash", request_envelope_hash),
        ("prompt_contract_hash", prompt_contract_hash),
        ("sampling_parameters_hash", sampling_parameters_hash),
    ):
        if not _is_hash(value):
            raise _input_invalid(f"{field_name} 必须是 lowercase SHA-256。")
    return request_envelope_hash, prompt_contract_hash, sampling_parameters_hash


def _new_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )


def _read_bounded_source(
    *,
    url: str,
    headers: dict[str, str],
    limit: int,
    unavailable: HTTPException,
) -> bytes:
    network_failed = False
    try:
        with _new_client() as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    raise _source_invalid(
                        "Redirect 被 current-authority source policy 禁止。"
                    )
                if response.status_code != 200:
                    raise unavailable
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = None
                    if declared is None:
                        raise _source_invalid("Source Content-Length 无效。")
                    if declared < 0 or declared > limit:
                        raise _source_invalid("Source response 超过冻结 byte cap。")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise _source_invalid("Source response 超过冻结 byte cap。")
                return bytes(body)
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.RequestError):
        network_failed = True
    if network_failed:
        raise unavailable
    raise _source_invalid()


def _fetch_models_source() -> bytes:
    try:
        api_key = read_report_credential()
    except ReportCredentialError as exc:
        raise _credential_unavailable(exc.code) from None
    return _read_bounded_source(
        url=MODELS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        limit=_MODELS_RESPONSE_LIMIT,
        unavailable=_qualification_unavailable(),
    )


def _validate_models_source(raw: bytes) -> None:
    parse_failed = False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
        parse_failed = True
    if parse_failed:
        raise _source_invalid("Models source 必须是 UTF-8 JSON。")
    if type(payload) is not dict or type(payload.get("data")) is not list:
        raise _source_invalid("Models source.data 必须是 exact list。")
    ids: list[str] = []
    for item in payload["data"]:
        if (
            type(item) is not dict
            or type(item.get("id")) is not str
            or not item["id"]
        ):
            raise _source_invalid("Models source item.id 无效。")
        ids.append(item["id"])
    if ids.count(MODEL_ID) != 1:
        raise _model_not_listed()


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif (
            self._table_depth == 1
            and tag in {"td", "th"}
            and self._row is not None
        ):
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if (
            self._table_depth == 1
            and tag in {"td", "th"}
            and self._cell_parts is not None
            and self._row is not None
        ):
            self._row.append(_normalize_cell("".join(self._cell_parts)))
            self._cell_parts = None
        elif (
            self._table_depth == 1
            and tag == "tr"
            and self._row is not None
            and self._rows is not None
        ):
            if any(self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            if self._table_depth == 1 and self._rows is not None:
                self.tables.append(self._rows)
                self._rows = None
            self._table_depth -= 1


_FOOTNOTE_RE = re.compile(r"\(\d+\)$")
_MAX_OUTPUT_PREFIX = "MAXIMUM:"


def _normalize_cell(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _normalize_model_cell(value: str) -> str:
    """Official model cells may carry a footnote marker, e.g. ``model(1)``."""
    return _FOOTNOTE_RE.sub("", _normalize_cell(value))


def _normalize_max_output_cell(value: str) -> str:
    """Official MAX OUTPUT cells may carry a ``MAXIMUM:`` prefix."""
    normalized = _normalize_cell(value)
    if normalized.upper().startswith(_MAX_OUTPUT_PREFIX):
        normalized = normalized[len(_MAX_OUTPUT_PREFIX):].strip()
    return normalized


def _label(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def _column_binding(
    table: list[list[str]], row_index: int, column_index: int
) -> tuple[str, str, str] | None:
    labels: dict[str, str] = {}
    for row in table:
        if not row or column_index >= len(row):
            continue
        row_label = _label(row[0])
        if row_label in {"MODEL VERSION", "CONTEXT LENGTH", "MAX OUTPUT"}:
            if row_label in labels:
                return None
            labels[row_label] = row[column_index]
    if set(labels) != {"MODEL VERSION", "CONTEXT LENGTH", "MAX OUTPUT"}:
        return None
    return (
        labels["MODEL VERSION"],
        labels["CONTEXT LENGTH"],
        labels["MAX OUTPUT"],
    )


def _row_binding(
    table: list[list[str]], row_index: int
) -> tuple[str, str, str] | None:
    if not table or row_index <= 0:
        return None
    header = [_label(cell) for cell in table[0]]
    row = table[row_index]
    required = ("MODEL VERSION", "CONTEXT LENGTH", "MAX OUTPUT")
    if any(header.count(name) != 1 for name in required):
        return None
    indexes = [header.index(name) for name in required]
    if any(index >= len(row) for index in indexes):
        return None
    return row[indexes[0]], row[indexes[1]], row[indexes[2]]


def _parse_metadata_source(raw: bytes) -> tuple[str, int, int]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise _source_invalid("Metadata source 必须是 UTF-8 HTML。")
    parser = _TableParser()
    parse_failed = False
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        parse_failed = True
    if parse_failed:
        raise _source_invalid("Metadata HTML 无法解析。")

    bindings: list[tuple[str, str, str]] = []
    for table in parser.tables:
        for row_index, row in enumerate(table):
            for column_index, cell in enumerate(row):
                if _normalize_model_cell(cell) != MODEL_ID:
                    continue
                local_candidates: list[tuple[str, str, str]] = []
                for candidate in (
                    _column_binding(table, row_index, column_index),
                    _row_binding(table, row_index),
                ):
                    if candidate is not None and candidate not in local_candidates:
                        local_candidates.append(candidate)
                if len(local_candidates) != 1:
                    raise _source_invalid(
                        "Metadata exact model occurrence 无法唯一绑定 row/column。"
                    )
                bindings.append(local_candidates[0])

    if len(bindings) != 1:
        raise _source_invalid("Metadata 必须唯一绑定同一 model row/column。")

    version, context, max_output = bindings[0]
    if version != MODEL_VERSION:
        raise _version_changed()
    if context != "1M" or _normalize_max_output_cell(max_output) != "384K":
        raise _constraint_changed()
    return version, CONTEXT_WINDOW_TOKENS, MAX_OUTPUT_TOKENS


def _fetch_metadata_source() -> bytes:
    return _read_bounded_source(
        url=METADATA_URL,
        headers={"Accept": "text/html,application/xhtml+xml"},
        limit=_METADATA_RESPONSE_LIMIT,
        unavailable=_metadata_unavailable(),
    )


def _validate_prepared_call(
    call: object, model_call_id: int
) -> dict[str, object]:
    if type(call) is not dict:
        raise _call_invalid()
    required_strings = (
        "rule_version",
        "output_schema_version",
        "benchmark_sample_pack_version",
        "task_type",
    )
    if call.get("model_call_id") != model_call_id:
        raise _call_invalid("Model Call id rebind 失败。")
    if (
        call.get("provider") != PROVIDER
        or call.get("model_id") != MODEL_ID
        or call.get("model_version") != MODEL_VERSION
    ):
        raise _call_invalid(
            "Prepared provider/model/version 与当前 qualified report model authority 不一致。"
        )
    if not _is_hash(call.get("call_identity_hash")):
        raise _call_invalid("Prepared call_identity_hash 无效。")
    for field in required_strings:
        if type(call.get(field)) is not str or not str(call[field]).strip():
            raise _call_invalid(f"Prepared {field} 无效。")
    return dict(call)


def _validate_regenerate_prepared_call(call: object, model_call_id: int) -> dict[str, object]:
    value = _validate_prepared_call(call, model_call_id)
    if (
        value["task_type"] != REGENERATE_TASK_TYPE
        or value["output_schema_version"] != REGENERATE_OUTPUT_SCHEMA_VERSION
        or value["rule_version"] != REGENERATE_RULE_VERSION
        or value["benchmark_sample_pack_version"] != REGENERATE_SAMPLE_PACK_VERSION
    ):
        raise _call_invalid("Prepared regenerate task/schema/rule/sample-pack identity 不一致。")
    return value


def _data_scope_payload(
    *,
    call: dict[str, object],
    final_context_manifest_hash: str,
    framed_payload_hash: str,
) -> dict[str, object]:
    return {
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "model_call_id": call["model_call_id"],
        "call_identity_hash": call["call_identity_hash"],
        "final_context_manifest_hash": final_context_manifest_hash,
        "framed_payload_hash": framed_payload_hash,
        "task_type": call["task_type"],
        "output_schema_version": call["output_schema_version"],
        "purpose_id": PURPOSE_ID,
    }


def _regenerate_data_scope_payload(
    *,
    call: Mapping[str, object],
    final_context_manifest_hash: str,
    framed_payload_hash: str,
    request_envelope_hash: str,
    prompt_contract_hash: str,
    sampling_parameters_hash: str,
) -> dict[str, object]:
    return {
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "model_call_id": call["model_call_id"],
        "call_identity_hash": call["call_identity_hash"],
        "final_context_manifest_hash": final_context_manifest_hash,
        "framed_payload_hash": framed_payload_hash,
        "request_envelope_hash": request_envelope_hash,
        "prompt_contract_hash": prompt_contract_hash,
        "sampling_parameters_hash": sampling_parameters_hash,
        "task_type": REGENERATE_TASK_TYPE,
        "output_schema_version": REGENERATE_OUTPUT_SCHEMA_VERSION,
        "purpose_id": REGENERATE_PURPOSE_ID,
    }


def _qualification_evidence(
    *,
    call: dict[str, object],
    models_raw: bytes,
    metadata_raw: bytes,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "authority_source_id": "deepseek_official_models_and_metadata",
        "authority_source_version": "v1",
        "authority_ref": {
            "model_list": "deepseek_api_models",
            "model_metadata": "deepseek_api_docs_models_pricing",
        },
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "rule_version": call["rule_version"],
        "output_schema_version": call["output_schema_version"],
        "benchmark_sample_pack_version": call[
            "benchmark_sample_pack_version"
        ],
        "qualification_status": "qualified",
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "checked_at": _now(),
        "models_source_hash": _bytes_hash(models_raw),
        "metadata_source_hash": _bytes_hash(metadata_raw),
    }
    evidence["evidence_hash"] = _stable_hash(evidence)
    return evidence


def _lookup_regenerate_qualification(
    *,
    call: Mapping[str, object],
    prompt_contract_hash: str,
    sampling_parameters_hash: str,
) -> Mapping[str, object]:
    lookup = {
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "task_type": REGENERATE_TASK_TYPE,
        "ai_contract_schema_version": AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": REGENERATE_OUTPUT_SCHEMA_VERSION,
        "prompt_version": REGENERATE_PROMPT_VERSION,
        "prompt_contract_hash": prompt_contract_hash,
        "rule_version": call["rule_version"],
        "sample_pack_version": call["benchmark_sample_pack_version"],
        "sampling_parameters_hash": sampling_parameters_hash,
    }
    try:
        record = model_qualification_registry.lookup_qualified_record(lookup)
    except model_qualification_registry.QualificationRegistryError as exc:
        if exc.code in {
            "QUALIFICATION_NOT_ADMITTED",
            "QUALIFICATION_IDENTITY_MISMATCH",
            "QUALIFICATION_LOOKUP_INVALID",
        }:
            raise _qualification_not_admitted(str(exc)) from exc
        raise _qualification_registry_invalid(str(exc)) from exc
    if not isinstance(record, Mapping):
        raise _qualification_registry_invalid("qualification registry 必须返回 exact Mapping。")
    if any(record.get(field) != expected for field, expected in lookup.items()):
        raise _qualification_registry_invalid("qualification registry 返回记录与 exact lookup identity 不一致。")
    if record.get("qualification_status") != model_qualification_registry.QUALIFIED_STATUS:
        raise _qualification_registry_invalid("qualification registry 返回记录不是 exact qualified admission。")
    return record


def _registry_qualification_evidence(record: Mapping[str, object]) -> dict[str, object]:
    required = (
        "provider",
        "model_id",
        "model_version",
        "task_type",
        "ai_contract_schema_version",
        "output_schema_version",
        "prompt_version",
        "prompt_contract_hash",
        "rule_version",
        "sample_pack_version",
        "sampling_parameters_hash",
        "sample_manifest_hash",
        "qualification_harness_commit",
        "evidence_manifest_hash",
        "review_ref",
        "qualification_status",
        "record_hash",
    )
    if any(field not in record for field in required):
        raise _qualification_registry_invalid("qualification registry record 缺少已冻结字段。")
    evidence: dict[str, object] = {
        "schema_version": REGISTRY_QUALIFICATION_SCHEMA_VERSION,
        "authority_source_id": "product_model_qualification_registry",
        "authority_source_version": "v1",
        "authority_ref": {
            "record_hash": record["record_hash"],
            "review_ref": record["review_ref"],
        },
        "provider": record["provider"],
        "model_id": record["model_id"],
        "model_version": record["model_version"],
        "task_type": record["task_type"],
        "ai_contract_schema_version": record["ai_contract_schema_version"],
        "output_schema_version": record["output_schema_version"],
        "prompt_version": record["prompt_version"],
        "prompt_contract_hash": record["prompt_contract_hash"],
        "rule_version": record["rule_version"],
        "sample_pack_version": record["sample_pack_version"],
        "sampling_parameters_hash": record["sampling_parameters_hash"],
        "qualification_status": record["qualification_status"],
        "sample_manifest_hash": record["sample_manifest_hash"],
        "qualification_harness_commit": record["qualification_harness_commit"],
        "evidence_manifest_hash": record["evidence_manifest_hash"],
        "review_ref": record["review_ref"],
        "record_hash": record["record_hash"],
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    evidence["evidence_hash"] = _stable_hash(evidence)
    return evidence


def _authorization_evidence_for_purpose(
    data_scope_hash: str, *, purpose_id: str
) -> dict[str, object]:
    if os.environ.get("ANXIN_DEEPSEEK_SEND_AUTHORIZED") != "true":
        raise _not_authorized()
    if os.environ.get("ANXIN_DEEPSEEK_SEND_PURPOSE_ID") != purpose_id:
        raise _not_authorized()
    permit_hash = os.environ.get("ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH")
    if permit_hash != data_scope_hash or not _is_hash(permit_hash):
        raise _not_authorized()
    evidence: dict[str, object] = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "authority_source_id": "windows_process_env_human_permit",
        "authority_source_version": "v1",
        "authority_ref": "process-env/exact-scope",
        "provider": PROVIDER,
        "authorized": True,
        "valid": True,
        "purpose_id": purpose_id,
        "data_scope_hash": data_scope_hash,
    }
    evidence["evidence_hash"] = _stable_hash(evidence)
    return evidence


def _authorization_evidence(data_scope_hash: str) -> dict[str, object]:
    return _authorization_evidence_for_purpose(data_scope_hash, purpose_id=PURPOSE_ID)


def _read_prepared_call(model_call_id: int, *, regenerate: bool) -> dict[str, object]:
    try:
        raw = get_model_call(model_call_id)
        return (
            _validate_regenerate_prepared_call(raw, model_call_id)
            if regenerate
            else _validate_prepared_call(raw, model_call_id)
        )
    except HTTPException as exc:
        if (
            isinstance(exc.detail, dict)
            and str(exc.detail.get("code", "")).startswith("DEEPSEEK_AUTHORITY_")
        ):
            raise
        raise _call_invalid() from exc


def resolve_deepseek_current_authority(
    *,
    model_call_id: int,
    final_context_manifest_hash: str,
    framed_payload_hash: str,
) -> dict[str, object]:
    """Resolve legacy daily-generate exact authority; behavior remains unchanged."""
    model_call_id, final_context_manifest_hash, framed_payload_hash = (
        _validate_direct_inputs(
            model_call_id,
            final_context_manifest_hash,
            framed_payload_hash,
        )
    )
    call = _read_prepared_call(model_call_id, regenerate=False)

    data_scope_hash = _stable_hash(
        _data_scope_payload(
            call=call,
            final_context_manifest_hash=final_context_manifest_hash,
            framed_payload_hash=framed_payload_hash,
        )
    )

    models_raw = _fetch_models_source()
    _validate_models_source(models_raw)
    metadata_raw = _fetch_metadata_source()
    _parse_metadata_source(metadata_raw)
    qualification = _qualification_evidence(
        call=call,
        models_raw=models_raw,
        metadata_raw=metadata_raw,
    )
    authorization = _authorization_evidence(data_scope_hash)

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "model_call_id": model_call_id,
        "call_identity_hash": call["call_identity_hash"],
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "purpose_id": PURPOSE_ID,
        "data_scope_hash": data_scope_hash,
        "qualification": qualification,
        "authorization": authorization,
    }
    result["authority_hash"] = _stable_hash(result)
    return result


def resolve_deepseek_regenerate_current_authority(
    *,
    model_call_id: int,
    final_context_manifest_hash: str,
    framed_payload_hash: str,
    request_envelope_hash: str,
    prompt_contract_hash: str,
    sampling_parameters_hash: str,
) -> dict[str, object]:
    """Resolve regenerate registry + exact Human scope authority without provider I/O."""
    model_call_id, final_context_manifest_hash, framed_payload_hash = _validate_direct_inputs(
        model_call_id,
        final_context_manifest_hash,
        framed_payload_hash,
    )
    request_envelope_hash, prompt_contract_hash, sampling_parameters_hash = (
        _validate_regenerate_hash_inputs(
            request_envelope_hash=request_envelope_hash,
            prompt_contract_hash=prompt_contract_hash,
            sampling_parameters_hash=sampling_parameters_hash,
        )
    )
    call = _read_prepared_call(model_call_id, regenerate=True)
    record = _lookup_regenerate_qualification(
        call=call,
        prompt_contract_hash=prompt_contract_hash,
        sampling_parameters_hash=sampling_parameters_hash,
    )
    qualification = _registry_qualification_evidence(record)
    data_scope_hash = _stable_hash(
        _regenerate_data_scope_payload(
            call=call,
            final_context_manifest_hash=final_context_manifest_hash,
            framed_payload_hash=framed_payload_hash,
            request_envelope_hash=request_envelope_hash,
            prompt_contract_hash=prompt_contract_hash,
            sampling_parameters_hash=sampling_parameters_hash,
        )
    )
    authorization = _authorization_evidence_for_purpose(
        data_scope_hash,
        purpose_id=REGENERATE_PURPOSE_ID,
    )
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "model_call_id": model_call_id,
        "call_identity_hash": call["call_identity_hash"],
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "purpose_id": REGENERATE_PURPOSE_ID,
        "data_scope_hash": data_scope_hash,
        "qualification": qualification,
        "authorization": authorization,
    }
    result["authority_hash"] = _stable_hash(result)
    return result
