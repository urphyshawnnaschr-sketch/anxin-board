"""Real HTTP/store/narrative closure; only profile authority is a synthetic seam."""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import anxin_board_reports as api
from app import anxin_board_report_store as store
from app import report_approval as approvals
from app import local_session_api
from app.anxin_board_report_v3 import build_anxin_board_report_v3
from test_approved_report_narrative import stored_fixture, fixture
import test_anxin_board_report_v3 as v3


@pytest.fixture
def route_fixture(tmp_path, monkeypatch):
    conn, args = stored_fixture()
    profile = v3._profile()
    row = dict(conn.execute('SELECT * FROM report_approval_snapshots').fetchone())
    row['profile_content_hash'] = profile['content_hash']
    row['human_acknowledged'] = True
    row['snapshot_json'] = approvals._canonical_json(approvals._snapshot_hash_payload(row))
    row['approval_snapshot_hash'] = approvals._stable_hash(approvals._snapshot_hash_payload(row))
    conn.execute('UPDATE report_approval_snapshots SET profile_content_hash=?,snapshot_json=?,approval_snapshot_hash=?',
                 (row['profile_content_hash'], row['snapshot_json'], row['approval_snapshot_hash']))
    approval = approvals._close_row(row)
    report = build_anxin_board_report_v3(
        project_name='正式项目', profile=profile, ai_raw=fixture()['ai_raw'],
        daily_change=v3._daily_change(), manager_supplement='已人工核对', approval_snapshot=approval)
    conn.execute('CREATE TABLE projects (id INTEGER, name TEXT)')
    conn.executemany('INSERT INTO projects VALUES (?,?)', [(1, '正式项目'), (2, '另一个项目')])
    conn.execute('''CREATE TABLE anxin_board_reports (id INTEGER,project_id INTEGER,schema_version TEXT,
        report_date TEXT,report_hash TEXT,report_json TEXT,created_at TEXT,
        approval_snapshot_id INTEGER,approval_snapshot_hash TEXT)''')
    conn.execute('INSERT INTO anxin_board_reports VALUES (1,1,?,?,?,?,?,?,?)', (
        report['schema_version'], report['report_date'], report['anxin_board_report_hash'],
        json.dumps(report, ensure_ascii=False), '2026-01-01', approval['approval_snapshot_id'], approval['approval_snapshot_hash']))
    conn.commit()
    path = tmp_path / 'narrative.sqlite3'
    with sqlite3.connect(path) as disk:
        conn.backup(disk)
    conn.close()
    traces, opened = [], []

    def readonly_connection():
        value = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, check_same_thread=False)
        value.row_factory = sqlite3.Row
        value.execute('PRAGMA query_only=ON')
        value.set_trace_callback(traces.append)
        opened.append(value)
        return value

    def bound_profile(profile_id, *, conn):
        assert profile_id == profile['id']
        return profile

    monkeypatch.setattr(api, 'get_connection', readonly_connection)
    monkeypatch.setattr(store, 'read_bound_project_profile_for_report', bound_profile)
    monkeypatch.setattr(store, '_require_profile_confirmation_provenance', lambda conn, profile: None)
    monkeypatch.setattr(api, 'load_latest_anxin_board_report', lambda **kw: pytest.fail('must not read latest'))
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(local_session_api.router)
    assert local_session_api.configure_local_session_guard('synthetic-narrative-bootstrap')
    try:
        with TestClient(app, base_url='http://127.0.0.1:5173') as client:
            exchange = client.post('/api/local-session/exchange',
                json={'bootstrap_secret': 'synthetic-narrative-bootstrap'},
                headers={'Origin': 'http://127.0.0.1:5173'})
            assert exchange.status_code == 200
            client.headers.update({'X-Anxin-Session': exchange.json()['session_token'],
                                   'X-Request-Id': 'narrative-synthetic-read'})
            yield client, path, report, traces
    finally:
        local_session_api.invalidate_local_session_guard()
        for value in opened:
            value.close()


def get(client, report, *, project=1, version=3, report_hash=None):
    return client.get(f'/api/projects/{project}/anxin-board/reports/{version}/narrative',
                      params={'report_hash': report_hash or report['anxin_board_report_hash']})


