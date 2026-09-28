from copy import deepcopy

import pytest

from app.brownfield_baseline_aggregate import aggregate_baseline, BaselineAggregateError
from app.project_profile_v2 import ProjectProfileV2Content

HEAD = 'a' * 40


def plan():
    return ProjectProfileV2Content.model_validate({
        'schema_version': 'project_profile_v2', 'project_summary': 'preserve plan',
        'planned_modules': [{'client_id': 'm1', 'requirements': ['first', 'second']},
                            {'client_id': 'm2', 'requirements': ['third']}],
        'notes': 'original notes', 'exclude_patterns': ['vendor/**'],
    }).model_dump()


def evidence():
    return {f'repo-code-{i}': {'evidence_id': f'repo-code-{i}', 'exact_head': HEAD,
                              'path': f'src/{i}.py'} for i in range(3)}


def row(index, status='implemented', refs=None, rationale='validated assessment'):
    return {'requirement_index': index, 'status': status,
            'evidence_ids': ([] if status == 'unknown' else ['repo-code-0']) if refs is None else refs,
            'rationale': rationale}


def record(*rows, allowed=None):
    return {'requirements': list(rows), 'bundle_hash': 'b' * 64,
            'allowed_evidence_ids': ['repo-code-0', 'repo-code-1', 'repo-code-2'] if allowed is None else allowed}


def rounds():
    return {'m1': [record(row(0)), record(row(1, 'unknown'))],
            'm2': [record(row(0, refs=['repo-code-2']))]}


def run(p=None, r=None, e=None):
    return aggregate_baseline(plan() if p is None else p, exact_head=HEAD,
                              rounds=rounds() if r is None else r, evidence=evidence() if e is None else e)


def test_split_requirements_merge_preserves_plan_and_removes_stale_assessments():
    p = plan()
    p['implementation_mappings'] = [{'stale': 'discard'}]
    p['unplanned_code_features'] = [{'stale': 'discard'}]
    before = deepcopy(p)
    result = run(p=p)
    assert p == before
    content = result['content']
    ProjectProfileV2Content.model_validate(content)
    assert all(content[k] == p[k] for k in p if k not in {'implementation_mappings', 'unplanned_code_features'})
    assert content['unplanned_code_features'] == []
    m1, m2 = content['implementation_mappings']
    assert [m1['status'], m2['status']] == ['partial', 'implemented']
    assert m1['evidence_ids'] == ['repo-code-0']
    assert [p['pattern'] for p in m1['paths']] == ['src/0.py']
    assert 'validated assessment' not in m1['rationale']
    assert [r['requirement_index'] for r in result['requirements']['m1']] == [0, 1]
    assert result['requirements']['m1'][1]['evidence_ids'] == []


@pytest.mark.parametrize(('statuses', 'expected'), [
    (['implemented', 'unknown'], 'implemented'),
    (['implemented', 'partial'], 'partial'),
    (['partial', 'unknown'], 'partial'),
    (['implemented', 'implemented'], 'implemented'),
])
def test_conservative_positive_merge_across_rounds(statuses, expected):
    r = rounds()
    r['m2'] = [record(row(0, status, refs=[] if status == 'unknown' else [f'repo-code-{i}']))
               for i, status in enumerate(statuses)]
    before = deepcopy(r)
    result = run(r=r)
    assert r == before
    merged = result['requirements']['m2'][0]
    assert merged['status'] == expected
    assert merged['evidence_ids'] == [f'repo-code-{i}' for i, status in enumerate(statuses) if status != 'unknown']


def test_empty_requirement_module_explicit_unknown_and_no_refs():
    p = plan(); p['planned_modules'][1]['requirements'] = []
    r = rounds(); r['m2'] = []
    result = run(p=p, r=r)
    mapping = result['content']['implementation_mappings'][1]
    assert mapping['status'] == 'unknown' and mapping['paths'] == [] and mapping['evidence_ids'] == []
    assert result['requirements']['m2'] == []


