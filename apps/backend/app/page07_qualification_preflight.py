"""Pure local early diagnosis; never grants qualification or send authority."""
from collections.abc import Mapping
from fastapi import HTTPException
from app import deepseek_transport, model_gateway, model_qualification_registry as registry
from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION
from app.model_provider_contract import ProviderCapability


def _fail(missing=False):
    code = 'PAGE07_REGENERATE_QUALIFICATION_NOT_ADMITTED' if missing else 'PAGE07_REGENERATE_QUALIFICATION_INVALID'
    message = ('当前安装版本尚未准入此模型的报告重分析，此次未调用模型。请安装具有对应准入记录的版本；修改授权码或反复重试不能解决。'
               if missing else '当前重分析模型身份或资格登记无法核对，此次未调用模型。请检查安装版本，系统不会自动重试。')
    return HTTPException(409,detail={'code':code,'message':message})


def require_regenerate_qualification(*, capability, rule_version, sample_pack_version, expected_identity=None):
    """Use the same exact registry and actual prompt/sampling hashes as send-time checks."""
    transport=deepseek_transport.get_deepseek_transport_capability(
        task_type='daily_report_regenerate',output_schema_version='daily-report-regenerate/1.0')
    fields=('provider','model_id','model_version','task_type','output_schema_version')
    if (not isinstance(capability,ProviderCapability)
            or any(getattr(capability,k)!=transport[k] for k in fields)
            or (expected_identity is not None and (not isinstance(expected_identity,Mapping)
                or any(getattr(capability,k)!=expected_identity.get(k) for k in fields)))):
        raise _fail()
    contract,prompt_hash,_=model_gateway._regenerate_prompt_contract()
    _,sampling_hash=model_gateway._regenerate_sampling_policy()
    lookup={**{k:getattr(capability,k) for k in fields},
        'ai_contract_schema_version':AI_CONTRACT_SCHEMA_VERSION,
        'prompt_version':contract['prompt_version'],'prompt_contract_hash':prompt_hash,
        'rule_version':rule_version,'sample_pack_version':sample_pack_version,
        'sampling_parameters_hash':sampling_hash}
    try:
        registry.lookup_qualified_record(lookup)
    except registry.QualificationRegistryError as exc:
        raise _fail(missing=exc.code=='QUALIFICATION_NOT_ADMITTED') from None
