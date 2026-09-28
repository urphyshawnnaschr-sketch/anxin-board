from copy import deepcopy
import json
import httpx
import pytest
from app import brownfield_baseline as core
from app import brownfield_strict_adapter as adapter
from app.brownfield_strict_wire import build_strict_wire, decode_strict_wire, StrictWireError
from test_brownfield_strict_adapter import multirow_request
from test_brownfield_strict_batch import batch_response
from dataclasses import replace


def single(kind):
    req=multirow_request(kind)
    data=json.loads(req.messages[-1]['content'])
    if kind=='orientation': data['planned_modules']=data['planned_modules'][1:2]
    else:
        data['planned_module']['requirements']=data['planned_module']['requirements'][1:2]
        data['requirement_catalog']=data['requirement_catalog'][1:2]
    return replace(req,messages=(req.messages[0],dict(req.messages[1],content=json.dumps(data))))

@pytest.mark.parametrize('kind',['orientation','verification'])
def test_flat_single_binding_schema_and_logical_identity(kind):
    req=single(kind)
    messages,schema=build_strict_wire(req.messages)
    source=json.loads(messages[-1]['content'])
    flat=source['required_output']
    assert ('rationale' in schema['properties']) == (kind == 'verification')
    assert set(schema['required'])==set(flat)
    assert not any(k.startswith(('row_','req_')) for k in flat)
    assert 'row_N' not in messages[0]['content'] and 'req_N' not in messages[0]['content']
    logical=decode_strict_wire(flat,req.messages)
    assert len(logical['modules' if kind=='orientation' else 'requirements'])==1
    if kind=='orientation': assert logical['modules'][0]['planned_module_id']==source['planned_modules'][0]['client_id']
    else: assert logical['requirements'][0]['requirement_index']==0
    with pytest.raises(StrictWireError):build_strict_wire(multirow_request(kind).messages)

@pytest.mark.parametrize('kind',['orientation','verification'])
@pytest.mark.parametrize('calls',[1,2,16])
def test_identical_flat_calls_only(kind,calls):
    req=single(kind)
    response=batch_response(req,calls)
    result=adapter.receipt(httpx.Response(200,json=response),req)
    assert result.total_tokens==5 and result.calls_received==calls and result.calls_collapsed==calls-1
    assert adapter.TRANSPORT_POLICY['format']=='strict-chat-single-binding/8'
    assert adapter.normalization_metadata(result)['unique_rows']==1

@pytest.mark.parametrize('mutation',['partial','extra','conflict','wrapper'])
def test_flat_never_merges_incomplete_or_conflicting_calls(mutation):
    req=single('orientation'); response=batch_response(req,2)
    call=response['choices'][0]['message']['tool_calls'][1]['function']
    flat=json.loads(call['arguments'])
    if mutation=='partial':flat.pop('seed_0')
    elif mutation=='extra':flat['row_0']={}
    elif mutation=='conflict':flat['rationale']='different'
    else:flat={'row_0':flat}
    call['arguments']=json.dumps(flat)
    with pytest.raises(Exception):adapter.receipt(httpx.Response(200,json=response),req)


def test_each_noncontiguous_requirement_keeps_all_source_partitions_and_original_text():
    from types import SimpleNamespace
    from test_brownfield_atlas_spike import fixture_catalog_bundle, plan
    _, bundle, _ = fixture_catalog_bundle()
    module=deepcopy(plan()['planned_modules'][2])
    module['requirements']=['original zero','unused original one','original two']
    evidence=deepcopy(bundle['evidence'][0])
    bundle=deepcopy(bundle)
    bundle['evidence']=[dict(deepcopy(evidence),evidence_id=f'repo-code-fragment-{i}',content='x'*14000) for i in range(3)]
    cap=SimpleNamespace(model_id='deepseek-flash',context_window_tokens=1000000)
    class Estimator:
        estimate_request_bytes=staticmethod(adapter.estimate_request_bytes)
    whole=core.verification_requests(module,bundle,Estimator(),cap,8000,[2])[0]
    cap.context_window_tokens=adapter.estimate_request_bytes(messages=whole['messages'],max_output_tokens=8000,model_id=cap.model_id)+8000+core.SAFETY_MARGIN-1
    pieces=core.verification_requests(module,bundle,Estimator(),cap,8000,[2,0])
    assert {tuple(p['indexes']) for p in pieces}=={(2,),(0,)}
    for index in [2,0]:
        owned=[p for p in pieces if p['indexes']==[index]]
        assert len(owned)>1
        assert all(p['module']['requirements']==[module['requirements'][index]] for p in owned)
        assert {e['evidence_id'] for p in owned for e in p['bundle']['evidence']}=={e['evidence_id'] for e in bundle['evidence']}
        assert all(p['bundle']['source_partition'] for p in owned)
        assert all(core._fits(p['messages'],Estimator(),cap,8000,1) for p in owned)
    assert module['requirements']==['original zero','unused original one','original two']


