import copy
import json
import pytest
from fastapi import HTTPException
from app import project_profiles as profiles
from app import project_profile_generation_v2 as generation
from app.profile_generation_failure import run_pre_send
from app.project_profile_generation_attempt import execute_profile_generation_once
from app import db
from test_project_profiles import env, _create_project, _confirm_prd, _base_content


def plan():
    return {'schema_version': 'project_profile_v2', 'planned_modules': [
        {'client_id': 'login', 'name': '登录', 'requirements': ['允许用户登录'], 'prd_refs': ['prd-test']}
    ]}


def test_prd_only_candidate_confirm_and_legacy_hash(env):
    client, _ = env
    pid = _create_project(client)
    _confirm_prd(client, pid)
    raw = profiles.ProjectProfileContent.model_validate(_base_content()).model_dump()
    assert profiles.parse_profile_content(raw).model_dump() == raw
    result = client.post(f'/api/projects/{pid}/profile-candidates', json=plan())
    assert result.status_code == 201, result.text
    candidate = result.json()
    assert candidate['status'] == 'candidate'
    assert candidate['content']['implementation_mappings'] == []
    confirmed = client.post(f"/api/profile-candidates/{candidate['id']}/confirm", json={'edit_version':1,'confirmed_by':'test-human'})
    assert confirmed.status_code == 200, confirmed.text
    assert profiles.read_current_confirmed_project_profile(pid)["content"]["schema_version"] == "project_profile_v2"


def test_plan_needs_no_git_or_code_evidence():
    result = generation.validate_result(plan(), prd_evidence_id='prd-test', repo_items=[], head='')
    assert result.implementation_mappings == []
    assert profiles._completeness_missing(result.model_dump()) == []


@pytest.mark.parametrize('mutation', ['wrong_prd','unknown_module','fake_evidence','wrong_path','wrong_head','not_started'])
def test_adversarial_mapping(mutation):
    value = plan()
    value['implementation_mappings'] = [{'planned_module_id':'login','status':'implemented','exact_head':'a'*40,'evidence_ids':['repo-code-1'],'paths':[{'type':'backend','pattern':'login.py'}],'rationale':'代码包含登录流程'}]
    mapping=value['implementation_mappings'][0]
    if mutation=='wrong_prd': value['planned_modules'][0]['prd_refs']=['repo-code-1']
    if mutation=='unknown_module': mapping['planned_module_id']='other'
    if mutation=='fake_evidence': mapping['evidence_ids']=['repo-code-fake']
    if mutation=='wrong_path': mapping['paths'][0]['pattern']='another.py'
    if mutation=='wrong_head': mapping['exact_head']='b'*40
    if mutation=='not_started': mapping['status']='not_started'
    with pytest.raises(HTTPException) as error:
        generation.validate_result(value,prd_evidence_id='prd-test',repo_items=[{'evidence_id':'repo-code-1','path':'login.py'}],head='a'*40)
    assert error.value.detail['code']=='PROFILE_GENERATION_AI_RESULT_INVALID'


def test_extra_code_is_not_planned_scope():
    value=plan()
    value['unplanned_code_features']=[{'client_id':'extra','name':'调试工具','exact_head':'a'*40,'evidence_ids':['repo-code-1'],'paths':[{'type':'backend','pattern':'debug.py'}]}]
    content=generation.validate_result(value,prd_evidence_id='prd-test',repo_items=[{'evidence_id':'repo-code-1','path':'debug.py'}],head='a'*40)
    assert [m['client_id'] for m in profiles.profile_planned_modules(content)]==['login']
    assert content.unplanned_code_features[0].client_id=='extra'


@pytest.mark.parametrize('code', ['CONTEXT_REDACTION_UNSAFE_AMBIGUITY','PROFILE_GENERATION_REPO_CONTEXT_TOO_LARGE','PROFILE_GENERATION_REPO_FILE_TOO_LARGE','PROFILE_GENERATION_REPO_TEXT_ENCODING_UNSUPPORTED','PROFILE_GENERATION_REPO_TREE_INVALID','PROFILE_GENERATION_CREDENTIAL_REQUIRED','PROFILE_GENERATION_CREDENTIAL_READ_FAILED','NEW_UNRECOGNIZED_ERROR'])
def test_presend_failure_is_durable_and_sanitized(tmp_path,monkeypatch,code):
    monkeypatch.setenv('ANXINBOARD_DB_PATH',str(tmp_path/'attempt.db'))
    count=[]
    def prepare():
        count.append(1)
        raise HTTPException(409,{'code':code,'message':'customer-private-secret'})
    def execute():
        return execute_profile_generation_once(project_id=2,idempotency_key='test-profile-v2-key-0001',operation=lambda:run_pre_send(prepare))
    for _ in range(2):
        with pytest.raises(HTTPException) as error: execute()
        assert error.value.detail['code'] != 'PROFILE_GENERATION_RESULT_UNKNOWN'
        assert 'customer-private-secret' not in str(error.value.detail)
    assert len(count)==1
    with db.get_connection() as conn:
        row=conn.execute('SELECT status,error_message FROM profile_generation_attempts').fetchone()
    assert row['status']=='failed_pre_send'


