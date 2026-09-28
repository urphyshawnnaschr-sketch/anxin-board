from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import deepseek_transport as transport  # noqa: E402
from app import model_settings_api  # noqa: E402
from app import project_profile_generation as profile_generation  # noqa: E402
from app.secret_store import SecretNotFoundError  # noqa: E402


def test_explicit_credential_transport_does_not_use_process_env(monkeypatch):
    captured = {}

    def fake_send(*, messages, max_tokens, api_key):
        captured.update(messages=messages, max_tokens=max_tokens, api_key=api_key)
        return {"ok": True}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-value-must-not-be-used")
    monkeypatch.setattr(transport, "_send_with_key", fake_send)
    result = transport.send_deepseek_v4_flash_with_api_key(
        messages=[{"role": "user", "content": "x"}],
        max_tokens=7,
        api_key="explicit-windows-credential",
    )
    assert result == {"ok": True}
    assert captured["api_key"] == "explicit-windows-credential"
    assert captured["max_tokens"] == 7


def test_model_settings_write_never_echoes_secret(monkeypatch):
    writes = []
    deletes = []

    class FakeStore:
        def put(self, ref, secret):
            writes.append((ref, secret))

        def delete(self, ref):
            deletes.append(ref)
            raise SecretNotFoundError()

    monkeypatch.setattr(model_settings_api, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model_settings_api, "_secret_store_factory", FakeStore)
    payload = model_settings_api.DeepSeekCredentialPayload(api_key="secret-value-for-test")
    result = model_settings_api.configure_deepseek_credential(payload, object())
    assert writes == [("deepseek-api-key", "secret-value-for-test")]
    assert deletes == ["deepseek-model-id"]
    assert result == {
        "provider": "deepseek",
        "credential_ref": "deepseek-api-key",
        "configured": True,
        "selected_model": None,
        "connected": False,
    }
    assert "secret-value-for-test" not in str(result)


def test_profile_ai_result_projects_to_editable_candidate_without_fake_confirmation():
    result = {
        "project_summary": "真实项目摘要",
        "features": [
            {
                "name": "PRD 导入",
                "description": "导入并确认 PRD 解析版本",
                "source_type": "prd_fact",
                "evidence_ids": ["prd-001"],
                "candidate": True,
            }
        ],
        "module_path_candidates": [
            {
                "path": "apps/backend/app/prd.py",
                "description": "PRD 导入对应的后端实现",
                "implementation_scope": "后端",
                "source_type": "git_fact",
                "evidence_ids": ["repo-001"],
                "candidate": True,
            }
        ],
        "terminology": [],
        "exclusion_rule_candidates": [],
        "analysis_rule_candidates": [],
        "unmapped_areas": [],
        "evidence_refs": ["prd-001", "repo-001"],
    }
    content = profile_generation._to_profile_content(result)
    assert content.schema_version == "project_profile_manual_v1"
    assert len(content.modules) == 1
    assert content.modules[0].name == "PRD 导入"
    assert content.modules[0].paths[0].pattern == "apps/backend/app/prd.py"
    assert content.modules[0].paths[0].type == "backend"
