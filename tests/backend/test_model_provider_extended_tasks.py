\
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_transport  # noqa: E402
from app.deepseek_extended_provider_adapter import build_deepseek_extended_provider_adapter  # noqa: E402
from app.model_provider_contract import ProviderCredentialRequest  # noqa: E402
from app.model_provider_runtime import resolve_default_model_provider_adapter  # noqa: E402


def test_extended_adapter_exposes_exact_remaining_task_capabilities():
    adapter = build_deepseek_extended_provider_adapter()
    profile = adapter.get_capability(
        task_type="project_profile_build",
        output_schema_version="project-profile-build/1.0",
    )
    contradiction = adapter.get_capability(
        task_type="report_contradiction_check",
        output_schema_version="report-contradiction-check/1.0",
    )
    assert profile.provider == contradiction.provider == "deepseek"
    assert profile.model_id == contradiction.model_id == deepseek_transport.MODEL_ID
    assert profile.model_version == contradiction.model_version == deepseek_transport.MODEL_VERSION


def test_explicit_credential_execution_never_uses_process_env(monkeypatch):
    captured = {}

    def fake_send(*, messages, max_tokens, api_key):
        captured.update(messages=messages, max_tokens=max_tokens, api_key=api_key)
        return {
            "provider": "deepseek",
            "provider_response_id": "resp-profile",
            "actual_model": deepseek_transport.MODEL_ID,
            "provider_runtime_fingerprint": "fp-profile",
            "finish_reason": "stop",
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "result": {"project_summary": "x"},
        }

    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-read")
    monkeypatch.setattr(deepseek_transport, "send_deepseek_v4_flash_with_api_key", fake_send)
    adapter = resolve_default_model_provider_adapter()
    capability = adapter.get_capability(
        task_type="project_profile_build",
        output_schema_version="project-profile-build/1.0",
    )
    request = ProviderCredentialRequest(
        local_task_id="profile-task-1",
        provider=capability.provider,
        model_id=capability.model_id,
        model_version=capability.model_version,
        task_type=capability.task_type,
        output_schema_version=capability.output_schema_version,
        messages=({"role": "user", "content": "x"},),
        max_output_tokens=24_000,
    )
    receipt = adapter.execute_with_credential(request, "windows-credential-value")
    assert receipt.provider_response_id == "resp-profile"
    assert captured["api_key"] == "windows-credential-value"
    assert captured["max_tokens"] == 24_000


def test_profile_business_module_has_no_direct_deepseek_transport_import():
    source = (BACKEND_ROOT / "app" / "project_profile_generation.py").read_text(encoding="utf-8")
    assert "from app.deepseek_transport" not in source
    assert "send_deepseek_v4_flash" not in source
    assert "_transport =" not in source
    assert "resolve_default_model_provider_adapter" in source

