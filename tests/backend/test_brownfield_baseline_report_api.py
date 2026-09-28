from contextlib import closing
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app import brownfield_baseline_report_api as api
from app import brownfield_baseline_store as store
from app import local_session_api
from test_brownfield_baseline_report import report_fixture, SYNTHETIC_PNG


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'report.db'
    data = report_fixture()
    project = dict(data['project'], git_url='https://example.invalid/repo', branch='main')
    identity = dict(project_id=3, plan_profile_id=2, prd_id=1, prd_source_hash='b'*64,
                    git_url=project['git_url'], git_branch='main', exact_head='a'*40)
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    with closing(connect()) as conn:
        store.ensure_schema(conn)
        conn.executescript('''CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT,git_url TEXT,branch TEXT);
            CREATE TABLE project_profiles(id INTEGER PRIMARY KEY,project_id INTEGER,status TEXT,content_json TEXT,content_hash TEXT,confirmed_by TEXT,confirmed_at TEXT,source_prd_id INTEGER,version_no INTEGER,edit_version INTEGER,created_at TEXT,updated_at TEXT);
            CREATE TABLE project_git_connections(project_id INTEGER,status TEXT,checked_url_hash TEXT,checked_branch TEXT,local_head TEXT,remote_head TEXT);
            CREATE TABLE prd_versions(id INTEGER PRIMARY KEY,project_id INTEGER,status TEXT,source_hash TEXT,version_no INTEGER);''')
        conn.execute('INSERT INTO projects VALUES(3,?,?,?)', (project['name'],project['git_url'],'main'))
        p = data['profile']
        conn.execute('INSERT INTO project_profiles VALUES(8,3,?,?,?,?,?,1,2,1,?,?)', (p['status'],json.dumps(p['content']),p['content_hash'],p['confirmed_by'],p['confirmed_at'],p['confirmed_at'],p['confirmed_at']))
        conn.execute('INSERT INTO project_git_connections VALUES(3,?,?,?,?,?)', ('connected',hashlib.sha256(project['git_url'].encode()).hexdigest(),'main','a'*40,'a'*40))
        conn.execute("INSERT INTO prd_versions VALUES(1,3,'parse_confirmed',?,1)", ('b'*64,))
        conn.commit()
        task = store.create_task(conn, project_id=3, authorization_nonce='report-auth', identity=identity, max_calls=3)
        store.save_output(conn, task['task_id'], output=data['output'])
        store.finish_task(conn, task['task_id'], status='succeeded', profile_id=8)
    traces = []
    def readonly_connect():
        conn = connect()
        conn.set_trace_callback(traces.append)
        return conn
    monkeypatch.setattr(api, 'get_connection', readonly_connect)
    guards = []
    monkeypatch.setattr(api, 'require_local_read_request', lambda request: guards.append(request))
    return SimpleNamespace(connect=connect, task=task, traces=traces, guards=guards, data=data)


def test_export_and_preview_are_protected_readonly_html(db):
    request = object()
    result = api.export_baseline_html(3, db.task['task_id'], request)
    assert result.media_type == 'text/html'
    assert result.headers['content-disposition'].startswith('attachment;')
    assert result.headers['cache-control'] == 'no-store'
    assert 'default-src' in result.headers['content-security-policy']
    assert db.guards == [request]
    assert api.preview_baseline_html(3, db.task['task_id'], request).headers['content-disposition'] == 'inline'
    assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','REPLACE')) for sql in db.traces)


def test_guard_rejects_before_any_database_access(db, monkeypatch):
    def deny(request):
        raise HTTPException(403, detail={'code':'LOCAL_SESSION_INVALID'})
    monkeypatch.setattr(api, 'require_local_read_request', deny)
    with pytest.raises(HTTPException) as caught:
        api.preview_baseline_html(3, db.task['task_id'], object())
    assert caught.value.status_code == 403
    assert db.traces == []


def test_project_boundary_and_candidate_export_gate(db):
    with pytest.raises(HTTPException) as caught:
        api.preview_baseline_html(4, db.task['task_id'], object())
    assert caught.value.status_code == 404
    with closing(db.connect()) as conn:
        conn.execute("UPDATE project_profiles SET status='candidate' WHERE id=8")
        conn.commit()
    assert b'<!doctype html>' in api.preview_baseline_html(3, db.task['task_id'], object()).body
    with pytest.raises(HTTPException) as caught:
        api.export_baseline_html(3, db.task['task_id'], object())
    assert caught.value.detail['code'] == 'BROWNFIELD_REPORT_CONFIRMATION_REQUIRED'