def test_real_generation_plan_only_uses_fake_provider_and_candidate(env,monkeypatch):
    from app import project_profile_generation as core
    from app.model_provider_contract import ProviderCapability,ProviderReceipt
    client,_=env
    pid=_create_project(client);_confirm_prd(client,pid)
    monkeypatch.setattr(generation,'read_repo_context',lambda *a:pytest.fail('plan must not read Git'))
    monkeypatch.setattr(core,'_read_provider_credential',lambda:'fake-test-only')
    calls=[]
    class FakeAdapter:
        def get_capability(self,**kw):
            return ProviderCapability(provider='deepseek',model_id='fake',model_version='fake',context_window_tokens=1000000,max_output_tokens=24000,**kw)
        def execute_with_credential(self,request,credential):
            calls.append(request)
            inputs=json.loads(request.messages[1]['content'])
            value=plan();value['planned_modules'][0]['prd_refs']=[inputs['prd_evidence_id']]
            return ProviderReceipt(provider='deepseek',provider_response_id='fake-response',actual_model='fake',provider_runtime_fingerprint='fake',finish_reason='stop',prompt_tokens=1,completion_tokens=1,total_tokens=2,result=value)
    monkeypatch.setattr(core,'_provider_adapter_resolver',FakeAdapter)
    result=generation.generate(pid,generation.GenerateV2Payload(authorized=True))
    assert result['profile']['status']=='candidate'
    assert result['profile']['content']['schema_version']=='project_profile_v2'
    assert len(calls)==1
    with db.get_connection() as conn:
        assert conn.execute('SELECT count(*) FROM profile_generation_runs WHERE project_id=?',(pid,)).fetchone()[0]==1


def test_v2_mounted_finalize_failure_remains_unknown_and_never_reexecutes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app import project_profile_generation_attempt as ledger
    monkeypatch.setenv('ANXINBOARD_DB_PATH', str(tmp_path/'attempt.db'))
    monkeypatch.setattr(generation, 'require_local_write_request', lambda *a,**k:None)
    calls=[]
    monkeypatch.setattr(generation, 'generate', lambda *a: calls.append(1) or {'profile':{'id':1,'status':'candidate'}})
    def failed_finalize(*a):
        raise HTTPException(500, {'code':'PROFILE_GENERATION_ATTEMPT_FINALIZE_FAILED'})
    monkeypatch.setattr(ledger, '_finish_success', failed_finalize)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            generation.generate_candidate(2,generation.GenerateV2Payload(authorized=True),SimpleNamespace(headers={'local-idempotency-key':'idem-v2-finalize-0001'}))
        assert error.value.detail['code']=='PROFILE_GENERATION_RESULT_UNKNOWN'
    assert calls==[1]


def test_plan_edit_invalidates_old_implementation_server_side(env):
    client,_=env
    pid=_create_project(client);_confirm_prd(client,pid)
    value=plan()
    value['implementation_mappings']=[{'planned_module_id':'login','status':'implemented','exact_head':'a'*40,'evidence_ids':['repo-code-1'],'paths':[{'type':'backend','pattern':'login.py'}],'rationale':'old scope'}]
    candidate=client.post(f'/api/projects/{pid}/profile-candidates',json=value).json()
    changed=copy.deepcopy(candidate['content'])
    changed['planned_modules'][0]['requirements']=['new much wider scope']
    response=client.put(f"/api/profile-candidates/{candidate['id']}",json={'edit_version':1,'content':changed})
    assert response.status_code==200,response.text
    assert response.json()['profile']['content']['implementation_mappings']==[]


def test_post_receipt_context_failure_is_after_send(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from app import project_profile_generation as core
    from app.model_provider_contract import ProviderReceipt
    monkeypatch.setenv('ANXINBOARD_DB_PATH',str(tmp_path/'attempt.db'))
    calls=[]
    class Fake:
        def execute_with_credential(self,*a):
            calls.append(1)
            return ProviderReceipt(provider='deepseek',provider_response_id='fake',actual_model='fake',provider_runtime_fingerprint='fake',finish_reason='stop',prompt_tokens=1,completion_tokens=1,total_tokens=2,result=plan())
    request=SimpleNamespace(local_task_id='fake')
    capability=SimpleNamespace(provider='deepseek',model_id='fake')
    monkeypatch.setattr(generation,'_prepare',lambda *a: ({}, {'remote_head':'a'*40}, 'prd-test', [], [], Fake(), capability, {}, 1, request, 'fake', None))
    def fail(*a):
        raise HTTPException(409,{'code':'PROFILE_GENERATION_GIT_CHECK_REQUIRED'})
    monkeypatch.setattr(core,'_read_current_inputs',fail)
    with pytest.raises(HTTPException) as error:
        execute_profile_generation_once(project_id=2,idempotency_key='idem-v2-after-send-001',operation=lambda:generation.generate(2,generation.GenerateV2Payload(authorized=True,include_implementation=True)))
    assert error.value.detail['code']=='PROFILE_GENERATION_SOURCE_CHANGED'
    assert calls==[1]
    with db.get_connection() as conn:
        assert conn.execute('SELECT status FROM profile_generation_attempts').fetchone()[0]=='failed_after_send'
