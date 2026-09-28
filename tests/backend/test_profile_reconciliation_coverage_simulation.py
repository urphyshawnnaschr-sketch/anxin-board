import hashlib
import sqlite3

import pytest

from app.profile_reconciliation_batches import BatchError, BatchFailure
from app.profile_reconciliation_coverage_plan import plan_coverage_batches
from app.profile_reconciliation_coverage_simulation import simulate_coverage_batch

HEAD = 'b' * 40


def plan(text='x' * 25000):
    return plan_coverage_batches(
        dict(schema_version='project_profile_v2', planned_modules=[
            dict(client_id='m1', name='Feature', requirements=['Feature works'])]),
        [dict(evidence_id='repo-code-one', path='src/file.unknown', exact_head=HEAD,
              content=text, content_hash=hashlib.sha256(text.encode()).hexdigest())],
        exact_head=HEAD, plan_profile_id=1,
        budget_record=dict(context_window_tokens=12000, max_output_tokens=1000,
                           reserved_output_tokens=1000, safety_margin_tokens=1000,
                           counting_policy_version='utf8_byte_upper_bound_v1'))


def success(batch):
    return dict(batch_id=batch['batch_id'],
                fragment_ids=[f['fragment_id'] for f in batch['fragments']])


def run(conn, value, index=0, callback=success, head=lambda: HEAD):
    return simulate_coverage_batch(conn, value, index, fake_dispatch=callback, current_head=head)


def test_persistent_resume_and_success_reuse(tmp_path):
    path = tmp_path / 'fake.sqlite'
    value = plan()
    assert len(value['batches']) > 1
    conn = sqlite3.connect(path)
    assert run(conn, value)['status'] == 'succeeded'
    conn.close()
    conn = sqlite3.connect(path)
    def forbidden(_):
        pytest.fail('already completed batch dispatched again')
    result = run(conn, value, callback=forbidden)
    assert result['reused'] is True
    assert result['execution_kind'] == 'local_fake'
    assert result['provider_calls'] == 0
    assert run(conn, value, 1)['status'] == 'succeeded'
    assert conn.execute('select count(*) from coverage_simulation_claims').fetchone()[0] == 2


@pytest.mark.parametrize('failure', ['failed_pre_send', 'failed_after_send', 'unknown', 'exception'])
def test_failure_terminal_and_does_not_discard_prior_batch(failure):
    conn = sqlite3.connect(':memory:')
    value = plan()
    run(conn, value)
    calls = []
    def fail(_):
        calls.append(1)
        if failure == 'exception':
            raise RuntimeError('secret must not be persisted')
        raise BatchFailure(failure)
    result = run(conn, value, 1, fail)
    assert result['status'] == ('unknown' if failure == 'exception' else failure)
    assert run(conn, value, 1, fail)['status'] == result['status']
    assert calls == [1]
    assert run(conn, value)['status'] == 'succeeded'
    assert 'secret' not in str(list(conn.iterdump()))


def test_crash_leaves_claim_and_blocks_retry(tmp_path):
    path = tmp_path / 'fake.sqlite'
    conn = sqlite3.connect(path)
    value = plan()
    def crash(_):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(conn, value, callback=crash)
    conn.close()
    conn = sqlite3.connect(path)
    assert run(conn, value, callback=crash)['status'] == 'claimed'
    assert run(conn, value, 1)['status'] == 'succeeded'


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'foreign', 'batch', 'extra'])
def test_exact_fragment_membership_required(bad):
    value = plan()
    def invalid(batch):
        result = success(batch)
        if bad == 'missing': result['fragment_ids'] = []
        elif bad == 'duplicate': result['fragment_ids'] *= 2
        elif bad == 'foreign': result['fragment_ids'] = [value['batches'][1]['fragments'][0]['fragment_id']]
        elif bad == 'batch': result['batch_id'] = value['batches'][1]['batch_id']
        else: result['provider_receipt'] = 'not allowed'
        return result
    assert run(sqlite3.connect(':memory:'), value, callback=invalid)['status'] == 'failed_after_send'


def test_head_drift_before_and_after_dispatch():
    value = plan()
    calls = []
    def dispatch(batch):
        calls.append(1)
        return success(batch)
    result = run(sqlite3.connect(':memory:'), value, callback=dispatch, head=lambda: 'c' * 40)
    assert result['status'] == 'failed_pre_send'
    assert not calls
    heads = iter([HEAD, 'c' * 40])
    result = run(sqlite3.connect(':memory:'), value, callback=dispatch, head=lambda: next(heads))
    assert result['status'] == 'failed_after_send'
    assert result['error_code'] == 'COVERAGE_HEAD_CHANGED'


def test_distinct_plan_hash_never_reuses_old_result():
    conn = sqlite3.connect(':memory:')
    first, second = plan(), plan('y' * 25000)
    assert first['coverage_plan_hash'] != second['coverage_plan_hash']
    run(conn, first)
    assert run(conn, second)['reused'] is False


def test_invalid_plan_rejected_before_any_claim():
    value = plan()
    value['exact_head'] = 'c' * 40
    conn = sqlite3.connect(':memory:')
    with pytest.raises(BatchError):
        run(conn, value)
    assert conn.execute("select count(*) from sqlite_master where name like 'coverage_simulation_%'").fetchone()[0] == 0


def test_replay_head_lookup_failure_is_unknown_without_dispatch():
    conn = sqlite3.connect(':memory:')
    value = plan()
    run(conn, value)
    def unavailable():
        raise OSError('sensitive local path')
    result = run(conn, value, head=unavailable)
    assert result['status'] == 'unknown'
    assert result['reused'] is True
    assert conn.execute('select status from coverage_simulation_results').fetchone()[0] == 'succeeded'


def test_callback_cannot_mutate_bound_plan_or_expected_membership():
    value = plan()
    expected = value['batches'][0]['fragments'][0]['fragment_id']
    def mutate(batch):
        batch['fragments'][0]['fragment_id'] = 'foreign'
        return success(batch)
    assert run(sqlite3.connect(':memory:'), value, callback=mutate)['status'] == 'failed_after_send'
    assert value['batches'][0]['fragments'][0]['fragment_id'] == expected


def test_success_replay_checks_head_without_rewriting_old_result():
    conn = sqlite3.connect(':memory:')
    value = plan()
    run(conn, value)
    assert run(conn, value, head=lambda: 'c' * 40)['error_code'] == 'COVERAGE_HEAD_CHANGED'
    assert conn.execute('select status from coverage_simulation_results').fetchone()[0] == 'succeeded'