def test_each_module_sees_every_catalog_partition_without_truncation():
    from types import SimpleNamespace
    from test_brownfield_atlas_spike import fixture_catalog_bundle, plan
    catalog,_,_=fixture_catalog_bundle()
    catalog=deepcopy(catalog)
    for path in catalog['paths']: path['synthetic_hint']='x'*8000
    sample=plan()
    cap=SimpleNamespace(model_id='deepseek-flash',context_window_tokens=1000000)
    class Estimator:
        estimate_request_bytes=staticmethod(adapter.estimate_request_bytes)
    first=core.orientation_requests(sample,catalog,['m1'],Estimator(),cap,8000)[0]
    cap.context_window_tokens=adapter.estimate_request_bytes(messages=first['messages'],max_output_tokens=8000,model_id=cap.model_id)+8000+core.SAFETY_MARGIN-1
    parts=core.orientation_requests(sample,catalog,['m1','m2'],Estimator(),cap,8000)
    for identity in ['m1','m2']:
        owned=[p for p in parts if p['module_ids']==[identity]]
        assert len(owned)>1
        assert {p['path_id'] for part in owned for p in part['catalog']['paths']}=={p['path_id'] for p in catalog['paths']}
        assert all(core._fits(p['messages'],Estimator(),cap,8000,1) for p in owned)

@pytest.mark.parametrize('kind',['orientation','verification'])
def test_zero_binding_rejected(kind):
    req=single(kind);data=json.loads(req.messages[-1]['content'])
    if kind=='orientation':data['planned_modules']=[]
    else:data['planned_module']['requirements']=[]
    with pytest.raises(StrictWireError):build_strict_wire((req.messages[0],dict(req.messages[1],content=json.dumps(data))))


def test_final_decode_failure_counts_one_binding_not_flat_fields(monkeypatch):
    req=single('orientation'); response=batch_response(req,1)
    original=adapter.decode_strict_wire
    calls=[]
    def decode(*args,**kwargs):
        calls.append(1)
        if len(calls)==2: raise ValueError('private-marker')
        return original(*args,**kwargs)
    monkeypatch.setattr(adapter,'decode_strict_wire',decode)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:adapter.receipt(httpx.Response(200,json=response),req)
    assert error.value.detail['diagnostic']['accepted_row_count']==1
    assert 'private-marker' not in str(error.value.detail)


def test_orientation_has_no_unused_prose_but_verification_keeps_explanation():
    from app.brownfield_atlas_spike import validate_orientation_result, SpikeContractError
    from test_brownfield_atlas_spike import fixture_catalog_bundle
    req=single('orientation')
    canonical=json.loads(req.messages[-1]['content'])
    assert canonical['schema_version']=='brownfield-atlas-orientation/2'
    assert 'rationale' not in canonical['required_output']['modules'][0]
    messages,schema=build_strict_wire(req.messages)
    flat=json.loads(messages[-1]['content'])['required_output']
    assert set(flat)=={f'seed_{i}' for i in range(12)}
    assert 'rationale' not in json.dumps(schema)
    assert 'Rationale' not in messages[0]['content']
    logical=decode_strict_wire(flat,req.messages)
    assert set(logical['modules'][0])=={'planned_module_id','seed_path_indexes'}
    catalog,_,_=fixture_catalog_bundle()
    identity=logical['modules'][0]['planned_module_id']
    assert validate_orientation_result(logical,catalog=catalog,module_ids=[identity])=={identity:[]}
    logical['modules'][0]['rationale']='unused prose'
    with pytest.raises(SpikeContractError):validate_orientation_result(logical,catalog=catalog,module_ids=[identity])
    with pytest.raises(StrictWireError):decode_strict_wire(dict(flat,rationale='unused prose'),req.messages)
    verification=single('verification')
    _,schema=build_strict_wire(verification.messages)
    assert schema['properties']['rationale']['pattern']==r'^[\s\S]{1,1200}$'
