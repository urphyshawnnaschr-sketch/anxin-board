"""Authority refresh for the current qualified DeepSeek model identity.

Covers the 2026-09 DeepSeek model rollover: the retired ``deepseek-flash``
name is no longer listed, and the qualified report model is ``deepseek-flash``
served by ``DeepSeek-V4.1-Flash``. Metadata parsing must accept the official
footnote suffix ``(1)`` on the model cell and the merged ``MAXIMUM: 384K`` /
``1M`` cells exactly as published.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_current_authority as authority  # noqa: E402
from app import deepseek_credential  # noqa: E402
from app.secret_store import InMemorySecretStore  # noqa: E402


@pytest.fixture(autouse=True)
def fake_report_credential_store(monkeypatch):
    monkeypatch.setattr(deepseek_credential, "_secret_store_factory", InMemorySecretStore)


MODELS_WITH_FLASH = json.dumps(
    {
        "object": "list",
        "data": [
            {"id": "deepseek-flash", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
        ],
    },
    separators=(",", ":"),
).encode()

MODELS_LEGACY_ALIAS_ONLY = json.dumps(
    {
        "object": "list",
        "data": [
            {"id": "deepseek" + "-v4-flash", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
        ],
    },
    separators=(",", ":"),
).encode()

METADATA_V41 = b"""
<html><body><table>
<tr><td colspan="3">MODEL</td><td>deepseek-flash<sup>(1)</sup></td><td>deepseek-v4-pro</td></tr>
<tr><td colspan="3">MODEL VERSION</td><td>DeepSeek-V4.1-Flash</td><td>DeepSeek-V4-Pro-0813</td></tr>
<tr><td colspan="3">CONTEXT LENGTH</td><td colspan="2">1M</td></tr>
<tr><td colspan="3">MAX OUTPUT</td><td colspan="2">MAXIMUM: 384K</td></tr>
</table></body></html>
"""


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _call(**overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "model_call_id": 7,
        "call_identity_hash": "a" * 64,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "samples/1.0",
        "task_type": "daily_report_generate",
    }
    value.update(overrides)
    return value


def _set_exact_permit(monkeypatch, call: dict[str, object]) -> str:
    scope_hash = authority._stable_hash(
        authority._data_scope_payload(
            call=call,
            final_context_manifest_hash="b" * 64,
            framed_payload_hash="c" * 64,
        )
    )
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_AUTHORIZED", "true")
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_PURPOSE_ID", authority.PURPOSE_ID)
    monkeypatch.setenv("ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH", scope_hash)
    return scope_hash


def _install_sources(monkeypatch, *, models: bytes, metadata: bytes):
    monkeypatch.setattr(authority, "_fetch_models_source", lambda: models)
    monkeypatch.setattr(authority, "_fetch_metadata_source", lambda: metadata)


def test_models_source_requires_exact_qualified_model_membership():
    authority._validate_models_source(MODELS_WITH_FLASH)
    with pytest.raises(HTTPException) as exc:
        authority._validate_models_source(MODELS_LEGACY_ALIAS_ONLY)
    assert _code(exc) == "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED"


def test_metadata_binding_accepts_footnote_suffix_and_merged_cells():
    version, context, max_output = authority._parse_metadata_source(METADATA_V41)
    assert version == "DeepSeek-V4.1-Flash"
    assert context == 1_000_000
    assert max_output == 384_000


def test_full_authority_resolves_for_flash_prepared_call(monkeypatch):
    call = _call()
    _set_exact_permit(monkeypatch, call)
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    _install_sources(monkeypatch, models=MODELS_WITH_FLASH, metadata=METADATA_V41)
    result = authority.resolve_deepseek_current_authority(
        model_call_id=7,
        final_context_manifest_hash="b" * 64,
        framed_payload_hash="c" * 64,
    )
    assert result["model_id"] == "deepseek-flash"
    assert result["model_version"] == "DeepSeek-V4.1-Flash"
    assert result["qualification"]["qualification_status"] == "qualified"


def test_legacy_prepared_call_is_rejected_before_source_network(monkeypatch):
    call = _call(model_id="deepseek-v4-flash", model_version="DeepSeek-V4-Flash-0731")
    _set_exact_permit(monkeypatch, call)
    monkeypatch.setattr(authority, "get_model_call", lambda _id: deepcopy(call))
    calls = {"models": 0, "metadata": 0}

    def fetch_models():
        calls["models"] += 1
        return MODELS_WITH_FLASH

    def fetch_metadata():
        calls["metadata"] += 1
        return METADATA_V41

    monkeypatch.setattr(authority, "_fetch_models_source", fetch_models)
    monkeypatch.setattr(authority, "_fetch_metadata_source", fetch_metadata)
    with pytest.raises(HTTPException) as exc:
        authority.resolve_deepseek_current_authority(
            model_call_id=7,
            final_context_manifest_hash="b" * 64,
            framed_payload_hash="c" * 64,
        )
    assert _code(exc) == "DEEPSEEK_AUTHORITY_CALL_IDENTITY_INVALID"
    assert calls == {"models": 0, "metadata": 0}
