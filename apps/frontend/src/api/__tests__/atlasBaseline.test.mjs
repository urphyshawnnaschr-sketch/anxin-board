import assert from 'node:assert/strict'
import test from 'node:test'
const session = new Map(), local = new Map()
const store = map => ({ getItem: k => map.get(k) ?? null, setItem: (k,v) => map.set(k,String(v)), removeItem: k => map.delete(k) })
globalThis.sessionStorage = store(session)
globalThis.localStorage = store(local)
globalThis.window = { location: { href: 'http://127.0.0.1:8000/#/projects/9/profile' } }
globalThis.history = { state:null, replaceState(){} }
const api = await import('../projectProfiles.js')
const hash = 'a'.repeat(64)
const task = { task_id:'atlas-1', status:'queued', completed_stages:0, stage_count:0, max_calls:8 }
const response = (body,status=200) => ({ok:status<400,status,json:async()=>body})
function reset() { session.clear(); local.clear(); session.set('anxinboard:local-browser-session:v1','session'); }
test('Atlas preflight is session protected and creates no task authorization',async()=>{
 reset(); const calls=[]
 globalThis.fetch=async(url,options)=>{calls.push([url,options]);return response({status:'ready',preflight:{provider_calls:0,credential_read:false}})}
 const result=await api.preflightAtlasBaseline(9,42)
 assert.equal(result.ok,true);assert.equal(calls[0][0],'/api/projects/9/project-state-baseline/atlas/preflight')
 assert.deepEqual(JSON.parse(calls[0][1].body),{plan_profile_id:42});assert.equal(local.size,0)
})
test('Atlas nonce persists across sessions and is scoped to profile and preflight',async()=>{
 reset();const payloads=[]
 globalThis.fetch=async(url,options)=>{payloads.push(JSON.parse(options.body));return response({task},202)}
 await api.createAtlasTask(9,42,hash)
 session.set('anxinboard:local-browser-session:v1','new-session')
 await api.createAtlasTask(9,42,hash)
 await api.createAtlasTask(9,43,hash)
 assert.equal(payloads[0].authorization_nonce,payloads[1].authorization_nonce)
 assert.notEqual(payloads[1].authorization_nonce,payloads[2].authorization_nonce)
})
test('lost task create response performs only a read recovery, never duplicate POST',async()=>{
 reset();const calls=[]
 globalThis.fetch=async(url,options)=>{calls.push([url,options?.method||'GET']);if(options)throw Error('lost');return response({task})}
 await api.createAtlasTask(9,42,hash)
 assert.deepEqual(calls.map(c=>c[1]),['POST','GET'])
 assert.match(calls[1][0],/tasks\/latest$/)
 assert.equal(local.size,1)
})
test('unknown task observation cannot invoke resume or create',async()=>{
 reset();const calls=[]
 globalThis.fetch=async(url,options)=>{calls.push(options);return response({task:{...task,status:'unknown'}})}
 const latest=await api.getLatestAtlasTask(9);const observed=await api.getAtlasTask(9,'atlas-1')
 assert.equal(latest.body.task.status,'unknown');assert.equal(observed.body.task.status,'unknown')
 assert.deepEqual(calls,[undefined,undefined])
})
test('storage failure prevents a task POST',async()=>{
 reset();let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected')}
 const saved=globalThis.localStorage;globalThis.localStorage={getItem(){throw Error('denied')}}
 try { await assert.rejects(api.createAtlasTask(9,42,hash));assert.equal(calls,0) } finally {globalThis.localStorage=saved}
})
test('explicit resume uses original task and session protected POST',async()=>{
 reset();let seen;globalThis.fetch=async(url,options)=>{seen=[url,options];return response({task})}
 await api.resumeAtlasTask(9,'atlas-1');assert.match(seen[0],/atlas-1\/resume$/)
 assert.equal(seen[1].method,'POST');assert.ok(seen[1].headers['Local-Idempotency-Key'])
})