def test_missing_requirements_uses_v2_empty_default_without_rewriting_plan():
    p = plan(); del p['planned_modules'][1]['requirements']
    r = rounds(); r['m2'] = []
    result = run(p=p, r=r)
    assert result['content']['planned_modules'] == p['planned_modules']
    assert result['content']['implementation_mappings'][1]['status'] == 'unknown'


def test_module_implemented_requires_every_requirement_and_unknown_module_is_empty():
    r = {'m1': [record(row(0), row(1))], 'm2': [record(row(0, 'unknown'))]}
    result = run(r=r)
    first, second = result['content']['implementation_mappings']
    assert first['status'] == 'implemented'
    assert second['status'] == 'unknown' and second['paths'] == [] and second['evidence_ids'] == []


def test_requirement_ui_summary_uses_partial_rationale_on_disagreement():
    r = rounds()
    r['m2'] = [record(row(0, rationale='complete claim')),
               record(row(0, 'partial', rationale='specific missing behavior'))]
    result = run(r=r)
    assert result['requirements']['m2'][0]['rationale'] == 'specific missing behavior'
    assert 'specific missing behavior' not in result['content']['implementation_mappings'][1]['rationale']


def test_all_unknown_rejected_with_existing_product_code():
    r = {'m1': [record(row(0, 'unknown'), row(1, 'unknown'))], 'm2': [record(row(0, 'unknown'))]}
    with pytest.raises(BaselineAggregateError, match='^ANALYSIS_EMPTY_NO_CODE_EVIDENCE$'):
        run(r=r)


@pytest.mark.parametrize('mutation', ['missing_module', 'extra_module', 'missing_requirement', 'duplicate_row', 'bool_index', 'outside_index', 'negative_index', 'wrong_status', 'unknown_foreign_refs', 'positive_empty', 'foreign_ref', 'unallowed_ref', 'wrong_head', 'wrong_identity', 'bad_path', 'bad_bundle_hash', 'duplicate_allowed', 'nonlist_refs', 'bad_rationale'])
def test_invalid_scope_and_evidence_fail_closed(mutation):
    p, r, e = plan(), rounds(), evidence()
    target = r['m1'][0]['requirements'][0]
    if mutation == 'missing_module': del r['m2']
    elif mutation == 'extra_module': r['outside'] = []
    elif mutation == 'missing_requirement': r['m1'] = r['m1'][:1]
    elif mutation == 'duplicate_row': r['m1'][0]['requirements'].append(deepcopy(target))
    elif mutation == 'bool_index': target['requirement_index'] = True
    elif mutation == 'outside_index': target['requirement_index'] = 2
    elif mutation == 'negative_index': target['requirement_index'] = -1
    elif mutation == 'wrong_status': target['status'] = 'not_started'
    elif mutation == 'unknown_foreign_refs': target.update(status='unknown', evidence_ids=['repo-code-outside'])
    elif mutation == 'positive_empty': target['evidence_ids'] = []
    elif mutation == 'foreign_ref': target['evidence_ids'] = ['repo-code-foreign']
    elif mutation == 'unallowed_ref': r['m1'][0]['allowed_evidence_ids'] = ['repo-code-1']
    elif mutation == 'wrong_head': e['repo-code-0']['exact_head'] = 'c' * 40
    elif mutation == 'wrong_identity': e['repo-code-0']['evidence_id'] = 'repo-code-other'
    elif mutation == 'bad_path': e['repo-code-0']['path'] = '../outside.py'
    elif mutation == 'bad_bundle_hash': r['m1'][0]['bundle_hash'] = 'wrong'
    elif mutation == 'duplicate_allowed': r['m1'][0]['allowed_evidence_ids'].append('repo-code-0')
    elif mutation == 'nonlist_refs': target['evidence_ids'] = 'repo-code-0'
    elif mutation == 'bad_rationale': target['rationale'] = ''
    with pytest.raises(BaselineAggregateError) as exc:
        run(p=p, r=r, e=e)
    assert str(exc.value).isupper() and 'validated assessment' not in str(exc.value)


