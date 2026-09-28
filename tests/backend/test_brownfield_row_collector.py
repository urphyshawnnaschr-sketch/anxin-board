"""Single-binding complete results only; no field union or cross-response state."""
import json
from copy import deepcopy
import httpx
import pytest
from fastapi import HTTPException
from app import brownfield_strict_adapter as adapter
from test_brownfield_strict_adapter import single_request, setup
from test_brownfield_strict_batch import batch_response

@pytest.mark.parametrize('kind',['orientation','verification'])
@pytest.mark.parametrize('copies',[1,2,16])
def test_identical_complete_results_only(kind,copies):
    req=single_request(kind);response=batch_response(req,copies)
    result=adapter.receipt(httpx.Response(200,json=response),req)
    assert len(result.result['modules' if kind=='orientation' else 'requirements'])==1
    assert result.total_tokens==5
    assert result.calls_received==result.row_occurrences_received==copies
    assert result.calls_collapsed==result.duplicate_rows_removed==copies-1
    assert adapter.TRANSPORT_POLICY['format']=='strict-chat-single-binding/8'
    assert adapter.normalization_metadata(result)['calls_policy']=='identical-complete-single-result/1'

@pytest.mark.parametrize('mutation',['missing','empty','wrapper','extra','conflict','type','slot','duplicate_id'])
def test_no_guessing_defaulting_or_conflict_selection(mutation):
    req=single_request('orientation');response=batch_response(req,2);calls=response['choices'][0]['message']['tool_calls']
    value=json.loads(calls[1]['function']['arguments'])
    if mutation=='missing':value.pop('seed_1')
    elif mutation=='empty':value={}
    elif mutation=='wrapper':value={'row_0':value}
    elif mutation=='extra':value['private-key']=deepcopy(value)
    elif mutation=='conflict':value['seed_0']=0
    elif mutation=='type':value['seed_0']=-1.0
    elif mutation=='slot':value.pop('seed_0')
    else:calls[1]['id']=calls[0]['id']
    calls[1]['function']['arguments']=json.dumps(value)
    with pytest.raises(HTTPException):adapter.receipt(httpx.Response(200,json=response),req)

@pytest.mark.parametrize('same_response',[True,False])
def test_no_union_of_complementary_fields_or_cross_response_state(same_response):
    req=single_request('orientation');response=batch_response(req,2)
    calls=response['choices'][0]['message']['tool_calls']
    for i,call in enumerate(calls):
        value=json.loads(call['function']['arguments'])
        call['function']['arguments']=json.dumps({k:v for k,v in value.items() if (k=='seed_0')==(i==0)})
    for group in ([calls] if same_response else [[calls[0]],[calls[1]]]):
        response['choices'][0]['message']['tool_calls']=group
        with pytest.raises(HTTPException):adapter.receipt(httpx.Response(200,json=response),req)

@pytest.mark.parametrize('mutation',['slot_order','unknown_citation','positive_without_citation','wrong_name','too_many'])
def test_complete_calls_keep_original_boundaries(mutation):
    req=single_request('verification');response=batch_response(req,2);calls=response['choices'][0]['message']['tool_calls']
    first=json.loads(calls[0]['function']['arguments']);second=json.loads(calls[1]['function']['arguments'])
    if mutation=='slot_order':
        first.update(status='partial',evidence_0=0);second.update(status='partial',evidence_1=0)
    elif mutation=='unknown_citation':second['evidence_0']=0
    elif mutation=='positive_without_citation':second['status']='implemented'
    elif mutation=='wrong_name':calls[1]['function']['name']='other'
    else:response['choices'][0]['message']['tool_calls']=calls*9
    calls[0]['function']['arguments']=json.dumps(first);calls[1]['function']['arguments']=json.dumps(second)
    with pytest.raises(HTTPException):adapter.receipt(httpx.Response(200,json=response),req)

def test_complete_results_are_one_send_with_usage_counted_once(monkeypatch):
    req=single_request('orientation');response=batch_response(req,2)
    live,sent,_=setup(monkeypatch,payload=response);result=live.execute_with_credential(req,'synthetic')
    assert len(sent)==1 and result.total_tokens==5
    assert len(sent[0][1]['content'])==live.estimate_request_bytes(messages=req.messages,max_output_tokens=req.max_output_tokens,model_id=req.model_id)
