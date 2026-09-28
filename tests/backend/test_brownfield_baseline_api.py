from contextlib import closing
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import brownfield_baseline_api as api
from app import brownfield_baseline_store as store

READ_STALE_SCOPE = api._stale_scope
REAL_THREAD = api.threading.Thread


@pytest.fixture
def setup_api(tmp_path, monkeypatch):
    db = tmp_path / "atlas-api.db"
    opened = []
    def connect():
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        opened.append(conn)
        return conn
    monkeypatch.setattr(api, "get_connection", connect)
    api.ensure_brownfield_baseline_schema()
    scope = dict(project_id=9, plan_profile_id=42, exact_head="a"*40,
                 prd_id=7, prd_source_hash="b"*64, plan_content_hash="c"*64,
                 git_url="https://example.invalid/repo", git_branch="main")
    identity = dict(scope, max_calls=8, capability=dict(provider="deepseek", model_id="deepseek-flash", model_version="v1", context_window_tokens=100000, max_output_tokens=8000))
    prepared = dict(scope=scope, identity=identity, summary=dict(
        preflight_identity_hash=api.core.digest(identity), provider_calls=0,
        credential_read=False, max_calls=8, module_count=3))
    calls = []
    def prepare(project_id, plan_profile_id):
        calls.append(("prepare", project_id, plan_profile_id))
        return deepcopy(prepared)
    monkeypatch.setattr(api.core, "prepare_baseline", prepare)
    monkeypatch.setattr(api, "_stale_scope", lambda conn, task: False)
    monkeypatch.setattr(api, "require_local_write_request", lambda request, **kwargs: calls.append(("guard", kwargs)))
    monkeypatch.setattr(api.profile_generation, "_read_provider_credential", lambda: pytest.fail("credential read forbidden"))
    monkeypatch.setattr(api.core, "run_baseline", lambda *a, **kw: pytest.fail("provider work forbidden"))
    threads = []
    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            threads.append(self)
        def start(self):
            calls.append(("thread_started",))
    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    api._WORKERS.clear()
    yield SimpleNamespace(connect=connect, prepared=prepared, calls=calls, threads=threads, opened=opened)
    api._WORKERS.clear()


def request(nonce="authorization-one"):
    return SimpleNamespace(headers={"local-idempotency-key": nonce})


def payload(ctx, nonce="authorization-one"):
    return api.BaselineExecutePayload(plan_profile_id=42, authorized=True,
        authorization_nonce=nonce, preflight_identity_hash=api.core.digest(ctx.prepared["identity"]))


def create(ctx):
    return api.create_atlas_task(9, payload(ctx), request())["task"]


def test_preflight_zero_credentials_no_task_admission(setup_api):
    ctx = setup_api
    result = api.preflight_atlas(9, api.BaselinePreflightPayload(plan_profile_id=42), request())
    assert result["preflight"]["provider_calls"] == 0
    assert result["preflight"]["credential_read"] is False
    assert ctx.threads == []
    with closing(ctx.connect()) as conn:
        assert store.latest_task(conn, 9) is None


def test_create_persists_before_thread_and_same_nonce_replays(setup_api):
    ctx = setup_api
    first = create(ctx)
    second = create(ctx)
    assert first["task_id"] == second["task_id"]
    assert first["authorization_nonce"] == "authorization-one"
    assert first["preflight_identity_hash"] == api.core.digest(ctx.prepared["identity"])
    assert first["plan_profile_id"] == 42 and first["exact_head"] == "a"*40
    assert first["resume_available"] is False
    assert len(ctx.threads) == 1
    with closing(ctx.connect()) as conn:
        assert store.get_task(conn, first["task_id"])["status"] == "queued"


def test_authorization_header_mismatch_stops_before_preparation(setup_api):
    ctx = setup_api
    with pytest.raises(HTTPException) as error:
        api.create_atlas_task(9, payload(ctx), request("wrong-nonce"))
    assert error.value.detail["code"] == "BROWNFIELD_AUTHORIZATION_IDENTITY_MISMATCH"
    assert not any(call[0] == "prepare" for call in ctx.calls)
    assert not ctx.threads