def test_exact_approved_narrative_route_is_readonly(route_fixture):
    client, path, report, traces = route_fixture
    before = path.read_bytes()
    response = get(client, report)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    value = response.json()
    assert value['project_id'] == 1 and value['report_version_id'] == 3
    assert value['anxin_board_report_hash'] == report['anxin_board_report_hash']
    assert value['content'] == {key: fixture()['ai_raw']['content'][key] for key in value['content']}
    assert traces[0] == 'BEGIN'
    assert all(sql.lstrip().startswith(('BEGIN', 'SELECT', 'PRAGMA', 'COMMIT')) for sql in traces)
    assert path.read_bytes() == before


@pytest.mark.parametrize('params,status,code', [
    ({'project': 99}, 404, 'PROJECT_NOT_FOUND'),
    ({'project': 2}, 404, 'APPROVED_REPORT_NARRATIVE_NOT_AVAILABLE'),
    ({'report_hash': 'f'*64}, 404, 'APPROVED_REPORT_NARRATIVE_NOT_AVAILABLE'),
    ({'version': 4}, 409, 'APPROVED_REPORT_NARRATIVE_IDENTITY_MISMATCH'),
    ({'report_hash': 'invalid'}, 400, 'APPROVED_REPORT_NARRATIVE_INPUT_INVALID'),
])
def test_route_exact_identity_failures(route_fixture, params, status, code):
    client, path, report, _ = route_fixture
    before = path.read_bytes()
    response = get(client, report, **params)
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert 'content' not in response.json()
    assert path.read_bytes() == before


def test_history_exact_hash_never_substitutes_latest(route_fixture):
    client, path, report, _ = route_fixture
    with sqlite3.connect(path) as conn:
        conn.execute('''INSERT INTO anxin_board_reports SELECT 99,project_id,schema_version,report_date,
                     ?,?,created_at,approval_snapshot_id,approval_snapshot_hash FROM anxin_board_reports''',
                     ('e'*64, '{"latest_private_text":"must-not-leak"}'))
    response = get(client, report)
    assert response.status_code == 200
    assert response.json()['anxin_board_report_hash'] == report['anxin_board_report_hash']
    assert 'must-not-leak' not in response.text


@pytest.mark.parametrize('sql', [
    "UPDATE model_execution_results SET validated_result_json='{\"private_raw\":\"must-not-leak\"}'",
    'UPDATE model_execution_results SET project_id=2',
    'UPDATE model_execution_results SET id=99',
])
def test_actual_loader_failure_never_returns_body_or_replacement(route_fixture, sql):
    client, path, report, _ = route_fixture
    with sqlite3.connect(path) as conn:
        conn.execute(sql)
    before = path.read_bytes()
    response = get(client, report)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'APPROVED_REPORT_NARRATIVE_INVALID'
    assert set(response.json()) == {'detail'}
    assert 'must-not-leak' not in response.text
    assert 'source_result' not in response.text
    assert path.read_bytes() == before



@pytest.mark.parametrize('session', [None, 'synthetic-wrong-session'])
def test_missing_or_wrong_session_rejected_before_database(route_fixture, monkeypatch, session):
    client, path, report, traces = route_fixture
    client.headers.pop('X-Anxin-Session')
    if session is not None:
        client.headers['X-Anxin-Session'] = session
    monkeypatch.setattr(api, 'get_connection', lambda: pytest.fail('DB must not be opened'))
    before = path.read_bytes()
    response = get(client, report)
    assert response.status_code == 403
    assert response.json()['detail']['code'] == 'LOCAL_SESSION_SESSION_INVALID'
    assert traces == [] and path.read_bytes() == before
    assert set(response.json()) == {'detail'}


def test_unavailable_session_rejected_before_database(route_fixture, monkeypatch):
    client, _, report, traces = route_fixture
    local_session_api.invalidate_local_session_guard()
    monkeypatch.setattr(api, 'get_connection', lambda: pytest.fail('DB must not be opened'))
    response = get(client, report)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'LOCAL_SESSION_UNAVAILABLE'
    assert traces == []
