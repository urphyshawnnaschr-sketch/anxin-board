import sqlite3,hashlib
from app.profile_reconciliation_batches import plan_reconciliation_batches,run_batch,BatchError
from app.profile_reconciliation_candidate import aggregate_candidate
from app.context_token_framing import COUNTING_POLICY_VERSION
import pytest

def test_38_module_candidate_complete_idempotent_and_plan_bound():
 h='a'*40
 plan={'schema_version':'project_profile_v2','project_summary':'','planned_modules':[{'client_id':f'm{i}','name':f'Module {i}','description':'','prd_refs':['prd-1'],'requirements':['feature'],'exclusions':[]} for i in range(38)],'implementation_mappings':[],'unplanned_code_features':[],'domain_glossary':[],'exclude_patterns':[],'notes':''}
 budget={'context_window_tokens':12000,'max_output_tokens':2000,'reserved_output_tokens':2000,'safety_margin_tokens':1000,'counting_policy_version':COUNTING_POLICY_VERSION}
 batches=plan_reconciliation_batches(plan,[],exact_head=h,plan_profile_id=1,budget_record=budget)
 c=sqlite3.connect(':memory:')
 for b in batches:run_batch(c,b,fake_dispatch=lambda b:[{'planned_module_id':m['client_id'],'status':'unknown','evidence_ids':[],'rationale':'Synthetic fixture has no implementation evidence.'} for m in b['planned_modules']],current_head=lambda:h)
 result=aggregate_candidate(c,batches,plan,current_head=lambda:h)
 assert result['content']['planned_modules']==plan['planned_modules']
 assert len(result['content']['implementation_mappings'])==38
 assert {m['planned_module_id'] for m in result['content']['implementation_mappings']}=={m['client_id'] for m in plan['planned_modules']}
 assert aggregate_candidate(c,batches,plan,current_head=lambda:h)==result
 assert c.execute('select count(*) from profile_reconciliation_candidates').fetchone()[0]==1
 with pytest.raises(BatchError,match='PLAN_CHANGED'):aggregate_candidate(c,batches,{**plan,'notes':'changed'},current_head=lambda:h)
 with pytest.raises(BatchError,match='HEAD_CHANGED'):aggregate_candidate(c,batches,plan,current_head=lambda:'b'*40)


def test_old_whole_repo_send_stops_before_authorization_or_credentials(monkeypatch):
 from app import project_profile_generation_v2 as v2
 from app import project_profile_generation as core
 from fastapi import HTTPException
 def forbidden(*a,**k):raise AssertionError('must not prepare or dispatch')
 for name in ['ensure_profile_generation_schema','_record_authorization','_read_provider_credential','_provider_adapter_resolver']:
  monkeypatch.setattr(core,name,forbidden)
 with pytest.raises(HTTPException) as exc:
  v2.generate(2,v2.GenerateV2Payload(authorized=True,include_implementation=True,plan_profile_id=1))
 assert exc.value.detail['code']=='PROFILE_RECONCILIATION_BATCH_PLAN_REQUIRED'
 assert exc.value.phase=='failed_pre_send'
