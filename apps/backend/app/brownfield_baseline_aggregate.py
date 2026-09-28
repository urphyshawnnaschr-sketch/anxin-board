"""Pure exact-HEAD requirement aggregation; no provider, persistence, or promotion.

The caller must authenticate each request/bundle association before supplying these
validated records. Bundle digests identify records; this function does not recreate
their source bundles or establish provider-response provenance.
"""
from collections import Counter
from copy import deepcopy
import re

from pydantic import ValidationError

from app.project_profile_v2 import ProjectProfileV2Content


class BaselineAggregateError(ValueError):
    """Fixed safe error codes only; never include source or provider text."""


_HEAD = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_STATUSES = {'implemented', 'partial', 'unknown'}


def _reject(code):
    raise BaselineAggregateError(code)


def _ids(values):
    if (type(values) is not list or any(type(value) is not str or not value.startswith('repo-code-')
                                      or value == 'repo-code-' for value in values)
            or len(set(values)) != len(values)):
        _reject('BASELINE_EVIDENCE_INVALID')
    return values


def _source(identity, evidence, exact_head):
    item = evidence.get(identity)
    if (type(item) is not dict or item.get('evidence_id') != identity
            or item.get('exact_head') != exact_head):
        _reject('BASELINE_EVIDENCE_BINDING_INVALID')
    path = item.get('path')
    if (type(path) is not str or not path or len(path) > 500 or path != path.strip()
            or path.startswith('/') or '\\' in path or ':' in path
            or any(part in {'', '.', '..'} for part in path.split('/'))
            or any(ord(char) < 32 for char in path)):
        _reject('BASELINE_EVIDENCE_PATH_INVALID')
    return path


def _check_plan(plan):
    if type(plan) is not dict:
        _reject('BASELINE_PLAN_INVALID')
    result = deepcopy(plan)
    result['implementation_mappings'] = []
    result['unplanned_code_features'] = []
    try:
        ProjectProfileV2Content.model_validate(result)
    except (ValidationError, TypeError, ValueError):
        _reject('BASELINE_PLAN_INVALID')
    modules = result.get('planned_modules')
    if type(modules) is not list or not modules:
        _reject('BASELINE_PLAN_INVALID')
    for module in modules:
        if (type(module) is not dict or type(module.get('client_id')) is not str
                or type(module.get('requirements', [])) is not list
                or any(type(value) is not str or not value.strip() for value in module.get('requirements', []))):
            _reject('BASELINE_PLAN_INVALID')
    return result


def _assessments(records, count, evidence, exact_head):
    if type(records) is not list:
        _reject('BASELINE_REQUIREMENT_COVERAGE_INVALID')
    collected = [[] for _ in range(count)]
    for record in records:
        if (type(record) is not dict
                or set(record) != {'requirements', 'bundle_hash', 'allowed_evidence_ids'}
                or type(record['bundle_hash']) is not str or not _HASH.fullmatch(record['bundle_hash'])
                or type(record['requirements']) is not list):
            _reject('BASELINE_REQUEST_RECORD_INVALID')
        allowed = set(_ids(record['allowed_evidence_ids']))
        for identity in allowed:
            _source(identity, evidence, exact_head)
        seen = set()
        for row in record['requirements']:
            if type(row) is not dict or set(row) != {'requirement_index', 'status', 'evidence_ids', 'rationale'}:
                _reject('BASELINE_REQUIREMENT_INVALID')
            index, status, rationale = row['requirement_index'], row['status'], row['rationale']
            if type(index) is not int or not 0 <= index < count or index in seen:
                _reject('BASELINE_REQUIREMENT_COVERAGE_INVALID')
            if type(status) is not str or status not in _STATUSES:
                _reject('BASELINE_REQUIREMENT_INVALID')
            refs = _ids(row['evidence_ids'])
            if not set(refs) <= allowed:
                _reject('BASELINE_FOREIGN_EVIDENCE')
            if status != 'unknown' and not refs:
                _reject('BASELINE_REQUIREMENT_EVIDENCE_INVALID')
            if type(rationale) is not str or not rationale.strip() or len(rationale.strip()) > 1200:
                _reject('BASELINE_REQUIREMENT_INVALID')
            seen.add(index)
            collected[index].append(row)
    if any(not rows for rows in collected):
        _reject('BASELINE_REQUIREMENT_COVERAGE_INVALID')
    return collected


def aggregate_baseline(plan: dict, *, exact_head: str, rounds: dict[str, list[dict]],
                       evidence: dict[str, dict]) -> dict:
    """Merge validated request assessments without modifying the original plan.

    Unknown assessments are uncertainty, possibly with contextual citations, not implementation proof. Any partial
    positive assessment prevents an implemented merge for that requirement.
    """
    if type(exact_head) is not str or not _HEAD.fullmatch(exact_head):
        _reject('BASELINE_HEAD_INVALID')
    content = _check_plan(plan)
    module_ids = [module['client_id'] for module in content['planned_modules']]
    if type(rounds) is not dict or set(rounds) != set(module_ids):
        _reject('BASELINE_MODULE_COVERAGE_INVALID')
    if type(evidence) is not dict:
        _reject('BASELINE_EVIDENCE_INVALID')
    summaries = {}
    any_positive = False
    for module in content['planned_modules']:
        identity = module['client_id']
        collected = _assessments(rounds[identity], len(module.get('requirements', [])), evidence, exact_head)
        rows = []
        for index, assessments in enumerate(collected):
            positives = [item for item in assessments if item['status'] != 'unknown']
            status = ('partial' if any(item['status'] == 'partial' for item in positives)
                      else 'implemented') if positives else 'unknown'
            refs = sorted({ref for item in (positives or assessments) for ref in item['evidence_ids']})
            if len(refs) > 100:
                _reject('CANDIDATE_EVIDENCE_CAPACITY_EXCEEDED')
            chosen = next(item for item in assessments if item['status'] == status)
            rows.append({'requirement_index': index, 'status': status, 'evidence_ids': refs,
                         'rationale': chosen['rationale'].strip()})
        summaries[identity] = rows
        counts = Counter(row['status'] for row in rows)
        status = ('implemented' if rows and counts['implemented'] == len(rows)
                  else 'partial' if counts['implemented'] + counts['partial'] else 'unknown')
        refs = sorted({ref for row in rows if row['status'] != 'unknown' for ref in row['evidence_ids']})
        paths = sorted({_source(ref, evidence, exact_head) for ref in refs})
        if len(refs) > 100 or len(paths) > 100:
            _reject('CANDIDATE_EVIDENCE_CAPACITY_EXCEEDED')
        any_positive |= status != 'unknown'
        content['implementation_mappings'].append({
            'planned_module_id': identity, 'status': status, 'exact_head': exact_head,
            'evidence_ids': refs,
            'paths': [{'type': 'other', 'pattern': path, 'required': True, 'note': ''} for path in paths],
            'rationale': (f"Requirement evidence summary: total={len(rows)}; "
                          f"implemented={counts['implemented']}; partial={counts['partial']}; "
                          f"unknown={counts['unknown']}. Static evidence only."),
        })
    if not any_positive:
        _reject('ANALYSIS_EMPTY_NO_CODE_EVIDENCE')
    try:
        ProjectProfileV2Content.model_validate(content)
    except (ValidationError, TypeError, ValueError):
        _reject('BASELINE_CANDIDATE_INVALID')
    return {'content': content, 'requirements': summaries}
