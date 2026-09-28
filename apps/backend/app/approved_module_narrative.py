"""Append-only, explicitly confirmed explanations of a frozen module baseline.

This annotation has its own identity. It never rewrites AI output, a profile,
an approval, progress or the formal V3 report. Reads do not install schema.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from app.db import get_connection
from app import anxin_board_report_store as report_store
from app import report_approval as approvals
from app.approved_report_narrative import _binding as report_binding, _safe
from app.project_profiles import read_bound_project_profile_for_report

SCHEMA_VERSION = 'approved_module_narrative_v1'
_TABLE = 'approved_module_narratives'
_TARGET = ('anxin_board_report_hash', 'report_content_hash', 'approval_snapshot_id',
           'approval_snapshot_hash', 'profile_id', 'profile_content_hash')
_SOURCE = {'project_id', 'profile_id', 'profile_content_hash', 'task_id',
           'task_identity_hash', 'output_hash', 'exact_head'}
_MODULE = {'module_id', 'summary', 'remaining', 'requirement_refs'}
_REQUEST = {*_TARGET, 'source', 'confirmed_by', 'cross_project_reuse_ack', 'idempotency_key', 'modules'}
_VALUE = {*_REQUEST, 'schema_version', 'project_id', 'report_version_id', 'attribution',
          'confirmed_at', 'historical_baseline', 'approved_git_head', 'module_narrative_hash'}
_COLUMNS = {'schema_version', 'project_id', 'report_version_id', 'idempotency_key',
            'request_hash', 'narrative_hash', 'narrative_json'}


class ApprovedModuleNarrativeError(ValueError):
    def __init__(self, code='APPROVED_MODULE_NARRATIVE_INVALID'):
        self.code = code
        super().__init__(code)


def _check(ok, code='APPROVED_MODULE_NARRATIVE_INVALID'):
    if not ok:
        raise ApprovedModuleNarrativeError(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _id(value):
    _check(type(value) is int and 0 < value <= 2**63 - 1)


def _digest(value):
    _check(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None)


def _text(value, maximum=4000):
    _check(type(value) is str and 0 < len(value) <= maximum and value == value.strip())
    _check(all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in value))


def _safe_value(value):
    try:
        _safe(value)
    except Exception:
        raise ApprovedModuleNarrativeError('APPROVED_MODULE_NARRATIVE_UNSAFE') from None


def _identity(project_id, report, approval, profile):
    report_binding(project_id, report, approval)
    _id(profile.get('id'))
    _check(profile.get('project_id') == project_id and profile.get('status') in {'confirmed', 'superseded'})
    _check(profile['id'] == approval.get('profile_id') == report.get('profile_id'))
    _check(profile.get('content_hash') == approval.get('profile_content_hash') == report.get('profile_content_hash'))
    _check(_hash(profile['content']) == profile['content_hash'])
    return dict(project_id=project_id, report_version_id=report['report_version_id'],
                **{k: report[k] for k in _TARGET})


def _source_identity(source):
    _check(type(source) is dict and set(source) == _SOURCE)
    for key in ('project_id', 'profile_id'):
        _id(source[key])
    for key in ('profile_content_hash', 'task_identity_hash', 'output_hash'):
        _digest(source[key])
    _check(type(source['task_id']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', source['task_id']) is not None)
    _check(type(source['exact_head']) is str and re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', source['exact_head']) is not None)


def _requirement_facts(content, requirements, head):
    """Close all saved indices, statuses and positive evidence against the mapping."""
    _check(content.get('schema_version') == 'project_profile_v2')
    modules, mappings = content['planned_modules'], content['implementation_mappings']
    _check(type(requirements) is dict and modules)
    mapped = {m['planned_module_id']: m for m in mappings}
    _check(len(mapped) == len(mappings) == len(modules))
    _check(set(requirements) == set(mapped) == {m['client_id'] for m in modules})
    result = {}
    for module in modules:
        rows = requirements[module['client_id']]
        _check(type(rows) is list and len(rows) == len(module['requirements']) and rows)
        _check(all(type(r) is dict and set(r) == {'requirement_index','status','rationale','evidence_ids'} for r in rows))
        indices = [r['requirement_index'] for r in rows]
        _check(all(type(i) is int for i in indices) and sorted(indices) == list(range(len(rows))))
        positives = set()
        for row in rows:
            _check(row['status'] in {'implemented', 'partial', 'unknown'})
            _text(row['rationale'], 1200)
            refs = row['evidence_ids']
            _check(type(refs) is list and len(refs) <= 100 and all(type(ref) is str and re.fullmatch(r'repo-code-[A-Za-z0-9_-]{1,128}', ref) for ref in refs))
            _check(len(refs) == len(set(refs)) and (row['status'] == 'unknown' or bool(refs)))
            if row['status'] != 'unknown':
                positives.update(refs)
        statuses = [r['status'] for r in rows]
        expected = 'implemented' if all(s == 'implemented' for s in statuses) else 'partial' if any(s != 'unknown' for s in statuses) else 'unknown'
        mapping = mapped[module['client_id']]
        _check(mapping['status'] == expected and mapping['exact_head'] == head)
        _check(len(mapping['evidence_ids']) == len(set(mapping['evidence_ids'])) and set(mapping['evidence_ids']) == positives)
        result[module['client_id']] = [dict(**deepcopy(r), requirement_text=module['requirements'][r['requirement_index']])
                                       for r in sorted(rows, key=lambda x: x['requirement_index'])]
    _safe_value(result)
    return result


def _modules(values, profile, facts, *, stored=False):
    _check(type(values) is list and len(values) == len(profile['content']['planned_modules']))
    _check(all(type(m) is dict and set(m) == (_MODULE | {'source_requirements'} if stored else _MODULE) for m in values))
    by_id = {m['module_id']: m for m in values}
    _check(len(by_id) == len(values) and set(by_id) == set(facts))
    result = []
    for module in profile['content']['planned_modules']:
        value = by_id[module['client_id']]
        _text(value['summary']); _text(value['remaining'])
        refs = value['requirement_refs']
        _check(type(refs) is list and refs and all(type(i) is int and 0 <= i < len(module['requirements']) for i in refs))
        _check(refs == sorted(set(refs)))
        if stored:
            _check(value['source_requirements'] == facts[module['client_id']])
        result.append(dict(**{k: deepcopy(value[k]) for k in _MODULE}, source_requirements=deepcopy(facts[module['client_id']])))
    _safe_value(result)
    return result


def load_module_narrative_target(conn, project_id, report_version_id, report_hash):
    """Read the exact stored V3 and historical profile in the caller's transaction."""
    _id(project_id); _id(report_version_id); _digest(report_hash)
    project = conn.execute('SELECT name FROM projects WHERE id=?', (project_id,)).fetchone()
    _check(project is not None, 'APPROVED_MODULE_NARRATIVE_NOT_AVAILABLE')
    rows = conn.execute(f'SELECT {report_store._read_columns(conn)} FROM anxin_board_reports WHERE project_id=? AND report_hash=?',
                        (project_id, report_hash)).fetchall()
    _check(len(rows) == 1, 'APPROVED_MODULE_NARRATIVE_NOT_AVAILABLE')
    _, report = report_store._validate_stored_row(rows[0], conn=conn, expected_project_id=project_id,
                                                 expected_project_name=project['name'], require_current_profile=False)
    _check(report.get('schema_version') == 'anxin_board_report_v3' and report.get('report_version_id') == report_version_id)
    row = conn.execute('SELECT * FROM report_approval_snapshots WHERE id=? AND project_id=?',
                       (report['approval_snapshot_id'], project_id)).fetchone()
    _check(row is not None)
    approval = approvals._close_row(row)
    profile = read_bound_project_profile_for_report(approval['profile_id'], conn=conn)
    _identity(project_id, report, approval, profile)
    version = conn.execute('SELECT * FROM report_versions WHERE id=? AND project_id=?', (report_version_id, project_id)).fetchone()
    _check(version is not None and version['lifecycle'] == 'approved'
           and version['state_version'] == approval['report_state_version_after']
           and version['version_no'] == approval['report_version_no']
           and version['report_content_hash'] == report['report_content_hash']
           and version['model_execution_result_id'] == approval['model_execution_result_id']
           and version['execution_result_hash'] == approval['execution_result_hash'])
    return report, approval, profile


