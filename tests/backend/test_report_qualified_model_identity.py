"""DeepSeek qualified model identity: one frozen catalog binding for the report chain.

The daily-report path may only run against the currently qualified DeepSeek model
identity. Every layer (authority, transport, gateways, adapters, settings copy)
must bind to the same catalog constants instead of carrying its own literal.
"""

from __future__ import annotations

from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_current_authority  # noqa: E402
from app import deepseek_execution  # noqa: E402
from app import deepseek_model_catalog  # noqa: E402
from app import deepseek_transport  # noqa: E402
from app import model_gateway  # noqa: E402


def test_catalog_exposes_single_qualified_report_model_identity():
    assert deepseek_model_catalog.REPORT_MODEL_ID == "deepseek-flash"
    assert deepseek_model_catalog.REPORT_MODEL_VERSION == "DeepSeek-V4.1-Flash"
    assert deepseek_model_catalog.REPORT_CONTEXT_WINDOW_TOKENS == 1_000_000
    assert deepseek_model_catalog.REPORT_MAX_OUTPUT_TOKENS == 384_000
    assert tuple(deepseek_model_catalog.REPORT_SELECTABLE_MODEL_IDS) == ("deepseek-flash",)


def test_report_chain_layers_bind_to_catalog_identity():
    assert deepseek_current_authority.MODEL_ID == deepseek_model_catalog.REPORT_MODEL_ID
    assert deepseek_current_authority.MODEL_VERSION == deepseek_model_catalog.REPORT_MODEL_VERSION
    assert deepseek_transport.MODEL_ID == deepseek_model_catalog.REPORT_MODEL_ID
    assert deepseek_transport.MODEL_VERSION == deepseek_model_catalog.REPORT_MODEL_VERSION
    assert deepseek_transport.MAX_OUTPUT_TOKENS == deepseek_model_catalog.REPORT_MAX_OUTPUT_TOKENS
    assert model_gateway._ACTIVATION_MODEL_ID == deepseek_model_catalog.REPORT_MODEL_ID
    assert model_gateway._ACTIVATION_MODEL_VERSION == deepseek_model_catalog.REPORT_MODEL_VERSION
    assert model_gateway._EXPECTED_CAPABILITY["model_id"] == deepseek_model_catalog.REPORT_MODEL_ID
    assert model_gateway._EXPECTED_CAPABILITY["model_version"] == deepseek_model_catalog.REPORT_MODEL_VERSION
    assert deepseek_execution._MODEL_ID == deepseek_model_catalog.REPORT_MODEL_ID
    assert deepseek_execution._MODEL_VERSION == deepseek_model_catalog.REPORT_MODEL_VERSION


def test_no_report_layer_keeps_retired_model_literal():
    retired_id = "deepseek" + "-v4-flash"
    retired_version = "DeepSeek" + "-V4-Flash-0731"
    assert deepseek_current_authority.MODEL_ID != retired_id
    assert deepseek_current_authority.MODEL_VERSION != retired_version
    assert deepseek_transport.MODEL_ID != retired_id
    assert deepseek_transport.MODEL_VERSION != retired_version
    assert model_gateway._ACTIVATION_MODEL_ID != retired_id
