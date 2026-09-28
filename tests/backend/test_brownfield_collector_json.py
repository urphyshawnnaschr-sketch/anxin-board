"""Only identical typed duplicate fields may be removed from collector arguments."""
import json

import pytest

from app.brownfield_collector_json import CollectorConflict, normalize_arguments


@pytest.mark.parametrize('raw,count', [
    ('{"row_index":0,"row_index":0}', 1),
    ('{"a":{"x":1,"y":[true,null]},"a":{"y":[true,null],"x":1}}', 1),
    ('{"a":{"x":1,"x":1},"a":{"x":1}}', 2),
    ('{"a":"same","a":"same","a":"same"}', 2),
])
def test_only_identical_fields_are_normalized(raw, count):
    text, duplicates = normalize_arguments(raw)
    assert duplicates == count
    assert json.loads(text) == json.loads(raw)
    pairs = []
    json.loads(text, object_pairs_hook=lambda values: pairs.extend(values))
    assert len([key for key, _ in pairs if key == 'row_index']) <= 1


@pytest.mark.parametrize('left,right', [
    ('1', 'true'), ('1', '1.0'), ('false', '0'),
    ('[1,2]', '[2,1]'), ('{"a":1}', '{"a":2}'),
    ('{"a":1}', '{"a":true}'), ('"text"', '"text "'),
    ('"é"', '"é"'), ('null', '"null"'),
])
def test_conflicts_never_choose_a_value(left, right):
    with pytest.raises(CollectorConflict) as exc:
        normalize_arguments('{"private-marker":'+left+',"private-marker":'+right+'}')
    assert 'private-marker' not in str(exc.value)


def test_inner_conflict_cannot_be_hidden_by_outer_duplicate():
    with pytest.raises(CollectorConflict):
        normalize_arguments('{"a":{"x":1,"x":2},"a":{"x":2}}')


@pytest.mark.parametrize('raw', ['{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '{"a":', '```json\n{}', '{"a":"\ud800"}'])
def test_other_invalid_json_is_not_repaired(raw):
    text, count = normalize_arguments(raw)
    # Existing strict receipt parser retains its detailed failure classification.
    assert text == raw and count == 0


def test_valid_json_without_duplicates_keeps_exact_text():
    raw = ' { "x" : [1, true, null, {"y":2}] } '
    assert normalize_arguments(raw) == (raw, 0)


def test_deep_json_is_not_repaired():
    raw = '[' * 1100 + '0' + ']' * 1100
    assert normalize_arguments(raw) == (raw, 0)
