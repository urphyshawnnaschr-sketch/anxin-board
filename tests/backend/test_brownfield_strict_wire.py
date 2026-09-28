import json
from copy import deepcopy

import pytest

from app.brownfield_strict_wire import build_strict_wire, decode_strict_wire
from app.brownfield_atlas_spike import (build_orientation_messages, build_requirement_verification_messages,
    validate_orientation_result, validate_requirement_result, SpikeContractError)
from test_brownfield_atlas_spike import plan, fixture_catalog_bundle


def fixture(kind):
    catalog, bundle, _ = fixture_catalog_bundle()
    if kind == 'orientation':
        messages = build_orientation_messages(plan(), catalog, ['m2'])
    else:
        messages = build_requirement_verification_messages(dict(plan()['planned_modules'][1], requirements=plan()['planned_modules'][1]['requirements'][:1]), bundle)
    rewritten, schema = build_strict_wire(messages)
    return messages, rewritten, schema, json.loads(rewritten[-1]['content'])['required_output']


@pytest.mark.parametrize('kind', ['orientation', 'verification'])
def test_schema_fixed_slots_and_all_input_preserved(kind):
    messages, rewritten, schema, result = fixture(kind)
    before, after = [json.loads(m[-1]['content']) for m in (messages, rewritten)]
    assert {k: v for k, v in before.items() if k not in ('required_output', 'required_output_schema')} == {k: v for k, v in after.items() if k not in ('required_output', 'required_output_schema')}
    assert before['required_output_schema'] != schema
    assert after['required_output_schema'] == schema
    def walk(node):
        assert node.get('type') != 'array'
        assert not {'minLength', 'maxLength', 'minItems', 'maxItems'}.intersection(node)
        if node.get('type') == 'object':
            assert set(node['required']) == set(node['properties'])
            assert node['additionalProperties'] is False
            for prop in node['properties'].values(): walk(prop)
    walk(schema)
    assert 'single top-level key modules' not in rewritten[0]['content']
    assert 'top-level key requirements' not in rewritten[0]['content']
    assert decode_strict_wire(result, messages) == decode_strict_wire(result, rewritten)
    assert build_strict_wire(messages) == (rewritten, schema)


def test_orientation_ordinal_binding_and_no_dedup_or_trim():
    messages, _, _, result = fixture('orientation')
    result.update(seed_0=0, seed_1=-1, seed_2=0)
    decoded = decode_strict_wire(result, messages)
    assert decoded['modules'][0] == {'planned_module_id':'m2', 'seed_path_indexes':[0, 0]}
    assert len(decoded['modules']) == 1
    catalog, _, _ = fixture_catalog_bundle()
    with pytest.raises(SpikeContractError):
        validate_orientation_result(decoded, catalog=catalog, module_ids=['m2'])


def test_verification_ordinal_binding_and_semantic_validator_retained():
    messages, _, _, result = fixture('verification')
    result.update(status='partial', evidence_0=0)
    decoded = decode_strict_wire(result, messages)
    assert [r['requirement_index'] for r in decoded['requirements']] == [0]
    _, bundle, _ = fixture_catalog_bundle()
    validate_requirement_result(decoded, module=dict(plan()['planned_modules'][1], requirements=plan()['planned_modules'][1]['requirements'][:1]), bundle=bundle)
    result['status'] = 'unknown'
    decoded = decode_strict_wire(result, messages)
    assert decoded['requirements'][0]['evidence_indexes'] == [0]
    validated = validate_requirement_result(decoded, module=dict(plan()['planned_modules'][1], requirements=plan()['planned_modules'][1]['requirements'][:1]), bundle=bundle)
    assert validated[0]['status'] == 'unknown' and validated[0]['evidence_ids']


