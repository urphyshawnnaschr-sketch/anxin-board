"""End-to-end generic fixtures: no source execution, network or provider calls."""
import hashlib,sqlite3
import pytest
from test_profile_repository_map import fake_git,HEAD
from app.profile_repository_map import build_repository_map
from app.repository_index import index_repository_map
from app.repository_evidence import ParseResult
from app.repository_parsers import PythonHeuristics,WebHeuristics
from app.repository_stack_detection import detect_stack
from app.profile_reconciliation_batches import plan_reconciliation_batches,run_batch,aggregate_batches

BUDGET=dict(context_window_tokens=18000,max_output_tokens=2000,reserved_output_tokens=2000,safety_margin_tokens=1000,counting_policy_version='utf8_byte_upper_bound_v1')

@pytest.mark.parametrize('path,source,adapter,kind,value',[
 ('service.py',"@router.get('/orders')\ndef orders(): pass\nCREATE TABLE orders(id INT);\ndef test_orders(): pass\n",'python-heuristics/1','route','/orders'),
 ('service.ts',"function orders() {}\nrouter.get('/orders',handler);\nfetch('/api/orders')\n",'js-ts-web-heuristics/2','api','/api/orders'),
 ('Panel.vue',"<script>\nfunction orders() {}\nfetch('/api/orders')\n</script>",'js-ts-web-heuristics/2','symbol','orders'),
 ('service.zig','const orders = inventory.orders;\npub fn calculate() void {}\n','generic-text/1',None,None),
])
@pytest.mark.parametrize('module_count',[12,25,70])
def test_inventory_adapter_or_fallback_to_dynamic_planner(fake_git,monkeypatch,path,source,adapter,kind,value,module_count):
 import socket
 from app import project_profile_generation as provider_boundary
 calls={'network':0,'provider':0}
 def forbidden(*a,**k):calls['network']+=1;raise AssertionError('network forbidden')
 monkeypatch.setattr(socket.socket,'connect',forbidden)
 def provider_forbidden(*a,**k):calls['provider']+=1;raise AssertionError('provider forbidden')
 monkeypatch.setattr(provider_boundary,'_provider_adapter_resolver',provider_forbidden)
 monkeypatch.setattr(provider_boundary,'_read_provider_credential',provider_forbidden)
 fake_git.add(path,source.encode());fake_git.add('build.zig',b'orders package inventory')
 raw=build_repository_map(2,{'git_url':'https://github.com/example/project.git'},{'remote_head':HEAD})
 assert raw['tracked_files']==2 and len(fake_git.opened)==2
 assert all('structured_metadata' not in e and 'language_hint' not in e for e in raw['evidence'])
 indexed=index_repository_map(raw)
 item=next(e for e in indexed['evidence'] if e['path']==path)
 assert item['adapter_id']==adapter
 assert item['object_sha'] and item['exact_head']==HEAD
 assert source[item['char_start']:item['char_end']]==item['content']
 if kind:assert value in item['structured_metadata'][kind]
 else:assert item['parser_status']=='fallback' and item['structured_metadata']=={} and 'orders' in item['terms']
 plan={'schema_version':'project_profile_v2','planned_modules':[{'client_id':f'm{i}','name':'orders','requirements':['inventory']} for i in range(module_count)]}
 batches=plan_reconciliation_batches(plan,indexed['evidence'],exact_head=HEAD,plan_profile_id=9,budget_record=BUDGET)
 assert {m['client_id'] for b in batches for m in b['planned_modules']}=={f'm{i}' for i in range(module_count)}
 assert any(b['repo_evidence'] for b in batches)
 c=sqlite3.connect(':memory:')
 for b in batches:
  run_batch(c,b,current_head=lambda:HEAD,fake_dispatch=lambda b:[{'planned_module_id':m['client_id'],'status':'unknown','evidence_ids':[],'rationale':'Synthetic evidence does not establish the requirement.'} for m in b['planned_modules']])
 assert all(r['status']=='unknown' for r in aggregate_batches(c,batches,current_head=lambda:HEAD))
 assert calls=={'network':0,'provider':0}
 assert indexed['provider_calls']==indexed['network_model_calls']==0


def test_detector_hints_do_not_install_deep_parsers():
 d=detect_stack(['pom.xml','Foo.java','project.csproj','go.mod','Cargo.toml','build.gradle','package.json','pyproject.toml'])
 assert {'java','csharp','go','rust','jvm','javascript','python'}<=set(d['languages'])
 assert d['status']=='local_filename_hints_only'