test('timeout recovery does not mistake an older succeeded task for current admission',async()=>{
 reset();globalThis.fetch=async(url,options)=>{if(options)throw Error('lost');return response({task:{...task,status:'succeeded',profile_id:3}})}
 const result=await api.createAtlasTask(9,42,hash)
 assert.equal(result.ok,false);assert.equal(result.status,0)
})
test('timeout recovery accepts only the exact persisted nonce and preflight',async()=>{
 reset();let payload
 globalThis.fetch=async(url,options)=>{if(options){payload=JSON.parse(options.body);throw Error('lost')}return response({task:{...task,authorization_nonce:payload.authorization_nonce,preflight_identity_hash:hash}})}
 const result=await api.createAtlasTask(9,42,hash)
 assert.equal(result.ok,true);assert.equal(result.body.task.task_id,'atlas-1')
})

async function panelHarness(overrides = {}) {
 const presentation = await import('../../features/project-profile/projectProfilePresentation.js')
 const { readFile } = await import('node:fs/promises')
 const { runInNewContext } = await import('node:vm')
 const source = await readFile(new URL('../../views/ProjectProfilePanel.vue',import.meta.url),'utf8')
 const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import[\s\S]*?from ['"][^'"]+['"]\r?\n/gm,'')
 const timers = new Map(), calls = [], lifecycle = {}, watchers = []
 const props = {projectId:9}
 const ref = value => ({value})
 const context = {
  ...presentation,
  ref, computed: fn => ({get value(){return fn()}}), defineProps:()=>props,
  onMounted:fn=>{lifecycle.mount=fn}, onUnmounted:fn=>{lifecycle.unmount=fn}, watch:(getter,callback)=>{watchers.push({getter,callback})},
  setTimeout:fn=>{const id=Symbol();timers.set(id,fn);return id}, clearTimeout:id=>timers.delete(id),
  isAtlasTask:api.isAtlasTask, formatProjectStateBaselineError:api.formatProjectStateBaselineError,
  useProjectProfilePanel:()=>({serverProfile:ref(null),historyViewing:ref(null),viewMode:ref('view'),
   activeConfirmed:ref(null),form:ref({modules:[]}),collapsedModules:ref([]),hasUnsaved:ref(false),
   saving:ref(false),confirming:ref(false),creating:ref(false),loadPanel:async()=>{calls.push('loadPanel')}}),
  getAtlasProfileResult:async()=>({ok:true,body:{task:context.harness.atlasTask.value}}),
  atlasReportErrorMessage:api.atlasReportErrorMessage,
  getProjectStateBaselineStatus:async()=>({ok:true,body:{status:'missing'}}),
  getLatestAtlasTask:async()=>({ok:true,body:{task:null}}),
  getAtlasTask:async()=>({ok:true,body:{task}}),resumeAtlasTask:async()=>{calls.push('resume');return{ok:true,body:{task}}},
  createProfileCandidate(){},getProjectProfile(){},listProjectProfiles(){},updateProfileCandidate(){},confirmProfileCandidate(){},
  ...overrides
 }
 runInNewContext(script+'\nglobalThis.harness={loadProfileResult,acceptAtlasTask,recoverAtlasTask,pollAtlasTask,continueAtlasTask,prepareStateBaseline,executeStateBaseline,loadBaselineStatus,atlasTask,baselineUnknown,baselineExecuting,atlasStalled,baselinePreflight,baselineLedgerStatus,baselinePreflighting,reconciliationAuthorized,serverProfile,historyViewing,hasUnsaved,baselineMessage,atlasRequirementRows:typeof atlasRequirementRows === "function" ? atlasRequirementRows : null,atlasCoverageSummary:typeof atlasCoverageSummary !== "undefined" ? atlasCoverageSummary : null}',context)
 return {...context.harness,timers,calls,lifecycle,props,watchers}
}
test('panel observes server task on new session and polling never sends provider work',async()=>{
 let reads=0
 const h=await panelHarness({getLatestAtlasTask:async()=>{reads++;return{ok:true,body:{task}}}})
 await h.recoverAtlasTask();assert.equal(reads,1);assert.equal(h.atlasTask.value.task_id,'atlas-1')
 assert.equal(h.timers.size,1);assert.deepEqual(h.calls,[])
 h.lifecycle.unmount();assert.equal(h.timers.size,0)
})
test('panel UNKNOWN stops polling and refuses manual resume',async()=>{
 const h=await panelHarness()
 await h.acceptAtlasTask(task)
 await h.acceptAtlasTask({...task,status:'unknown',resume_available:true})
 await h.continueAtlasTask()
 assert.equal(h.baselineUnknown.value,true);assert.equal(h.timers.size,0);assert.deepEqual(h.calls,[])
})
test('panel successful task refreshes candidate once without confirmation',async()=>{
 const h=await panelHarness()
 await h.acceptAtlasTask({...task,status:'succeeded',profile_id:12})
 await h.acceptAtlasTask({...task,status:'succeeded',profile_id:12})
 assert.deepEqual(h.calls,['loadPanel']);assert.equal(h.timers.size,0)
})
test('panel requires server resume_available and ignores responses after unmount',async()=>{
 let resolve
 const h=await panelHarness({getLatestAtlasTask:()=>new Promise(r=>{resolve=r})})
 await h.acceptAtlasTask({...task,resume_available:false});await h.continueAtlasTask()
 assert.deepEqual(h.calls,[])
 const pending=h.recoverAtlasTask();h.lifecycle.unmount();resolve({ok:true,body:{task:{...task,status:'succeeded'}}});await pending
 assert.equal(h.atlasTask.value.status,'queued');assert.equal(h.timers.size,0)
})