@pytest.mark.parametrize('kind', ['orientation', 'verification'])
@pytest.mark.parametrize('bad', [True, False, 0.0, '0', None, [], {}, -2, 999999])
def test_rejects_bad_slot_without_body_leak(kind, bad):
    messages, _, _, result = fixture(kind)
    slot = 'seed_0' if kind == 'orientation' else 'evidence_0'
    result[slot] = bad
    with pytest.raises(ValueError, match='^BROWNFIELD_STRICT_WIRE_INDEX_INVALID$'):
        decode_strict_wire(result, messages)


@pytest.mark.parametrize('mutation', ['missing_row', 'extra_row', 'missing_slot', 'extra_field', 'wrong_row_type', 'nonobject'])
@pytest.mark.parametrize('kind', ['orientation', 'verification'])
def test_requires_exact_row_and_field_sets(kind, mutation):
    messages, _, _, result = fixture(kind)
    if mutation == 'missing_row': result = {}
    elif mutation == 'extra_row': result = {'row_0': result}
    elif mutation == 'missing_slot': del result['seed_0' if kind == 'orientation' else 'evidence_0']
    elif mutation == 'extra_field': result['private-marker'] = 'private-body'
    elif mutation == 'wrong_row_type': result = {'req_0': []}
    else: result = []
    with pytest.raises(ValueError) as exc:
        decode_strict_wire(result, messages)
    assert 'private' not in str(exc.value)


@pytest.mark.parametrize('value', ['', '   ', 'x'*1201, None, True])
def test_rationale_hard_limit(value):
    messages, _, _, result = fixture('verification')
    result['rationale'] = value
    with pytest.raises(ValueError, match='RATIONALE_INVALID'):
        decode_strict_wire(result, messages)


@pytest.mark.parametrize('value', ['x'*241, 'x'*1200, 'line\nnext'])
def test_rationale_valid_preserved(value):
    messages, _, schema, result = fixture('verification')
    result['rationale'] = value
    assert decode_strict_wire(result, messages)['requirements'][0]['rationale'] == value
    assert schema['properties']['rationale']['pattern'] == r'^[\s\S]{1,1200}$'


@pytest.mark.parametrize('status', ['complete', '', True, [], None])
def test_status_enum(status):
    messages, _, _, result = fixture('verification')
    result['status'] = status
    with pytest.raises(ValueError, match='STATUS_INVALID'):
        decode_strict_wire(result, messages)


def test_empty_catalog_only_allows_sentinel():
    messages, _, _, _ = fixture('orientation')
    payload = json.loads(messages[-1]['content'])
    payload['repository_atlas']['paths'] = []
    messages = (messages[0], dict(messages[1], content=json.dumps(payload)))
    rewritten, schema = build_strict_wire(messages)
    result = json.loads(rewritten[-1]['content'])['required_output']
    assert schema['properties']['seed_0']['maximum'] == -1
    assert decode_strict_wire(result, messages)['modules'][0]['seed_path_indexes'] == []
    result['seed_0'] = 0
    with pytest.raises(ValueError, match='INDEX_INVALID'): decode_strict_wire(result, messages)


@pytest.mark.parametrize('messages', [[], ({'role':'user','content':'private'},), ({'role':'system','content':'x'},{'role':'user','content':'{"secret":NaN}'}), ({'role':'system','content':'x'},{'role':'user','content':'{}'})])
def test_invalid_messages_fixed_error(messages):
    with pytest.raises(ValueError, match='^BROWNFIELD_STRICT_WIRE_MESSAGES_INVALID$'):
        build_strict_wire(messages)


@pytest.mark.parametrize('value', ['', 'unused explanation', None, 'x'*1201])
def test_orientation_has_no_rationale_and_rejects_extra_field(value):
    messages, _, schema, result = fixture('orientation')
    assert set(schema['properties']) == {f'seed_{i}' for i in range(12)}
    assert 'rationale' not in result
    result['rationale'] = value
    with pytest.raises(ValueError, match='FIELDS_INVALID'):
        decode_strict_wire(result, messages)
