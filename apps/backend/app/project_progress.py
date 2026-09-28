"""Immutable, approval-transaction-owned cumulative development progress.

No connections, commits, Git reads or model calls are made here. The caller must
validate approval authority and Git ancestry before supplying a parent HEAD.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3

from app.client_stage_summary import build_client_stage_summary
from app.project_profile_v2 import profile_planned_modules

SCHEMA_VERSION = 'project_progress_v1'
_SCOPE_KEYS = {'project_id', 'git_url', 'branch', 'profile_id', 'profile_content_hash', 'analysis_lineage_id'}
_REPORT_KEYS = {'report_version_id', 'report_content_hash', 'approval_snapshot_id', 'approval_snapshot_hash', 'head_sha', 'parent_head_sha'}
_STAGES = ('暂时无法确认', '开发中', '等待联调', '等待测试', '测试中', '已完成')
_BASE = {'implemented': '已完成', 'partial': '开发中', 'unknown': '暂时无法确认', 'not_started': '暂时无法确认'}


class ProjectProgressError(ValueError):
    code = 'PROJECT_PROGRESS_INVALID'

    def __init__(self):
        super().__init__(self.code)


def _require(condition):
    if not condition:
        raise ProjectProgressError()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProjectProgressError() from exc


def _hash(value):
    try:
        return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()
    except UnicodeError as exc:
        raise ProjectProgressError() from exc


def _text(value, limit=500):
    return type(value) is str and 0 < len(value) <= limit and value == value.strip() and not any(ord(c) < 32 for c in value)


def _hex(value, lengths=(64,)):
    return type(value) is str and len(value) in lengths and re.fullmatch('[0-9a-f]+', value) is not None


def _scope(scope):
    _require(type(scope) is dict and set(scope) == _SCOPE_KEYS)
    for key in ('project_id', 'profile_id', 'analysis_lineage_id'):
        _require(type(scope[key]) is int and scope[key] > 0)
    for key in ('git_url', 'branch'):
        _require(_text(scope[key]))
    _require(_hex(scope['profile_content_hash']))
    return _hash(scope)


def _refs(value):
    _require(type(value) is list and len(value) <= 100)
    _require(all(_text(item) for item in value))
    _require(len(set(value)) == len(value))
    return list(value)


def _summary(name, stage):
    # Local semantics: development complete does not assert testing/acceptance.
    try:
        result = build_client_stage_summary(module_name=name, stage=stage)
    except ValueError as exc:
        raise ProjectProgressError() from exc
    if stage == '已完成':
        result['summary'] = '这项功能已完成当前约定范围内的开发。'
        result['next_step'] = '根据约定继续验证和验收；发现问题时更新进展。'
        result['client_stage_summary_hash'] = _hash({k: v for k, v in result.items() if k != 'client_stage_summary_hash'})
    return result


def _closed(value):
    _require(type(value) is dict and value.get('schema_version') == SCHEMA_VERSION)
    _require(value.get('snapshot_hash') == _hash({k: v for k, v in value.items() if k != 'snapshot_hash'}))
    _require(value.get('scope_hash') == _scope(value.get('scope')))
    return copy.deepcopy(value)


def compute_snapshot(*, scope, profile, report, updates, previous=None):
    """Compute from confirmed scope and explicitly bound, approved module updates.

    Unknown observations never erase prior knowledge. A backward stage transition
    needs explicit regression_reason plus evidence. These are authority inputs,
    not a claim that this module independently proves evidence semantics.
    """
    scope_hash = _scope(scope)
    _require(type(profile) is dict and profile.get('status') == 'confirmed')
    _require(profile.get('id') == scope['profile_id'] and profile.get('content_hash') == scope['profile_content_hash'])
    _require('project_id' not in profile or profile['project_id'] == scope['project_id'])
    content = profile.get('content')
    _require(type(content) is dict and _hash(content) == scope['profile_content_hash'])
    _require(type(report) is dict and set(report) == _REPORT_KEYS)
    for key in ('report_version_id', 'approval_snapshot_id'):
        _require(type(report[key]) is int and report[key] > 0)
    for key in ('report_content_hash', 'approval_snapshot_hash'):
        _require(_hex(report[key]))
    for key in ('head_sha', 'parent_head_sha'):
        _require(_hex(report[key], (40, 64)))
    _require(type(updates) is dict)
    planned = profile_planned_modules(content)
    _require(type(planned) is list and 0 < len(planned) <= 100)
    _require(all(type(item) is dict for item in planned))
    ids = [item.get('client_id') for item in planned]
    names = [item.get('name') for item in planned]
    _require(all(_text(name, 80) for name in names) and len(set(names)) == len(names))
    _require(all(type(x) is str and re.fullmatch('[A-Za-z0-9_-]{1,64}', x) for x in ids))
    _require(len(set(ids)) == len(ids) and not set(updates) - set(ids))
    mappings = content.get('implementation_mappings', [])
    _require(type(mappings) is list and all(type(item) is dict for item in mappings))
    mapping_ids = [item.get('planned_module_id') for item in mappings]
    _require(all(type(x) is str for x in mapping_ids))
    _require(len(set(mapping_ids)) == len(mapping_ids) and not set(mapping_ids) - set(ids))
    mapping = {item['planned_module_id']: item for item in mappings}
    heads = {item.get('exact_head') for item in mappings if item.get('exact_head')}
    _require(len(heads) <= 1 and all(_hex(head, (40, 64)) for head in heads))
    if previous is not None:
        previous = _closed(previous)
        _require(previous['scope'] == scope and previous['scope_hash'] == scope_hash)
        _require(report['parent_head_sha'] == previous['report']['head_sha'])
        _require([m['module_id'] for m in previous['modules']] == ids)
        _require(report['report_version_id'] != previous['report']['report_version_id'])
        prior = {m['module_id']: m for m in previous['modules']}
    else:
        if heads:
            _require(report['parent_head_sha'] == next(iter(heads)))
        prior = {}
    modules = []
    for module in planned:
        module_id, name = module['client_id'], module.get('name')
        baseline = mapping.get(module_id, {})
        status = baseline.get('status', 'unknown')
        _require(status in _BASE)
        stage = _BASE[status]
        item = copy.deepcopy(prior.get(module_id))
        if item is None:
            refs = _refs(baseline.get('evidence_ids', []))
            _require(status not in ('implemented', 'partial') or bool(refs))
            item = {'module_id': module_id, 'module_name': name, 'stage': stage,
                    'baseline_status': status, 'evidence_ids': refs, 'regression_reason': None,
                    'source_report_version_id': None, 'source_head_sha': baseline.get('exact_head') or None,
                    'client_stage_summary': _summary(name, stage)}
        _require(item['module_name'] == name)
        if module_id in updates:
            update = updates[module_id]
            _require(type(update) is dict and {'stage', 'evidence_ids'} <= set(update) <= {'stage', 'evidence_ids', 'regression_reason'})
            stage = update['stage']
            _require(stage in _STAGES)
            refs = _refs(update['evidence_ids'])
            reason = update.get('regression_reason')
            _require(reason is None or _text(reason, 2000))
            if stage != '暂时无法确认' or reason is not None:
                _require(bool(refs))
                if _STAGES.index(stage) < _STAGES.index(item['stage']):
                    _require(reason is not None)
                item.update(stage=stage, evidence_ids=refs, regression_reason=reason,
                            source_report_version_id=report['report_version_id'], source_head_sha=report['head_sha'],
                            client_stage_summary=_summary(name, stage))
        modules.append(item)
    result = {'schema_version': SCHEMA_VERSION, 'scope': copy.deepcopy(scope), 'scope_hash': scope_hash,
              'report': copy.deepcopy(report), 'previous_snapshot_hash': previous['snapshot_hash'] if previous else None,
              'modules': modules}
    result['snapshot_hash'] = _hash(result)
    return result


def to_module_summaries(snapshot):
    """Project the frozen cumulative state without changing the V3 summary shape."""
    value = _closed(snapshot)
    return [_summary(item['module_name'], item['stage']) for item in value['modules']]


def ensure_schema(conn: sqlite3.Connection):
    """Called by startup/migration owner; never commits an existing transaction."""
    conn.execute('''CREATE TABLE IF NOT EXISTS project_progress_snapshots (
        id INTEGER PRIMARY KEY, scope_hash TEXT NOT NULL, report_version_id INTEGER NOT NULL UNIQUE,
        approval_snapshot_id INTEGER NOT NULL UNIQUE, input_hash TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS project_progress_scope ON project_progress_snapshots(scope_hash, id)')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS project_progress_no_{action.lower()}
            BEFORE {action} ON project_progress_snapshots BEGIN
            SELECT RAISE(ABORT, 'PROJECT_PROGRESS_IMMUTABLE'); END''')


def read_latest(conn: sqlite3.Connection, *, scope):
    scope_hash = _scope(scope)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_progress_snapshots'").fetchone() is None:
        return None
    row = conn.execute('SELECT snapshot_json FROM project_progress_snapshots WHERE scope_hash=? ORDER BY id DESC LIMIT 1', (scope_hash,)).fetchone()
    if row is None:
        return None
    try:
        value = _closed(json.loads(row[0]))
        _require(value['scope'] == scope)
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise ProjectProgressError() from exc


def append_snapshot(conn: sqlite3.Connection, *, scope, profile, report, updates):
    """Append inside caller's Human approval transaction; never begins/commits."""
    _require(conn.in_transaction)
    _scope(scope)
    input_hash = _hash({'scope': scope, 'profile': profile, 'report': report, 'updates': updates})
    _require(type(report) is dict and type(report.get('report_version_id')) is int)
    row = conn.execute('SELECT input_hash,snapshot_json FROM project_progress_snapshots WHERE report_version_id=?', (report['report_version_id'],)).fetchone()
    if row is not None:
        _require(row[0] == input_hash)
        return _closed(json.loads(row[1]))
    value = compute_snapshot(scope=scope, profile=profile, report=report, updates=updates, previous=read_latest(conn, scope=scope))
    try:
        conn.execute('INSERT INTO project_progress_snapshots(scope_hash,report_version_id,approval_snapshot_id,input_hash,snapshot_hash,snapshot_json) VALUES(?,?,?,?,?,?)',
                     (value['scope_hash'], report['report_version_id'], report['approval_snapshot_id'], input_hash, value['snapshot_hash'], _json(value)))
    except sqlite3.IntegrityError as exc:
        raise ProjectProgressError() from exc
    return value
