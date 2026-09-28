"""Real offline candidate/manifest/redaction/framing closure for frozen progress."""
import copy
import hashlib
import json
import socket
import sqlite3

import httpx

from app import context_manifest, context_redaction, context_redaction_inputs, context_resolver, context_token_framing
from app import project_progress, project_progress_context, project_profile_generation
from app.context_candidate_runtime import candidate_materialization_scope
import test_context_candidate_set as candidate_tests
import test_context_redaction_inputs as input_tests
import test_model_call_ledger as ledger_tests


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def test_nonempty_approved_progress_freezes_through_real_candidate_manifest_redaction_and_budget(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network/provider/credential must not be used')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(project_profile_generation, '_read_provider_credential', forbidden)
    secret = 'sk-' + 'z' * 40  # Synthetic only; never loaded from a credential store.
    frozen_previous = []

    def approved_previous(conn, *, scope):
        row = conn.execute('SELECT * FROM project_profiles WHERE id=?', (scope['profile_id'],)).fetchone()
        profile = dict(row)
        profile['content'] = json.loads(profile.pop('content_json'))
        git = conn.execute('SELECT from_commit FROM git_snapshots WHERE project_id=?', (scope['project_id'],)).fetchone()
        head = git['from_commit']
        report = {'report_version_id':777, 'report_content_hash':'b'*64, 'approval_snapshot_id':777,
                  'approval_snapshot_hash':'c'*64, 'head_sha':head, 'parent_head_sha':head}
        module = profile['content']['modules'][0]['client_id']
        value = project_progress.compute_snapshot(scope=scope, profile=profile, report=report, updates={
            module: {'stage':'开发中', 'evidence_ids':['git-prior-approved'],
                     'regression_reason':'人工审核记录 ' + '累计上下文完整预算 ' * 60 + 'Bearer ' + secret}})
        frozen_previous.append(copy.deepcopy(value))
        return value

    monkeypatch.setattr(project_progress_context, 'read_latest', approved_previous)
    state = candidate_tests._make_state(tmp_path, monkeypatch)
    assert len(frozen_previous) == 1
    # A changed latest ledger cannot alter this historical EvidenceSnapshot.
    monkeypatch.setattr(project_progress_context, 'read_latest', forbidden)
    budget = input_tests._budget_record(counting_policy_version='utf8_byte_upper_bound_v1')
    with candidate_materialization_scope():
        prepared = ledger_tests._prepare(state, local_task_id='progress-pipeline', call_prepare_key='progress-pipeline')
        candidate = context_resolver.build_context_candidate_set(state['snapshot_id'])
        context = candidate['profile']['progress_context']
        assert context['previous'] == frozen_previous[0]
        assert candidate['profile']['progress_context_hash'] == context['context_hash']
        core = context_manifest.build_context_manifest_core(model_call_id=prepared['model_call_id'], budget_record=budget)
        assert core['candidate_set_hash'] == candidate['candidate_set_hash']
        assert core['profile']['progress_context_hash'] == context['context_hash']
        raw = context_redaction_inputs.resolve_context_redaction_input(model_call_id=prepared['model_call_id'], budget_record=budget, target='profile')
        assert raw['raw_body']['approved_project_progress']['previous'] == frozen_previous[0]
        redacted = context_redaction.build_context_redaction_result(model_call_id=prepared['model_call_id'], budget_record=budget, target='profile')
        assert secret.encode() not in canonical(redacted)
        assert redacted['redaction_match_count'] >= 1
        assert redacted['redacted_body']['approved_project_progress']['previous']['modules'][0]['stage'] == '开发中'
        framing = context_token_framing.build_context_token_framing_accounting(model_call_id=prepared['model_call_id'], budget_record=budget)
        payload = context_token_framing._materialize_context_payload_transient(model_call_id=prepared['model_call_id'], budget_record=budget)['payload']
        assert secret.encode() not in payload
        assert '累计上下文完整预算'.encode() in payload
        assert framing['framed_payload_utf8_bytes'] == len(payload)
        assert framing['conservative_input_token_upper_bound'] == len(payload)
        assert framing['framed_payload_hash'] == hashlib.sha256(payload).hexdigest()
        frames = [json.loads(line) for line in payload.splitlines()]
        profile_frame = next(frame for frame in frames if frame['target'] == 'profile')
        assert profile_frame['body'] == redacted['redacted_body']
        assert len(payload) >= len(canonical(redacted['redacted_body']))
        frozen_candidate_hash = candidate['candidate_set_hash']
    # Rebuild outside the request-local cache: the frozen state is still unchanged.
    with candidate_materialization_scope():
        assert context_resolver.build_context_candidate_set(state['snapshot_id'])['candidate_set_hash'] == frozen_candidate_hash

    # Historical schema: remove only the synthetic companion, reproducing an old
    # snapshot created before cumulative contexts existed. Original profile bytes
    # remain untouched; the normal resolver must not invent a companion.
    with sqlite3.connect(state['db_path']) as conn:
        original_profile_json = conn.execute('SELECT content_json FROM project_profiles WHERE id=?', (state['profile_id'],)).fetchone()[0]
        conn.execute('DROP TABLE evidence_progress_contexts')
    with candidate_materialization_scope():
        old_prepared = ledger_tests._prepare(state, local_task_id='legacy-progress-pipeline', call_prepare_key='legacy-progress-pipeline')
        old_candidate = context_resolver.build_context_candidate_set(state['snapshot_id'])
        assert 'progress_context' not in old_candidate['profile']
        assert old_candidate['candidate_set_hash'] != frozen_candidate_hash
        old_core = context_manifest.build_context_manifest_core(model_call_id=old_prepared['model_call_id'], budget_record=budget)
        assert 'progress_context_hash' not in old_core['profile']
        old_raw = context_redaction_inputs.resolve_context_redaction_input(model_call_id=old_prepared['model_call_id'], budget_record=budget, target='profile')
        assert canonical(old_raw['raw_body']) == original_profile_json.encode('utf-8')
        assert 'approved_project_progress' not in old_raw['raw_body']
