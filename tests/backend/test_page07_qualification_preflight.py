"""No real qualification, Git, credential or provider use."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import page07_model_preparation as prep,page07_send_authorization as preview,deepseek_transport
from app.model_provider_contract import ProviderCapability
from test_page07_preparation_scope import install


def capability():
    raw=deepseek_transport.get_deepseek_transport_capability(task_type=prep.TASK_TYPE,output_schema_version=prep.OUTPUT_SCHEMA_VERSION)
    return ProviderCapability(**{k:raw[k] for k in ('provider','model_id','model_version','task_type','output_schema_version')},context_window_tokens=1000000,max_output_tokens=raw['max_output_tokens'])

@pytest.mark.parametrize('flow',['prepare','preview'])
def test_shipping_missing_admission_stops_before_git_or_ledger(monkeypatch,flow):
    def forbidden(*a,**kw):pytest.fail('expensive work, credential or provider forbidden')
    run=install(monkeypatch,flow,forbidden,qualified=False)
    monkeypatch.setattr(prep,'_resolve_capability',lambda *a,**kw:capability())
    monkeypatch.setattr(preview.model_provider_runtime,'resolve_model_provider_adapter',lambda provider:SimpleNamespace(get_capability=lambda **kw:capability()))
    monkeypatch.setattr(prep.model_call_ledger,'prepare_model_call',forbidden)
    with pytest.raises(HTTPException) as caught:run()
    assert caught.value.detail['code']=='PAGE07_REGENERATE_QUALIFICATION_NOT_ADMITTED'


@pytest.mark.parametrize('bad',['provider','model_id','model_version','task_type','output_schema_version'])
def test_foreign_capability_is_not_queried_as_deepseek(monkeypatch,bad):
    from app import page07_qualification_preflight as helper
    monkeypatch.setattr(helper.registry,'lookup_qualified_record',lambda lookup:pytest.fail('foreign lookup'))
    with pytest.raises(HTTPException) as caught:
        helper.require_regenerate_qualification(capability=replace(capability(),**{bad:'foreign'}),rule_version=prep.RULE_VERSION,sample_pack_version=prep.SAMPLE_PACK_VERSION)
    assert caught.value.detail['code']=='PAGE07_REGENERATE_QUALIFICATION_INVALID'

@pytest.mark.parametrize('registry_code',['QUALIFICATION_REGISTRY_INVALID','QUALIFICATION_AMBIGUOUS','QUALIFICATION_LOOKUP_INVALID'])
def test_invalid_registry_is_distinct_and_never_leaks_text(monkeypatch,registry_code):
    from app import page07_qualification_preflight as helper
    def invalid(lookup):raise helper.registry.QualificationRegistryError(registry_code,'private text')
    monkeypatch.setattr(helper.registry,'lookup_qualified_record',invalid)
    with pytest.raises(HTTPException) as caught:
        helper.require_regenerate_qualification(capability=capability(),rule_version=prep.RULE_VERSION,sample_pack_version=prep.SAMPLE_PACK_VERSION)
    assert caught.value.detail['code']=='PAGE07_REGENERATE_QUALIFICATION_INVALID'
    assert 'private text' not in str(caught.value.detail)


def test_shipping_registry_stays_empty():
    from app import model_qualification_registry as registry
    assert registry._SHIPPING_ADMITTED_RECORDS==()

@pytest.mark.parametrize('drift',['prompt','sampling'])
def test_changed_exact_hash_cannot_reuse_synthetic_admission(monkeypatch,drift):
    from app import page07_qualification_preflight as helper
    run=install(monkeypatch,'prepare',lambda value:pytest.fail('must stop before manifest'))
    if drift=='prompt':
        monkeypatch.setattr(helper.model_gateway,'PROMPT_POLICY_TEXT',helper.model_gateway.PROMPT_POLICY_TEXT+' synthetic revision')
    else:
        original=helper.model_gateway._regenerate_sampling_policy
        monkeypatch.setattr(helper.model_gateway,'_regenerate_sampling_policy',lambda:(original()[0],'9'*64))
    monkeypatch.setattr(prep.model_call_ledger,'prepare_model_call',lambda **kw:pytest.fail('must stop before ledger'))
    with pytest.raises(HTTPException) as caught:run()
    assert caught.value.detail['code']=='PAGE07_REGENERATE_QUALIFICATION_NOT_ADMITTED'
