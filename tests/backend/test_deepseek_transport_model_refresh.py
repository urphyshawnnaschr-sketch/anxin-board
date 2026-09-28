"""Transport-level binding: the single report request must use the qualified model."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_transport  # noqa: E402


def test_transport_capability_reports_qualified_model_identity():
    capability = deepseek_transport.get_deepseek_transport_capability(
        task_type=deepseek_transport.TASK_TYPE,
        output_schema_version=deepseek_transport.OUTPUT_SCHEMA_VERSION,
    )
    assert capability["provider"] == "deepseek"
    assert capability["model_id"] == "deepseek-flash"
    assert capability["model_version"] == "DeepSeek-V4.1-Flash"
    assert capability["max_output_tokens"] == 384_000


def test_request_body_model_is_bound_to_catalog_constant():
    # The request body model must come from the qualified catalog binding, never
    # from a divergent literal that could silently drift at the next rollover.
    source = inspect.getsource(deepseek_transport._send_with_key)
    assert '"model": MODEL_ID' in source
    assert ("deepseek" + "-v4-flash") not in source
    assert deepseek_transport.MODEL_ID == "deepseek-flash"


def test_send_with_explicit_key_forwards_messages_and_budget(monkeypatch):
    captured: dict[str, object] = {}

    def fake_send_with_key(*, messages, max_tokens, api_key):
        captured["api_key"] = api_key
        captured["max_tokens"] = max_tokens
        captured["messages"] = messages
        return {"result": {}}

    monkeypatch.setattr(deepseek_transport, "_send_with_key", fake_send_with_key)
    deepseek_transport.send_deepseek_v4_flash_with_api_key(
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
        api_key="test-only-key",
    )
    assert captured == {
        "api_key": "test-only-key",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }
