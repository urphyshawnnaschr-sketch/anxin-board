"""Synthetic preflight scope tests: no Git, database, credentials or provider."""
import pytest
from app import context_candidate_runtime as cache
from app import page07_model_execution as subject


@pytest.mark.parametrize('fail',[False,True],ids=['success','error'])
def test_each_regenerate_preflight_reuses_locally_and_then_clears(monkeypatch,fail):
    reads=[]
    def read(snapshot):
        reads.append(snapshot)
        return {'items':[]}
    def gateway(**kwargs):
        first=cache.build_context_candidate_set(4)
        first['items'].append('private caller mutation')
        assert cache.build_context_candidate_set(4)['items']==[]
        if fail: raise RuntimeError('synthetic validation failure')
        return {'model_call_id':13}
    monkeypatch.setattr(cache,'_build_context_candidate_set',read)
    monkeypatch.setattr(subject.model_execution,'_require_input',lambda **kw:(13,{}))
    monkeypatch.setattr(subject.model_execution,'_require_ready_preflight',lambda value,**kw:value)
    monkeypatch.setattr(subject.model_gateway,'build_daily_report_regenerate_gateway_preflight',gateway)
    for attempt in range(1,3):
        if fail:
            with pytest.raises(RuntimeError,match='synthetic validation failure'):
                subject.build_ready_daily_report_regenerate_preflight(model_call_id=13,budget_record={},report_version_id=6)
        else:
            assert subject.build_ready_daily_report_regenerate_preflight(model_call_id=13,budget_record={},report_version_id=6)=={'model_call_id':13}
        assert reads==[4]*attempt
        assert cache.request_cache_namespace('candidate') is None


def test_execute_has_no_preflight_cache_when_consuming_transient_or_sending(monkeypatch):
    events=[]
    def gateway(**kwargs):
        cache.build_context_candidate_set(4)
        cache.build_context_candidate_set(4)
        events.append('preflight')
        return {'provider':'synthetic','model_call_id':13}
    def outside(name,result):
        assert cache.request_cache_namespace('candidate') is None
        events.append(name)
        return result
    monkeypatch.setattr(cache,'_build_context_candidate_set',lambda snapshot:{})
    monkeypatch.setattr(subject.model_execution,'_require_input',lambda **kw:(13,{}))
    monkeypatch.setattr(subject.model_execution,'_require_ready_preflight',lambda value,**kw:value)
    monkeypatch.setattr(subject.model_gateway,'build_daily_report_regenerate_gateway_preflight',gateway)
    from types import SimpleNamespace
    adapter=SimpleNamespace(execute=lambda req:outside('execute',{}))
    monkeypatch.setattr(subject.model_provider_runtime,'resolve_model_provider_adapter',lambda provider:outside('adapter',adapter))
    transient={'task_type':'daily_report_regenerate','output_schema_version':'daily-report-regenerate/1.0'}
    monkeypatch.setattr(subject.model_gateway,'materialize_daily_report_regenerate_gateway_request_transient',lambda **kw:outside('transient',transient))
    monkeypatch.setattr(subject.model_execution,'_require_transient',lambda value,**kw:value)
    from app import report_reanalysis_cancellation
    monkeypatch.setattr(report_reanalysis_cancellation,'claim_reanalysis_send_once',lambda **kw:outside('claim',None))
    monkeypatch.setattr(subject.model_execution,'_provider_request',lambda value:value)
    monkeypatch.setattr(subject.model_execution,'_receipt_mapping',lambda value:value)
    monkeypatch.setattr(subject.model_execution_results,'record_model_execution_result',lambda **kw:outside('record',{'id':1}))
    assert subject.execute_daily_report_regenerate_model_call(model_call_id=13,budget_record={},report_version_id=6)=={'id':1}
    assert events==['preflight','adapter','transient','claim','execute','record']
