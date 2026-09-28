from types import SimpleNamespace
import pytest
from app.brownfield_spike_session import SpikeSession
from app.brownfield_atlas_spike import SpikeContractError


def setup_session(tmp_path, scope=None, execute=None):
    calls = []
    cap = SimpleNamespace(provider='deepseek', model_id='test', model_version='test', context_window_tokens=100000, max_output_tokens=8000)
    live = SimpleNamespace(get_capability=lambda **kw: cap, execute_with_credential=execute or (lambda req, key: calls.append(req) or 'receipt'))
    s = SpikeSession(live, cap, lambda: (scope or {'head': 'a'}), lambda: calls.append('credential') or 'fake', tmp_path / 'claim', max_output=4000)
    return s, calls, cap


def test_session_revalidates_before_credential_and_counts_ambiguous_send(tmp_path):
    scope = {'head': 'a'}
    s, calls, cap = setup_session(tmp_path, scope)
    scope['head'] = 'b'
    with pytest.raises(SpikeContractError, match='SCOPE_CHANGED'):
        s.send('orientation', ({'role':'user','content':'x'},))
    assert calls == []
    assert not (tmp_path / 'claim').exists()


def test_session_blocks_reentry_and_does_not_retry(tmp_path):
    def ambiguous(req, key):
        raise TimeoutError()
    s, calls, _ = setup_session(tmp_path, execute=ambiguous)
    with pytest.raises(TimeoutError):
        s.send('orientation', ({'role':'user','content':'x'},))
    assert s.attempts == 1
    with pytest.raises(SpikeContractError, match='SESSION_STOPPED'):
        s.send('orientation', ({'role':'user','content':'x'},))
    second, _, _ = setup_session(tmp_path)
    with pytest.raises(SpikeContractError, match='ALREADY_CLAIMED'):
        second.send('orientation', ({'role':'user','content':'x'},))


def test_session_budget_is_checked_before_claim_or_key(tmp_path):
    s, calls, cap = setup_session(tmp_path)
    cap.context_window_tokens = 21000
    # Changing capabilities itself invalidates the prepared session.
    with pytest.raises(SpikeContractError):
        s.send('orientation', ({'role':'user','content':'x'*5000},))
    assert calls == []


def test_actual_wire_over_budget_without_capability_drift(tmp_path):
    s, calls, _ = setup_session(tmp_path)
    with pytest.raises(SpikeContractError, match='REQUEST_OVER_BUDGET'):
        s.send('orientation', ({'role': 'user', 'content': 'x' * 100000},))
    assert calls == []
    assert not (tmp_path / 'claim').exists()


def test_session_does_not_refreeze_changed_prepared_scope(tmp_path):
    s, _, cap = setup_session(tmp_path)
    with pytest.raises(SpikeContractError, match='SCOPE_CHANGED'):
        SpikeSession(s.live, cap, lambda: {'head': 'b'}, lambda: 'fake', tmp_path/'other', expected_scope={'head':'a'})


def test_completed_send_still_checks_scope_and_blocks_further_work(tmp_path):
    scope = {'head':'a'}
    def drift(req, key):
        scope['head'] = 'b'
        return 'receipt'
    s, calls, _ = setup_session(tmp_path, scope, drift)
    with pytest.raises(SpikeContractError, match='SCOPE_CHANGED'):
        s.send('orientation', ({'role':'user','content':'x'},))
    assert s.attempts == 1
    with pytest.raises(SpikeContractError, match='SESSION_STOPPED'):
        s.send('orientation', ({'role':'user','content':'x'},))


def test_adapter_wire_estimator_admits_exact_envelope_before_any_effect(tmp_path):
    s, calls, _ = setup_session(tmp_path)
    s.live.estimate_request_bytes = lambda **kw: 100000
    with pytest.raises(SpikeContractError, match='REQUEST_OVER_BUDGET'):
        s.send('orientation', ({'role': 'user', 'content': 'small message'},))
    assert calls == []
    assert not (tmp_path / 'claim').exists()


@pytest.mark.parametrize('invalid_size', [0, -1, True, 2.5, None])
def test_adapter_wire_estimator_cannot_bypass_admission(tmp_path, invalid_size):
    s, calls, _ = setup_session(tmp_path)
    s.live.estimate_request_bytes = lambda **kw: invalid_size
    with pytest.raises(SpikeContractError, match='WIRE_ESTIMATE_INVALID'):
        s.send('orientation', ({'role': 'user', 'content': 'small'},))
    assert calls == []
