"""Page07 request-local reuse only: synthetic objects, no Git, DB or provider."""
from types import SimpleNamespace
from dataclasses import replace
from app import deepseek_transport
from app.model_provider_contract import ProviderCapability
import pytest
from app import context_candidate_runtime as cache
from app import context_redaction_runtime as redaction
from app import page07_model_preparation as prep
from app import page07_send_authorization as preview


def install(monkeypatch, flow, probe, *, qualified=True):
    task = dict(project_id=1,id=2,local_task_id='synthetic',evidence_snapshot_id=3,identity_hash='a'*64,task_type=prep.TASK_TYPE,state='queued')
    call = dict(model_call_id=7, project_id=1, local_task_id='synthetic', task_type=prep.TASK_TYPE,
                output_schema_version=prep.OUTPUT_SCHEMA_VERSION,rule_version=prep.RULE_VERSION,
                benchmark_sample_pack_version=prep.SAMPLE_PACK_VERSION,preparation_state='prepared',
                call_identity_hash='b'*64,provider='synthetic',model_id='synthetic',model_version='v1')
    transport = deepseek_transport.get_deepseek_transport_capability(task_type=prep.TASK_TYPE, output_schema_version=prep.OUTPUT_SCHEMA_VERSION)
    call.update({key: transport[key] for key in ('provider','model_id','model_version')})
    manifest = dict(call, final_context_manifest_hash='c'*64,framed_payload_hash='d'*64,
                    local_request_readiness_state='ready_for_gateway_evaluation')
    capability=ProviderCapability(**{key:transport[key] for key in ('provider','model_id','model_version','task_type','output_schema_version')}, context_window_tokens=1000000,max_output_tokens=transport['max_output_tokens'])
    if qualified:
        # Explicit test-only evidence admission; never changes the shipping registry.
        from app import model_qualification_registry as registry, model_gateway
        from test_model_qualification_registry import _synthetic_record
        contract,prompt_hash,_=model_gateway._regenerate_prompt_contract()
        _,sampling_hash=model_gateway._regenerate_sampling_policy()
        record=_synthetic_record(**{key:transport[key] for key in ('provider','model_id','model_version')},prompt_contract_hash=prompt_hash,sampling_parameters_hash=sampling_hash)
        monkeypatch.setattr(registry,'lookup_qualified_record',lambda lookup:registry._lookup_exact_record_from_registry(lookup,(record,)))
    if flow == 'prepare':
        monkeypatch.setattr(prep,'get_report_regeneration_task',lambda **kw:task)
        monkeypatch.setattr(prep,'_resolve_capability',lambda:capability)
        monkeypatch.setattr(prep,'_budget_record',lambda cap:{})
        monkeypatch.setattr(prep.model_call_ledger,'prepare_model_call',lambda **kw:call)
        monkeypatch.setattr(prep.model_budget_profiles,'build_model_budget_profile',lambda **kw:{'budget_profile_hash':'e'*64})
        monkeypatch.setattr(prep.final_context_manifest,'build_final_context_manifest',lambda **kw:probe(manifest))
        monkeypatch.setattr(prep,'_assert_binding',lambda **kw:None)
        return lambda:prep.prepare_daily_report_regenerate_model_call(project_id=1,local_task_id='synthetic',preparation_authorized=True)
    monkeypatch.setattr(preview.report_review,'get_reanalysis_request',lambda **kw:{'replacement_task':task})
    monkeypatch.setattr(preview,'get_model_call',lambda mid:call)
    monkeypatch.setattr(preview,'get_daily_report_regenerate_budget_record',lambda **kw:{})
    monkeypatch.setattr(preview,'_manifest_for',lambda *a:probe(manifest))
    monkeypatch.setattr(preview.model_gateway,'_materialize_payload_rebound',lambda **kw:probe(b'synthetic'))
    monkeypatch.setattr(preview.model_gateway,'_read_regenerate_model_call',lambda **kw:call)
    monkeypatch.setattr(preview.model_gateway,'_read_regenerate_subject',lambda **kw:{})
    monkeypatch.setattr(preview.model_gateway,'_load_regenerate_capability',lambda:transport)
    capability=ProviderCapability(**{key:transport[key] for key in ('provider','model_id','model_version','task_type','output_schema_version')}, context_window_tokens=1000000,max_output_tokens=transport['max_output_tokens'])
    monkeypatch.setattr(preview.model_provider_runtime,'resolve_model_provider_adapter',lambda provider:SimpleNamespace(get_capability=lambda **kw:capability))
    monkeypatch.setattr(preview.model_gateway,'_build_regenerate_gateway_request_plan',lambda **kw:{'request_envelope_hash':'f'*64,'reserved_output_tokens':64,'conservative_local_request_upper_bound':1000})
    monkeypatch.setattr(preview,'_fit_or_block',lambda **kw:None)
    monkeypatch.setattr(preview.model_gateway,'_expected_regenerate_data_scope_hash',lambda **kw:'a'*64)
    return lambda:preview.build_reanalysis_send_authorization_preview(project_id=1,report_version_id=2,model_call_id=7)


