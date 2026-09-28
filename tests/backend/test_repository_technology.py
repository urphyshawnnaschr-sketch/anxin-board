"""Independent technology evidence contracts over fake Git and the real safe Core."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_profile_repository_map import fake_git, HEAD
from app import profile_repository_map as core
from app.repository_index import index_repository_map
from app.repository_technology import enrich_technology
from app.profile_reconciliation_batches import (
    plan_reconciliation_batches, run_batch, aggregate_batches,
)

BUDGET = dict(context_window_tokens=18000, max_output_tokens=2000,
              reserved_output_tokens=2000, safety_margin_tokens=1000,
              counting_policy_version='utf8_byte_upper_bound_v1')
MVC = ('import org.springframework.web.bind.annotation.RestController;\n'
       '@RestController public class Orders {}\n')
BOOT = "import Vue from 'vue';\nnew Vue({});\n"


def test_rich_evidence_is_hash_bound_and_cross_file_rejected(fake_git):
    _, indexed = build(fake_git, {'orders.java': MVC})
    plan = {'schema_version':'project_profile_v2', 'planned_modules':[{'client_id':'orders','name':'Orders'}]}
    batches = plan_reconciliation_batches(plan, indexed['evidence'], exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)
    assert any(e['technology_evidence'] for b in batches for e in b['repo_evidence'])
    changed = copy.deepcopy(indexed['evidence'])
    changed[0]['technology_evidence'][0]['source_path'] = 'other.java'
    with pytest.raises(ValueError, match='TECHNOLOGY_EVIDENCE_BINDING_INVALID'):
        plan_reconciliation_batches(plan, changed, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)


def test_inventory_blocked_state_cannot_feed_adapter(fake_git):
    raw, _ = build(fake_git, {'orders.java': MVC})
    raw['files'][0]['coverage_state'] = 'redaction_blocked'
    with pytest.raises(ValueError, match='TECHNOLOGY_SOURCE_NOT_CORE_SAFE'):
        enrich_technology(raw)


def test_context_lineage_exports_only_actual_client(fake_git):
    files = {'package.json':'{}', 'src/client.js':"import axios from 'axios'; const client=axios.create({}); export default unrelated;",
        'src/call.js':"import query from './client'; query({method:'post',url:endpoint});"}
    _, indexed = build(fake_git, files)
    assert not any(r['source_path']=='src/call.js' for r in records(indexed))


def test_java_detection_precedes_deep_extraction(monkeypatch):
    from app import repository_java_adapters as java
    monkeypatch.setattr(java, '_raw_matches', lambda *a: (_ for _ in ()).throw(AssertionError('deep called')))
    assert java.detect('Orders.java', MVC) == {'SpringMVC'}


def test_unreadable_child_manifest_preserves_scope_boundary(fake_git):
    fake_git.add('child/package.json', b'\xff')
    _, result = build(fake_git, {'package.json':'{}', 'main.js':BOOT,
        'child/Panel.vue':'<template><div/></template><script>export default {}</script>'})
    assert states(result)['child', 'Vue2'] == 'UNKNOWN'
    assert not any(r['source_path']=='child/Panel.vue' for r in records(result))


def build(fake_git, files, **kwargs):
    for path, source in files.items():
        fake_git.add(path, source.encode())
    raw = core.build_repository_map(2, {'git_url': 'https://github.com/example/project.git'},
                                    {'remote_head': HEAD})
    return raw, index_repository_map(raw, **kwargs)


def states(indexed):
    return {(r['scope'], r['technology']): r['classification']
            for r in indexed['technology_detection']['classifications']}


def records(indexed):
    return {r['evidence_id']: r for c in indexed['evidence']
            for r in c['technology_evidence']}.values()


@pytest.mark.parametrize('path,source,technology', [
    ('package.json', '{"dependencies":{"vue":"2.7","axios":"1"}}', 'Vue2'),
    ('pom.xml', '<project><dependencies><dependency><artifactId>spring-webmvc</artifactId>'
     '</dependency></dependencies></project>', 'SpringMVC'),
])
def test_manifest_alone_never_enables_source_adapter(fake_git, path, source, technology):
    _, result = build(fake_git, {path: source})
    assert states(result)['.', technology] == 'DECLARED_ONLY'
    assert result['technology_detection']['enabled_adapters'] == []
    assert list(records(result)) == []


@pytest.mark.parametrize('path,source,technology', [
    ('Dead.java', 'import org.springframework.web.bind.annotation.RestController;\nclass Dead {}', 'SpringMVC'),
    ('dead.js', "import Vue from 'vue';\nconst unused = 1;", 'Vue2'),
    ('dead.js', "import axios from 'axios';\n// axios.get('/fake')\nconst sample=\"axios.get()\";", 'Axios'),
    ('Custom.java', 'import local.annotations.Controller;\n@Controller class Custom {}', 'SpringMVC'),
    ('Custom.java', 'import local.Mapper;\n@Mapper interface Custom {}', 'MyBatis'),
    ('Custom.java', 'import org.springframework.stereotype.Controller;\n@interface Controller {}\n@Controller class Custom {}', 'SpringMVC'),
])
def test_dead_import_comment_and_custom_annotations_are_unknown(fake_git, path, source, technology):
    _, result = build(fake_git, {path: source})
    assert states(result)['.', technology] == 'UNKNOWN'
    assert not any(r['technology'] == technology for r in records(result))


def test_stale_dependency_does_not_override_actual_source(fake_git):
    _, result = build(fake_git, {'package.json': '{"dependencies":{"axios":"1"}}',
                                'main.js': BOOT})
    assert states(result)['.', 'Axios'] == 'DECLARED_ONLY'
    assert states(result)['.', 'Vue2'] == 'SOURCE_CONFIRMED'
    assert any(r['technology'] == 'Vue2' for r in records(result))


def test_monorepo_scope_does_not_inherit_neighbor_bootstrap(fake_git):
    panel = '<template><div>Orders</div></template><script>export default {}</script>'
    _, result = build(fake_git, {'a/package.json': '{}', 'a/main.js': BOOT, 'a/Panel.vue': panel,
                                'b/package.json': '{"dependencies":{"vue":"2"}}', 'b/Panel.vue': panel})
    assert states(result)['a', 'Vue2'] == 'SOURCE_CONFIRMED'
    assert states(result)['b', 'Vue2'] == 'DECLARED_ONLY'
    paths = {r['source_path'] for r in records(result) if r['technology'] == 'Vue2'}
    assert 'a/Panel.vue' in paths and 'b/Panel.vue' not in paths


def test_source_removal_does_not_reuse_prior_confirmation(fake_git):
    raw, original = build(fake_git, {'package.json': '{"dependencies":{"vue":"2"}}', 'main.js': BOOT})
    assert states(original)['.', 'Vue2'] == 'SOURCE_CONFIRMED'
    # A second exact-object inventory without main.js; no persisted classifier cache.
    remaining = copy.deepcopy(raw)
    remaining['files'] = [f for f in remaining['files'] if f['path'] == 'package.json']
    remaining['evidence'] = [e for e in remaining['evidence'] if e['path'] == 'package.json']
    result = index_repository_map(remaining)
    assert states(result)['.', 'Vue2'] == 'DECLARED_ONLY'
    assert list(records(result)) == []


def test_actual_java_usage_records_exact_source_identity(fake_git):
    raw, result = build(fake_git, {'Orders.java': MVC})
    assert states(result)['.', 'SpringMVC'] == 'SOURCE_CONFIRMED'
    source = ''.join(e['content'] for e in raw['evidence'])
    assert list(records(result))
    for record in records(result):
        assert record['exact_head'] == HEAD
        assert record['object_sha'] == raw['evidence'][0]['object_sha']
        assert record['source_evidence_ids']
        assert record['symbol'] in source[record['char_start']:record['char_end']]
        assert record['line_start'] == source[:record['char_start']].count('\n') + 1
        assert record['classification'] == 'SOURCE_CONFIRMED'
        assert record['confidence'] == 'source_usage_not_feature_completeness'


def test_manifest_conflict_keeps_nearest_scope_source_truth(fake_git):
    _, result = build(fake_git, {'package.json': '{"dependencies":{"axios":"1"}}',
                                'child/package.json': '{"dependencies":{"vue":"2"}}',
                                'child/main.js': "import axios from 'axios'; axios.get('/orders');"})
    assert states(result)['.', 'Axios'] == 'DECLARED_ONLY'
    assert states(result)['child', 'Axios'] == 'SOURCE_CONFIRMED'
    assert states(result)['child', 'Vue2'] == 'DECLARED_ONLY'


def test_sensitive_and_redaction_blocked_files_never_reach_adapters(fake_git, monkeypatch):
    opened = []
    original_redact = core._redact_text
    def redact(text, **kwargs):
        if 'quarantine-marker' in text:
            raise HTTPException(409, {'code': 'CONTEXT_REDACTION_UNSAFE_AMBIGUITY'})
        return original_redact(text, **kwargs)
    monkeypatch.setattr(core, '_redact_text', redact)
    secret_sha = fake_git.add('.env', b'synthetic-sensitive-marker')
    adapter = SimpleNamespace(TECHNOLOGIES={'Fixture': ()},
        detect=lambda path, text: opened.append((path, text)) or set(),
        analyze=lambda *args, **kwargs: pytest.fail('adapter not enabled'))
    raw, result = build(fake_git, {'blocked.java': 'quarantine-marker', 'plain.zig': 'orders inventory'},
                        technology_adapters=(adapter,))
    assert secret_sha not in fake_git.opened
    assert raw['coverage']['sensitive_excluded'] == 1
    assert raw['coverage']['redaction_blocked'] == 1
    assert opened == [('plain.zig', 'orders inventory')]
    assert 'synthetic-sensitive-marker' not in json.dumps(result)


def test_redacted_source_is_only_adapter_input(fake_git, monkeypatch):
    monkeypatch.setattr(core, '_redact_text', lambda text, **kwargs: ('safe replacement', []))
    seen = []
    adapter = SimpleNamespace(TECHNOLOGIES={'Fixture': ()},
        detect=lambda path, text: seen.append(text) or set(), analyze=lambda *a, **k: [])
    _, result = build(fake_git, {'input.txt': 'synthetic-private-value'}, technology_adapters=(adapter,))
    assert seen == ['safe replacement']
    assert 'synthetic-private-value' not in json.dumps(result)


def test_wrong_head_stops_before_adapter(fake_git):
    raw, _ = build(fake_git, {'plain.txt': 'orders'})
    raw['evidence'][0]['exact_head'] = 'b' * 40
    adapter = SimpleNamespace(TECHNOLOGIES={'Fixture': ()},
        detect=lambda *a: pytest.fail('head must gate adapter'), analyze=lambda *a, **k: [])
    with pytest.raises(ValueError, match='TECHNOLOGY_HEAD_MISMATCH'):
        enrich_technology(raw, adapters=(adapter,))


@pytest.mark.parametrize('stage', ['detect', 'analyze', 'scope_eligible'])
def test_adapter_crash_preserves_generic_safe_evidence(fake_git, stage):
    def crash(*args, **kwargs):
        raise RuntimeError('synthetic-private-exception')
    adapter = SimpleNamespace(TECHNOLOGIES={'Fixture': ()},
        detect=lambda *a: {'Fixture'}, analyze=lambda *a, **k: [], scope_eligible=lambda *a: set())
    setattr(adapter, stage, crash)
    raw, result = build(fake_git, {'plain.zig': 'orders inventory'}, technology_adapters=(adapter,))
    assert result['evidence'][0]['content'] == raw['evidence'][0]['content']
    assert result['technology_detection']['fallbacks']
    assert 'synthetic-private-exception' not in json.dumps(result)


def test_unknown_stack_batches_resume_without_network_provider_or_file_read(fake_git, monkeypatch):
    import builtins
    import socket
    import subprocess
    from app import project_profile_generation as provider
    raw, _ = build(fake_git, {'orders.zig': 'pub fn orders() void {} // inventory'})
    counts = {'io': 0, 'provider': 0}
    def no_io(*a, **k):
        counts['io'] += 1
        raise AssertionError('unexpected IO')
    def no_provider(*a, **k):
        counts['provider'] += 1
        raise AssertionError('unexpected provider')
    # All inputs are already Core-safe strings. No source or dependency loader is needed.
    with monkeypatch.context() as guard:
        guard.setattr(builtins, 'open', no_io)
        guard.setattr(socket.socket, 'connect', no_io)
        guard.setattr(socket, 'getaddrinfo', no_io)
        guard.setattr(subprocess, 'run', no_io)
        guard.setattr(provider, '_provider_adapter_resolver', no_provider)
        guard.setattr(provider, '_read_provider_credential', no_provider)
        indexed = index_repository_map(raw)
        assert all(v == 'UNKNOWN' for v in states(indexed).values())
        plan = {'schema_version': 'project_profile_v2', 'planned_modules': [
            {'client_id': 'orders', 'name': 'orders', 'requirements': ['inventory']}]}
        batches = plan_reconciliation_batches(plan, indexed['evidence'], exact_head=HEAD,
                                               plan_profile_id=1, budget_record=BUDGET)
        with sqlite3.connect(':memory:') as conn:
            for batch in batches:
                first = run_batch(conn, batch, current_head=lambda: HEAD,
                    fake_dispatch=lambda b: [{'planned_module_id': 'orders', 'status': 'unknown', 'evidence_ids': [], 'rationale': 'Synthetic unknown stack has insufficient implementation evidence.'}])
                assert first['status'] == 'succeeded'
                replay = run_batch(conn, batch, current_head=lambda: HEAD,
                                   fake_dispatch=lambda b: pytest.fail('completed batch resent'))
                assert replay['status'] == 'succeeded'
            assert aggregate_batches(conn, batches, current_head=lambda: HEAD)[0]['status'] == 'unknown'
        assert counts == {'io': 0, 'provider': 0}
        assert indexed['provider_calls'] == indexed['network_model_calls'] == 0
