from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps' / 'backend'))
from app import report_generation_batches as batches


def test_split_preserves_every_unicode_character_and_never_exceeds_budget():
    body = '中文\\"\n' * 900
    parts = batches.split_text_ranges(body, max_bytes=127)
    assert ''.join(body[a:b] for a, b in parts) == body
    assert all(len(body[a:b].encode('utf-8')) <= 127 for a, b in parts)
    assert parts[0][0] == 0 and parts[-1][1] == len(body)


def test_split_rejects_budget_too_small_instead_of_truncating():
    with pytest.raises(Exception):
        batches.split_text_ranges('中', max_bytes=2)


@pytest.fixture
def durable_plan(tmp_path, monkeypatch):
    monkeypatch.setenv('ANXINBOARD_DB_PATH', str(tmp_path / 'batches.sqlite'))
    batches._init()
    plan = {'parent_call_id': 1, 'batches': [
        {'ordinal': n, 'model_call_id': n + 1, 'estimated_input_tokens': 123,
         'call_identity_hash': str(n) * 64, 'chunks': [], 'evidence_ids': []}
        for n in (1, 2)]}
    with batches.get_connection() as conn:
        conn.execute('INSERT INTO report_batch_plans VALUES (1,7,?,?,?)',
                     ('task', batches._hash(plan), batches._json(plan)))
        conn.executemany("INSERT INTO report_generation_batches VALUES (1,?,?,'pending',NULL)", [(1, 2), (2, 3)])
    return plan


def test_send_claim_is_durable_and_cannot_be_claimed_again(durable_plan):
    batches._set_state(1, 1, 'pending', 'claimed')
    with pytest.raises(Exception):
        batches._set_state(1, 1, 'pending', 'claimed')
    assert batches.get_plan(1)['states'][0]['state'] == 'claimed'


def test_claimed_without_receipt_never_becomes_resumable(durable_plan, monkeypatch):
    batches._set_state(1, 1, 'pending', 'claimed')
    monkeypatch.setattr(batches, '_existing_child_result', lambda _: None)
    assert batches.summary(batches.get_plan(1))['can_resume'] is False
    batches._recover_completed_claims(batches.get_plan(1))
    assert batches.get_plan(1)['states'][0]['state'] == 'claimed'


def test_crash_after_result_is_recovered_without_provider_or_repeat_claim(durable_plan, monkeypatch):
    batches._set_state(1, 1, 'pending', 'claimed')
    monkeypatch.setattr(batches, '_existing_child_result', lambda _: {'model_result_id': 22})
    # GET summary is read-only, but advertises that local finalization is possible.
    assert batches.summary(batches.get_plan(1))['completed_batch_count'] == 1
    assert batches.get_plan(1)['states'][0]['state'] == 'claimed'
    recovered = batches._recover_completed_claims(batches.get_plan(1))
    assert recovered['states'][0]['state'] == 'succeeded'
    assert recovered['states'][0]['result_id'] == 22
    assert recovered['states'][1]['state'] == 'pending'


def test_modified_plan_hash_fails_closed(durable_plan):
    with batches.get_connection() as conn:
        conn.execute("UPDATE report_batch_plans SET plan_json='{}' WHERE parent_call_id=1")
    with pytest.raises(Exception):
        batches.get_plan(1)
