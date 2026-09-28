"""Real local Git/snapshot/ledger/gateway/review chain; provider is an inert fixture."""
import json
import os
from pathlib import Path
import socket
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/backend'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_context_candidate_set as fixtures
from app import (deepseek_current_authority as authority, deepseek_transport,
                 model_call_ledger, model_execution, model_provider_runtime,
                 report_generation_batches as batches, report_generation_execution as execution,
                 report_generation_preparation as preparation, report_generation_tasks as tasks,
                 report_send_authorization as permits, report_review)
from app.model_provider_contract import ProviderCapability, ProviderCurrentAuthority, ProviderReceipt


class LocalAdapter:
    provider_id = authority.PROVIDER
    window = 120000

    def __init__(self):
        self.requests = []
        self.authority_reads = 0
        self.stop_before_call = None

    def get_capability(self, *, task_type, output_schema_version):
        return ProviderCapability(self.provider_id, authority.MODEL_ID, authority.MODEL_VERSION,
                                  task_type, output_schema_version, self.window, 64000)

    def estimate_request_utf8_bytes(self, *, messages, max_output_tokens):
        return len(json.dumps({'messages': list(messages), 'max_tokens': max_output_tokens},
                              ensure_ascii=False).encode())

    def resolve_current_authority(self, **kwargs):
        self.authority_reads += 1
        if kwargs['model_call_id'] == self.stop_before_call:
            raise HTTPException(409, detail={'code': 'FIXTURE_AUTHORITY_UNAVAILABLE'})
        call = model_call_ledger.get_model_call(kwargs['model_call_id'])
        scope = authority._stable_hash(authority._data_scope_payload(
            call=call, final_context_manifest_hash=kwargs['final_context_manifest_hash'],
            framed_payload_hash=kwargs['framed_payload_hash']))
        assert os.environ.get('ANXIN_DEEPSEEK_SEND_DATA_SCOPE_HASH') == scope
        return ProviderCurrentAuthority(
            model_call_id=call['model_call_id'], call_identity_hash=call['call_identity_hash'],
            provider=self.provider_id, model_id=authority.MODEL_ID, model_version=authority.MODEL_VERSION,
            context_window_tokens=self.window, max_output_tokens=64000,
            purpose_id=authority.PURPOSE_ID, data_scope_hash=scope,
            qualification_rule_version=call['rule_version'],
            qualification_output_schema_version=call['output_schema_version'],
            qualification_benchmark_sample_pack_version=call['benchmark_sample_pack_version'],
            qualification_status='qualified', qualification_authority_ref='local-fixture',
            qualification_evidence_hash='a'*64, authorization_authorized=True, authorization_valid=True,
            authorization_authority_ref='local-fixture', authorization_evidence_hash='b'*64)

    def execute(self, request):
        assert self.estimate_request_utf8_bytes(messages=request.messages, max_output_tokens=request.max_output_tokens) + 64000 + 16384 <= self.window
        self.requests.append(request)
        selection = batches.get_batch_call_selection(request.model_call_id)
        result = dict(plain_summary=f'本地假模型第 {len(self.requests)} 批分析。', feature_progress=[],
                      code_change_summary=[], test_evidence=[], risks=[], unknown_items=[], source_warnings=[])
        if selection['evidence_ids']:
            result['code_change_summary'].append(dict(content='该批证据的局部结论', source_type='ai_analysis',
                implementation_scope='暂时无法确认', evidence_ids=[selection['evidence_ids'][0]]))
        return ProviderReceipt(self.provider_id, f'fixture-{request.model_call_id}', request.model_id,
                               'local-only-fixture', 'stop', 10, 10, 20, result)


