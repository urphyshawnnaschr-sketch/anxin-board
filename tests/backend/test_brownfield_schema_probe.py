import importlib.util
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest


@pytest.mark.parametrize('outcome, expected', [('valid', 0), ('invalid', 92), ('unknown', 91)])
def test_schema_probe_is_synthetic_single_send_and_cannot_reenter(monkeypatch, tmp_path, capsys, outcome, expected):
    monkeypatch.setenv('ANXINBOARD_OWNER_LIVE_ATLAS_SPIKE', 'YES')
    monkeypatch.setenv('ANXINBOARD_ATLAS_AUTHORIZATION_ID', 'synthetic-schema-probe')
    path = Path(__file__).resolve().parents[2] / 'ops/spikes/run_brownfield_schema_probe.py'
    spec = importlib.util.spec_from_file_location('synthetic_probe', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    cap = SimpleNamespace(provider='deepseek', model_id='deepseek-flash', model_version='deepseek-flash', context_window_tokens=1000000, max_output_tokens=8000)
    calls = []
    def execute(request, key):
        calls.append(request)
        if outcome == 'unknown':
            raise HTTPException(502, detail={'code': 'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN'})
        return SimpleNamespace(result={'values': [0, 1]} if outcome == 'valid' else {'values': [0, 1, 2]})
    live = SimpleNamespace(get_capability=lambda **kw: cap, execute_with_credential=execute)
    monkeypatch.setattr(probe, 'build_brownfield_responses_adapter', lambda: live)
    monkeypatch.setattr(probe.core, '_read_provider_credential', lambda: 'synthetic')
    monkeypatch.setattr(probe, 'get_db_path', lambda: tmp_path / 'unused.db')
    assert probe.main() == expected
    assert len(calls) == 1
    assert not (tmp_path / 'unused.db').exists()
    output = capsys.readouterr().out
    assert 'CUSTOMER_DATA_SENT=NO' in output
    assert ('SCHEMA_PROBE=PASS' in output) == (outcome == 'valid')
    assert probe.main() == 92
    assert len(calls) == 1