def _read_source(conn, source, profile, approval):
    _source_identity(source)
    origin = read_bound_project_profile_for_report(source['profile_id'], conn=conn)
    _check(origin['project_id'] == source['project_id'] and origin['content_hash'] == source['profile_content_hash'])
    report_store._require_profile_confirmation_provenance(conn, origin)
    _check(conn.execute('SELECT 1 FROM projects WHERE id=?', (source['project_id'],)).fetchone() is not None)
    _check(origin['content'].get('schema_version') == profile['content'].get('schema_version') == 'project_profile_v2')
    for field in ('planned_modules', 'implementation_mappings'):
        _check(origin['content'][field] == profile['content'][field], 'APPROVED_MODULE_NARRATIVE_PROFILE_MISMATCH')
    task = conn.execute('SELECT * FROM brownfield_baseline_tasks WHERE task_id=?', (source['task_id'],)).fetchone()
    _check(task is not None and task['project_id'] == source['project_id']
           and task['profile_id'] == source['profile_id'] and task['status'] == 'succeeded')
    identity = json.loads(task['identity_json'])
    _check(_hash(identity) == task['identity_hash'] == source['task_identity_hash'])
    _check(identity['project_id'] == source['project_id'] and identity['exact_head'] == source['exact_head']
           and identity['git_url'] == approval['project_repository_url'])
    output_row = conn.execute('SELECT * FROM brownfield_baseline_outputs WHERE task_id=?', (source['task_id'],)).fetchone()
    _check(output_row is not None)
    output = json.loads(output_row['output_json'])
    _check(_hash(output) == output_row['output_hash'] == source['output_hash'])
    _check(output['content'] == origin['content'] and _hash(output['content']) == origin['content_hash'])
    return _requirement_facts(origin['content'], output['requirements'], source['exact_head'])