@pytest.mark.parametrize('flow',['prepare','preview'])
@pytest.mark.parametrize('fail',[False,True],ids=['success','exception'])
def test_page07_reuses_inside_request_but_clears_for_next_request(monkeypatch,flow,fail):
    calls={'candidate':0,'redaction':0}
    def candidate(snapshot):
        calls['candidate']+=1
        return {'snapshot':snapshot,'items':[]}
    def redact(**kw):
        calls['redaction']+=1
        return {'content':'synthetic redacted'}
    monkeypatch.setattr(cache,'_build_context_candidate_set',candidate)
    monkeypatch.setattr(redaction,'_build_context_redaction_result',redact)
    def probe(result):
        first=cache.build_context_candidate_set(3)
        first['items'].append('local mutation')
        assert cache.build_context_candidate_set(3)['items']==[]
        redaction.build_context_redaction_result(model_call_id=7,budget_record={},target='profile')
        redaction.build_context_redaction_result(model_call_id=7,budget_record={},target='profile')
        if fail: raise RuntimeError('synthetic failure')
        return result
    run=install(monkeypatch,flow,probe)
    for request in range(1,3):
        if fail:
            with pytest.raises(RuntimeError,match='synthetic failure'):run()
        else:
            assert run()['provider_send_state']=='not_attempted'
        assert calls=={'candidate':request,'redaction':request}
        assert cache.request_cache_namespace('candidate') is None

@pytest.mark.parametrize('changed', [None, 'provider', 'model_id', 'model_version', 'task_type', 'output_schema_version', 'context_window_tokens', 'max_output_tokens'], ids=['valid','provider','model','version','task','schema','context','output'])
def test_regenerate_uses_real_transport_shape_and_bound_adapter_capability(monkeypatch,changed):
    from fastapi import HTTPException
    real_fit=preview._fit_or_block
    run=install(monkeypatch,'preview',lambda value:value)
    monkeypatch.setattr(preview,'_fit_or_block',real_fit)
    transport=deepseek_transport.get_deepseek_transport_capability(task_type=prep.TASK_TYPE,output_schema_version=prep.OUTPUT_SCHEMA_VERSION)
    assert 'context_window_tokens' not in transport
    capability=ProviderCapability(**{key:transport[key] for key in ('provider','model_id','model_version','task_type','output_schema_version')},context_window_tokens=1000,max_output_tokens=64)
    if changed:
        capability=replace(capability,**{changed:True if changed.endswith('_tokens') else 'foreign'})
    monkeypatch.setattr(preview.model_provider_runtime,'resolve_model_provider_adapter',lambda provider:SimpleNamespace(get_capability=lambda **kw:capability))
    monkeypatch.setattr(preview.model_gateway,'_build_regenerate_gateway_request_plan',lambda **kw:dict(request_envelope_hash='f'*64,reserved_output_tokens=64,conservative_local_request_upper_bound=1000))
    if changed:
        with pytest.raises(HTTPException):run()
    else:
        assert run()['authorization_state']=='awaiting_human_confirmation'

@pytest.mark.parametrize('reserved,upper,allowed',[(64,1000,True),(65,1000,False),(64,1001,False),(True,1,False),(1,True,False)])
def test_typed_capability_fit_boundaries(reserved,upper,allowed):
    from fastapi import HTTPException
    capability=ProviderCapability('synthetic','model','v1','task','schema',1000,64)
    if allowed:
        preview._fit_or_block(reserved_output=reserved,upper=upper,capability=capability)
    else:
        with pytest.raises(HTTPException):preview._fit_or_block(reserved_output=reserved,upper=upper,capability=capability)


def test_transport_output_limit_still_blocks_when_adapter_would_fit(monkeypatch):
    from fastapi import HTTPException
    real_fit=preview._fit_or_block
    run=install(monkeypatch,'preview',lambda value:value)
    monkeypatch.setattr(preview,'_fit_or_block',real_fit)
    transport=deepseek_transport.get_deepseek_transport_capability(task_type=prep.TASK_TYPE,output_schema_version=prep.OUTPUT_SCHEMA_VERSION)
    transport['max_output_tokens']=63
    monkeypatch.setattr(preview.model_gateway,'_load_regenerate_capability',lambda:transport)
    with pytest.raises(HTTPException) as caught:run()
    assert caught.value.detail['code']=='PAGE07_SEND_AUTHORIZATION_REQUEST_NOT_FIT'