test('missing session preflight is zero-call failure, not provider UNKNOWN',async()=>{
 reset();session.clear();let calls=0
 globalThis.fetch=async()=>{calls++;throw Error('unexpected')}
 const result=await api.preflightAtlasBaseline(9,42)
 assert.equal(result.body.detail.code,'PROJECT_STATE_BASELINE_LOCAL_SESSION_UNAVAILABLE');assert.equal(calls,0)
})

test('explicit known failure retry rotates once while uncertain tasks retain nonce',async()=>{
 reset();const payloads=[]
 globalThis.fetch=async(url,options)=>{payloads.push(JSON.parse(options.body));return response({task},202)}
 await api.createAtlasTask(9,42,hash)
 const failed={...task,status:'failed_after_send',authorization_nonce:payloads[0].authorization_nonce,plan_profile_id:42,preflight_identity_hash:hash}
 await api.createAtlasTask(9,42,hash,{retryTask:failed})
 await api.createAtlasTask(9,42,hash,{retryTask:failed})
 assert.notEqual(payloads[0].authorization_nonce,payloads[1].authorization_nonce)
 assert.equal(payloads[1].authorization_nonce,payloads[2].authorization_nonce)
 await api.createAtlasTask(9,42,hash,{retryTask:{...failed,status:'unknown',authorization_nonce:payloads[2].authorization_nonce}})
 assert.equal(payloads[2].authorization_nonce,payloads[3].authorization_nonce)
})

test('old terminal tasks do not block a proven different scope but active work stays visible',async()=>{
 for(const prior of [
  {...task,status:'unknown',plan_profile_id:41,stale_scope:null},
  {...task,status:'unknown',plan_profile_id:42,stale_scope:true},
 ]){
  const h=await panelHarness({getLatestAtlasTask:async()=>({ok:true,body:{task:prior}})})
  h.serverProfile.value={id:42,status:'confirmed',content:{schema_version:'project_profile_v2'}}
  await h.recoverAtlasTask();assert.equal(h.baselineUnknown.value,false);assert.equal(h.atlasTask.value,null)
 }
 const h=await panelHarness({getLatestAtlasTask:async()=>({ok:true,body:{task:{...task,status:'running',plan_profile_id:41,stale_scope:true}}})})
 h.serverProfile.value={id:42,status:'confirmed',content:{schema_version:'project_profile_v2'}}
 await h.recoverAtlasTask();assert.equal(h.baselineExecuting.value,true)
})

test('unavailable stale-scope decision retains current unknown task protection',async()=>{
 const h=await panelHarness({getLatestAtlasTask:async()=>({ok:true,body:{task:{...task,status:'unknown',plan_profile_id:42,stale_scope:null}}})})
 h.serverProfile.value={id:42,status:'confirmed',content:{schema_version:'project_profile_v2'}}
 await h.recoverAtlasTask();assert.equal(h.baselineUnknown.value,true)
})

