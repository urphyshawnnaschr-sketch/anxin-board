"""A displayed profile retains its exact result independently of newer work."""
from contextlib import closing
from copy import deepcopy
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app import brownfield_baseline_api as api
from app import brownfield_baseline_store as store
from app import project_profiles as profiles
from app import local_session_api
from test_brownfield_baseline_report_api import db


@pytest.fixture
def result_db(db, monkeypatch):
    monkeypatch.setattr(api, 'get_connection', db.connect)
    monkeypatch.setattr(api, 'require_local_read_request', lambda request: db.guards.append(request), raising=False)
    monkeypatch.setattr(api, '_stale_scope', lambda conn, task: False)
    monkeypatch.setattr(api.core, 'prepare_baseline', lambda *a: pytest.fail('no repository scan'))
    monkeypatch.setattr(api.profile_generation, '_read_provider_credential', lambda: pytest.fail('no credential'))
    monkeypatch.setattr(profiles, 'get_connection', db.connect)
    with closing(db.connect()) as conn:
        conn.execute('INSERT INTO project_profiles SELECT 9,project_id,status,content_json,content_hash,confirmed_by,confirmed_at,source_prd_id,version_no+1,edit_version,created_at,updated_at FROM project_profiles WHERE id=8')
        conn.commit()
    return db


@pytest.mark.parametrize('status', ['queued', 'running', 'failed_pre_send', 'failed_after_send', 'unknown'])
def test_newer_work_does_not_replace_displayed_profile_result(result_db, status):
    db = result_db
    with closing(db.connect()) as conn:
        newer = store.create_task(conn, project_id=3, authorization_nonce='newer', identity={'other': 1}, max_calls=3)
        conn.execute('UPDATE brownfield_baseline_tasks SET status=? WHERE task_id=?', (status, newer['task_id']))
        conn.commit()
    result = api.get_profile_result(3, 8, object())['task']
    assert result['task_id'] == db.task['task_id']
    assert result['status'] == 'succeeded'
    assert result['profile_id'] == 8
    assert result['generated_content_hash'] == db.data['profile']['content_hash']
    assert result['module_requirements'] == db.data['output']['requirements']
    with closing(db.connect()) as conn:
        assert store.latest_task(conn, 3)['task_id'] == newer['task_id']


def test_result_guard_precedes_database(result_db, monkeypatch):
    def deny(_):
        raise HTTPException(403, detail={'code': 'LOCAL_SESSION_SESSION_INVALID'})
    monkeypatch.setattr(api, 'require_local_read_request', deny)
    monkeypatch.setattr(api, 'get_connection', lambda: pytest.fail('must guard before DB'))
    with pytest.raises(HTTPException) as exc:
        api.get_profile_result(3, 8, object())
    assert exc.value.status_code == 403


def test_result_project_boundary_and_absent_analysis(result_db):
    with pytest.raises(HTTPException) as exc:
        api.get_profile_result(4, 8, object())
    assert exc.value.status_code == 404
    assert api.get_profile_result(3, 9, object()) == {'task': None}


@pytest.mark.parametrize('kind', ['profile', 'output', 'missing'])
def test_corrupt_or_missing_identity_never_returns_result(result_db, monkeypatch, kind):
    if kind == 'profile':
        with closing(result_db.connect()) as conn:
            conn.execute("UPDATE project_profiles SET content_hash='changed' WHERE id=8")
            conn.commit()
    else:
        with closing(result_db.connect()) as conn:
            saved = store.get_output(conn, result_db.task['task_id'])
        saved['output_hash'] = 'changed'
        monkeypatch.setattr(store, 'get_output', lambda *a: None if kind == 'missing' else saved)
    with pytest.raises(HTTPException) as exc:
        api.get_profile_result(3, 8, object())
    assert exc.value.detail['code'] == 'BROWNFIELD_RESULT_IDENTITY_INVALID'


def test_multiple_successes_bound_to_one_profile_fail_closed(result_db):
    with closing(result_db.connect()) as conn:
        duplicate = store.create_task(conn, project_id=3, authorization_nonce='duplicate', identity={'other': 1}, max_calls=3)
        store.finish_task(conn, duplicate['task_id'], status='succeeded', profile_id=8)
    with pytest.raises(HTTPException) as exc:
        api.get_profile_result(3, 8, object())
    assert exc.value.detail['code'] == 'BROWNFIELD_RESULT_AMBIGUOUS'


