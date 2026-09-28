"""Independent source-binding regressions using only synthetic Core Git objects."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_profile_repository_map import fake_git, HEAD
from app.profile_repository_map import build_repository_map
from app.repository_index import index_repository_map
from app.repository_technology import enrich_technology, safe_sources
from app.repository_evidence import validate_technology_evidence
from app.profile_reconciliation_batches import plan_reconciliation_batches

SOURCE = ('import org.springframework.web.bind.annotation.RestController;\n'
          '@RestController public class Orders {}\n'
          'class Unrelated {}\n')
BUDGET = dict(context_window_tokens=18000, max_output_tokens=2000,
              reserved_output_tokens=2000, safety_margin_tokens=1000,
              counting_policy_version='utf8_byte_upper_bound_v1')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def resign(record):
    # Integrity digest is not an authorization signature. Recompute it so failures
    # must come from actual source bindings, not merely an outdated checksum.
    value = {k: v for k, v in record.items() if k != 'evidence_id'}
    record['evidence_id'] = 'technology-' + digest(json.dumps(value, sort_keys=True))[:24]


def raw_map(fake_git, files=None):
    for path, text in (files or {'Orders.java': SOURCE, 'Elsewhere.java': 'class OtherOnly {}'}).items():
        fake_git.add(path, text.encode())
    return build_repository_map(2, {'git_url': 'https://github.com/example/project.git'},
                                {'remote_head': HEAD})


def evidence(fake_git):
    indexed = index_repository_map(raw_map(fake_git))
    return copy.deepcopy(next(c for c in indexed['evidence'] if c['technology_evidence']))


def plan(fragment):
    return plan_reconciliation_batches(
        {'schema_version': 'project_profile_v2', 'planned_modules': [
            {'client_id': 'orders', 'name': 'Orders', 'requirements': ['controller']}]},
        [fragment], exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)


def test_valid_record_binds_exact_slice_and_core_source_hash(fake_git):
    fragment = evidence(fake_git)
    validate_technology_evidence(fragment)
    for record in fragment['technology_evidence']:
        start = record['char_start'] - fragment['char_start']
        end = record['char_end'] - fragment['char_start']
        assert record['source_slice_hash'] == digest(fragment['content'][start:end])
        assert record['source_hash'] == fragment['source_hash']
    assert plan(fragment)


@pytest.mark.parametrize('mutation', [
    'head', 'path', 'object', 'source_id', 'range', 'slice', 'source_hash',
    'symbol_outside_slice', 'cross_file_relation', 'classification',
])
def test_rehashed_invalid_record_is_rejected_before_batching(fake_git, mutation):
    fragment = evidence(fake_git)
    record = fragment['technology_evidence'][0]
    if mutation == 'head': record['exact_head'] = 'b' * 40
    elif mutation == 'path': record['source_path'] = 'Elsewhere.java'
    elif mutation == 'object': record['object_sha'] = 'b' * 40
    elif mutation == 'source_id': record['source_evidence_ids'] = ['repo-code-not-the-current-chunk']
    elif mutation == 'range': record['char_end'] = fragment['char_end'] + 1
    elif mutation == 'slice': record['source_slice_hash'] = digest('absent source slice')
    elif mutation == 'source_hash': record['source_hash'] = digest('different file source')
    elif mutation == 'symbol_outside_slice':
        # Primary symbol must be inside the precise relation span, not merely in file.
        record['symbol'] = 'Unrelated'
    elif mutation == 'cross_file_relation':
        record['related_symbol'] = 'OtherOnly'
        record['related_path'] = 'Elsewhere.java'
    elif mutation == 'classification': record['classification'] = 'DECLARED_ONLY'
    resign(record)
    with pytest.raises(ValueError):
        validate_technology_evidence(fragment)
    with pytest.raises(ValueError):
        plan(fragment)


def test_adapter_cannot_attach_relation_from_another_source_file(fake_git):
    raw = raw_map(fake_git, {'source.txt': 'localSymbol\n', 'other.txt': 'foreignSymbol\n'})
    adapter = SimpleNamespace(
        TECHNOLOGIES={'Fixture': ()}, detect=lambda path, text: {'Fixture'} if path == 'source.txt' else set(),
        analyze=lambda *a, **k: [dict(technology='Fixture', adapter_id='fixture',
            adapter_version='1', evidence_type='relation', symbol='localSymbol',
            relation='uses', related_symbol='foreignSymbol', char_start=0, char_end=11)])
    result = enrich_technology(raw, adapters=(adapter,))
    assert not any(c['technology_evidence'] for c in result['evidence'])
    assert result['technology_detection']['fallbacks']


def test_self_consistent_chunk_mutation_disagrees_with_core_inventory(fake_git):
    raw = raw_map(fake_git, {'source.txt': 'original source\n'})
    fragment = raw['evidence'][0]
    fragment['content'] = fragment['content'].replace('original', 'replaced')
    fragment['content_hash'] = digest(fragment['content'])
    fragment['char_end'] = len(fragment['content'])
    with pytest.raises(ValueError):
        safe_sources(raw)


@pytest.mark.parametrize('remove_all', [False, True])
def test_missing_tail_or_all_chunks_cannot_claim_complete_safe_source(fake_git, remove_all):
    raw = raw_map(fake_git, {'source.txt': 'ordinary source line\n' * 1200})
    assert len(raw['evidence']) > 1
    raw['evidence'] = [] if remove_all else raw['evidence'][:-1]
    with pytest.raises(ValueError):
        safe_sources(raw)
