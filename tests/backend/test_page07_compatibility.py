"""Offline synthetic receipts; no execution authorization or Human scores."""
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from qualification.page07.evaluator import evaluate_compatibility, digest, METRICS, SPECIAL, current_identity

def fixture(tmp_path):
    side=json.loads((ROOT/'qualification/page07/samples.json').read_text(encoding='utf-8'))
    slots=[]; requests={}
    for sample in side['samples']:
        sid=sample['sample_id']; messages=[{'role':'user','content':'Synthetic fixture '+sid}]
        wire={'model':'synthetic-model','messages':messages}
        request={'wire':wire,'wire_hash':digest(wire),'request_hash':digest(sid),'plan':{'messages':messages,'messages_hash':digest(messages),'request_envelope_hash':digest(sid)}}
        requests[sid]=request
        slots.append({'slot_id':'base-'+sid,'sample_id':sid,'phase':'base','wire_hash':request['wire_hash'],'request_hash':request['request_hash']})
    for sid in ('BASE-JSTS-04','BASE-JAVA-02','BASE-PY-04'):
        for n in range(1,4): slots.append({**next(s for s in slots if s['sample_id']==sid),'slot_id':f'repeat-{sid}-{n}','phase':'repeat'})
    m={'source_identity':{'source_commit':'a'*40},'sidecar_hash':digest(side),'provider':'synthetic','model_id':'synthetic-model','model_version':'synthetic-v1','capability':{'provider':'synthetic','model_id':'synthetic-model','model_version':'synthetic-v1'},'slots':slots,'requests':requests}
    m['qualification_identity']=current_identity(m['capability'])
    for request in requests.values(): request['plan'].update({k:m['qualification_identity'][k] for k in ('prompt_contract_hash','sampling_parameters_hash','prompt_version')})
    m['manifest_hash']=digest(m)
    def write(name,value): (tmp_path/name).write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    write('intent.json',{'manifest_hash':m['manifest_hash'],'max_attempts':21})
    review={'manifest_pin':m['manifest_hash'],'source_identity':m['source_identity'],'reviewer_kind':'independent_agent_not_human','human_readability_reviews':None,'base_samples':[],'repeat_samples':[]}
    samples={s['sample_id']:s for s in side['samples']}
    for i,s in enumerate(slots):
        sample=samples[s['sample_id']];report=deepcopy(sample['original_report']);p=report['feature_progress'][0]
        p.update(stage=sample['base_allowed_stage_set'][0],implementation_scope=sample['base_allowed_implementation_scope_set'][0])
        receipt={**s,'status':'succeeded','actual_model':'synthetic-model','provider_response_id':f'offline-{i}','provider_runtime_fingerprint':'offline-test-only','usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2},'result':{'new_report':report,'correction_trace':[{'handled':True,'reason':'Synthetic only','evidence_ids':[sample['human_correction']['evidence_id']]}]}}
        receipt['response_hash']=digest(receipt);write(s['slot_id']+'.receipt.json',receipt);write(s['slot_id']+'.claim.json',s)
        row={'slot_id':s['slot_id'],'response_hash':receipt['response_hash'],'machine_observation':{'sample_id':s['sample_id'],'hard_failures':[],**{k:True for k in METRICS}},**{k:True for k in SPECIAL}}
        if s['phase']=='repeat':row['repeat_core']={'main_feature':p['feature'],'stage':p['stage'],'implementation_scope':p['implementation_scope'],'contradiction':False}
        review[s['phase']+'_samples'].append(row)
    return m,side,review

def run(tmp_path,m,s,r): return evaluate_compatibility(m,s,tmp_path,[r],pin=m['manifest_hash'],current_capability=m['capability'])

def test_complete_v2_keeps_original_human_gate_and_never_admits(tmp_path):
    m,s,r=fixture(tmp_path);value=run(tmp_path,m,s,r)
    assert value['compatibility_passed'] is True
    assert value['original_harness']['qualification_status']=='human_gate_required'
    assert value['human_readability_certified'] is False
    assert value['automatic_admission'] is False

@pytest.mark.parametrize('attack',['old_pack','missing_review','builder','repeat_lie','repeat_fabrication','special','diagnostic','missing_receipt','reused_receipt','extra_slot','old_pin','old_identity','old_prompt'])
def test_fail_closed(tmp_path,attack):
    m,s,r=fixture(tmp_path)
    if attack=='old_pack':s['sample_pack_version']='page07-regenerate-qualification-pack/1.0'
    if attack=='missing_review':r['base_samples'].pop()
    if attack=='builder':r['reviewer_kind']='fixture_builder_agent'
    if attack=='repeat_lie':r['repeat_samples'][0]['repeat_core']['stage']='wrong'
    if attack=='repeat_fabrication':r['repeat_samples'][0]['machine_observation']['no_fabrication']=False
    if attack=='special':r['base_samples'][0][SPECIAL[0]]=False
    if attack=='diagnostic':(tmp_path/'intent.json').write_text(json.dumps({'synthetic_diagnostic_only':True}),encoding='utf-8')
    if attack=='missing_receipt':(tmp_path/(m['slots'][0]['slot_id']+'.receipt.json')).unlink()
    if attack=='reused_receipt':
        p=tmp_path/(m['slots'][1]['slot_id']+'.receipt.json');v=json.loads(p.read_text(encoding='utf-8'));v['provider_response_id']='offline-0';v['response_hash']=digest({k:x for k,x in v.items() if k!='response_hash'});p.write_text(json.dumps(v),encoding='utf-8')
    if attack=='extra_slot':m['slots'].append(deepcopy(m['slots'][0]))
    if attack=='old_identity':m['qualification_identity']['rule_version']='page07-regenerate-rules/1.0'
    if attack=='old_prompt':m['qualification_identity']['prompt_contract_hash']='0'*64
    if attack=='old_pin':r['manifest_pin']='0'*64
    try: value=run(tmp_path,m,s,r)
    except ValueError: return
    assert value['compatibility_passed'] is False