def test_generated_candidate_cannot_silently_change_report_binding(result_db):
    db = result_db
    with closing(db.connect()) as conn:
        conn.execute("UPDATE project_profiles SET status='candidate', confirmed_by=NULL, confirmed_at=NULL")
        conn.commit()
    original = profiles.get_profile(8)
    assert original['generated_baseline_result'] is True
    assert next(p for p in profiles.list_profiles(3) if p['id'] == 8)['generated_baseline_result'] is True
    changed = deepcopy(db.data['profile']['content'])
    changed['project_summary'] = 'An ordinary changed summary'
    with pytest.raises(HTTPException) as exc:
        profiles.update_candidate(8, profiles.ProfileUpdatePayload(edit_version=1, content=changed))
    assert exc.value.detail['code'] == 'PROJECT_PROFILE_BASELINE_RESULT_READ_ONLY'
    after = profiles.get_profile(8)
    assert after['content_hash'] == original['content_hash']
    assert after['edit_version'] == original['edit_version']
    assert profiles.update_candidate(8, profiles.ProfileUpdatePayload(edit_version=1, content=original['content']))['changed'] is False


def test_manual_candidate_stays_editable(result_db):
    with closing(result_db.connect()) as conn:
        conn.execute("UPDATE project_profiles SET status='candidate', confirmed_by=NULL, confirmed_at=NULL WHERE id=9")
        conn.commit()
    assert profiles.get_profile(9)['generated_baseline_result'] is False
    content = deepcopy(result_db.data['profile']['content'])
    content['project_summary'] = 'Changed manual summary'
    assert profiles.update_candidate(9, profiles.ProfileUpdatePayload(edit_version=1, content=content))['changed'] is True


def test_generated_candidate_still_allows_explicit_confirmation(result_db):
    with closing(result_db.connect()) as conn:
        conn.execute("UPDATE project_profiles SET status='candidate', confirmed_by=NULL, confirmed_at=NULL WHERE id=8")
        conn.commit()
    result = profiles.confirm_candidate(8, profiles.ProfileConfirmPayload(edit_version=1, confirmed_by='Synthetic reviewer'))
    assert result['status'] == 'confirmed'
    assert result['content_hash'] == result_db.data['profile']['content_hash']


def test_promoted_candidate_is_readonly_before_task_finish_or_after_crash(result_db):
    with closing(result_db.connect()) as conn:
        # Promotion commits the source record atomically with the candidate;
        # task completion is a later transaction and might never be reached.
        conn.execute('CREATE TABLE project_state_baseline_candidates (project_id INTEGER, profile_id INTEGER)')
        conn.execute('INSERT INTO project_state_baseline_candidates VALUES(3,9)')
        conn.execute("UPDATE project_profiles SET status='candidate', confirmed_by=NULL, confirmed_at=NULL WHERE id=9")
        conn.commit()
        pending = store.create_task(conn, project_id=3, authorization_nonce='interrupted-promotion', identity={'pending': 1}, max_calls=3)
        store.save_output(conn, pending['task_id'], output=result_db.data['output'])
    assert profiles.get_profile(9)['generated_baseline_result'] is True
    content = deepcopy(result_db.data['profile']['content'])
    content['project_summary'] = 'Must not change frozen result'
    with pytest.raises(HTTPException) as exc:
        profiles.update_candidate(9, profiles.ProfileUpdatePayload(edit_version=1, content=content))
    assert exc.value.detail['code'] == 'PROJECT_PROFILE_BASELINE_RESULT_READ_ONLY'
    with closing(result_db.connect()) as conn:
        assert store.get_task(conn, pending['task_id'])['status'] == 'queued'


def test_result_route_requires_real_guard_and_makes_no_database_writes(result_db, monkeypatch):
    monkeypatch.setattr(api, 'require_local_read_request', local_session_api.require_local_read_request)
    traces = []
    def connection():
        conn = result_db.connect()
        conn.set_trace_callback(traces.append)
        return conn
    monkeypatch.setattr(api, 'get_connection', connection)
    app = FastAPI()
    app.include_router(local_session_api.router)
    app.include_router(api.router)
    local_session_api.configure_local_session_guard('synthetic-profile-result-bootstrap')
    try:
        with TestClient(app) as client:
            url = '/api/projects/3/project-state-baseline/atlas/results/8'
            host = {'host': '127.0.0.1:5173', 'origin': 'http://127.0.0.1:5173'}
            token = client.post('/api/local-session/exchange', json={'bootstrap_secret': 'synthetic-profile-result-bootstrap'}, headers=host).json()['session_token']
            assert client.get(url, headers={'host': host['host']}).status_code == 403
            assert traces == []
            valid = {'host': host['host'], 'x-anxin-session': token, 'x-request-id': 'result-read'}
            assert client.get(url, headers=valid).json()['task']['profile_id'] == 8
            assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'REPLACE')) for sql in traces)
    finally:
        local_session_api.configure_local_session_guard(None)
