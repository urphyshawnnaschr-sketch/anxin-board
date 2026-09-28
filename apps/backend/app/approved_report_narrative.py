"""Read-only projection of exact approved daily output. Never rewrites historical V3."""
from contextlib import closing
from copy import deepcopy
import json
import re
import sqlite3

from app.db import get_connection
from app import model_execution_results as results
from app import model_call_ledger as calls
from app import report_approval as approvals
from app.ai_contract_validation import _validate_result_contract
from app.context_redaction import _redact_text

SCHEMA_VERSION = 'approved_report_narrative_v1'
CONTENT_FIELDS = ('plain_summary', 'code_change_summary', 'test_evidence', 'risks', 'unknown_items', 'source_warnings')
_BINDINGS = ('approval_snapshot_id', 'approval_snapshot_hash', 'report_version_id',
             'report_content_hash', 'model_execution_result_id', 'execution_result_hash')
_KEYS = {'schema_version', 'project_id', 'anxin_board_report_hash', *_BINDINGS,
         'source_task_type', 'source_result', 'content', 'narrative_hash'}


class ApprovedReportNarrativeError(ValueError):
    def __init__(self, code='APPROVED_REPORT_NARRATIVE_INVALID'):
        self.code = code
        super().__init__(code)


def _check(ok):
    if not ok:
        raise ApprovedReportNarrativeError()


def _hash(value):
    return results._stable_hash(value, stored=True)


def _safe(value):
    if isinstance(value, dict):
        for key, item in value.items():
            _safe(key)
            _safe(item)
    elif isinstance(value, list):
        for item in value:
            _safe(item)
    elif isinstance(value, str):
        clean, _ = _redact_text(value, include_assignments=True)
        if clean != value:
            raise ApprovedReportNarrativeError('APPROVED_REPORT_NARRATIVE_UNSAFE')


def _binding(project_id, report, approval):
    _check(type(project_id) is int and project_id > 0 and approval.get('project_id') == project_id)
    _check(report.get('schema_version') == 'anxin_board_report_v3' and approval.get('human_acknowledged') is True)
    _check(_hash({k:v for k,v in report.items() if k != 'anxin_board_report_hash'}) == report.get('anxin_board_report_hash'))
    for field in _BINDINGS:
        value = report.get(field)
        _check(value == approval.get(field))
        _check((type(value) is int and value > 0) if field.endswith('_id') else
               (type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None))
    return dict(project_id=project_id, anxin_board_report_hash=report['anxin_board_report_hash'],
                **{field: report[field] for field in _BINDINGS})


def _content(task_type, source, expected_hash):
    _check(task_type in ('daily_report_generate', 'daily_report_regenerate') and type(source) is dict)
    _check(not _validate_result_contract(task_type, source))
    _check(_hash(source) == expected_hash)
    _safe(source)
    body = source['new_report'] if task_type == 'daily_report_regenerate' else source
    return {field: deepcopy(body[field]) for field in CONTENT_FIELDS}


def build_approved_report_narrative(*, project_id, report, approval_snapshot, ai_raw):
    """Pure internal builder; trusted report/approval authority must come from server reads."""
    try:
        identity = _binding(project_id, report, approval_snapshot)
        _check(ai_raw.get('model_execution_result_id') == identity['model_execution_result_id']
               and ai_raw.get('execution_result_hash') == identity['execution_result_hash']
               and ai_raw.get('validated_result_hash') == identity['report_content_hash'])
        content = _content(ai_raw.get('task_type'), ai_raw.get('content'), identity['report_content_hash'])
        value = dict(schema_version=SCHEMA_VERSION, **identity, source_task_type=ai_raw['task_type'],
                     source_result=deepcopy(ai_raw['content']), content=content)
        value['narrative_hash'] = _hash(value)
        return value
    except ApprovedReportNarrativeError:
        raise
    except Exception:
        raise ApprovedReportNarrativeError() from None