test('out of order running response cannot replace terminal or newer task',async()=>{
 let resolve
 const h=await panelHarness({getAtlasTask:()=>new Promise(r=>{resolve=r})})
 await h.acceptAtlasTask(task)
 const pending=h.pollAtlasTask()
 await h.acceptAtlasTask({...task,status:'succeeded'})
 resolve({ok:true,body:{task:{...task,status:'running'}}});await pending
 assert.equal(h.atlasTask.value.status,'succeeded');assert.equal(h.timers.size,0)
 await h.acceptAtlasTask({...task,task_id:'new-task'})
 await h.acceptAtlasTask({...task,task_id:'new-task',status:'unknown'})
 await h.acceptAtlasTask({...task,task_id:'new-task',status:'running'})
 assert.equal(h.atlasTask.value.status,'unknown')
})

test('actual project watcher invalidates old preflight and status responses',async()=>{
 let resolveScan,resolveStatus
 const h=await panelHarness({preflightAtlasBaseline:()=>new Promise(r=>{resolveScan=r}),getProjectStateBaselineStatus:()=>new Promise(r=>{resolveStatus=r})})
 h.serverProfile.value={id:42,status:'confirmed',content:{schema_version:'project_profile_v2'}}
 const scan=h.prepareStateBaseline(), status=h.loadBaselineStatus()
 h.props.projectId=10
 await h.watchers[0].callback()
 resolveScan({ok:true,status:200,body:{status:'ready',preflight:{preflight_identity_hash:hash}}})
 resolveStatus({ok:true,body:{status:'established'}})
 await Promise.all([scan,status])
 assert.equal(h.baselinePreflight.value,null);assert.equal(h.baselineLedgerStatus.value,null)
 assert.equal(h.baselinePreflighting.value,false)
})

test('queries cannot replace a task admission still in flight and retry requires fresh consent',async()=>{
 let resolveCreate,reads=0,option
 const failed={...task,status:'failed_after_send',plan_profile_id:42,preflight_identity_hash:hash,authorization_nonce:'old'}
 const h=await panelHarness({createAtlasTask:(_project,_profile,_hash,options)=>{option=options;return new Promise(r=>{resolveCreate=r})},getLatestAtlasTask:async()=>{reads++;return{ok:true,body:{task:failed}}}})
 h.serverProfile.value={id:42,status:'confirmed',content:{schema_version:'project_profile_v2'}}
 h.baselinePreflight.value={preflight_identity_hash:hash};h.reconciliationAuthorized.value=true
 await h.acceptAtlasTask(failed)
 assert.equal(h.reconciliationAuthorized.value,false)
 h.reconciliationAuthorized.value=true
 const pending=h.executeStateBaseline()
 await h.recoverAtlasTask();assert.equal(reads,0)
 resolveCreate({ok:true,status:202,body:{task:{...task,task_id:'new'}}});await pending
 assert.equal(option.retryTask.task_id,failed.task_id)
 assert.equal(h.atlasTask.value.task_id,'new')
})


test('requirement detail rows bind only to the displayed saved profile and module',async()=>{
 const h=await panelHarness()
 const module={client_id:'m1',requirements:['First requirement','Second requirement']}
 h.serverProfile.value={id:12,status:'candidate',content_hash:hash,content:{schema_version:'project_profile_v2'}}
 await h.acceptAtlasTask({...task,status:'succeeded',profile_id:12,generated_content_hash:hash,module_requirements:{m1:[{requirement_index:1,status:'partial',rationale:'Bound result',evidence_ids:['ev1']}]}})
 await h.loadProfileResult()
 const rows=h.atlasRequirementRows(module)
 assert.equal(rows[0].text,'Second requirement');assert.equal(rows[0].status,'partial')
 assert.equal(rows[0].rationale,'Bound result');assert.equal(rows[0].evidence_ids[0],'ev1')
 assert.equal(h.atlasRequirementRows({client_id:'m2',requirements:['Other']}).length,0)
 h.historyViewing.value={id:13,content:{schema_version:'project_profile_v2'}}
 assert.equal(h.atlasRequirementRows(module).length,0)
 h.historyViewing.value=null;h.hasUnsaved.value=true
 assert.equal(h.atlasRequirementRows(module).length,0)
 h.hasUnsaved.value=false;h.serverProfile.value={id:14,status:'candidate'}
 assert.equal(h.atlasRequirementRows(module).length,0)
})

