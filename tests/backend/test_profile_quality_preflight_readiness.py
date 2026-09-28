"""Exercise owner preflight reporting without Product state or provider access."""
import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts/profile_reconciliation_provider_quality_preflight.py"
    spec = importlib.util.spec_from_file_location("quality_preflight_readiness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("missing", [0, 1, None, "absent"])
def test_packed_coverage_preserves_zero_and_fails_closed_when_missing(missing):
    retrieval = {
        "requirement_count": 2,
        "requirements_without_source_matches": 0,
        "requirements_with_selected_source": 2,
        "selected_source_requirement_indexes": [0, 1],
    }
    if missing != "absent":
        retrieval["requirements_without_selected_source"] = missing
    batch = {
        "batch_id": "fixture-batch",
        "planned_modules": [{"client_id": "fixture-module"}],
        "repo_evidence": [{"quality_evidence_role": "source_code"}],
        "quality_retrieval": retrieval,
    }
    result = _module()._sanitized_batch(batch, {"requests": [{"batch_id": "fixture-batch"}]})
    assert result["requirements_without_selected_source"] == (2 if missing in (None, "absent") else missing)
    assert result["software_evidence_readiness"] == ("ready" if missing == 0 else "insufficient_source_evidence")


def test_coverage_preflight_is_explicit_and_preserves_local_only_status(monkeypatch, tmp_path):
    import json
    import sys
    module = _module()
    monkeypatch.setattr(module, '_discover_confirmed_v2', lambda: [])
    seen = {}
    local_summary = {'provider_calls': 0, 'provider_dispatch_supported': False,
                     'model_analysis_complete': False, 'candidate_ready': False}
    def prepare(project_id, profile_id, **kwargs):
        seen.update(kwargs)
        return dict(prd_id=1, prd_source_hash='a', plan_profile_id=profile_id,
            plan_content_hash='b', exact_head='c', batches=[], summary={
                **{k: 1 for k in ('execution_identity_hash', 'batch_set_hash', 'batch_count',
                    'provider', 'model_id', 'model_version', 'max_output_tokens',
                    'max_input_bytes', 'total_wire_bytes')},
                'coverage_supplement': local_summary})
    monkeypatch.setattr(module, 'prepare_product_quality_execution', prepare)
    monkeypatch.setattr(module, '_find_module_batch', lambda *args: {})
    monkeypatch.setattr(module, '_sanitized_batch', lambda *args: {'software_evidence_readiness': 'ready'})
    output = tmp_path / 'preflight.json'
    monkeypatch.setattr(sys, 'argv', ['preflight', '--project-id', '2', '--plan-profile-id', '1',
        '--include-coverage-plan', '--output', str(output)])
    assert module.main() == 0
    assert seen['include_coverage_plan'] is True
    assert json.loads(output.read_text())['coverage_supplement'] == local_summary


def test_scope_is_rechecked_after_supplement_planning(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app import project_profile_generation as core, project_profiles, repository_index
    from app import profile_reconciliation_provider_quality as quality
    from app import profile_reconciliation_coverage_plan as coverage
    from app.profile_reconciliation_batches import BatchError
    state = {'head': 'a' * 40}
    profile = {'id': 1, 'source_prd_id': 1, 'content_hash': 'p',
               'content': {'schema_version': 'project_profile_v2'}}
    monkeypatch.setattr(core, '_read_current_inputs', lambda _: (
        {'id': 2}, {'id': 1, 'source_hash': 'prd'}, {'remote_head': state['head']}))
    monkeypatch.setattr(core, 'get_connection', lambda: nullcontext(None))
    monkeypatch.setattr(project_profiles, 'read_current_confirmed_project_profile', lambda *a, **k: profile)
    monkeypatch.setattr(repository_index, 'build_indexed_repository_map', lambda *a: {
        'exact_head': state['head'], 'evidence': []})
    monkeypatch.setattr(quality, 'plan_quality_reconciliation_batches', lambda *a, **k: [])
    execution = {'provider': 'fixture', 'model_id': 'fixture', 'model_version': 'v1', 'batch_set_hash': 'b'}
    monkeypatch.setattr(quality, 'build_quality_execution_plan', lambda *a, **k: execution)
    monkeypatch.setattr(quality, 'public_quality_preflight_summary', lambda *a: {})
    def changed(*a, **k):
        state['head'] = 'b' * 40
        return {}
    monkeypatch.setattr(coverage, 'plan_coverage_batches', changed)
    monkeypatch.setattr(coverage, 'public_coverage_summary', lambda *a: {})
    adapter = SimpleNamespace(get_capability=lambda **k: SimpleNamespace(**{
        key: execution[key] for key in ('provider', 'model_id', 'model_version')},
        max_output_tokens=8000, context_window_tokens=200000))
    with pytest.raises(BatchError, match='PREPARATION_SCOPE_CHANGED'):
        quality.prepare_product_quality_execution(2, 1, adapter=adapter, include_coverage_plan=True)
