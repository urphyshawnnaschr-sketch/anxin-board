"""Read-only change counts from the exact GitSnapshot bound by a V3 approval."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re

from app.db import get_connection
from app.approved_report_narrative import _binding as report_binding
from app.approved_module_narrative import load_module_narrative_target

SCHEMA_VERSION = 'approved_report_git_metrics_v1'
_BINDINGS = ('report_version_id', 'anxin_board_report_hash', 'approval_snapshot_id',
             'approval_snapshot_hash', 'git_snapshot_id', 'git_facts_hash')
_FACTS = {'branch', 'from_commit', 'to_commit', 'commits', 'commit_count',
          'changed_file_count', 'added_lines', 'deleted_lines', 'diff_bytes'}
_COUNTS = ('added_lines', 'deleted_lines', 'changed_file_count', 'commit_count')
_KEYS = {'schema_version', 'project_id', *_BINDINGS, 'source_git_facts', 'metrics', 'metrics_hash'}


class ApprovedReportGitMetricsError(ValueError):
    def __init__(self, code='APPROVED_REPORT_GIT_METRICS_INVALID'):
        self.code = code
        super().__init__(code)


def _check(ok):
    if not ok:
        raise ApprovedReportGitMetricsError()


def _hash(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _identity(project_id, report, approval):
    report_binding(project_id, report, approval)
    _check(type(project_id) is int and 0 < project_id <= 2**63 - 1)
    for key in ('git_snapshot_id', 'git_facts_hash'):
        value = report.get(key)
        _check(value == approval.get(key))
        _check((type(value) is int and 0 < value <= 2**63 - 1) if key.endswith('_id') else
               (type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None))
    return dict(project_id=project_id, **{key: report[key] for key in _BINDINGS})


def _metrics(facts, report, approval):
    _check(type(facts) is dict and set(facts) == _FACTS)
    branch = facts['branch']
    _check(type(branch) is str and branch.strip() == branch and branch)
    _check(all(ord(c) >= 32 and ord(c) != 127 for c in branch))
    for field, bound in (('branch', 'git_branch'), ('from_commit', 'git_from_commit'), ('to_commit', 'git_to_commit')):
        _check(facts[field] == report.get(bound) == approval.get(bound))
    for field in ('from_commit', 'to_commit'):
        _check(type(facts[field]) is str and re.fullmatch(r'[0-9a-f]{40}', facts[field]) is not None)
    for field in (*_COUNTS, 'diff_bytes'):
        _check(type(facts[field]) is int and 0 <= facts[field] <= 2**53 - 1)
    commits = facts['commits']
    _check(type(commits) is list and all(type(value) is str and re.fullmatch(r'[0-9a-f]{40}', value) for value in commits))
    _check(len(commits) == len(set(commits)) == facts['commit_count'])
    _check(facts['changed_file_count'] > 0 or facts['added_lines'] == facts['deleted_lines'] == 0)
    _check(_hash(facts) == report['git_facts_hash'] == approval['git_facts_hash'])
    total = facts['added_lines'] + facts['deleted_lines']
    _check(total <= 2**53 - 1)
    return dict(**{key: facts[key] for key in _COUNTS},
                net_added_lines=facts['added_lines'] - facts['deleted_lines'], line_change_total=total)


def validate_approved_report_git_metrics(value, report, approval_snapshot):
    """Pure hash/approval closure for frontend-equivalent and email render consumers."""
    try:
        _check(type(value) is dict and set(value) == _KEYS and value['schema_version'] == SCHEMA_VERSION)
        identity = _identity(value['project_id'], report, approval_snapshot)
        _check(all(value[key] == expected for key, expected in identity.items()))
        expected_metrics = _metrics(value['source_git_facts'], report, approval_snapshot)
        _check(type(value['metrics']) is dict and set(value['metrics']) == set(expected_metrics))
        _check(all(type(value['metrics'][key]) is int and value['metrics'][key] == expected for key, expected in expected_metrics.items()))
        _check(_hash({key: item for key, item in value.items() if key != 'metrics_hash'}) == value['metrics_hash'])
        return deepcopy(value)
    except ApprovedReportGitMetricsError:
        raise
    except Exception:
        raise ApprovedReportGitMetricsError() from None


def load_approved_report_git_metrics(project_id, report, approval_snapshot, conn=None):
    """Use the frozen approval's snapshot ID, never latest state or a fresh Git scan.

    Missing or corrupt facts raise an error. Zero is returned only when the frozen
    hash-closed facts contain a genuine numeric zero. No schema writes occur.
    """
    def read(active):
        identity = _identity(project_id, report, approval_snapshot)
        actual_report, actual_approval, _ = load_module_narrative_target(
            active, project_id, identity['report_version_id'], identity['anxin_board_report_hash'])
        _check(actual_report == report and all(actual_approval.get(key) == item for key, item in approval_snapshot.items()))
        row = active.execute('SELECT * FROM git_snapshots WHERE id=? AND project_id=?',
                             (identity['git_snapshot_id'], project_id)).fetchone()
        _check(row is not None)
        facts = {key: row[key] for key in _FACTS if key != 'commits'}
        facts['commits'] = json.loads(row['commits_json'])
        metrics = _metrics(facts, actual_report, actual_approval)
        value = dict(schema_version=SCHEMA_VERSION, **identity, source_git_facts=facts, metrics=metrics)
        value['metrics_hash'] = _hash(value)
        return validate_approved_report_git_metrics(value, actual_report, actual_approval)
    try:
        if conn is not None:
            return read(conn)
        with closing(get_connection()) as owned:
            owned.execute('BEGIN')
            return read(owned)
    except ApprovedReportGitMetricsError:
        raise
    except Exception:
        raise ApprovedReportGitMetricsError() from None