@pytest.mark.parametrize('sql', [
    "UPDATE projects SET branch='other'", "UPDATE projects SET git_url='https://example.invalid/other'",
    "UPDATE project_git_connections SET local_head='changed'", "UPDATE project_git_connections SET remote_head='changed'",
    "UPDATE project_git_connections SET status='failed'", "UPDATE project_git_connections SET checked_branch='other'",
    "UPDATE project_git_connections SET checked_url_hash='changed'", "UPDATE prd_versions SET source_hash='changed'",
])
def test_export_scope_change_blocks_but_saved_history_preview_remains(db, sql):
    with closing(db.connect()) as conn:
        conn.execute(sql)
        conn.commit()
    with pytest.raises(HTTPException) as caught:
        api.export_baseline_html(3, db.task['task_id'], object())
    assert caught.value.detail['code'] == 'BROWNFIELD_REPORT_SCOPE_CHANGED'
    assert '历史'.encode() in api.preview_baseline_html(3, db.task['task_id'], object()).body


def test_current_profile_change_blocks_export(db, monkeypatch):
    monkeypatch.setattr(api, 'read_current_confirmed_project_profile', lambda *a, **kw: dict(db.data['profile'], id=9))
    with pytest.raises(HTTPException) as caught:
        api.export_baseline_html(3, db.task['task_id'], object())
    assert caught.value.detail['code'] == 'BROWNFIELD_REPORT_SCOPE_CHANGED'


def test_real_session_guard_protects_both_routes_without_origin_requirement(db, monkeypatch):
    monkeypatch.setattr(api, 'require_local_read_request', local_session_api.require_local_read_request)
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.configure_local_session_guard('synthetic-report-bootstrap')
    try:
        with TestClient(app) as client:
            headers = {'host': '127.0.0.1:5173', 'origin': 'http://127.0.0.1:5173'}
            token = client.post('/api/local-session/exchange', json={'bootstrap_secret': 'synthetic-report-bootstrap'}, headers=headers).json()['session_token']
            for suffix in ('review-html', 'export-html'):
                url = f'/api/projects/3/project-state-baseline/atlas/tasks/{db.task["task_id"]}/{suffix}'
                valid = {'host': '127.0.0.1:5173', 'x-anxin-session': token, 'x-request-id': 'report-read-one'}
                for invalid in ({'host': '127.0.0.1:5173'}, dict(valid, origin='http://evil.invalid'), dict(valid, **{'x-request-id': ''})):
                    db.traces.clear()
                    assert client.get(url, headers=invalid).status_code == 403
                    assert db.traces == []
                response = client.get(url, headers=valid)
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('text/html')
                assert 'http-equiv="Content-Security-Policy"' in response.text
                assert 'form-action' in response.text
    finally:
        local_session_api.invalidate_local_session_guard()


def test_changed_candidate_content_never_pairs_old_requirements(db):
    with closing(db.connect()) as conn:
        conn.execute("UPDATE project_profiles SET content_hash=? WHERE id=8", ('c'*64,))
        conn.commit()
    with pytest.raises(HTTPException) as caught:
        api.preview_baseline_html(3, db.task['task_id'], object())
    assert caught.value.detail['code'] == 'BROWNFIELD_REPORT_IDENTITY_INVALID'


def test_no_newer_prd_can_be_exported_as_original_analysis(db):
    with closing(db.connect()) as conn:
        conn.execute("INSERT INTO prd_versions VALUES(2,3,'parse_confirmed',?,2)", ('d'*64,))
        conn.commit()
    with pytest.raises(HTTPException) as caught:
        api.export_baseline_html(3, db.task['task_id'], object())
    assert caught.value.detail['code'] == 'BROWNFIELD_REPORT_SCOPE_CHANGED'


def test_brand_asset_reads_only_one_fixed_name_plain_png(tmp_path):
    assets = tmp_path / 'ui' / 'assets'
    assert api._read_brand_png(assets) is None
    assets.mkdir(parents=True)
    target = assets / 'anxin-board-calligraphy-abcdefgh.png'
    target.write_bytes(SYNTHETIC_PNG)
    assert api._read_brand_png(assets) == SYNTHETIC_PNG
    (assets / 'unrelated.png').write_bytes(b'unrelated')
    assert api._read_brand_png(assets) == SYNTHETIC_PNG
    second = assets / 'anxin-board-calligraphy-ijklmnop.png'
    second.write_bytes(SYNTHETIC_PNG)
    assert api._read_brand_png(assets) is None
    second.unlink()
    for invalid in [b'<svg>invalid</svg>', SYNTHETIC_PNG + b'x' * 65536]:
        target.write_bytes(invalid)
        assert api._read_brand_png(assets) is None


def test_brand_asset_reparse_is_not_followed(tmp_path, monkeypatch):
    assets = tmp_path / 'ui' / 'assets'
    assets.mkdir(parents=True)
    (assets / 'anxin-board-calligraphy-abcdefgh.png').write_bytes(SYNTHETIC_PNG)
    monkeypatch.setattr(api, '_plain_asset_path', lambda path: False)
    assert api._read_brand_png(assets) is None
