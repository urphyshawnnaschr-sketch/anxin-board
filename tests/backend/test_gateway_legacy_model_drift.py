"""A prepared legacy-model call must fail closed as provider incompatibility.

If a durable Model Call was prepared against the retired model identity, the
Gateway must not attempt authority resolution or send. It must surface a clear
provider-compat block and leave the durable task queued (no running claim, no
send attempt), so the operator re-prepares under the current model.
"""

from __future__ import annotations

from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import model_gateway  # noqa: E402


def test_manifest_with_legacy_model_is_blocked_at_provider_compat():
    manifest = {
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "provider": "deepseek",
        "model_id": "deepseek-v4-flash",
        "model_version": "DeepSeek-V4-Flash-0731",
    }
    capability = {
        "provider": "deepseek",
        "model_id": model_gateway._ACTIVATION_MODEL_ID,
        "model_version": model_gateway._ACTIVATION_MODEL_VERSION,
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
    }
    assert model_gateway._manifest_matches_activation(manifest, capability) is False


def test_manifest_with_qualified_model_matches_activation():
    manifest = {
        "task_type": "daily_report_generate",
        "output_schema_version": "daily-report/1.0",
        "provider": "deepseek",
        "model_id": model_gateway._ACTIVATION_MODEL_ID,
        "model_version": model_gateway._ACTIVATION_MODEL_VERSION,
    }
    capability = {
        "provider": model_gateway._ACTIVATION_PROVIDER,
        "model_id": model_gateway._ACTIVATION_MODEL_ID,
        "model_version": model_gateway._ACTIVATION_MODEL_VERSION,
        "task_type": model_gateway._ACTIVATION_TASK_TYPE,
        "output_schema_version": model_gateway._ACTIVATION_OUTPUT_SCHEMA_VERSION,
    }
    assert model_gateway._manifest_matches_activation(manifest, capability) is True
