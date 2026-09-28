import json
from contextlib import closing

import pytest
from fastapi import HTTPException

from app import brownfield_baseline as core
from app import brownfield_baseline_api as api
from app import brownfield_baseline_store as store
from test_brownfield_baseline_api import setup_api, create


def test_expected_rows_are_safe_metadata_not_raw_provider_content():
    error = HTTPException(502, detail={'diagnostic': {
        'expected_row_count': 3, 'tool_call_count': 1,
        'raw_body': 'private-marker', 'rationale': 'private-marker',
        'arguments': 'private-marker'}})
    assert core.safe_diagnostic(error) == {'expected_row_count': 3, 'tool_call_count': 1}


@pytest.mark.parametrize('value', [True, -1, 2**31, 'private-marker', None, []])
def test_expected_rows_reject_non_bounded_integers(value):
    assert core.safe_diagnostic(HTTPException(502, detail={
        'diagnostic': {'expected_row_count': value}})) == {}


def test_terminal_task_exposes_only_bounded_failure_metadata(setup_api):
    ctx = setup_api
    task = create(ctx)
    code = 'PROFILE_GENERATION_STRICT_TOOL_COUNT_INVALID'
    with closing(ctx.connect()) as conn:
        store.claim_stage(conn, task_id=task['task_id'], stage_key='orientation/0/2',
                          input_hash='d'*64, wire_bytes=42)
        store.finish_stage(conn, task_id=task['task_id'], stage_key='orientation/0/2',
                           status='failed_after_send', error_code=code,
                           result={'diagnostic': {'expected_row_count': 3, 'tool_call_count': 1,
                               'tool_call_ordinal': True, 'raw_body': 'private-marker'}})
        store.finish_task(conn, task['task_id'], status='failed_after_send', error_code=code)
    observed = api.latest_atlas_task(9)['task']
    assert observed['failure_diagnostic'] == {'expected_row_count': 3, 'tool_call_count': 1}
    assert observed['resume_available'] is False
    assert 'private-marker' not in json.dumps(observed)
    assert len(ctx.threads) == 1  # querying cannot start another worker


@pytest.mark.parametrize('reason', ['ROWS_INVALID', 'FIELDS_INVALID', 'RATIONALE_INVALID',
    'INDEX_INVALID', 'STATUS_INVALID', 'STATUS_CITATION_INVALID', 'MESSAGES_INVALID', 'CONTEXT_INVALID'])
def test_wire_reason_is_a_fixed_safe_category(reason):
    assert core.safe_diagnostic(HTTPException(502, detail={'diagnostic': {
        'wire_reason': reason, 'row_ordinal': 0, 'slot_ordinal': 4,
        'missing_field_count': 1, 'extra_field_count': 0,
        'missing_row_count': 0, 'extra_row_count': 0,
        'raw_value': 'private-marker', 'raw_key': 'private-marker'}})) == {
        'wire_reason': reason, 'row_ordinal': 0, 'slot_ordinal': 4,
        'missing_field_count': 1, 'extra_field_count': 0,
        'missing_row_count': 0, 'extra_row_count': 0}


@pytest.mark.parametrize('value', ['private-marker', 'constructor', True, None, {}, ['ROWS_INVALID']])
def test_untrusted_wire_reason_is_discarded(value):
    assert core.safe_diagnostic(HTTPException(502, detail={'diagnostic': {'wire_reason': value}})) == {}


@pytest.mark.parametrize('key', ['row_ordinal', 'slot_ordinal', 'missing_row_count',
    'extra_row_count', 'missing_field_count', 'extra_field_count'])
@pytest.mark.parametrize('value', [True, -1, 2**31, 1.5, 'private-marker', {}, []])
def test_wire_positions_and_counts_must_be_bounded_integers(key, value):
    assert core.safe_diagnostic(HTTPException(502, detail={'diagnostic': {key: value}})) == {}


def test_wire_diagnostic_survives_durable_terminal_read_without_raw_content(setup_api):
    ctx = setup_api
    task = create(ctx)
    code = 'PROFILE_GENERATION_STRICT_WIRE_INVALID'
    safe = {'wire_reason': 'INDEX_INVALID', 'wire_rule': 'INTEGER_RANGE', 'tool_call_ordinal': 1,
            'row_ordinal': 0, 'slot_ordinal': 3, 'expected_row_count': 2, 'tool_call_count': 2}
    with closing(ctx.connect()) as conn:
        store.claim_stage(conn, task_id=task['task_id'], stage_key='orientation/0/0',
                          input_hash='f'*64, wire_bytes=42)
        store.finish_stage(conn, task_id=task['task_id'], stage_key='orientation/0/0',
                           status='failed_after_send', error_code=code,
                           result={'diagnostic': {**safe, 'raw_body': 'private-marker'}})
        store.finish_task(conn, task_id=task['task_id'], status='failed_after_send', error_code=code)
    observed = api.latest_atlas_task(9)['task']
    assert observed['failure_diagnostic'] == safe
    assert observed['resume_available'] is False
    assert 'private-marker' not in json.dumps(observed)
    assert len(ctx.threads) == 1


@pytest.mark.parametrize('rule', ['INTEGER_TYPE', 'INTEGER_RANGE'])
def test_exact_index_rule_survives_safe_filter(rule):
    value = {'wire_reason': 'INDEX_INVALID', 'wire_rule': rule}
    assert core.safe_diagnostic(HTTPException(502, detail={'diagnostic': value})) == value


@pytest.mark.parametrize('rule', ['STRING_TYPE', 'private-marker', 'constructor', True, {}, []])
def test_rule_must_belong_to_known_reason(rule):
    value = {'wire_reason': 'INDEX_INVALID', 'wire_rule': rule}
    assert core.safe_diagnostic(HTTPException(502, detail={'diagnostic': value})) == {'wire_reason': 'INDEX_INVALID'}

@pytest.mark.parametrize('rule', ['STATUS_CITATIONS', 'UNKNOWN_WITH_CITATIONS', 'POSITIVE_WITHOUT_CITATIONS', 'POSITIVE_FIRST_SLOT_EMPTY'])
def test_citation_rules_remain_safe_and_visible_in_terminal_api(setup_api,rule):
    ctx=setup_api;task=create(ctx);code='PROFILE_GENERATION_STRICT_WIRE_INVALID'
    expected={'wire_reason':'STATUS_CITATION_INVALID','wire_rule':rule}
    with closing(ctx.connect()) as conn:
        store.claim_stage(conn,task_id=task['task_id'],stage_key='verify/0/m1/0/0',input_hash='e'*64,wire_bytes=42)
        store.finish_stage(conn,task_id=task['task_id'],stage_key='verify/0/m1/0/0',status='failed_after_send',error_code=code,result={'diagnostic':dict(expected,raw_body='private-marker')})
        store.finish_task(conn,task['task_id'],status='failed_after_send',error_code=code)
    result=api.latest_atlas_task(9)['task']
    assert result['failure_diagnostic']==expected
    assert result['resume_available'] is False
    assert 'private-marker' not in json.dumps(result)
    assert len(ctx.threads)==1
