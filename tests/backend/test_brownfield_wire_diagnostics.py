"""Safe structural diagnostics using synthetic complete batches only."""
import json

import httpx
import pytest
from fastapi import HTTPException

from app import brownfield_strict_adapter as adapter
from test_brownfield_strict_adapter import single_request
from test_brownfield_strict_batch import batch_response


@pytest.mark.parametrize('mutation,reason,metadata', [
    ('missing', 'FIELDS_INVALID', {'row_ordinal':0, 'missing_field_count': 1, 'extra_field_count': 0}),
    ('extra', 'FIELDS_INVALID', {'row_ordinal': 0, 'missing_field_count': 0, 'extra_field_count': 1}),
    ('blank', 'RATIONALE_INVALID', {'row_ordinal': 0}),
    ('string', 'INDEX_INVALID', {'row_ordinal': 0, 'slot_ordinal': 2}),
    ('range', 'INDEX_INVALID', {'row_ordinal': 0, 'slot_ordinal': 2}),
    ('status', 'STATUS_INVALID', {'row_ordinal': 0}),
    ('citation', 'STATUS_CITATION_INVALID', {'row_ordinal': 0}),
])
def test_failure_reason_without_model_content(mutation, reason, metadata):
    req = single_request('verification' if mutation in {'status', 'citation', 'blank'} else 'orientation')
    response = batch_response(req, 2)
    function = response['choices'][0]['message']['tool_calls'][1]['function']
    batch = json.loads(function['arguments'])
    if mutation == 'missing':
        batch.pop('seed_0')
        first = response['choices'][0]['message']['tool_calls'][0]['function']
        first['arguments'] = json.dumps(batch)
    elif mutation == 'extra': batch['private-key-marker'] = 'private-value-marker'
    elif mutation == 'blank': batch['rationale'] = ' '
    elif mutation == 'string': batch['seed_2'] = 'private-value-marker'
    elif mutation == 'range': batch['seed_2'] = 999999
    elif mutation == 'status': batch['status'] = 'private-value-marker'
    elif mutation == 'citation': batch['status'] = 'partial'
    function['arguments'] = json.dumps(batch)
    with pytest.raises(HTTPException) as raised:
        adapter.receipt(httpx.Response(200, json=response), req)
    detail = raised.value.detail
    assert detail['code'] == 'PROFILE_GENERATION_STRICT_WIRE_INVALID'
    assert detail['diagnostic']['accepted_row_count'] == (0 if mutation == 'missing' else 1)
    assert detail['diagnostic'] == dict(accepted_row_count=0 if mutation == 'missing' else 1, tool_call_count=2, expected_row_count=1,
                                      **({'tool_call_ordinal': 0} if mutation == 'missing' else {'tool_call_ordinal': 1}), wire_reason=reason, wire_rule={'missing':'EXACT_KEYS','extra':'EXACT_KEYS','blank':'NONBLANK_STRING','string':'INTEGER_TYPE','range':'INTEGER_RANGE','status':'STATUS_ENUM','citation':'POSITIVE_WITHOUT_CITATIONS'}[mutation], **metadata)
    assert 'private-' not in json.dumps(detail)
    assert '999999' not in json.dumps(detail)


def test_untyped_value_error_text_is_never_exposed(monkeypatch):
    req = single_request('orientation')
    def fail(*args, **kwargs):
        raise ValueError('private-value-marker')
    monkeypatch.setattr(adapter, 'decode_strict_wire', fail)
    with pytest.raises(HTTPException) as raised:
        adapter.receipt(httpx.Response(200, json=batch_response(req)), req)
    assert raised.value.detail['diagnostic'] == {'tool_call_count': 1, 'expected_row_count': 1,
                                                'tool_call_ordinal': 0, 'accepted_row_count': 0}
    assert 'private-' not in json.dumps(raised.value.detail)


@pytest.mark.parametrize('kind', ['extra_row', 'missing_field', 'nonobject_row', 'nonobject_batch'])
def test_structural_counts_do_not_include_keys(kind):
    from app.brownfield_strict_wire import StrictWireError, decode_strict_wire
    req = single_request('orientation')
    batch = json.loads(batch_response(req)['choices'][0]['message']['tool_calls'][0]['function']['arguments'])
    if kind == 'extra_row':
        batch['private-key-marker'] = {}
        expected = dict(wire_reason='FIELDS_INVALID', row_ordinal=0, missing_field_count=0, extra_field_count=1)
    elif kind == 'missing_field':
        batch.pop('seed_0')
        expected = dict(wire_reason='FIELDS_INVALID', row_ordinal=0, missing_field_count=1, extra_field_count=0)
    elif kind == 'nonobject_row':
        batch = ['private-value-marker']
        expected = dict(wire_reason='FIELDS_INVALID')
    else:
        batch = ['private-value-marker']
        expected = dict(wire_reason='FIELDS_INVALID')
    with pytest.raises(StrictWireError) as raised:
        decode_strict_wire(batch, req.messages)
    expected['wire_rule'] = 'EXACT_KEYS' if kind in {'extra_row','missing_field'} else 'OBJECT_TYPE'
    assert raised.value.diagnostic == expected
    assert 'private-' not in str(raised.value)


def test_diagnostic_constructor_is_fixed_and_bounded():
    from app.brownfield_strict_wire import StrictWireError
    error = StrictWireError('INDEX_INVALID', row_ordinal=True, slot_ordinal=-1,
                            extra_field_count=2**31, private_key='private-value-marker')
    assert error.diagnostic == {'wire_reason': 'INDEX_INVALID'}
    with pytest.raises(ValueError, match='^BROWNFIELD_STRICT_WIRE_DIAGNOSTIC_INVALID$'):
        StrictWireError('private-value-marker')