@pytest.mark.parametrize('same_path', [False, True])
def test_capacity_overflow_not_silently_truncated(same_path):
    e = {f'repo-code-{i}': {'evidence_id': f'repo-code-{i}', 'exact_head': HEAD,
                           'path': 'src/shared.py' if same_path else f'src/{i}.py'} for i in range(101)}
    r = rounds()
    r['m2'] = [record(row(0, refs=[identity]), allowed=[identity]) for identity in e]
    with pytest.raises(BaselineAggregateError, match='^CANDIDATE_EVIDENCE_CAPACITY_EXCEEDED$'):
        run(r=r, e=e)


def test_100_evidence_boundary_and_deterministic_path_union():
    e = {f'repo-code-{i}': {'evidence_id': f'repo-code-{i}', 'exact_head': HEAD,
                           'path': f'src/{i}.py'} for i in range(100)}
    r = rounds(); r['m2'] = [record(row(0, refs=[identity]), allowed=[identity]) for identity in e]
    result = run(r=r, e=e)
    assert len(result['content']['implementation_mappings'][1]['evidence_ids']) == 100
    assert result == run(r=deepcopy(r), e=deepcopy(e))


@pytest.mark.parametrize('head', ['', 'unknown', True])
def test_invalid_head_rejected(head):
    with pytest.raises(BaselineAggregateError):
        aggregate_baseline(plan(), exact_head=head, rounds=rounds(), evidence=evidence())

def test_unknown_context_refs_preserved_without_implementation_mapping_credit():
    r=rounds();r['m1'][1]=record(row(1,'unknown',refs=['repo-code-1']))
    r['m2']=[record(row(0,'unknown',refs=['repo-code-2']))]
    result=run(r=r)
    assert result['requirements']['m1'][1]['evidence_ids']==['repo-code-1']
    assert result['requirements']['m1'][1]['status']=='unknown'
    assert result['content']['implementation_mappings'][0]['evidence_ids']==['repo-code-0']
    assert result['requirements']['m2'][0]['evidence_ids']==['repo-code-2']
    assert result['content']['implementation_mappings'][1]['status']=='unknown'
    assert result['content']['implementation_mappings'][1]['evidence_ids']==[]
    assert result['content']['implementation_mappings'][1]['paths']==[]

def test_unknown_context_refs_never_join_positive_requirement_evidence():
    r=rounds();r['m2'].append(record(row(0,'unknown',refs=['repo-code-1'])))
    result=run(r=r)
    assert result['requirements']['m2'][0]['status']=='implemented'
    assert result['requirements']['m2'][0]['evidence_ids']==['repo-code-2']

def test_all_unknown_with_context_refs_still_not_useful_result():
    r={'m1':[record(row(0,'unknown',refs=['repo-code-0']),row(1,'unknown',refs=['repo-code-1']))], 'm2':[record(row(0,'unknown',refs=['repo-code-2']))]}
    with pytest.raises(BaselineAggregateError,match='ANALYSIS_EMPTY_NO_CODE_EVIDENCE'):run(r=r)

@pytest.mark.parametrize('mutation',['head','bundle','duplicate'])
def test_unknown_context_refs_keep_evidence_owner_boundaries(mutation):
    r=rounds();e=evidence();r['m1'][1]=record(row(1,'unknown',refs=['repo-code-1']))
    if mutation=='head':e['repo-code-1']['exact_head']='b'*40
    elif mutation=='bundle':r['m1'][1]['allowed_evidence_ids']=['repo-code-0']
    else:r['m1'][1]['requirements'][0]['evidence_ids']=['repo-code-1','repo-code-1']
    with pytest.raises(BaselineAggregateError):run(r=r,e=e)

def test_unknown_requirement_reference_capacity_is_not_silently_truncated():
    r=rounds();e=evidence();records=[]
    for i in range(101):
        ref=f'repo-code-context-{i}'
        e[ref]={'evidence_id':ref,'exact_head':HEAD,'path':f'context/{i}.py'}
        records.append(record(row(1,'unknown',refs=[ref]),allowed=[ref]))
    r['m1']=[record(row(0))]+records
    with pytest.raises(BaselineAggregateError,match='CANDIDATE_EVIDENCE_CAPACITY_EXCEEDED'):run(r=r,e=e)