def test_preflight_drift_has_no_admission(setup_api):
    ctx = setup_api
    original = payload(ctx)
    ctx.prepared["identity"]["exact_head"] = "d"*40
    with pytest.raises(HTTPException) as error:
        api.create_atlas_task(9, original, request())
    assert error.value.detail["code"] == "BROWNFIELD_PREFLIGHT_SCOPE_CHANGED"
    with closing(ctx.connect()) as conn:
        assert store.latest_task(conn, 9) is None


def test_nonce_collision_different_scope_rejected(setup_api):
    ctx = setup_api
    create(ctx)
    ctx.prepared["identity"]["exact_head"] = "d"*40
    with pytest.raises(HTTPException) as error:
        api.create_atlas_task(9, payload(ctx), request())
    assert error.value.detail["code"] == "BROWNFIELD_STORE_AUTHORIZATION_MISMATCH"
    assert len(ctx.threads) == 1


def test_get_is_read_only_and_project_boundary_enforced(setup_api):
    ctx = setup_api
    task = create(ctx)
    ctx.calls.clear()
    assert api.latest_atlas_task(9)["task"]["task_id"] == task["task_id"]
    assert api.get_atlas_task(9, task["task_id"])["task"]["status"] == "queued"
    assert ctx.calls == []
    with pytest.raises(HTTPException) as error:
        api.get_atlas_task(8, task["task_id"])
    assert error.value.status_code == 404
    assert api.latest_atlas_task(8) == {"task": None}


def test_orphan_get_is_unknown_without_mutation_and_resume_persists_unknown(setup_api):
    ctx = setup_api
    task = create(ctx)
    api._WORKERS.clear()
    with closing(ctx.connect()) as conn:
        store.claim_stage(conn, task_id=task["task_id"], stage_key="orientation/0", input_hash="f"*64, wire_bytes=99)
    public = api.get_atlas_task(9, task["task_id"])["task"]
    assert public["status"] == "unknown" and public["resume_available"] is False
    with closing(ctx.connect()) as conn:
        assert store.get_task(conn, task["task_id"])["status"] == "running"
    result = api.resume_atlas_task(9, task["task_id"], request())["task"]
    assert result["status"] == "unknown"
    assert len(ctx.threads) == 1
    with closing(ctx.connect()) as conn:
        assert store.get_task(conn, task["task_id"])["status"] == "unknown"


def test_resume_only_pending_fresh_scope_and_no_duplicate_worker(setup_api):
    ctx = setup_api
    task = create(ctx)
    api.resume_atlas_task(9, task["task_id"], request())
    assert len(ctx.threads) == 1
    api._WORKERS.clear()
    ctx.prepared["identity"]["exact_head"] = "d"*40
    with pytest.raises(HTTPException) as error:
        api.resume_atlas_task(9, task["task_id"], request())
    assert error.value.detail["code"] == "BROWNFIELD_SCOPE_CHANGED"
    assert len(ctx.threads) == 1


@pytest.mark.parametrize("state", ["succeeded", "unknown", "failed_pre_send", "failed_after_send"])
def test_terminal_tasks_cannot_resume(setup_api, state):
    ctx = setup_api
    task = create(ctx)
    api._WORKERS.clear()
    with closing(ctx.connect()) as conn:
        store.finish_task(conn, task["task_id"], status=state)
    with pytest.raises(HTTPException) as error:
        api.resume_atlas_task(9, task["task_id"], request())
    assert error.value.detail["code"] == "BROWNFIELD_TASK_NOT_RESUMABLE"
    assert len(ctx.threads) == 1