def _request(value):
    request = {k: deepcopy(value[k]) for k in _REQUEST if k != 'modules'}
    request['modules'] = [{k: deepcopy(m[k]) for k in _MODULE} for m in value['modules']]
    return request


def _request_hash(value):
    value = deepcopy(value)
    value['modules'] = sorted(value['modules'], key=lambda item: item['module_id'])
    return _hash(value)


def validate_approved_module_narrative(value, *, report, approval_snapshot, profile):
    """Pure renderer check. Trust comes from the database getter, never request DTOs."""
    try:
        _check(type(value) is dict and set(value) == _VALUE and value['schema_version'] == SCHEMA_VERSION)
        identity = _identity(value['project_id'], report, approval_snapshot, profile)
        _check(all(value[k] == v for k, v in identity.items()))
        _source_identity(value['source'])
        _text(value['confirmed_by'], 200)
        _check(value['attribution'] == '已确认的模块说明')
        _check(type(value['cross_project_reuse_ack']) is bool and
               (value['source']['project_id'] == value['project_id'] or value['cross_project_reuse_ack']))
        _check(type(value['idempotency_key']) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value['idempotency_key']) is not None)
        _check(type(value['confirmed_at']) is str and datetime.fromisoformat(value['confirmed_at']).tzinfo is not None)
        _check(value['approved_git_head'] == approval_snapshot['git_to_commit'])
        _check(type(value['historical_baseline']) is bool and value['historical_baseline'] == (value['source']['exact_head'] != value['approved_git_head']))
        facts = {m['module_id']: m['source_requirements'] for m in value['modules']}
        raw = {key: [{k: v for k, v in r.items() if k != 'requirement_text'} for r in rows] for key, rows in facts.items()}
        expected = _requirement_facts(profile['content'], raw, value['source']['exact_head'])
        _check(value['modules'] == _modules(value['modules'], profile, expected, stored=True))
        _check(_hash({k: v for k, v in value.items() if k != 'module_narrative_hash'}) == value['module_narrative_hash'])
        _safe_value(value)
        return deepcopy(value)
    except ApprovedModuleNarrativeError:
        raise
    except Exception:
        raise ApprovedModuleNarrativeError() from None


def _close_row(conn, row, project_id, report, approval, profile):
    value = json.loads(row['narrative_json'])
    validate_approved_module_narrative(value, report=report, approval_snapshot=approval, profile=profile)
    _check(row['schema_version'] == SCHEMA_VERSION and row['project_id'] == project_id
           and row['report_version_id'] == value['report_version_id']
           and row['idempotency_key'] == value['idempotency_key']
           and row['narrative_hash'] == value['module_narrative_hash']
           and row['request_hash'] == _request_hash(_request(value)))
    facts = _read_source(conn, value['source'], profile, approval)
    _check(value['modules'] == _modules(value['modules'], profile, facts, stored=True))
    return value


def get_approved_module_narrative(project_id, report, approval_snapshot, profile, conn=None):
    """Return None for an unannotated legacy report; corrupt annotations fail closed."""
    def read(active):
        _identity(project_id, report, approval_snapshot, profile)
        if active.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone() is None:
            return None
        row = active.execute(f'SELECT * FROM {_TABLE} WHERE project_id=? AND report_version_id=?',
                             (project_id, report['report_version_id'])).fetchone()
        if row is None:
            return None
        actual_report, actual_approval, actual_profile = load_module_narrative_target(
            active, project_id, report['report_version_id'], report['anxin_board_report_hash'])
        _check(actual_report == report and all(actual_approval.get(k) == v for k, v in approval_snapshot.items()))
        _check(actual_profile['id'] == profile['id'] and actual_profile['content_hash'] == profile['content_hash'])
        return _close_row(active, row, project_id, actual_report, actual_approval, actual_profile)
    try:
        if conn is not None:
            return read(conn)
        with closing(get_connection()) as owned:
            owned.execute('BEGIN')
            return read(owned)
    except ApprovedModuleNarrativeError:
        raise
    except Exception:
        raise ApprovedModuleNarrativeError() from None