@pytest.fixture()
def product(tmp_path, monkeypatch):
    original_write = fixtures._write
    def large_write(repo, path, data):
        if path == 'src/a.txt' and data == b'alpha\na2\n':
            data = b'alpha\n' + b'business calculation output\n' * 2300
        original_write(repo, path, data)
    monkeypatch.setattr(fixtures, '_write', large_write)
    state = fixtures._make_state(tmp_path, monkeypatch)
    report_review.init_report_review_schema()  # same initialization as app.main lifespan
    def forbidden(*args, **kwargs):
        pytest.fail('Real network / provider transport must not be called')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(deepseek_transport, '_send_with_key', forbidden)
    adapter = LocalAdapter()
    monkeypatch.setattr(model_provider_runtime, 'resolve_model_provider_adapter', lambda provider: adapter)
    model_execution._SEND_CLAIMED_MODEL_CALL_IDS.clear()
    permits.revoke_report_send_authorization()
    task = tasks.create_report_generation_task(project_id=state['project_id'], local_task_id='batch-product-fixture',
        evidence_snapshot_id=state['snapshot_id'], create_key='batch-product-fixture')
    yield state, task, adapter
    permits.revoke_report_send_authorization()
    model_execution._SEND_CLAIMED_MODEL_CALL_IDS.clear()


def prepare(state, task):
    return preparation.prepare_report_generation_model_call(project_id=state['project_id'],
        local_task_id=task['local_task_id'], preparation_authorized=True)


def authorized_execute(state, task, prepared):
    args = dict(project_id=state['project_id'], local_task_id=task['local_task_id'])
    call_id = prepared['model_call_id']
    preview = permits.build_report_send_authorization_preview(**args, expected_model_call_id=call_id)
    permits.authorize_report_send_scope(**args, model_call_id=call_id,
        expected_data_scope_hash=preview['data_scope_hash'], human_confirmed=True)
    permits.begin_authorized_report_execution(**args, model_call_id=call_id,
        expected_data_scope_hash=preview['data_scope_hash'])
    try:
        return execution.execute_prepared_report_generation(**args, model_call_id=call_id)
    finally:
        permits.finish_authorized_report_execution(preview['data_scope_hash'])


def test_real_local_chain_batches_resumes_pending_and_materializes_review(product):
    state, task, adapter = product
    print('FLOW: local preparation', flush=True)
    prepared = prepare(state, task)
    assert prepared['batch_plan']['batch_count'] >= 2
    assert not adapter.requests and adapter.authority_reads == 0
    plan = batches.get_plan(prepared['model_call_id'])
    coverage = {}
    for batch in plan['batches']:
        assert set(batch['evidence_ids']) == {c['target'] for c in batch['chunks']} - {'profile'}
        for chunk in batch['chunks']:
            previous_end, previous_hash = coverage.get(chunk['target'], (0, chunk['body_hash']))
            assert chunk['start'] == previous_end
            assert chunk['body_hash'] == previous_hash
            coverage[chunk['target']] = (chunk['end'], chunk['body_hash'])
    adapter.stop_before_call = plan['batches'][1]['model_call_id']
    with pytest.raises(HTTPException):
        authorized_execute(state, task, prepared)
    assert len(adapter.requests) == 1
    print('FLOW: first batch durable; pre-send interruption', flush=True)
    restored = tasks.get_report_generation_task(project_id=state['project_id'], local_task_id=task['local_task_id'])
    assert restored['state'] == 'running'
    assert restored['batch_plan']['completed_batch_count'] == 1
    assert restored['batch_plan']['can_resume'] is True
    first_call = adapter.requests[0].model_call_id
    adapter.stop_before_call = None
    model_execution._SEND_CLAIMED_MODEL_CALL_IDS.clear()  # simulate process restart; durable claims still hold
    print('FLOW: resume pending batches', flush=True)
    prepared = prepare(state, task)
    result = authorized_execute(state, task, prepared)
    assert result['task_state'] == 'succeeded'
    assert [r.model_call_id for r in adapter.requests].count(first_call) == 1
    assert len(adapter.requests) == prepared['batch_plan']['batch_count']
    print('FLOW: report materialized; review read', flush=True)
    bundle = report_review.get_review_bundle(project_id=state['project_id'], report_version_id=result['report_version_id'])
    assert bundle['report_version']['report_version_id'] == result['report_version_id']
