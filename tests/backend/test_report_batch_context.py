"""Frozen redacted ranges and batch-scoped result provenance."""
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps' / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import context_token_framing as framing, model_execution_results as results
import test_context_token_framing as framing_tests
import test_model_execution_results as result_tests
from test_model_execution_results import ledger_state


def _selection(specs, target='git:1', start=1, end=8):
    encoded = framing._canonical_bytes(specs[target]['body'])
    return {'chunks': [{'target': target, 'start': start, 'end': end,
                        'body_hash': hashlib.sha256(encoded).hexdigest()}],
            'evidence_ids': [target] if target != 'profile' else []}


def test_range_accounting_matches_actual_payload_and_excludes_other_targets(monkeypatch):
    specs = framing_tests._specs(unicode_body=True)
    framing_tests._install(monkeypatch, specs)
    selected = _selection(specs, end=5)
    monkeypatch.setattr(framing, '_batch_selection', lambda _: selected)
    accounting = framing_tests._run()
    payload = framing._materialize_context_payload_transient(model_call_id=7, budget_record={})
    assert accounting['framed_payload_hash'] == payload['framed_payload_hash']
    assert accounting['framed_payload_utf8_bytes'] == len(payload['payload'])
    frame = json.loads(payload['payload'])
    assert frame['target'] == 'git:1'
    assert frame['body']['text'] == framing._canonical_bytes(specs['git:1']['body']).decode()[1:5]
    assert b'prd:1' not in payload['payload']


@pytest.mark.parametrize('mutation', ['hash', 'range', 'duplicate', 'evidence'])
def test_bad_frozen_range_fails_closed(monkeypatch, mutation):
    specs = framing_tests._specs()
    framing_tests._install(monkeypatch, specs)
    selected = _selection(specs)
    if mutation == 'hash': selected['chunks'][0]['body_hash'] = '0' * 64
    if mutation == 'range': selected['chunks'][0]['end'] = 99999
    if mutation == 'duplicate': selected['chunks'] *= 2
    if mutation == 'evidence': selected['evidence_ids'] = ['prd:1']
    monkeypatch.setattr(framing, '_batch_selection', lambda _: selected)
    with pytest.raises(HTTPException): framing_tests._run()
    with pytest.raises(HTTPException): framing._materialize_context_payload_transient(model_call_id=7, budget_record={})


def test_cross_batch_evidence_rejected_on_write_and_read(ledger_state, monkeypatch):
    from app import report_generation_batches as batches
    call = result_tests._prepare(ledger_state)
    monkeypatch.setattr(batches, 'get_batch_call_selection', lambda _: {'evidence_ids': []})
    with pytest.raises(HTTPException):
        results.record_model_execution_result(model_call_id=call['model_call_id'], receipt=result_tests._receipt(ledger_state, call))
    monkeypatch.setattr(batches, 'get_batch_call_selection', lambda _: None)
    saved = results.record_model_execution_result(model_call_id=call['model_call_id'], receipt=result_tests._receipt(ledger_state, call))
    monkeypatch.setattr(batches, 'get_batch_call_selection', lambda _: {'evidence_ids': []})
    with pytest.raises(HTTPException): results.get_model_execution_result(saved['model_result_id'])


def test_durable_batch_result_read_does_not_replay_git(ledger_state, monkeypatch):
    from app import report_generation_batches as batches
    call = result_tests._prepare(ledger_state)
    monkeypatch.setattr(batches, 'get_batch_call_selection', lambda _: {'evidence_ids': [ledger_state['evidence_id']]})
    saved = results.record_model_execution_result(model_call_id=call['model_call_id'], receipt=result_tests._receipt(ledger_state, call))
    monkeypatch.setattr(results, 'build_context_candidate_set', lambda _: pytest.fail('durable result read must not replay Git'))
    assert results.get_model_execution_result(saved['model_result_id']) == saved


def test_deterministic_aggregate_rebinds_complete_children(ledger_state, monkeypatch):
    from app import report_generation_batches as batches
    parent = result_tests._prepare(ledger_state, call_prepare_key='parent')
    calls = [result_tests._prepare(ledger_state, call_prepare_key=f'child-{n}') for n in range(2)]
    monkeypatch.setattr(batches, 'get_batch_call_selection', lambda _: None)
    monkeypatch.setattr(batches, 'get_batch_parent_call_ids', lambda _: [c['model_call_id'] for c in calls])
    monkeypatch.setattr(batches, 'get_plan', lambda _: {'denied_target_count': 1, 'unsupported_context_sources': []})
    receipts = [result_tests._receipt(ledger_state, c) for c in calls]
    receipts[1]['result']['feature_progress'][0]['stage'] = '已完成'
    for ordinal, receipt in enumerate(receipts):
        receipt['result']['plain_summary'] = '安全的局部分析。' * 1000
        base = receipt['result']['feature_progress'][0]
        receipt['result']['feature_progress'].extend(
            [{**base, 'feature': f'批{ordinal}功能{n}'} for n in range(109)]
        )
    children = [results.record_model_execution_result(model_call_id=c['model_call_id'], receipt=r) for c, r in zip(calls, receipts)]
    ids = [c['model_result_id'] for c in children]
    with pytest.raises(HTTPException): results.record_aggregate_model_execution_result(parent['model_call_id'], ids[:1])
    with pytest.raises(HTTPException): results.record_aggregate_model_execution_result(parent['model_call_id'], ids[::-1])
    merged = results.record_aggregate_model_execution_result(parent['model_call_id'], ids)
    assert merged['schema_version'] == 'model_execution_aggregate_v1'
    assert merged['provider_response_id'].startswith('local-aggregate:')
    assert merged['prompt_tokens'] == 22
    assert len(merged['validated_result']['feature_progress']) == 220
    assert sum('局部结论' in item['feature'] for item in merged['validated_result']['feature_progress']) == 2
    assert len(merged['validated_result']['plain_summary']) > 12000
    assert '排除 1 项' in merged['validated_result']['plain_summary']
    assert '[批次 2]' in merged['validated_result']['plain_summary']
    assert results.record_aggregate_model_execution_result(parent['model_call_id'], ids)['model_result_id'] == merged['model_result_id']
    assert results.get_model_execution_result(merged['model_result_id']) == merged
    with results.get_connection() as conn:
        conn.execute('UPDATE model_execution_aggregate_sources SET aggregate_hash = ? WHERE parent_result_id = ?', ('0' * 64, merged['model_result_id']))
    with pytest.raises(HTTPException): results.get_model_execution_result(merged['model_result_id'])


def test_fragment_instructions_are_in_actual_budget_messages():
    from app import model_provider_gateway as gateway
    from unittest.mock import patch
    payload = json.dumps({'body': {'encoding': 'canonical_json_fragment_v1', 'text': 'partial'}}).encode()
    with patch.object(gateway, '_task_envelope', return_value={}):
        messages = gateway._build_messages(manifest={}, payload=payload, instruction='contract')
    assert 'partial batch' in messages[0]['content']
    assert 'does not prove missing implementation' in messages[0]['content']
