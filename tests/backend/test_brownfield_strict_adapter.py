import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app import brownfield_strict_adapter as adapter
from app.brownfield_strict_wire import build_strict_wire
from app.brownfield_atlas_spike import build_orientation_messages, validate_orientation_result, SpikeContractError
from app.model_provider_contract import ProviderCredentialRequest
from test_brownfield_atlas_spike import plan, fixture_catalog_bundle


def request():
    catalog, _, _ = fixture_catalog_bundle()
    messages = build_orientation_messages(plan(), catalog, ['m1'])
    return ProviderCredentialRequest('test', 'deepseek', 'deepseek-flash', 'deepseek-flash', 'project_profile_build', 'project-profile-build/2.0', messages, 8000)


def envelope():
    rewritten, _ = build_strict_wire(request().messages)
    result = json.loads(rewritten[-1]['content'])['required_output']
    return {'id':'chat-test','object':'chat.completion','model':'deepseek-flash',
            'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
                'tool_calls':[{'id':'call-test','type':'function','function':{'name':adapter.TOOL_NAME,'arguments':json.dumps(result)}}]}}],
            'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}


def setup(monkeypatch, payload=None, error=None, status=200, raw=None):
    cap = SimpleNamespace(provider='deepseek',model_id='deepseek-flash',model_version='deepseek-flash',context_window_tokens=1000000,max_output_tokens=32000)
    base = SimpleNamespace(get_capability=lambda **kwargs:cap)
    monkeypatch.setattr(adapter, 'build_live_deepseek_profile_adapter', lambda:base)
    sent=[]
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):
            sent.append((url,kwargs))
            if error:raise error
            return httpx.Response(status,content=raw if raw is not None else json.dumps(envelope() if payload is None else payload).encode())
    monkeypatch.setattr(adapter,'_client',Client)
    return adapter.build_brownfield_strict_adapter(),sent,cap


def test_named_strict_tool_exact_wire_single_request_and_same_capability(monkeypatch):
    live,sent,cap=setup(monkeypatch)
    assert live.get_capability() is cap
    req=request()
    result=live.execute_with_credential(req,'synthetic')
    assert len(sent)==1
    url, options=sent[0]
    assert url=='https://api.deepseek.com/beta/chat/completions'
    wire=options['content']; payload=json.loads(wire)
    assert len(wire)==live.estimate_request_bytes(messages=req.messages,max_output_tokens=req.max_output_tokens,model_id=req.model_id)
    assert payload['model']=='deepseek-flash'
    assert payload['thinking']=={'type':'disabled'}
    assert payload['temperature']==0 and payload['max_tokens']==8000 and payload['stream'] is False
    assert payload['tool_choice']=={'type':'function','function':{'name':adapter.TOOL_NAME}}
    assert len(payload['tools'])==1
    function=payload['tools'][0]['function']
    assert function['strict'] is True and function['name']==adapter.TOOL_NAME
    assert function['parameters']==json.loads(payload['messages'][-1]['content'])['required_output_schema']
    assert set(payload)=={'model','messages','thinking','temperature','max_tokens','stream','tools','tool_choice'}
    assert result.result['modules'][0]['planned_module_id']=='m1'
    assert result.prompt_tokens==2 and result.completion_tokens==3 and result.total_tokens==5
    assert result.actual_model=='deepseek-flash'


