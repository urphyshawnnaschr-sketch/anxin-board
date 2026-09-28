import assert from 'node:assert/strict'
import test from 'node:test'
import { summarizeRequirementRows, summarizeAtlasRequirements, atlasTaskPresentation } from '../projectProfilePresentation.js'
import * as presentation from '../projectProfilePresentation.js'
const hash='a'.repeat(64)
const profile={id:8,content_hash:hash,content:{planned_modules:[{client_id:'a',requirements:['one','two']},{client_id:'b',requirements:['three']}]}}
const task={status:'succeeded',profile_id:8,generated_content_hash:hash,module_requirements:{a:[{requirement_index:1,status:'partial'},{requirement_index:0,status:'implemented'}],b:[{requirement_index:0,status:'unknown'}]}}
test('summary uses actual requirement rows and never counts modules as requirements',()=>{
 assert.deepEqual(summarizeAtlasRequirements(task,profile),{total:3,implemented:1,partial:1,unknown:1})
})
test('summary cannot pair a previous result with edited or different candidate text',()=>{
 for(const changed of [{...profile,id:9},{...profile,content_hash:'b'.repeat(64)}]) assert.equal(summarizeAtlasRequirements(task,changed),null)
 assert.equal(summarizeAtlasRequirements(task,profile,true),null)
 assert.equal(summarizeAtlasRequirements({...task,status:'running'},profile),null)
})
test('incomplete duplicate or unrecognized rows cannot fabricate unknown totals',()=>{
 for(const rows of [[],[{requirement_index:0,status:'unknown'}],[{requirement_index:0,status:'unknown'},{requirement_index:0,status:'partial'}],[{requirement_index:0,status:'unknown'},{requirement_index:1,status:'not_started'}]]) assert.equal(summarizeRequirementRows(rows,2),null)
 assert.equal(summarizeAtlasRequirements({...task,module_requirements:{a:task.module_requirements.a}},profile),null)
})
test('module count summary is dynamic and raw rows are unchanged',()=>{
 const rows=[{requirement_index:0,status:'partial',rationale:'Original English conclusion.'}]
 assert.deepEqual(summarizeRequirementRows(rows,1),{total:1,implemented:0,partial:1,unknown:0})
 assert.equal(rows[0].rationale,'Original English conclusion.')
})
test('task states have distinct understandable labels and no raw machine state',()=>{
 const states=['queued','running','succeeded','unknown','failed_pre_send','failed_after_send']
 const labels=states.map(state=>atlasTaskPresentation(state).label)
 assert.ok(labels.every(label=>/[\u4e00-\u9fff]/.test(label)))
 assert.ok(new Set(labels).size>=5)
 assert.equal(atlasTaskPresentation('unknown').tone,'warning')
 assert.equal(atlasTaskPresentation('failed_after_send').tone,'error')
})

function moduleContent(statuses) {
 return {schema_version:'project_profile_v2',planned_modules:statuses.map((_,i)=>({client_id:`module-${i}`})),implementation_mappings:statuses.map((status,i)=>({planned_module_id:`module-${i}`,status}))}
}

test('saved module mappings count 11, 16 and 70 modules without inferring requirement progress',()=>{
 for(const total of [11,16,70]) {
  const content=moduleContent(Array(total).fill('unknown'))
  const original=JSON.stringify(content)
  assert.deepEqual(presentation.summarizeModuleMappings(content),{total,implemented:0,partial:0,unknown:total,not_started:0})
  assert.equal(JSON.stringify(content),original)
 }
})

test('saved module mappings preserve all four states and match IDs rather than array positions',()=>{
 const content=moduleContent(['implemented','partial','unknown','not_started','implemented'])
 content.implementation_mappings.reverse()
 assert.deepEqual(presentation.summarizeModuleMappings(content),{total:5,implemented:2,partial:1,unknown:1,not_started:1})
})

test('module summary rejects unsupported, empty or malformed content',()=>{
 for(const content of [null,{},moduleContent([]),{...moduleContent(['unknown']),schema_version:'1.0'},{...moduleContent(['unknown']),planned_modules:null},{...moduleContent(['unknown']),implementation_mappings:null}]) {
  assert.equal(presentation.summarizeModuleMappings(content),null)
 }
})

test('module summary requires unique schema-valid client IDs',()=>{
 for(const id of ['', ' ', 'has space', '中文', 'a'.repeat(65), null, 4]) {
  const content=moduleContent(['unknown']);content.planned_modules[0].client_id=id;content.implementation_mappings[0].planned_module_id=id
  assert.equal(presentation.summarizeModuleMappings(content),null)
 }
 const duplicate=moduleContent(['unknown','partial']);duplicate.planned_modules[1].client_id='module-0'
 assert.equal(presentation.summarizeModuleMappings(duplicate),null)
 const valid=moduleContent(['implemented']);valid.planned_modules[0].client_id='A_'.repeat(32);valid.implementation_mappings[0].planned_module_id='A_'.repeat(32)
 assert.equal(presentation.summarizeModuleMappings(valid).implemented,1)
})

test('module summary rejects missing, duplicate, foreign and invalid mappings without inventing unknowns',()=>{
 const missing=moduleContent(['unknown','partial']);missing.implementation_mappings.pop()
 const duplicate=moduleContent(['unknown','partial']);duplicate.implementation_mappings[1].planned_module_id='module-0'
 const foreign=moduleContent(['unknown']);foreign.implementation_mappings[0].planned_module_id='foreign'
 const extra=moduleContent(['unknown']);extra.implementation_mappings.push({planned_module_id:'foreign',status:'implemented'})
 for(const content of [missing,duplicate,foreign,extra]) assert.equal(presentation.summarizeModuleMappings(content),null)
 for(const status of ['missing','complete','toString','__proto__',null,0]) {
  const content=moduleContent([status]);assert.equal(presentation.summarizeModuleMappings(content),null)
 }
 for(const field of ['planned_modules','implementation_mappings']) {
  const content=moduleContent(['unknown']);content[field][0]=null;assert.equal(presentation.summarizeModuleMappings(content),null)
 }
})

test('ready local preflight marks only existing terminal records as previous, preserving status tones',()=>{
 for(const status of ['succeeded','unknown','failed_pre_send','failed_after_send']) {
  const original=atlasTaskPresentation(status)
  const ready=atlasTaskPresentation(status,true)
  assert.equal(ready.label,'上次'+original.label)
  assert.equal(ready.tone,original.tone)
  assert.equal(ready.previousRecord,true)
 }
 for(const status of ['queued','running','unrecognized']) assert.deepEqual(atlasTaskPresentation(status,true),atlasTaskPresentation(status))
 assert.equal(atlasTaskPresentation('failed_after_send').label,'分析已停止')
})


test('only previous success and known failure records may collapse, never unknown',()=>{
 for(const status of ['succeeded','failed_pre_send','failed_after_send']) assert.equal(atlasTaskPresentation(status,true).foldPreviousRecord,true)
 for(const status of ['unknown','queued','running','unrecognized']) assert.notEqual(atlasTaskPresentation(status,true).foldPreviousRecord,true)
 for(const status of ['succeeded','failed_pre_send','failed_after_send','unknown']) assert.notEqual(atlasTaskPresentation(status).foldPreviousRecord,true)
})