def validate_approved_report_narrative(value, *, report, approval_snapshot):
    try:
        _check(type(value) is dict and set(value) == _KEYS and value['schema_version'] == SCHEMA_VERSION)
        identity = _binding(value['project_id'], report, approval_snapshot)
        _check(all(value[key] == item for key,item in identity.items()))
        content = _content(value['source_task_type'], value['source_result'], identity['report_content_hash'])
        _check(value['content'] == content and _hash({k:v for k,v in value.items() if k != 'narrative_hash'}) == value['narrative_hash'])
        return deepcopy(value)
    except ApprovedReportNarrativeError:
        raise
    except Exception:
        raise ApprovedReportNarrativeError() from None


def _load(conn, project_id, report, approval_snapshot):
    identity = _binding(project_id, report, approval_snapshot)
    approval_row = conn.execute('SELECT * FROM report_approval_snapshots WHERE id=? AND project_id=?',
                               (identity['approval_snapshot_id'], project_id)).fetchone()
    _check(approval_row is not None)
    actual_approval = approvals._close_row(approval_row)
    _check(all(actual_approval.get(k) == v for k,v in approval_snapshot.items()))
    version = conn.execute('SELECT * FROM report_versions WHERE id=? AND project_id=?',
                           (identity['report_version_id'], project_id)).fetchone()
    _check(version is not None)
    version = dict(version)
    expected_state = actual_approval['report_state_version_after']
    _check(version['lifecycle'] == 'approved' and version['state_version'] == expected_state
           and version['version_no'] == actual_approval['report_version_no'])
    _check(version['report_content_hash'] == identity['report_content_hash']
           and version['model_execution_result_id'] == identity['model_execution_result_id']
           and version['execution_result_hash'] == identity['execution_result_hash'])
    row = results._read_by_id(conn, identity['model_execution_result_id'])
    _check(row is not None)
    row = dict(row)
    call_row = calls._read_by_id(conn, row['model_call_id'])
    _check(call_row is not None)
    call = calls._close_row(call_row)
    results._assert_call_binding(row, call)
    _check(row['schema_version'] in ('model_execution_result_v1', 'model_execution_aggregate_v1'))
    _check(row['project_id'] == project_id and row['execution_result_hash'] == identity['execution_result_hash'])
    _check(_hash(results._execution_identity_payload(row)) == identity['execution_result_hash'])
    source, _ = results._stored_json_mapping(row['validated_result_json'])
    formal, _ = results._stored_json_mapping(row['formal_response_json'])
    _check(_hash(source) == row['validated_result_hash'] == version['validated_result_hash'] == identity['report_content_hash'])
    _check(_hash(formal) == row['formal_response_hash'] == version['formal_response_hash'])
    _check(formal.get('result') == source and formal.get('status') == 'succeeded'
           and formal.get('task_type') == row['task_type'])
    for field in ('model_call_id', 'call_identity_hash'):
        _check(row[field] == version[field] == actual_approval[field])
    _check(row['snapshot_id'] == version['evidence_snapshot_id'] == actual_approval['evidence_snapshot_id'])
    return build_approved_report_narrative(project_id=project_id, report=report, approval_snapshot=actual_approval,
        ai_raw=dict(model_execution_result_id=row['id'], execution_result_hash=row['execution_result_hash'],
                    validated_result_hash=row['validated_result_hash'], task_type=row['task_type'], content=source))


def load_approved_report_narrative(*, project_id, report, approval_snapshot, conn=None):
    """SELECT-only, exact IDs; supplied admission transaction is never committed or closed."""
    try:
        if conn is not None:
            return _load(conn, project_id, report, approval_snapshot)
        with closing(get_connection()) as owned:
            owned.execute("BEGIN")
            return _load(owned, project_id, report, approval_snapshot)
    except ApprovedReportNarrativeError:
        raise
    except Exception:
        raise ApprovedReportNarrativeError() from None
