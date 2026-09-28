"""Exercise the real spike orchestration with synthetic repositories and no network."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException
import pytest
from test_brownfield_atlas_spike import indexed, plan

def load_runner(monkeypatch):
    monkeypatch.setenv('ANXINBOARD_OWNER_LIVE_ATLAS_SPIKE', 'YES')
    monkeypatch.setenv('ANXINBOARD_ATLAS_AUTHORIZATION_ID', 'synthetic-authorization')
    path = Path(__file__).resolve().parents[2] / 'ops/spikes/run_brownfield_atlas_live_spike.py'
    spec = importlib.util.spec_from_file_location('synthetic_atlas_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize(('first_seeds', 'expected_calls', 'model'), [([0], 8, 'test'), ([0, 1], 4, 'test'), ([0], 0, 'changed')])
def test_live_orchestration_executes_gap_and_verifies_without_candidate(monkeypatch, tmp_path, capsys, first_seeds, expected_calls, model):
    runner = load_runner(monkeypatch)
    cap = SimpleNamespace(provider='deepseek', model_id=model, model_version=model, context_window_tokens=1000000, max_output_tokens=8000)
    calls = []
    def execute(request, key):
        payload = json.loads(request.messages[1]['content'])
        calls.append(payload['schema_version'])
        if 'repository_atlas' in payload:
            checked = payload['repository_atlas'].get('excluded_checked_path_count', 0)
            result = {'modules': [{'planned_module_id': m['client_id'], 'seed_path_indexes': [payload['repository_atlas']['paths'][0]['path_index']] if checked else first_seeds} for m in payload['planned_modules']]}
        else:
            result = {'requirements': [{'requirement_index': i, 'status':'partial', 'evidence_indexes':[0], 'rationale':'synthetic evidence'} for i, _ in enumerate(payload['planned_module']['requirements'])]}
        return SimpleNamespace(result=result, prompt_tokens=10)
    live = SimpleNamespace(get_capability=lambda **kw: cap, execute_with_credential=execute)
    monkeypatch.setattr(runner, 'scope_identity', lambda: {'head':'a'*40})
    monkeypatch.setattr(runner, 'read_scope', lambda: ({}, {}, {'content':plan()}))
    monkeypatch.setattr(runner, 'build_indexed_repository_map', lambda *args: indexed())
    monkeypatch.setattr(runner, 'EXPECTED_HEAD', 'a'*40)
    monkeypatch.setattr(runner, 'EXPECTED_MODEL', 'test', raising=False)
    for name in ('MIN_SAFE_PATHS','MIN_METADATA_PATHS','MIN_RELATION_EDGES'):
        monkeypatch.setattr(runner, name, 0)
    monkeypatch.setattr(runner, 'build_brownfield_responses_adapter', lambda: live)
    monkeypatch.setattr(runner.core, '_read_provider_credential', lambda: 'synthetic')
    monkeypatch.setattr(runner, 'get_db_path', lambda: tmp_path/'unused.db')
    if expected_calls == 0:
        assert runner.main() == 92
        assert calls == []
        assert 'SPIKE_MODEL_CHANGED' in capsys.readouterr().out
        assert not (tmp_path / 'atlas-spike-claims').exists()
        return
    assert runner.main() == 0
    output = capsys.readouterr().out
    assert 'GAP_CHECK_COMPLETED=YES' in output
    assert 'ARCHITECTURE_PROOF=PASS' in output
    assert len(calls) == expected_calls
    if expected_calls == 4:
        assert 'GAP_ORIENTATION_VALID=LOCAL_EMPTY' in output
        assert 'GAP_REMAINING_SAFE_PATHS=0' in output
    assert 'def add_device' not in output
    assert not (tmp_path/'unused.db').exists()
    # Reentering the same proof does not read credentials or repeat any model call.
    assert runner.main() == 92
    assert len(calls) == expected_calls
