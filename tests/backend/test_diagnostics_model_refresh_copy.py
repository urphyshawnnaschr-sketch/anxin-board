"""Operator-facing copy for model rollover must name the current qualified model.

The 'not currently listed' diagnostic previously hard-coded the retired model
name; after the rollover that copy would itself be misleading. It must refer to
the catalog-qualified model and stay within the safe diagnostic allowlist.
"""

from __future__ import annotations

from pathlib import Path
import sys

from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_model_catalog  # noqa: E402
from app.model_gateway_diagnostics import safe_authority_error  # noqa: E402


def test_not_currently_listed_message_names_qualified_model():
    exc = safe_authority_error(
        {
            "code": "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT",
            "cause_code": "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED",
        }
    )
    assert isinstance(exc, HTTPException)
    detail = exc.detail
    assert detail["code"] == "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT"
    assert detail["cause_code"] == "DEEPSEEK_AUTHORITY_MODEL_NOT_CURRENTLY_LISTED"
    assert deepseek_model_catalog.REPORT_MODEL_ID in detail["message"]
    assert ("deepseek" + "-v4-flash") not in detail["message"]
    assert detail["stage"] == "model_availability"


def test_version_changed_message_stays_safe_and_generic():
    exc = safe_authority_error(
        {
            "code": "MODEL_PROVIDER_QUALIFICATION_NOT_CURRENT",
            "cause_code": "DEEPSEEK_AUTHORITY_MODEL_VERSION_CHANGED",
        }
    )
    detail = exc.detail
    assert detail["stage"] == "model_metadata"
    assert "sk-" not in detail["message"]