def test_output_public_contract_and_worker_promotion_identity(setup_api, monkeypatch):
    ctx = setup_api
    task = create(ctx)
    api._WORKERS.clear()
    content = {"schema_version": "project_profile_v2", "planned_modules": [{"client_id": "m", "name": "not exposed", "requirements": ["Original requirement"]}]}
    with closing(ctx.connect()) as conn:
        store.save_output(conn, task["task_id"], output={"content": content, "requirements": {"m": []}, "coverage": {"inspected_paths": 2}})
    public = api.get_atlas_task(9, task["task_id"])["task"]
    assert public["module_requirements"] == {"m": []}
    assert public["coverage"]["inspected_paths"] == 2
    assert public["generated_content_hash"] == api._canonicalize(api.ProjectProfileV2Content.model_validate(content))[1]
    changed = deepcopy(content)
    changed["planned_modules"][0]["requirements"] = ["Changed requirement"]
    assert public["generated_content_hash"] != api._canonicalize(api.ProjectProfileV2Content.model_validate(changed))[1]
    assert "content" not in public
    promotions = []
    monkeypatch.setattr(api, "_promote_generated_candidate", lambda **kwargs: promotions.append(kwargs) or {"id": 123})
    monkeypatch.setattr(api.core, "run_baseline", lambda prepared, stored, **kwargs: kwargs["promote"]({"safe": True}))
    with closing(ctx.connect()) as conn:
        stored = store.get_task(conn, task["task_id"])
    api._worker(ctx.prepared, stored, object())
    assert promotions[0]["authorization_hash"] == api.core.digest({"task_id": task["task_id"], "identity_hash": stored["identity_hash"]})


def test_exception_messages_do_not_expose_provider_body(setup_api, monkeypatch):
    def broken(*args):
        raise RuntimeError("Bearer secret-provider-body")
    monkeypatch.setattr(api.core, "prepare_baseline", broken)
    with pytest.raises(HTTPException) as error:
        api.preflight_atlas(9, api.BaselinePreflightPayload(plan_profile_id=42), request())
    assert error.value.detail["code"] == "BROWNFIELD_RUNTIME_FAILED"
    assert "secret" not in str(error.value.detail)


def test_schema_and_endpoint_connections_are_closed(setup_api):
    ctx = setup_api
    api.latest_atlas_task(9)
    for conn in ctx.opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

def test_metadata_staleness_is_boolean_or_unknown_without_git_or_provider(setup_api, monkeypatch):
    ctx = setup_api
    task = create(ctx)
    with closing(ctx.connect()) as conn:
        stored = store.get_task(conn, task["task_id"])
    scope = ctx.prepared["scope"]
    cap = SimpleNamespace(**ctx.prepared["identity"]["capability"])
    monkeypatch.setattr(api.core, "build_brownfield_strict_adapter", lambda: SimpleNamespace(get_capability=lambda **kwargs: cap))
    inputs = ({"git_url": scope["git_url"], "branch": scope["git_branch"]},
              {"id": scope["prd_id"], "source_hash": scope["prd_source_hash"]},
              {"remote_head": scope["exact_head"], "local_head": scope["exact_head"]})
    monkeypatch.setattr(api.profile_generation, "_read_current_inputs", lambda project: inputs)
    monkeypatch.setattr(api, "read_current_confirmed_project_profile", lambda project, conn: {"id":42,"content_hash":scope["plan_content_hash"]})
    with closing(ctx.connect()) as conn:
        assert READ_STALE_SCOPE(conn, stored) is False
        cap.model_id = "deepseek-v4-pro"
        assert READ_STALE_SCOPE(conn, stored) is True
        cap.model_id = "deepseek-flash"
        inputs[2]["remote_head"] = "e"*40
        assert READ_STALE_SCOPE(conn, stored) is True
        monkeypatch.setattr(api.profile_generation, "_read_current_inputs", lambda project: (_ for _ in ()).throw(RuntimeError("unavailable")))
        assert READ_STALE_SCOPE(conn, stored) is None


