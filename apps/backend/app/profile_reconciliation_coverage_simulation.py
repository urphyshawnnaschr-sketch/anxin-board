"""Durable LOCAL FAKE coverage validation, never provider or candidate evidence.

The caller supplies an isolated SQLite connection and fake callback. This module
has no runtime database, credential, network or provider integration. Its ledger
names and execution kind are deliberately separate from production results.
"""
from copy import deepcopy
import sqlite3

from app.profile_reconciliation_batches import BatchError, BatchFailure
from app.profile_reconciliation_coverage_plan import validate_coverage_plan


def _schema(conn):
    if conn.in_transaction:
        raise BatchError('COVERAGE_SIMULATION_ACTIVE_TRANSACTION')
    conn.execute('''CREATE TABLE IF NOT EXISTS coverage_simulation_claims (
        coverage_plan_hash TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        exact_head TEXT NOT NULL,
        execution_kind TEXT NOT NULL CHECK(execution_kind = 'local_fake'),
        claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(coverage_plan_hash, batch_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS coverage_simulation_results (
        coverage_plan_hash TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        execution_kind TEXT NOT NULL CHECK(execution_kind = 'local_fake'),
        status TEXT NOT NULL CHECK(status IN (
            'succeeded', 'failed_pre_send', 'failed_after_send', 'unknown')),
        error_code TEXT,
        completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(coverage_plan_hash, batch_id))''')
    conn.commit()


def _safe(plan_hash, batch_id, status, error_code=None, *, reused=False):
    return dict(coverage_plan_hash=plan_hash, batch_id=batch_id,
                execution_kind='local_fake', status=status, error_code=error_code,
                reused=reused, provider_calls=0, network_model_calls=0)


def simulate_coverage_batch(conn, plan, batch_index, *, fake_dispatch, current_head):
    """Run one fake batch at most once, resuming only batches never claimed.

    A hard interruption leaves the committed claim intact. Neither such claims
    nor failures can be retried by this API. Fake responses contain exactly a
    batch ID and complete unique fragment IDs; they contain no implementation
    judgments, receipts or candidate data.
    """
    snapshot = deepcopy(plan)
    validate_coverage_plan(snapshot)
    if (type(batch_index) is not int or batch_index < 0
            or batch_index >= len(snapshot['batches'])):
        raise BatchError('COVERAGE_SIMULATION_INVALID_BATCH')
    if not callable(fake_dispatch) or not callable(current_head):
        raise BatchError('COVERAGE_SIMULATION_INVALID_CALLBACK')
    batch = snapshot['batches'][batch_index]
    plan_hash, batch_id = snapshot['coverage_plan_hash'], batch['batch_id']
    expected_head = snapshot['exact_head']
    expected_ids = [fragment['fragment_id'] for fragment in batch['fragments']]
    _schema(conn)
    try:
        conn.execute('INSERT INTO coverage_simulation_claims '
                     '(coverage_plan_hash, batch_id, exact_head, execution_kind) '
                     "VALUES (?, ?, ?, 'local_fake')", (plan_hash, batch_id, expected_head))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        # Replays still fail closed on HEAD drift, without changing old evidence.
        try:
            replay_head = current_head()
        except Exception:
            return _safe(plan_hash, batch_id, 'unknown',
                         'COVERAGE_SIMULATION_UNKNOWN', reused=True)
        if replay_head != expected_head:
            return _safe(plan_hash, batch_id, 'failed_pre_send',
                         'COVERAGE_HEAD_CHANGED', reused=True)
        result = conn.execute('SELECT status, error_code FROM coverage_simulation_results '
                              'WHERE coverage_plan_hash = ? AND batch_id = ?',
                              (plan_hash, batch_id)).fetchone()
        return _safe(plan_hash, batch_id, result[0] if result else 'claimed',
                     result[1] if result else 'COVERAGE_SIMULATION_CLAIMED', reused=True)

    status, error = 'unknown', 'COVERAGE_SIMULATION_UNKNOWN'
    try:
        if current_head() != expected_head:
            status, error = 'failed_pre_send', 'COVERAGE_HEAD_CHANGED'
        else:
            response = fake_dispatch(deepcopy(batch))
            if current_head() != expected_head:
                status, error = 'failed_after_send', 'COVERAGE_HEAD_CHANGED'
            elif (not isinstance(response, dict)
                  or set(response) != {'batch_id', 'fragment_ids'}
                  or response['batch_id'] != batch_id
                  or not isinstance(response['fragment_ids'], list)
                  or any(not isinstance(value, str) for value in response['fragment_ids'])
                  or len(response['fragment_ids']) != len(expected_ids)
                  or set(response['fragment_ids']) != set(expected_ids)):
                status, error = 'failed_after_send', 'COVERAGE_SIMULATION_RESULT_INVALID'
            else:
                status, error = 'succeeded', None
    except BatchFailure as failure:
        status, error = failure.status, 'COVERAGE_SIMULATION_EXPLICIT_FAILURE'
    except Exception:
        # Never persist exception strings, fragments, fake output, or credentials.
        status, error = 'unknown', 'COVERAGE_SIMULATION_UNKNOWN'
    conn.execute('INSERT INTO coverage_simulation_results '
                 '(coverage_plan_hash, batch_id, execution_kind, status, error_code) '
                 "VALUES (?, ?, 'local_fake', ?, ?)", (plan_hash, batch_id, status, error))
    conn.commit()
    return _safe(plan_hash, batch_id, status, error)
