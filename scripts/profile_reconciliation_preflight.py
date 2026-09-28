"""Offline Project Profile reconciliation preflight. No real provider integration.

Reads Product metadata in SQLite read-only mode, writes only the explicitly supplied
local output directory. Fake results stay in an isolated SQLite store, never Product.
"""
import argparse,json,sqlite3,pathlib,sys,os,socket,ast
sys.dont_write_bytecode=True
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'apps/backend'))
def forbidden(*args,**kwargs): raise RuntimeError('PREFLIGHT_NETWORK_FORBIDDEN')
socket.socket.connect=forbidden
socket.create_connection=forbidden
from app import project_profile_generation as core
from app.project_profiles import read_current_confirmed_project_profile
from app.repository_index import build_indexed_repository_map as build_repository_map
from app.profile_reconciliation_batches import plan_reconciliation_batches,run_batch,_bytes,COUNTING_POLICY_VERSION
from app.profile_reconciliation_candidate import aggregate_candidate

def constant(path,name):
 tree=ast.parse((ROOT/path).read_text(encoding='utf-8'))
 for stmt in tree.body:
  if isinstance(stmt,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in stmt.targets):return ast.literal_eval(stmt.value)
 raise ValueError('MISSING_BUDGET_AUTHORITY')

def main():
 p=argparse.ArgumentParser();p.add_argument('--project-id',type=int,required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args()
 db=pathlib.Path(os.environ['LOCALAPPDATA'])/'AnxinBoard/anxinboard.db'
 a.output=a.output.resolve()
 if a.output==db.parent.resolve() or db.parent.resolve() in a.output.parents:raise ValueError('OUTPUT_MUST_BE_ISOLATED_FROM_PRODUCT')
 a.output.mkdir(parents=True,exist_ok=True)
 def ro():
  c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('pragma query_only=on');return c
 core.get_connection=ro
 project,prd,git=core._read_current_inputs(a.project_id)
 with ro() as c:
  profile=read_current_confirmed_project_profile(a.project_id,conn=c)
  model=c.execute('select actual_model from profile_generation_runs where project_id=? order by id desc limit 1',(a.project_id,)).fetchone()
 m=build_repository_map(a.project_id,project,git)
 # Existing declared Product capability and report margin; no provider capability
 # lookup or Windows credential selection read. Exact token count is unknown.
 cap='apps/backend/app/deepseek_live_profile_adapter.py';report='apps/backend/app/report_generation_preparation.py'
 max_output=constant(cap,'PROFILE_MAX_OUTPUT_TOKENS')
 budget={'context_window_tokens':constant(cap,'_CONTEXT_WINDOW_TOKENS'),'max_output_tokens':max_output,
         'reserved_output_tokens':min(max_output,constant(report,'_RESERVED_OUTPUT_TOKENS')),
         'safety_margin_tokens':constant(report,'_SAFETY_MARGIN_TOKENS'),'counting_policy_version':COUNTING_POLICY_VERSION}
 batches=plan_reconciliation_batches(profile['content'],m['evidence'],exact_head=m['exact_head'],plan_profile_id=profile['id'],budget_record=budget,repository_coverage={k:m[k] for k in ['coverage','complete_inventory','complete_safe_analysis']})
 def current_head():
  with ro() as check_conn:
   fresh=read_current_confirmed_project_profile(a.project_id,conn=check_conn)
   if fresh['id']!=profile['id'] or fresh['content_hash']!=profile['content_hash']:raise ValueError('PLAN_CHANGED')
  from app.git_client import GitClient,resolve_workspace_paths,open_workspace_access
  paths=resolve_workspace_paths(a.project_id,'0'*32,create=False)
  access=open_workspace_access(paths,project['git_url'])
  try:
   client=GitClient()
   local=client.get_local_head(access)
   if client.get_remote_head_ref(access,project['branch'])!=local:raise ValueError('HEAD_CHANGED')
   return local
  finally:access.close()
 def fake(b):return [{'planned_module_id':x['client_id'],'status':'unknown','evidence_ids':[]} for x in b['planned_modules']]
 # Verify actual HEAD before and after local fake execution without hundreds of
 # redundant subprocesses. Per-batch guard retains the attested head in this fake run.
 if current_head()!=m['exact_head']:raise ValueError('HEAD_CHANGED')
 c=sqlite3.connect(a.output/'fake-batches.sqlite')
 for b in batches:
  result=run_batch(c,b,fake_dispatch=fake,current_head=lambda:m['exact_head'])
  if result['status']!='succeeded':raise ValueError('FAKE_BATCH_INCOMPLETE')
 result=aggregate_candidate(c,batches,profile['content'],current_head=current_head)
 tokens=[len(_bytes(b)) for b in batches]
 summary={k:m[k] for k in ['exact_head','tracked_files','safe_text_bytes','coverage','complete_inventory','complete_safe_analysis']}
 summary.update(plan_profile_id=profile['id'],planned_modules=len(profile['content']['planned_modules']),model_from_previous_receipt=model[0] if model else None,
    capability_source='committed Product declaration; no live provider capability lookup',budget=budget,
    batch_count=len(batches),batch_input_token_upper_bounds=tokens,total_input_token_upper_bound=sum(tokens),exact_input_tokens=None,
    fake_candidate_modules=len(result['content']['implementation_mappings']),fake_candidate_status=result['status'],
    provider_calls=0,product_db_writes=0,unplanned_code_feature_discovery='not assessed by this module reconciliation preflight')
 (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(summary,ensure_ascii=True))
if __name__=='__main__':main()