test('successful result remains visible after human confirmation changes source scope',async()=>{
 const h=await panelHarness()
 h.serverProfile.value={id:12,status:'confirmed',content_hash:hash,content:{schema_version:'project_profile_v2'}}
 await h.acceptAtlasTask({...task,status:'succeeded',profile_id:12,generated_content_hash:hash,plan_profile_id:11,stale_scope:true,module_requirements:{m1:[{requirement_index:0,status:'implemented',rationale:'Bound result',evidence_ids:['ev1']}]}})
 await h.loadProfileResult()
 assert.equal(h.atlasTask.value?.status,'succeeded')
 assert.equal(h.atlasRequirementRows({client_id:'m1',requirements:['Requirement']}).length,1)
})

test('coverage states bounded counts and gap check without claiming exhaustive proof',async()=>{
 const h=await panelHarness()
 await h.acceptAtlasTask({...task,status:'succeeded',coverage:{inspected_paths:93,unexplained_safe_paths:1628,gap_check_completed:true}})
 assert.match(h.atlasCoverageSummary.value,/93/);assert.match(h.atlasCoverageSummary.value,/1628/)
 assert.match(h.atlasCoverageSummary.value,/遗漏检查已完成/)
 assert.match(h.atlasCoverageSummary.value,/不代表整仓语义已证明/)
 h.atlasTask.value.coverage={inspected_paths:-1,unexplained_safe_paths:'secret',gap_check_completed:'yes'}
 assert.doesNotMatch(h.atlasCoverageSummary.value,/-1|secret|遗漏检查已完成/)
})

test('preflight error shows safe actionable diagnostic code',async()=>{
 const h=await panelHarness({preflightAtlasBaseline:async()=>({ok:false,status:409,body:{detail:{message:'Scope changed',code:'ATLAS_SCOPE_CHANGED',provider_body:'secret'}}})})
 h.serverProfile.value={id:42,status:'confirmed',content:{schema_version:'project_profile_v2'}}
 await h.prepareStateBaseline()
 assert.match(h.baselineMessage.value,/ATLAS_SCOPE_CHANGED/);assert.doesNotMatch(h.baselineMessage.value,/secret/)
})


test('module evidence template renders bound requirement text status rationale and references',async()=>{
 const {readFile}=await import('node:fs/promises')
 const source=await readFile(new URL('../../views/ProjectProfilePanel.vue',import.meta.url),'utf8')
 const detail=source.split('<details v-if="baselineMapping(module)"')[1].split('</article>')[0]
 assert.match(detail,/v-for="row in atlasRequirementRows\(module\)"/)
 for(const binding of ['row.text','implementationLabel(row.status)','row.rationale','row.evidence_ids']) assert.ok(detail.includes(binding))
 assert.match(source,/<p v-if="atlasCoverageSummary">{{ atlasCoverageSummary }}<\/p>/)
})


test('saved same-id requirement edits cannot reuse generated evidence indexes',async()=>{
 const h=await panelHarness()
 const module={client_id:'m1',requirements:['Original requirement']}
 h.serverProfile.value={id:12,status:'candidate',content_hash:hash}
 await h.acceptAtlasTask({...task,status:'succeeded',profile_id:12,generated_content_hash:hash,module_requirements:{m1:[{requirement_index:0,status:'implemented',rationale:'Old evidence',evidence_ids:['ev1']}]}})
 await h.loadProfileResult()
 assert.equal(h.atlasRequirementRows(module).length,1)
 h.serverProfile.value={id:12,status:'candidate',content_hash:'b'.repeat(64)}
 assert.equal(h.hasUnsaved.value,false)
 assert.equal(h.atlasRequirementRows({...module,requirements:['Edited and saved requirement']}).length,0)
 h.serverProfile.value.content_hash=hash
 delete h.atlasTask.value.generated_content_hash
 assert.equal(h.atlasRequirementRows(module).length,0)
 h.atlasTask.value.generated_content_hash=hash;delete h.serverProfile.value.content_hash
 assert.equal(h.atlasRequirementRows(module).length,0)
})
