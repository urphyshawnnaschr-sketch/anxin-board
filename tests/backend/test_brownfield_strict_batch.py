"""Complete-batch collector contract; every provider interaction is a local double."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from app import brownfield_strict_adapter as adapter
from app.brownfield_strict_wire import build_strict_wire
from test_brownfield_strict_adapter import envelope, single_request, setup


def batch_response(req, copies=1):
    rewritten, _ = build_strict_wire(req.messages)
    batch = json.loads(rewritten[-1]['content'])['required_output']
    response = envelope()
    response['choices'][0]['message']['tool_calls'] = [
        {'id': f'complete-{i}', 'type': 'function', 'function': {'name': adapter.TOOL_NAME, 'arguments': json.dumps(batch)}} for i in range(copies)]
    return response


@pytest.mark.parametrize('kind', ['orientation', 'verification'])
@pytest.mark.parametrize('copies', [1, 2, 16])
def test_complete_single_result_or_identical_calls_no_usage_multiplication(monkeypatch, kind, copies):
    req = single_request(kind)
    response = batch_response(req, copies)
    live, sent, _ = setup(monkeypatch, payload=response)
    result = live.execute_with_credential(req, 'synthetic')
    assert len(result.result['modules' if kind == 'orientation' else 'requirements']) == 1
    assert len(sent) == 1
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (2, 3, 5)
    metadata = adapter.normalization_metadata(result)
    assert metadata['calls_received'] == copies and metadata['calls_collapsed'] == copies - 1
    assert metadata['tool_calls_normalized'] == 0
    schema = json.loads(sent[0][1]['content'])['tools'][0]['function']['parameters']
    assert set(schema['required']) == ({f'seed_{i}' for i in range(12)} if kind == 'orientation' else {f'evidence_{i}' for i in range(6)} | {'status','rationale'})
    assert schema['additionalProperties'] is False


@pytest.mark.parametrize('kind', ['orientation', 'verification'])
@pytest.mark.parametrize('mutation', ['extra', 'rationale', 'slot_order', 'duplicate_id', 'wrong_name', 'too_many', 'float', 'bool'])
def test_complete_batches_cannot_be_merged_or_fuzzily_compared(monkeypatch, kind, mutation):
    req = single_request(kind)
    response = batch_response(req, 17 if mutation == 'too_many' else 2)
    calls = response['choices'][0]['message']['tool_calls']
    slot = 'seed' if kind == 'orientation' else 'evidence'
    if mutation == 'duplicate_id': calls[1]['id'] = calls[0]['id']
    elif mutation == 'wrong_name': calls[1]['function']['name'] = 'other'
    elif mutation != 'too_many':
        batch = json.loads(calls[1]['function']['arguments'])
        if mutation in {'missing', 'partial_second'}: batch.pop(slot+'_0')
        elif mutation == 'extra': batch['row_999'] = deepcopy(batch)
        elif mutation == 'rationale': batch['rationale'] = batch.get('rationale', '') + ' different'
        elif mutation == 'slot_order':
            first = json.loads(calls[0]['function']['arguments'])
            first[slot+'_0'] = 0
            batch[slot+'_1'] = 0
            if kind == 'verification': first['status'] = batch['status'] = 'partial'
            calls[0]['function']['arguments'] = json.dumps(first)
        elif mutation == 'float': batch[slot+'_0'] = -1.0
        elif mutation == 'bool': batch[slot+'_0'] = False
        calls[1]['function']['arguments'] = json.dumps(batch)
    live, sent, _ = setup(monkeypatch, payload=response)
    with pytest.raises(HTTPException) as error:
        live.execute_with_credential(req, 'synthetic')
    assert len(sent) == 1 and error.value.status_code == 502
    assert error.value.detail.get('diagnostic', {}).get('expected_row_count') == 1


def test_complementary_partial_fields_cannot_form_a_complete_result(monkeypatch):
    req = single_request('orientation')
    response = batch_response(req, 2)
    for i, call in enumerate(response['choices'][0]['message']['tool_calls']):
        value = json.loads(call['function']['arguments'])
        call['function']['arguments'] = json.dumps({k:v for k,v in value.items() if (k == 'seed_0') == (i == 0)})
    live, sent, _ = setup(monkeypatch, payload=response)
    with pytest.raises(HTTPException): live.execute_with_credential(req, 'synthetic')
    assert len(sent) == 1


@pytest.mark.parametrize('mutation', ['slot_positions', 'changed_citation'])
def test_each_independently_valid_result_must_match_exact_slots_and_citations(monkeypatch, mutation):
    from dataclasses import replace
    req = single_request('verification')
    payload = json.loads(req.messages[-1]['content'])
    payload['source_evidence'].append(dict(payload['source_evidence'][0], evidence_index=1))
    req = replace(req, messages=(req.messages[0], dict(req.messages[1], content=json.dumps(payload))))
    response = batch_response(req, 2)
    calls = response['choices'][0]['message']['tool_calls']
    batch = json.loads(calls[0]['function']['arguments'])
    batch.update(status='partial', evidence_0=0)
    if mutation == 'slot_positions': batch['evidence_1'] = 1
    calls[0]['function']['arguments'] = json.dumps(batch)
    if mutation == 'slot_positions': batch.update(evidence_0=1, evidence_1=0)
    else: batch['evidence_0'] = 1
    calls[1]['function']['arguments'] = json.dumps(batch)
    live, sent, _ = setup(monkeypatch, payload=response)
    with pytest.raises(HTTPException, match='BATCH_CONFLICT'): live.execute_with_credential(req, 'synthetic')
    assert len(sent) == 1


def test_equivalent_json_key_order_folds_but_counts_field_and_call_normalization_separately(monkeypatch):
    req = single_request('orientation')
    response = batch_response(req, 2)
    fn = response['choices'][0]['message']['tool_calls'][1]['function']
    batch = json.loads(fn['arguments'])
    fn['arguments'] = json.dumps(dict(reversed(list(batch.items()))))[:-1] + ',"seed_0":' + json.dumps(batch['seed_0']) + '}'
    live, sent, _ = setup(monkeypatch, payload=response)
    result = live.execute_with_credential(req, 'synthetic')
    assert adapter.normalization_metadata(result) == {'policy': 'identical-typed-duplicates/1', 'duplicate_fields_removed': 1, 'tool_calls_normalized': 1, 'calls_received': 2, 'calls_collapsed': 1, 'row_occurrences_received': 2, 'duplicate_rows_removed': 1, 'unique_rows': 1, 'calls_policy': 'identical-complete-single-result/1'}
    assert len(sent) == 1


def test_new_batch_policy_identity_cannot_reuse_old_task_stage():
    import sqlite3
    from app import brownfield_baseline_store as store
    from app import brownfield_baseline as core
    assert core.TRANSPORT_POLICY == adapter.TRANSPORT_POLICY
    current_identity = {'scope': 'synthetic', 'transport_policy': deepcopy(adapter.TRANSPORT_POLICY)}
    old_identity = deepcopy(current_identity)
    old_identity['transport_policy']['format'] = 'strict-chat-single-binding/7'
    assert core.digest(current_identity) != core.digest(old_identity)
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        store.ensure_schema(conn)
        old = store.create_task(conn, project_id=1, authorization_nonce='old-synthetic', identity=old_identity, max_calls=3)
        store.claim_stage(conn, task_id=old['task_id'], stage_key='orientation/0/0', input_hash='same-test-input', wire_bytes=123)
        store.finish_stage(conn, task_id=old['task_id'], stage_key='orientation/0/0', status='succeeded', result={'value': 'synthetic'})
        store.finish_task(conn, old['task_id'], status='failed_after_send', error_code='SYNTHETIC_KNOWN_FAILURE')
        new = store.create_task(conn, project_id=1, authorization_nonce='new-explicit-synthetic', identity=current_identity, max_calls=3)
        assert store.reuse_successful_stage(conn, task_id=new['task_id'], stage_key='orientation/0/0', input_hash='same-test-input', wire_bytes=123) is None
        assert store.list_stages(conn, new['task_id']) == []
        assert store.get_stage(conn, old['task_id'], 'orientation/0/0')['status'] == 'succeeded'
