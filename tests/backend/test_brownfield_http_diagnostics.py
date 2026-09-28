import json
import pytest
from fastapi import HTTPException
from app.deepseek_live_profile_adapter import _profile_http_error
from app import brownfield_baseline as core
from app import brownfield_baseline_store as store
from app import brownfield_baseline_api as api
from test_brownfield_baseline import setup
from test_brownfield_baseline_api import setup_api, create

@pytest.mark.parametrize('status,category', [(400,'request'),(401,'auth'),(402,'payment'),(403,'forbidden'),(404,'model_unavailable'),(422,'request'),(429,'rate'),(500,'service'),(599,'service'),(418,'other')])
def test_http_error_metadata_is_bounded_and_durable(tmp_path,status,category):
    error=_profile_http_error(status)
    expected={'provider_http_status':status,'provider_http_class':category}
    assert core.safe_diagnostic(error)==expected
    model,prepared,task,connect=setup(tmp_path)
    model.fail=error
    core.run_baseline(prepared,task,connection_factory=connect,scope_reader=lambda:prepared['scope'],credential_reader=lambda:'synthetic',promote=lambda _:pytest.fail('promotion'))
    with connect() as conn:
        assert store.get_task(conn,task['task_id'])['status']=='failed_after_send'
        assert store.list_stages(conn,task['task_id'])[0]['result']=={'diagnostic':expected}
    assert len(model.calls)==1

@pytest.mark.parametrize('status',[True,99,600,'402',None,{},-1])
def test_invalid_status_never_reaches_safe_metadata(status):
    assert core.safe_diagnostic(HTTPException(502,detail={'diagnostic':{'provider_http_status':status,'provider_http_class':'payment','raw_body':'private-marker'}}))=={}

def test_api_retains_only_safe_http_metadata(setup_api):
    ctx=setup_api
    task=create(ctx)
    with ctx.connect() as conn:
        store.claim_stage(conn,task_id=task['task_id'],stage_key='verify/0/test/0/0',input_hash='e'*64,wire_bytes=42)
        store.finish_stage(conn,task_id=task['task_id'],stage_key='verify/0/test/0/0',status='failed_after_send',error_code='PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED',result={'diagnostic':{'provider_http_status':402,'provider_http_class':'payment','raw_body':'private-marker','headers':'private-marker'}})
        store.finish_task(conn,task['task_id'],status='failed_after_send',error_code='PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED')
    public=api.latest_atlas_task(9)['task']
    assert public['failure_diagnostic']=={'provider_http_status':402,'provider_http_class':'payment'}
    assert 'private-marker' not in json.dumps(public)
    assert public['resume_available'] is False


@pytest.mark.parametrize('status', [301,400,401,402,403,404,418,422,429,500,599])
def test_actual_strict_adapter_preserves_status_without_response_content(monkeypatch,status):
    from test_brownfield_strict_adapter import setup as setup_transport, request
    live,sent,_=setup_transport(monkeypatch,payload={'secret':'private-marker'},status=status)
    with pytest.raises(HTTPException) as raised:
        live.execute_with_credential(request(),'synthetic')
    assert core.safe_diagnostic(raised.value)['provider_http_status']==status
    assert 'private-marker' not in json.dumps(raised.value.detail)
    assert len(sent)==1

def test_http_category_is_derived_not_copied_from_untrusted_value():
    assert core.safe_diagnostic(HTTPException(502,detail={'diagnostic':{'provider_http_status':402,'provider_http_class':'private-marker','headers':'private-marker'}}))=={'provider_http_status':402,'provider_http_class':'payment'}


def test_shared_reconciliation_caller_classifies_payment_as_received():
    from app.profile_reconciliation_provider import _classify
    assert _classify(_profile_http_error(402)) == ('failed_after_send', 'PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED')

def test_app_registered_generation_attempt_persists_payment_without_retry(tmp_path,monkeypatch):
    import sqlite3
    from app import main
    from app import project_profile_generation_attempt as attempt
    db=tmp_path/'payment-attempt.db'
    monkeypatch.setenv('ANXINBOARD_DB_PATH',str(db))
    calls=[]
    def operation():
        calls.append(1)
        raise _profile_http_error(402)
    for _ in range(2):
        with pytest.raises(HTTPException) as raised:
            attempt.execute_profile_generation_once(project_id=7,idempotency_key='payment-no-retry-001',operation=operation)
        assert raised.value.detail['code']=='PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED'
    assert calls==[1]
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status,error_code FROM profile_generation_attempts').fetchone()==('failed_after_send','PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED')