def test_http_route_latest_precedes_task_id_and_returns_async_admission(setup_api, monkeypatch):
    monkeypatch.setattr(api.threading, "Thread", REAL_THREAD)
    monkeypatch.setattr(api, "_start_worker", lambda prepared, task: False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        prefix = "/api/projects/9/project-state-baseline/atlas"
        assert client.get(prefix+"/tasks/latest").json() == {"task":None}
        result = client.post(prefix+"/tasks", json=payload(setup_api).model_dump(), headers={"local-idempotency-key":"authorization-one"})
        assert result.status_code == 202
        task_id = result.json()["task"]["task_id"]
        assert client.get(prefix+"/tasks/"+task_id).json()["task"]["task_id"] == task_id
        assert client.post(prefix+"/tasks", json={"authorized":False}).status_code == 422


def test_worker_start_failure_leaves_durable_resumable_task(setup_api, monkeypatch):
    ctx = setup_api
    class BrokenThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError("thread unavailable")
    monkeypatch.setattr(api.threading, "Thread", BrokenThread)
    with pytest.raises(HTTPException) as error:
        create(ctx)
    assert error.value.detail["code"] == "BROWNFIELD_WORKER_START_FAILED"
    restored = api.latest_atlas_task(9)["task"]
    assert restored["status"] == "queued" and restored["resume_available"] is True
    assert api._WORKERS == {}
    assert 9 not in api._PREPARING

@pytest.mark.parametrize('worker_registered', [True, False])
def test_active_task_preflight_stops_before_repository_preparation(setup_api,monkeypatch,worker_registered):
    ctx=setup_api;task=create(ctx)
    if not worker_registered:api._WORKERS.clear()
    ctx.calls.clear()
    with pytest.raises(HTTPException) as raised:
        api.preflight_atlas(9,api.BaselinePreflightPayload(plan_profile_id=42),request())
    assert raised.value.status_code==409
    assert not any(call[0]=='prepare' for call in ctx.calls)
    with closing(ctx.connect()) as conn:
        assert store.get_task(conn,task['task_id'])['status']=='queued'
        assert store.list_stages(conn,task['task_id'])==[]


def test_identical_authorization_replay_never_prepares_again(setup_api):
    ctx=setup_api;first=create(ctx);ctx.calls.clear()
    second=create(ctx)
    assert second['task_id']==first['task_id']
    assert not any(call[0]=='prepare' for call in ctx.calls)
    assert len(ctx.threads)==1


def test_new_authorization_cannot_prepare_while_project_task_active(setup_api):
    ctx=setup_api;create(ctx);ctx.calls.clear()
    with pytest.raises(HTTPException) as raised:
        api.create_atlas_task(9,payload(ctx,'authorization-two'),request('authorization-two'))
    assert raised.value.status_code==409
    assert not any(call[0]=='prepare' for call in ctx.calls)
    assert len(ctx.threads)==1


def test_preflight_reservation_blocks_same_project_create_not_other_project(setup_api,monkeypatch):
    import threading
    ctx=setup_api;entered=threading.Event();release=threading.Event();errors=[];calls=[]
    def prepare(project,plan):
        calls.append(project)
        if project==9:
            entered.set()
            assert release.wait(5)
        return deepcopy(ctx.prepared)
    monkeypatch.setattr(api.core,'prepare_baseline',prepare)
    def background():
        try:api.preflight_atlas(9,api.BaselinePreflightPayload(plan_profile_id=42),request())
        except Exception as exc:errors.append(type(exc).__name__)
    thread=REAL_THREAD(target=background);thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(HTTPException) as raised:
            api.create_atlas_task(9,payload(ctx),request())
        assert raised.value.status_code==409
        assert calls==[9]
        assert api.preflight_atlas(10,api.BaselinePreflightPayload(plan_profile_id=42),request())['status']=='ready'
        assert calls==[9,10]
        assert len(ctx.threads)==0
    finally:
        release.set();thread.join(6)
    assert not thread.is_alive() and not errors


def test_failed_preparation_releases_project_reservation(setup_api,monkeypatch):
    ctx=setup_api;original=api.core.prepare_baseline
    def fail(*args):raise ValueError('BROWNFIELD_SYNTHETIC_FAILURE')
    monkeypatch.setattr(api.core,'prepare_baseline',fail)
    with pytest.raises(HTTPException):api.preflight_atlas(9,api.BaselinePreflightPayload(plan_profile_id=42),request())
    monkeypatch.setattr(api.core,'prepare_baseline',original)
    assert api.preflight_atlas(9,api.BaselinePreflightPayload(plan_profile_id=42),request())['status']=='ready'

@pytest.mark.parametrize('field,value',[('project_id',8),('plan_profile_id',43),('preflight_identity_hash','f'*64)])
def test_nonce_fast_path_must_keep_project_plan_identity_binding(setup_api,field,value):
    ctx=setup_api;create(ctx);ctx.calls.clear()
    project=value if field=='project_id' else 9
    body=payload(ctx)
    if field!='project_id':body=body.model_copy(update={field:value})
    with pytest.raises(HTTPException) as raised:api.create_atlas_task(project,body,request())
    assert raised.value.detail['code']=='BROWNFIELD_STORE_AUTHORIZATION_MISMATCH'
    assert not any(call[0]=='prepare' for call in ctx.calls)


def test_concurrent_resume_cannot_scan_while_other_resume_prepares(setup_api,monkeypatch):
    import threading
    ctx=setup_api;task=create(ctx);api._WORKERS.clear()
    entered=threading.Event();release=threading.Event();errors=[];preparations=[]
    def prepare(*args):
        preparations.append(args);entered.set();assert release.wait(5);return deepcopy(ctx.prepared)
    monkeypatch.setattr(api.core,'prepare_baseline',prepare)
    def background():
        try:api.resume_atlas_task(9,task['task_id'],request())
        except Exception as exc:errors.append(type(exc).__name__)
    thread=REAL_THREAD(target=background);thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(HTTPException) as raised:api.resume_atlas_task(9,task['task_id'],request())
        assert raised.value.status_code==409
        assert len(preparations)==1
    finally:release.set();thread.join(6)
    assert not thread.is_alive() and not errors

def test_replayed_authorization_with_dead_worker_requires_explicit_resume(setup_api):
    ctx=setup_api;first=create(ctx);api._WORKERS.clear();ctx.calls.clear()
    replay=create(ctx)
    assert replay['task_id']==first['task_id'] and replay['resume_available'] is True
    assert not any(call[0]=='prepare' for call in ctx.calls)
    assert len(ctx.threads)==1
    api.resume_atlas_task(9,first['task_id'],request())
    assert len(ctx.threads)==2
    assert len([call for call in ctx.calls if call[0]=='prepare'])==1


def test_known_failed_task_new_authorization_keeps_original_identity_and_stage_reuse(setup_api):
    ctx=setup_api;first=create(ctx);api._WORKERS.clear()
    with closing(ctx.connect()) as conn:
        store.claim_stage(conn,task_id=first['task_id'],stage_key='orientation/0/0',input_hash='same-input',wire_bytes=42)
        store.finish_stage(conn,task_id=first['task_id'],stage_key='orientation/0/0',status='succeeded',result={'value':{'m':['opaque']}})
        store.finish_task(conn,first['task_id'],status='failed_after_send',error_code='PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED')
    second=api.create_atlas_task(9,payload(ctx,'authorization-two'),request('authorization-two'))['task']
    assert second['preflight_identity_hash']==first['preflight_identity_hash']
    with closing(ctx.connect()) as conn:
        reused=store.reuse_successful_stage(conn,task_id=second['task_id'],stage_key='orientation/0/0',input_hash='same-input',wire_bytes=42)
        assert reused['result']['reused_from_task_id']==first['task_id']
        assert reused['result']['value']=={'m':['opaque']}
        assert store.get_task(conn,first['task_id'])['status']=='failed_after_send'

@pytest.mark.parametrize('code,message',[
    ('BROWNFIELD_PROJECT_TASK_ACTIVE','已有分析任务正在进行，请查看原任务，暂不能重新扫描或创建任务。'),
    ('BROWNFIELD_PREPARATION_IN_PROGRESS','正在检查本项目的代码范围，请稍后；未发起新的模型请求。')])
def test_endpoint_busy_messages_are_fixed_safe_and_actionable(setup_api,monkeypatch,code,message):
    # AnyIO must not capture the worker-only fake Thread on its first import.
    monkeypatch.setattr(api.threading,'Thread',REAL_THREAD)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    ctx=setup_api
    if code=='BROWNFIELD_PROJECT_TASK_ACTIVE':
        with closing(ctx.connect()) as conn:
            store.create_task(conn,project_id=9,authorization_nonce='busy-endpoint',
                              identity=ctx.prepared['identity'],max_calls=ctx.prepared['identity']['max_calls'])
    else:api._PREPARING.add(9)
    app=FastAPI();app.include_router(api.router)
    try:
        with TestClient(app) as client:
            result=client.post('/api/projects/9/project-state-baseline/atlas/preflight',json={'plan_profile_id':42})
        assert result.status_code==409
        assert result.json()['detail']=={'code':code,'message':message}
    finally:api._PREPARING.discard(9)