@pytest.mark.parametrize('content',['absent',None,''])
def test_optional_empty_content(monkeypatch,content):
    payload=envelope(); message=payload['choices'][0]['message']
    if content=='absent':del message['content']
    else:message['content']=content
    live,sent,_=setup(monkeypatch,payload=payload)
    live.execute_with_credential(request(),'synthetic')
    assert len(sent)==1


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(model='wrong'),lambda p:p.update(object='wrong'),lambda p:p.update(id=''),
    lambda p:p.update(choices=[]),lambda p:p['choices'].append(p['choices'][0].copy()),
    lambda p:p['choices'][0].update(index=False),lambda p:p['choices'][0].update(finish_reason='stop'),
    lambda p:p['choices'][0]['message'].update(content='private-body-marker'),
    lambda p:p['choices'][0]['message'].update(role='user'),
    lambda p:p['choices'][0]['message'].update(tool_calls=[]),
    lambda p:p['choices'][0]['message']['tool_calls'].append(p['choices'][0]['message']['tool_calls'][0].copy()),
    lambda p:p['choices'][0]['message']['tool_calls'][0].update(type='other'),
    lambda p:p['choices'][0]['message']['tool_calls'][0].update(id=''),
    lambda p:p['choices'][0]['message']['tool_calls'][0]['function'].update(name='other'),
    lambda p:p['choices'][0]['message']['tool_calls'][0]['function'].update(arguments={}),
    lambda p:p.update(usage={'prompt_tokens':True,'completion_tokens':3,'total_tokens':4}),
    lambda p:p.update(usage={'prompt_tokens':2,'completion_tokens':3,'total_tokens':6}),
])
def test_bad_receipt_rejected_once_without_body_leak(monkeypatch,mutate):
    payload=envelope(); mutate(payload)
    live,sent,_=setup(monkeypatch,payload=payload)
    with pytest.raises(HTTPException) as exc:live.execute_with_credential(request(),'synthetic')
    assert len(sent)==1
    assert 'private-body-marker' not in str(exc.value.detail)


@pytest.mark.parametrize('raw',[b'{"object":"chat.completion","object":"private-body-marker"}',b'{"x":NaN}',b'private-body-marker',b''])
def test_outer_json_is_strict(monkeypatch,raw):
    live,sent,_=setup(monkeypatch,raw=raw)
    with pytest.raises(HTTPException) as exc:live.execute_with_credential(request(),'synthetic')
    assert exc.value.detail['code']=='PROFILE_GENERATION_STRICT_ENVELOPE_INVALID'
    assert len(sent)==1 and 'private-body-marker' not in str(exc.value.detail)


@pytest.mark.parametrize('arguments,suffix',[
    ('{"private-body-marker":1,"private-body-marker":2}','STRICT_ARGUMENT_CONFLICT'),
    ('{"private-body-marker":NaN}','CONTENT_JSON_NONFINITE_INVALID'),
    ('{"private-body-marker":','CONTENT_JSON_SYNTAX_INVALID'),
    ('{"private-body-marker":"\ud800"}','CONTENT_UTF8_INVALID'),
])
def test_tool_arguments_strict_json_safe_diagnostics(monkeypatch,arguments,suffix):
    payload=envelope();payload['choices'][0]['message']['tool_calls'][0]['function']['arguments']=arguments
    live,sent,_=setup(monkeypatch,payload=payload)
    with pytest.raises(HTTPException) as exc:live.execute_with_credential(request(),'synthetic')
    assert exc.value.detail['code'].endswith(suffix)
    assert 'private-body-marker' not in str(exc.value.detail)
    assert len(sent)==1


@pytest.mark.parametrize('error',[httpx.ReadTimeout('private-body-marker'),httpx.ConnectError('private-body-marker')])
def test_network_unknown_never_retries(monkeypatch,error):
    live,sent,_=setup(monkeypatch,error=error)
    with pytest.raises(HTTPException) as exc:live.execute_with_credential(request(),'synthetic')
    assert exc.value.detail['code']=='PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN'
    assert 'private-body-marker' not in str(exc.value.detail) and len(sent)==1


@pytest.mark.parametrize('status',[301,400,401,429,500])
def test_http_failures_no_retry(monkeypatch,status):
    live,sent,_=setup(monkeypatch,payload={'secret':'private-body-marker'},status=status)
    with pytest.raises(HTTPException) as exc:live.execute_with_credential(request(),'synthetic')
    assert 'private-body-marker' not in str(exc.value.detail) and len(sent)==1


def test_wire_boundary_is_actual_serialized_tool_request(monkeypatch):
    live,sent,cap=setup(monkeypatch);req=request()
    size=adapter.estimate_request_bytes(messages=req.messages,max_output_tokens=8000,model_id=req.model_id)
    cap.context_window_tokens=size+8000+16384-1
    with pytest.raises(HTTPException,match='REQUEST_OVER_BUDGET'):live.execute_with_credential(req,'synthetic')
    assert not sent
    cap.context_window_tokens+=1
    live.execute_with_credential(req,'synthetic')
    assert len(sent)==1


