"""Real core/strict transport codec integration using synthetic inputs and local HTTP doubles."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app import brownfield_baseline as core, brownfield_baseline_store as store
from app import brownfield_strict_adapter as transport
from app import project_profiles
from test_brownfield_atlas_spike import indexed, plan


@pytest.mark.parametrize('module_count,output_cap', [(1, 8000), (11, 32000), (16, 32000)])
@pytest.mark.parametrize('duplicate_fields', [False, True])
@pytest.mark.parametrize('complete_calls', [1, 2])
def test_core_strict_wire_to_durable_candidate(tmp_path, monkeypatch, module_count, output_cap, duplicate_fields, complete_calls, scattered=False):
    selected = SimpleNamespace(provider='deepseek', model_id='deepseek-flash', model_version='deepseek-flash',
                               context_window_tokens=1000000, max_output_tokens=output_cap)
    monkeypatch.setattr(transport, 'build_live_deepseek_profile_adapter',
                        lambda: SimpleNamespace(get_capability=lambda **kwargs: selected))
    monkeypatch.setattr(project_profiles, 'confirm_candidate', lambda *args, **kwargs: pytest.fail('No automatic confirmation'))
    sent = []
    verification_rows = 0
    scattered_kinds = set()

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            nonlocal verification_rows
            assert url == transport.URL
            wire = kwargs['content']
            payload = json.loads(wire)
            assert payload['model'] == selected.model_id
            assert payload['thinking'] == {'type': 'disabled'}
            assert payload['temperature'] == 0
            assert payload['max_tokens'] == output_cap
            assert len(wire) <= selected.context_window_tokens - output_cap - core.SAFETY_MARGIN
            # A collector result is never sent back as an executable tool conversation.
            assert [m['role'] for m in payload['messages']] == ['system', 'user']
            assert payload['tool_choice'] == {'type': 'function', 'function': {'name': transport.TOOL_NAME}}
            assert len(payload['tools']) == 1
            tool = payload['tools'][0]['function']
            assert tool['strict'] is True and tool['name'] == transport.TOOL_NAME
            source = json.loads(payload['messages'][-1]['content'])
            schema = tool['parameters']
            assert schema == source['required_output_schema']
            assert schema['additionalProperties'] is False
            result = deepcopy(source['required_output'])
            assert set(schema['required']) == set(result)
            if 'repository_atlas' in source:
                assert len(source['planned_modules']) == 1
                result['seed_0'] = source['repository_atlas']['paths'][0]['path_index']
            else:
                assert len(source['planned_module']['requirements']) == 1
                verification_rows += 1
                result['status'] = 'partial'
                result['evidence_0'] = source['source_evidence'][0]['evidence_index']
                result['rationale'] = 'Synthetic directly supported behavior. ' * 9
            sent.append(wire)
            response = {'id': f'synthetic-chat-{len(sent)}', 'object': 'chat.completion', 'model': selected.model_id,
                        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                            'tool_calls': [{'id': f'synthetic-call-{len(sent)}-{i}', 'type': 'function',
                                'function': {'name': transport.TOOL_NAME, 'arguments': json.dumps(result)}} for i in range(complete_calls)]}}],
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
            if duplicate_fields:
                for call in response['choices'][0]['message']['tool_calls']:
                    function = call['function']
                    row = json.loads(function['arguments'])
                    key = next(iter(row))
                    function['arguments'] = function['arguments'][:-1] + ',' + json.dumps(key) + ':' + json.dumps(row[key]) + '}'
            return httpx.Response(200, json=response)

    monkeypatch.setattr(transport, '_client', Client)
    adapter = transport.build_brownfield_strict_adapter()
    sample = plan()
    sample['planned_modules'] = [dict(deepcopy(sample['planned_modules'][i % 4]), client_id=f'module_{i}') for i in range(module_count)]
    if scattered:
        for module in sample['planned_modules']:
            module['requirements'] = [*module['requirements'], 'Synthetic additional requirement for row collection.']
    source = indexed()
    scope = {'project_id': 1, 'plan_profile_id': 2, 'plan_content_hash': core.digest(sample), 'prd_id': 3,
             'prd_source_hash': 'c' * 64, 'git_url': 'https://example.test/synthetic.git', 'git_branch': 'main', 'exact_head': source['exact_head']}
    prepared = core.prepare_from_inputs(scope=scope, plan=sample, indexed=source, adapter=adapter)
    credential_reads = []
    assert sent == [] and prepared['summary']['provider_calls'] == 0
    assert prepared['summary']['credential_read'] is False

    def connect():
        conn = sqlite3.connect(tmp_path / 'synthetic-atlas.db')
        conn.row_factory = sqlite3.Row
        return conn

    with closing(connect()) as conn:
        store.ensure_schema(conn)
        task = store.create_task(conn, project_id=1, authorization_nonce='synthetic-integration-only',
                                 identity=prepared['identity'], max_calls=prepared['summary']['max_calls'])
    candidates = []

    def promote(content):
        candidate = {'id': 91, 'status': 'candidate', 'content': deepcopy(content)}
        candidates.append(candidate)
        return candidate

    def synthetic_credential():
        credential_reads.append(True)
        return 'synthetic-test-value'

    core.run_baseline(prepared, task, connection_factory=connect, scope_reader=lambda: scope,
                      credential_reader=synthetic_credential, promote=promote)
    with closing(connect()) as conn:
        final = store.get_task(conn, task['task_id'])
        output = store.get_output(conn, task['task_id'])['output']
        claims = conn.execute('SELECT stage_key, wire_bytes FROM brownfield_stage_claims WHERE task_id=? ORDER BY sequence', (task['task_id'],)).fetchall()
        results = conn.execute('SELECT status, result_json FROM brownfield_stage_results WHERE task_id=?', (task['task_id'],)).fetchall()
    assert final['status'] == 'succeeded', final['error_code']
    assert final['profile_id'] == 91 and len(candidates) == 1
    assert candidates[0]['status'] == 'candidate'
    assert candidates[0]['content'] == output['content']
    assert len(sent) == len(claims) == len(results) <= task['max_calls']
    assert all(row['status'] == 'succeeded' for row in results)
    for row in results:
        metadata = json.loads(row['result_json'])['collector_normalization']
        assert metadata['policy'] == 'identical-typed-duplicates/1'
        assert (metadata['duplicate_fields_removed'] > 0) is duplicate_fields
        assert metadata['tool_calls_normalized'] == metadata['duplicate_fields_removed']
        assert metadata['calls_received'] == complete_calls
        assert metadata['calls_collapsed'] == complete_calls - 1
        assert metadata['unique_rows'] == 1
        assert metadata['calls_policy'] == 'identical-complete-single-result/1'
        assert json.loads(row['result_json'])['prompt_tokens'] == 10
        assert json.loads(row['result_json'])['completion_tokens'] == 20
    assert [row['wire_bytes'] for row in claims] == [len(wire) for wire in sent]
    assert any(row['stage_key'].startswith('orientation/') for row in claims)
    assert any(row['stage_key'].startswith('verify/') for row in claims)
    assert len(credential_reads) == 1
    assert verification_rows >= sum(len(m['requirements']) for m in sample['planned_modules'])
    assert len(output['content']['implementation_mappings']) == module_count
    assert set(output['requirements']) == {m['client_id'] for m in sample['planned_modules']}
    for module in sample['planned_modules']:
        rows = output['requirements'][module['client_id']]
        assert [row['requirement_index'] for row in rows] == list(range(len(module['requirements'])))
        assert all(row['status'] == 'partial' and row['evidence_ids'] for row in rows)
    assert all(m['exact_head'] == scope['exact_head'] and m['status'] == 'partial' for m in output['content']['implementation_mappings'])
    assert output['coverage']['gap_check_completed'] is True
    assert output['coverage']['complete_repository_semantic_proof'] is False
    # Re-entering a terminal task cannot cause a second send or another candidate.
    core.run_baseline(prepared, task, connection_factory=connect, scope_reader=lambda: scope,
                      credential_reader=lambda: pytest.fail('No credential reread'), promote=lambda _: pytest.fail('No second promotion'))
    assert len(sent) == len(claims)


def test_multiple_original_requirements_reach_durable_unconfirmed_candidate(tmp_path, monkeypatch):
    test_core_strict_wire_to_durable_candidate(
        tmp_path, monkeypatch, 5, 32000, False, 1, scattered=True)
