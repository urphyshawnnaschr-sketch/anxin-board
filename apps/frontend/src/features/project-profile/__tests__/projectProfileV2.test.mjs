import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { profileContentToForm, profileFormToContent, plannedModules, implementationLabel } from '../projectProfileForm.js'

test('V2 preserves plans, implementation status and unplanned code through edits', () => {
  const raw = { schema_version:'project_profile_v2', project_summary:'plan', planned_modules:[{client_id:'m',name:'Feature',description:'',prd_refs:['prd-1'],requirements:['Need'],exclusions:[]}], implementation_mappings:[{planned_module_id:'m',status:'partial',exact_head:'a'.repeat(40),evidence_ids:['repo-code-1'],paths:[{type:'backend',pattern:'a.py',required:true,note:''}],rationale:'Part covered'}], unplanned_code_features:[{client_id:'x',name:'extra',description:'',exact_head:'a'.repeat(40),evidence_ids:['repo-code-2'],paths:[{type:'backend',pattern:'x.py',required:true,note:''}]}], domain_glossary:[],exclude_patterns:[],notes:'' }
  assert.deepEqual(profileFormToContent(profileContentToForm(raw)), raw)
  assert.deepEqual(plannedModules(raw).map(m=>m.client_id), ['m'])
  const form = profileContentToForm(raw)
  form.modules[0].name='Updated plan'
  assert.equal(profileFormToContent(form).planned_modules[0].name, 'Updated plan')
})
test('missing mapping is unknown, never not_started', () => {
  assert.equal(implementationLabel(undefined),'暂时无法确认')
  assert.equal(implementationLabel('not_started'),'尚未开始')
})
test('Product UNKNOWN has no reset-and-generate affordance', () => {
  const source=readFileSync(new URL('../../../views/ProjectProfilePanel.vue',import.meta.url),'utf8')
  assert.ok(!source.includes('startFreshGenerationAttempt'))
  assert.ok(!source.includes('discardProfileGenerationAttempt'))
  assert.ok(source.includes('generating.value || generationUnknown.value || !props.projectId'))
})

test("plan semantic edit drops obsolete mappings", () => {
 const raw={schema_version:"project_profile_v2",planned_modules:[{client_id:"m",name:"Old",description:"",prd_refs:["prd-1"],requirements:["old"],exclusions:[]}],implementation_mappings:[{planned_module_id:"m",status:"implemented"}],unplanned_code_features:[]};
 const form=profileContentToForm(raw);form.modules[0].requirements_text="new scope";
 assert.deepEqual(profileFormToContent(form).implementation_mappings,[]);
})