@pytest.mark.parametrize('change',[{'provider':'other'},{'model_id':'deepseek-v4-pro'},{'model_version':'changed'},{'max_output_tokens':32001}])
def test_selection_and_output_bounds_prevent_send(monkeypatch,change):
    live,sent,_=setup(monkeypatch)
    with pytest.raises(HTTPException):live.execute_with_credential(replace(request(),**change),'synthetic')
    assert not sent


def test_decoded_duplicates_preserved_for_existing_validator(monkeypatch):
    payload=envelope();fn=payload['choices'][0]['message']['tool_calls'][0]['function']
    result=json.loads(fn['arguments']);result.update(seed_0=0,seed_1=0);fn['arguments']=json.dumps(result)
    live,sent,_=setup(monkeypatch,payload=payload)
    logical=live.execute_with_credential(request(),'synthetic').result
    assert logical['modules'][0]['seed_path_indexes']==[0,0]
    catalog,_,_=fixture_catalog_bundle()
    with pytest.raises(SpikeContractError):validate_orientation_result(logical,catalog=catalog,module_ids=['m1'])
    assert len(sent)==1

@pytest.mark.parametrize('model',['deepseek-flash','deepseek-v4-pro'])
def test_supported_selected_model_is_not_substituted(monkeypatch,model):
    payload=envelope();payload['model']=model
    live,sent,cap=setup(monkeypatch,payload=payload)
    cap.model_id=cap.model_version=model
    receipt=live.execute_with_credential(replace(request(),model_id=model,model_version=model),'synthetic')
    assert receipt.actual_model==model
    assert json.loads(sent[0][1]['content'])['model']==model


@pytest.mark.parametrize('change',[{'provider':'other'},{'model_id':'deepseek-chat'}])
def test_unsupported_selected_capability_never_sends(monkeypatch,change):
    live,sent,cap=setup(monkeypatch)
    for key,value in change.items():setattr(cap,key,value)
    with pytest.raises(HTTPException,match='MODEL_UNSUPPORTED'):live.execute_with_credential(request(),'synthetic')
    assert not sent


@pytest.mark.parametrize('credential',[None,'','   '])
def test_missing_credential_never_sends(monkeypatch,credential):
    live,sent,_=setup(monkeypatch)
    with pytest.raises(HTTPException,match='CREDENTIAL_REQUIRED'):live.execute_with_credential(request(),credential)
    assert not sent


@pytest.mark.parametrize('slot', [True, 'private-body-marker', -2, 999999])
def test_invalid_decoded_slot_is_known_http_failure(monkeypatch, slot):
    payload = envelope()
    function = payload['choices'][0]['message']['tool_calls'][0]['function']
    result = json.loads(function['arguments'])
    result['seed_0'] = slot
    function['arguments'] = json.dumps(result)
    live, sent, _ = setup(monkeypatch, payload=payload)
    with pytest.raises(HTTPException) as exc:
        live.execute_with_credential(request(), 'synthetic')
    assert exc.value.status_code == 502
    assert exc.value.detail['code'] == 'PROFILE_GENERATION_STRICT_WIRE_INVALID'
    assert 'private-body-marker' not in str(exc.value.detail)
    assert len(sent) == 1


def multirow_request(kind):
    catalog, bundle, _ = fixture_catalog_bundle()
    if kind == 'orientation':
        messages = build_orientation_messages(plan(), catalog, ['m3', 'm1', 'm2'])
    else:
        from app.brownfield_atlas_spike import build_requirement_verification_messages
        messages = build_requirement_verification_messages(plan()['planned_modules'][2], bundle)
    return replace(request(), messages=messages)


def single_request(kind):
    req = multirow_request(kind)
    source = json.loads(req.messages[-1]['content'])
    if kind == 'orientation':
        source['planned_modules'] = source['planned_modules'][:1]
    else:
        source['planned_module']['requirements'] = source['planned_module']['requirements'][:1]
        source['requirement_catalog'] = source['requirement_catalog'][:1]
    return replace(req, messages=(req.messages[0], dict(req.messages[1], content=json.dumps(source))))