@pytest.mark.parametrize('field,value,rule', [
    ('seed_0', 'private-marker', 'INTEGER_TYPE'),
    ('seed_0', 999999, 'INTEGER_RANGE'),
    ('rationale', True, 'STRING_TYPE'),
    ('rationale', '  ', 'NONBLANK_STRING'),
    ('rationale', 'x'*1201, 'STRING_LENGTH'),
])
def test_wire_rule_distinguishes_actionable_failure_without_values(field, value, rule):
    req = single_request('verification' if field == 'rationale' else 'orientation')
    response = batch_response(req)
    function = response['choices'][0]['message']['tool_calls'][0]['function']
    batch = json.loads(function['arguments'])
    batch[field] = value
    function['arguments'] = json.dumps(batch)
    with pytest.raises(HTTPException) as raised:
        adapter.receipt(httpx.Response(200, json=response), req)
    assert raised.value.detail['diagnostic']['wire_rule'] == rule
    assert 'private-marker' not in json.dumps(raised.value.detail)
    assert '999999' not in json.dumps(raised.value.detail)


@pytest.mark.parametrize('bad', [{'row_99':{}},{'req_0':{}},{'modules':[]},{'private-secret-key':'private-secret-value'},{}])
def test_wrappers_and_unknown_fields_still_rejected_without_body(bad):
    from app.brownfield_baseline import safe_diagnostic
    req=single_request('orientation');response=batch_response(req,2)
    response['choices'][0]['message']['tool_calls'][1]['function']['arguments']=json.dumps(bad)
    with pytest.raises(HTTPException) as raised:adapter.receipt(httpx.Response(200,json=response),req)
    diagnostic=safe_diagnostic(raised.value)
    assert diagnostic['wire_reason']=='FIELDS_INVALID'
    assert diagnostic['wire_rule']=='EXACT_KEYS'
    assert diagnostic['accepted_row_count']==1 and diagnostic['expected_row_count']==1
    assert diagnostic['tool_call_ordinal']==1 and diagnostic['missing_field_count']==12
    assert diagnostic['extra_field_count']==len(bad)
    assert 'private-secret' not in json.dumps(raised.value.detail)


def test_new_diagnostic_allowlist_rejects_untrusted_types_and_fields():
    from app.brownfield_baseline import safe_diagnostic
    detail = {'wire_reason':'ROWS_INVALID','wire_rule':'EXACT_KEYS', 'row_key_shape':'private-secret',
              'accepted_row_count':True,'numeric_row_key_count':-1,'wrapper_key_count':2**31,
              'other_key_count':'1','private-secret-key':'private-secret-value'}
    assert safe_diagnostic(HTTPException(502, detail={'diagnostic':detail})) == {
        'wire_reason':'ROWS_INVALID','wire_rule':'EXACT_KEYS'}


@pytest.mark.parametrize('value', [True, -1, 2**31, '1', None, [], {}])
def test_row_key_diagnostic_counts_reject_nonbounded_integers(value):
    from app.brownfield_strict_wire import StrictWireError
    from app.brownfield_baseline import safe_diagnostic
    counts = {k:value for k in ('numeric_row_key_count','wrapper_key_count','other_key_count')}
    error = StrictWireError('ROWS_INVALID', wire_rule='EXACT_KEYS', row_key_shape='private-secret', **counts)
    assert error.diagnostic == {'wire_reason':'ROWS_INVALID','wire_rule':'EXACT_KEYS'}
    assert safe_diagnostic(HTTPException(502, detail={'diagnostic':dict(error.diagnostic, accepted_row_count=value)})) == error.diagnostic


def test_row_shape_is_not_accepted_for_unrelated_wire_reason():
    from app.brownfield_strict_wire import StrictWireError
    from app.brownfield_baseline import safe_diagnostic
    error = StrictWireError('INDEX_INVALID', wire_rule='INTEGER_TYPE', row_key_shape='EMPTY_OBJECT')
    assert 'row_key_shape' not in error.diagnostic
    assert 'row_key_shape' not in safe_diagnostic(HTTPException(502, detail={'diagnostic':dict(error.diagnostic, row_key_shape='EMPTY_OBJECT')}))

@pytest.mark.parametrize('status,refs,rule', [
    ('partial', [-1]*6, 'POSITIVE_WITHOUT_CITATIONS'),
    ('implemented', [-1,0,-1,-1,-1,-1], 'POSITIVE_FIRST_SLOT_EMPTY'),
])
def test_citation_failure_branch_has_exact_safe_rule(status, refs, rule):
    req=single_request('verification');response=batch_response(req)
    function=response['choices'][0]['message']['tool_calls'][0]['function']
    value=json.loads(function['arguments']);value.update(status=status,rationale='private-rationale-marker')
    value.update({f'evidence_{i}':v for i,v in enumerate(refs)})
    function['arguments']=json.dumps(value)
    with pytest.raises(HTTPException) as raised:
        adapter.receipt(httpx.Response(200,json=response),req)
    from app.brownfield_baseline import safe_diagnostic
    diagnostic=safe_diagnostic(raised.value)
    assert diagnostic['wire_reason']=='STATUS_CITATION_INVALID'
    assert diagnostic['wire_rule']==rule
    assert diagnostic['accepted_row_count']==0
    assert 'private-rationale-marker' not in json.dumps(raised.value.detail)
    assert adapter.TRANSPORT_POLICY['format']=='strict-chat-single-binding/8'