def test_adapter_failure_falls_back_without_losing_evidence(fake_git):
 class Broken:
  adapter_id='broken/1';language='exotic';extensions=frozenset({'.exotic'});manifests=frozenset()
  def parse(self,path,safe_text):raise ValueError('do not leak raw parser exception')
 fake_git.add('orders.exotic',b'orders inventory')
 raw=build_repository_map(2,{'git_url':'https://github.com/example/project.git'},{'remote_head':HEAD})
 indexed=index_repository_map(raw,adapters=(Broken(),))
 e=indexed['evidence'][0]
 assert e['parser_status']=='parser_failed_fallback'
 assert e['content']==raw['evidence'][0]['content']
 assert 'exception' not in str(e)


def test_registering_new_adapter_changes_no_core_or_planner(fake_git):
 class Extension:
  adapter_id='fixture-only/1';language='fixture';extensions=frozenset({'.custom'});manifests=frozenset()
  def parse(self,path,safe_text):return ParseResult(self.adapter_id,'heuristic',{'symbol':['orders']})
 fake_git.add('orders.custom',b'orders inventory')
 raw=build_repository_map(2,{'git_url':'https://github.com/example/project.git'},{'remote_head':HEAD})
 fallback=index_repository_map(raw);specific=index_repository_map(raw,adapters=(Extension(),))
 assert specific['evidence'][0]['adapter_id']=='fixture-only/1'
 plan={'schema_version':'project_profile_v2','planned_modules':[{'client_id':'orders','name':'orders'}]}
 def batch(m):return plan_reconciliation_batches(plan,m['evidence'],exact_head=HEAD,plan_profile_id=1,budget_record=BUDGET)[0]
 assert batch(fallback)['input_hash']!=batch(specific)['input_hash']
 assert raw['evidence'][0]['content']==specific['evidence'][0]['content']


def test_generic_core_has_no_parser_stack_dependency():
 import ast
 from pathlib import Path
 from app import profile_repository_map as core
 tree=ast.parse(Path(core.__file__).read_text(encoding='utf-8'))
 imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
 assert not any('parser' in name or 'stack_detection' in name for name in imports)
 assert not hasattr(core,'map_text')


def test_replanned_fragments_do_not_inherit_outside_metadata(fake_git):
 from app.profile_repository_map import chunk_text
 from app.repository_index import index_repository_map
 source='def orders(): pass\n'+'# orders inventory data\n'*400+'def tail(): pass\n'
 raw={'exact_head':HEAD,'files':[{'path':'orders.py','coverage_state':'safe_text','object_sha':'b'*40,'safe_hash':hashlib.sha256(source.encode()).hexdigest(),'safe_bytes':len(source.encode())}], 'evidence':chunk_text(source,head=HEAD,path='orders.py',blob_hash='b'*40,max_bytes=999999)}
 indexed=index_repository_map(raw)
 plan={'schema_version':'project_profile_v2','planned_modules':[{'client_id':'orders','name':'orders'}]}
 batches=plan_reconciliation_batches(plan,indexed['evidence'],exact_head=HEAD,plan_profile_id=1,budget_record={**BUDGET,'context_window_tokens':7000})
 assert len(batches)>1
 for b in batches:
  for e in b['repo_evidence']:
   assert source[e['char_start']:e['char_end']]==e['content']
   assert e['line_start']==1+source[:e['char_start']].count('\n')
   assert e['line_end']==1+source[:e['char_end']].count('\n')-int(e['content'].endswith('\n'))
   assert all(value in e['content'] for values in e['structured_metadata'].values() for value in values)


def test_adapter_manifest_registration_is_used_without_core_change(fake_git):
 class ManifestAdapter:
  adapter_id='manifest-fixture/1';language='fixture';extensions=frozenset();manifests=frozenset({'fixture.build'})
  def parse(self,path,safe_text):return ParseResult(self.adapter_id,'heuristic',{'dependency_clue':['orders']})
 fake_git.add('fixture.build',b'orders dependency')
 raw=build_repository_map(2,{'git_url':'https://github.com/example/project.git'},{'remote_head':HEAD})
 indexed=index_repository_map(raw,adapters=(ManifestAdapter(),))
 assert indexed['evidence'][0]['adapter_id']=='manifest-fixture/1'
 assert indexed['stack_detection']['manifest_clues'][0]['language_hint']=='fixture'