def ensure_approved_module_narrative_schema(conn):
    """Install only empty append-only storage in the caller's transaction."""
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {_TABLE} (
        schema_version TEXT NOT NULL CHECK(schema_version='{SCHEMA_VERSION}'),
        project_id INTEGER NOT NULL CHECK(project_id>0), report_version_id INTEGER NOT NULL CHECK(report_version_id>0),
        idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
        narrative_hash TEXT NOT NULL UNIQUE, narrative_json TEXT NOT NULL,
        PRIMARY KEY(project_id,report_version_id))''')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_{action.lower()}
            BEFORE {action} ON {_TABLE} BEGIN SELECT RAISE(ABORT,'module narrative is append-only'); END''')
    _check({r['name'] for r in conn.execute(f'PRAGMA table_info({_TABLE})')} == _COLUMNS)


def confirm_approved_module_narrative(*, project_id, report_version_id, payload):
    """Record one explicit local confirmation, with all authorities in one write snapshot."""
    try:
        _check(type(payload) is dict and set(payload) == _REQUEST, 'APPROVED_MODULE_NARRATIVE_INPUT_INVALID')
        _id(project_id); _id(report_version_id)
        for key in _TARGET:
            (_id if key.endswith('_id') else _digest)(payload[key])
        _text(payload['confirmed_by'], 200)
        _check(type(payload['cross_project_reuse_ack']) is bool)
        _check(type(payload['idempotency_key']) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', payload['idempotency_key']) is not None)
        _safe_value(payload)
        with closing(get_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ensure_approved_module_narrative_schema(conn)
            report, approval, profile = load_module_narrative_target(conn, project_id, report_version_id, payload['anxin_board_report_hash'])
            identity = _identity(project_id, report, approval, profile)
            _check(all(payload[k] == identity[k] for k in _TARGET))
            existing = conn.execute(f'SELECT * FROM {_TABLE} WHERE idempotency_key=? OR (project_id=? AND report_version_id=?)',
                                    (payload['idempotency_key'], project_id, report_version_id)).fetchall()
            if existing:
                _check(len(existing) == 1)
                row = existing[0]
                _check(row['project_id'] == project_id and row['report_version_id'] == report_version_id,
                       'APPROVED_MODULE_NARRATIVE_IDEMPOTENCY_CONFLICT')
                value = _close_row(conn, row, project_id, report, approval, profile)
                _check(row['idempotency_key'] == payload['idempotency_key'], 'APPROVED_MODULE_NARRATIVE_ALREADY_CONFIRMED')
                _check(row['request_hash'] == _request_hash(payload), 'APPROVED_MODULE_NARRATIVE_IDEMPOTENCY_CONFLICT')
                return value
            source = payload['source']
            _source_identity(source)
            _check(source['project_id'] == project_id or payload['cross_project_reuse_ack'],
                   'APPROVED_MODULE_NARRATIVE_CROSS_PROJECT_ACK_REQUIRED')
            facts = _read_source(conn, source, profile, approval)
            modules = _modules(payload['modules'], profile, facts)
            value = dict(schema_version=SCHEMA_VERSION, **identity, source=deepcopy(source),
                         attribution='已确认的模块说明', confirmed_by=payload['confirmed_by'],
                         confirmed_at=datetime.now(timezone.utc).isoformat(),
                         cross_project_reuse_ack=payload['cross_project_reuse_ack'],
                         historical_baseline=source['exact_head'] != approval['git_to_commit'],
                         approved_git_head=approval['git_to_commit'], idempotency_key=payload['idempotency_key'], modules=modules)
            value['module_narrative_hash'] = _hash(value)
            validate_approved_module_narrative(value, report=report, approval_snapshot=approval, profile=profile)
            conn.execute(f'INSERT INTO {_TABLE} VALUES (?,?,?,?,?,?,?)',
                         (SCHEMA_VERSION, project_id, report_version_id, payload['idempotency_key'],
                          _request_hash(_request(value)), value['module_narrative_hash'], _json(value)))
            return value
    except ApprovedModuleNarrativeError:
        raise
    except Exception:
        raise ApprovedModuleNarrativeError() from None