def multirow_envelope(req):
    payload = adapter.build_payload(messages=req.messages, max_output_tokens=req.max_output_tokens, model_id=req.model_id)
    example = json.loads(payload['messages'][-1]['content'])['required_output']
    response = envelope()
    response['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = json.dumps(example)
    return payload, response


@pytest.mark.parametrize('kind', ['orientation', 'verification'])
def test_complete_batch_preserves_all_input_and_deterministic_row_order(monkeypatch, kind):
    req = single_request(kind)
    wire, response = multirow_envelope(req)
    schema = wire['tools'][0]['function']['parameters']
    assert set(schema['required']) == set(schema['properties'])
    assert schema['additionalProperties'] is False
    before = json.loads(req.messages[-1]['content'])
    after = json.loads(wire['messages'][-1]['content'])
    assert {k:v for k,v in before.items() if k not in {'required_output','required_output_schema'}} == {k:v for k,v in after.items() if k not in {'required_output','required_output_schema'}}
    assert 'ONE complete flat result' in wire['messages'][0]['content']
    assert adapter.TRANSPORT_POLICY['format'] == 'strict-chat-single-binding/8'
    assert adapter.TRANSPORT_POLICY['max_collector_calls'] == 16
    fn = response['choices'][0]['message']['tool_calls'][0]['function']
    fn['arguments'] = json.dumps(dict(reversed(list(json.loads(fn['arguments']).items()))))
    live, sent, _ = setup(monkeypatch, payload=response)
    result = live.execute_with_credential(req, 'synthetic')
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (2, 3, 5)
    assert len(sent) == 1
    if kind == 'orientation':
        assert [r['planned_module_id'] for r in result.result['modules']] == ['m3']
    else:
        assert [r['requirement_index'] for r in result.result['requirements']] == [0]


def test_verification_schema_encodes_status_citation_relation_for_every_requirement():
    req = single_request('verification')
    payload, _ = multirow_envelope(req)
    schema = payload['tools'][0]['function']['parameters']
    assert schema['type'] == 'object' and schema['additionalProperties'] is False
    assert set(schema['required']) == set(schema['properties'])
    for assessment in [schema]:
        unknown, positive = assessment['anyOf']
        for branch in (unknown, positive):
            assert branch['type'] == 'object' and branch['additionalProperties'] is False
            assert set(branch['required']) == set(branch['properties']) == {'status', 'rationale'} | {f'evidence_{i}' for i in range(6)}
            assert branch['properties']['rationale'] == {'type':'string','pattern':r'^[\s\S]{1,1200}$'}
        assert unknown['properties']['status']['enum'] == ['unknown']
        assert all(unknown['properties'][f'evidence_{i}'] == {'type':'integer','minimum':-1,'maximum':len(json.loads(req.messages[-1]['content'])['source_evidence'])-1} for i in range(6))
        assert positive['properties']['status']['enum'] == ['implemented', 'partial']
        last = len(json.loads(req.messages[-1]['content'])['source_evidence']) - 1
        assert positive['properties']['evidence_0'] == {'type':'integer','minimum':0,'maximum':last}
        assert all(positive['properties'][f'evidence_{i}'] == {'type':'integer','minimum':-1,'maximum':last} for i in range(1,6))


def test_no_source_evidence_only_allows_unknown_schema_variant():
    req = single_request('verification')
    source = json.loads(req.messages[-1]['content'])
    source['source_evidence'] = []
    req = replace(req, messages=(req.messages[0], dict(req.messages[1], content=json.dumps(source))))
    payload, _ = multirow_envelope(req)
    for row in [payload['tools'][0]['function']['parameters']]:
        assert len(row['anyOf']) == 1
        assert row['anyOf'][0]['properties']['status']['enum'] == ['unknown']


@pytest.mark.parametrize('status,citation', [('implemented',-1), ('partial',-1)])
def test_assessment_contradiction_is_never_accepted_or_repaired(monkeypatch,status,citation):
    req = single_request('verification')
    _, response = multirow_envelope(req)
    fn = response['choices'][0]['message']['tool_calls'][0]['function']
    arguments = json.loads(fn['arguments'])
    arguments.update(status=status,evidence_0=citation)
    fn['arguments'] = json.dumps(arguments)
    live, sent, _ = setup(monkeypatch,payload=response)
    with pytest.raises(HTTPException, match='WIRE_INVALID'):
        live.execute_with_credential(req,'synthetic')
    assert len(sent) == 1


@pytest.mark.parametrize('mutation', ['missing_field','extra_top','null','list','extra_inner','old_row_index'])
def test_batch_and_row_exact_keys(monkeypatch,mutation):
    req = single_request('verification')
    _, response = multirow_envelope(req)
    fn = response['choices'][0]['message']['tool_calls'][0]['function']
    arguments = json.loads(fn['arguments'])
    if mutation == 'missing_field': del arguments['rationale']
    elif mutation == 'extra_top': arguments['private-marker'] = 'private-marker'
    elif mutation == 'null': arguments = None
    elif mutation == 'list': arguments = []
    elif mutation == 'extra_inner': arguments['private-marker'] = 'private-marker'
    elif mutation == 'old_row_index': arguments = dict(row_index=0, assessment=arguments)
    fn['arguments'] = json.dumps(arguments)
    live, sent, _ = setup(monkeypatch,payload=response)
    with pytest.raises(HTTPException) as exc: live.execute_with_credential(req,'synthetic')
    assert exc.value.detail['code'] in {'PROFILE_GENERATION_STRICT_WIRE_INVALID', 'PROFILE_GENERATION_RESPONSES_CONTENT_NOT_OBJECT'}
    assert 'private-marker' not in str(exc.value.detail) and len(sent) == 1


@pytest.mark.parametrize('kind', ['orientation', 'verification'])
def test_identical_collector_fields_normalized_once_with_safe_counts(monkeypatch, kind):
    req = single_request(kind)
    _, response = multirow_envelope(req)
    fn = response['choices'][0]['message']['tool_calls'][0]['function']
    arguments = json.loads(fn['arguments'])
    key = next(iter(arguments))
    fn['arguments'] = fn['arguments'][:-1] + ',' + json.dumps(key) + ':' + json.dumps(arguments[key]) + '}'
    live, sent, _ = setup(monkeypatch, payload=response)
    result = live.execute_with_credential(req, 'synthetic')
    assert len(sent) == 1
    assert adapter.normalization_metadata(result) == {
        'policy': 'identical-typed-duplicates/1', 'duplicate_fields_removed': 1, 'tool_calls_normalized': 1,
        'calls_received': 1, 'calls_collapsed': 0, 'row_occurrences_received': 1, 'duplicate_rows_removed': 0, 'unique_rows': 1, 'calls_policy': 'identical-complete-single-result/1',
    }
    assert len(result.result['modules' if kind == 'orientation' else 'requirements']) == 1


def test_equal_outer_duplicate_still_rejected(monkeypatch):
    raw = json.dumps(envelope())[:-1] + ',"object":"chat.completion"}'
    live, sent, _ = setup(monkeypatch, raw=raw.encode())
    with pytest.raises(HTTPException, match='STRICT_ENVELOPE_INVALID'): live.execute_with_credential(request(), 'synthetic')
    assert len(sent) == 1


def test_conflict_diagnostic_is_ordinal_only(monkeypatch):
    payload = envelope()
    fn = payload['choices'][0]['message']['tool_calls'][0]['function']
    fn['arguments'] = fn['arguments'][:-1] + ',"seed_0":"private-body-marker"}'
    live, sent, _ = setup(monkeypatch, payload=payload)
    with pytest.raises(HTTPException) as exc: live.execute_with_credential(request(), 'synthetic')
    assert exc.value.detail['code'] == 'PROFILE_GENERATION_STRICT_ARGUMENT_CONFLICT'
    assert exc.value.detail['diagnostic'] == {'tool_call_count': 1, 'expected_row_count': 1, 'tool_call_ordinal': 0}
    assert 'private-body-marker' not in str(exc.value.detail) and len(sent) == 1

def test_unknown_valid_citations_are_context_not_positive():
    from test_brownfield_strict_batch import batch_response
    req=single_request('verification');payload=batch_response(req)
    function=payload['choices'][0]['message']['tool_calls'][0]['function']
    row=json.loads(function['arguments']);row.update(status='unknown',evidence_0=0)
    function['arguments']=json.dumps(row)
    receipt=adapter.receipt(httpx.Response(200,json=payload),req)
    assert receipt.result['requirements'][0]['status']=='unknown'
    assert receipt.result['requirements'][0]['evidence_indexes']==[0]
