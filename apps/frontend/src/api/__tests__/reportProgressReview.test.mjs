import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
const source = fs.readFileSync(new URL('../../views/ReportReviewView.vue', import.meta.url), 'utf8')
const PROGRESS_STAGES = ['暂时无法确认','开发中','等待联调','等待测试','测试中','已完成']
function run(name, args) {
  const names = ['normalizeProgressPreview','buildProgressCorrections']
  const blocks = names.map(n => source.match(new RegExp(`function ${n}\\([^]*?\n\}`))?.[0])
  assert.ok(blocks.every(Boolean), 'review projection and submission validation exist')
  return JSON.parse(JSON.stringify(vm.runInNewContext(`${blocks.join('\n')}; ${name}(...args)`, {args, PROGRESS_STAGES})))
}
const rows = [{module_id:'A',name:'功能A',previous_stage:'开发中',stage:'已完成',evidence_ids:['e1']},{module_id:'B',name:'功能B',previous_stage:'已完成',stage:'已完成',evidence_ids:[]}]
const refs = [{evidence_id:'e1',type:'git_file_fact'},{evidence_id:'prd',type:'prd_block'}]
test('legacy absence never fabricates cumulative modules; complete preview retained',()=>{
 assert.deepEqual(run('normalizeProgressPreview',[null]),[])
 assert.deepEqual(run('normalizeProgressPreview',[{state:'ready',modules:rows}]),rows)
 assert.deepEqual(run('normalizeProgressPreview',[{state:'ready',modules:[rows[0],rows[0]]}]),[])
})
test('default and disabled correction submit nothing',()=>{
 assert.deepEqual(run('buildProgressCorrections',[rows,{},refs]),[])
 assert.deepEqual(run('buildProgressCorrections',[rows,{A:{enabled:false}},refs]),[])
})
test('explicit correction preserves chosen stage reason and bound git refs',()=>{
 assert.deepEqual(run('buildProgressCorrections',[rows,{A:{enabled:true,stage:'开发中',reason:'代码回退',evidence_ids:['e1']}},refs]),[{module_id:'A',stage:'开发中',reason:'代码回退',evidence_ids:['e1']}])
})
test('blank reason, non-git refs, foreign module and invalid stages block submission',()=>{
 for(const patch of [{reason:''},{evidence_ids:['prd']},{evidence_ids:['invented']},{evidence_ids:[]},{stage:'任意状态'}]) assert.throws(()=>run('buildProgressCorrections',[rows,{A:{enabled:true,stage:'开发中',reason:'依据',evidence_ids:['e1'],...patch}},refs]), /PROGRESS_CORRECTION_INVALID/)
 assert.throws(()=>run('buildProgressCorrections',[rows,{foreign:{enabled:true,stage:'开发中',reason:'依据',evidence_ids:['e1']}},refs]), /PROGRESS_CORRECTION_INVALID/)
})
test('reload invalidates corrections and approval only includes validated nonempty corrections',()=>{
 const reset = source.match(/function invalidateLoadedAuthority\(\) \{[^]*?\n\}/)[0]
 const scope = Object.fromEntries(['bundle','approvalSnapshot','reanalysisBundle','approvalOpen','correctionOpen','approvalSaving','modelSendOpen','modelSendPreview','modelSendFlow','modelSendAck','progressEdits'].map(k=>[k,{value:{old:true}}]))
 vm.runInNewContext(`${reset}; invalidateLoadedAuthority()`,scope)
 assert.equal(Object.keys(scope.progressEdits.value).length,0)
 assert.match(source,/progress_corrections: corrections/)
 assert.match(source,/:disabled="!canApprove \|\| approvalSaving"/)
})

async function approve(edits, canApprove = true) {
 const calls = []
 const build = source.match(/function buildProgressCorrections\([^]*?\n\}/)[0]
 const confirm = source.match(/async function confirmApproval\([^]*?\n\}/)[0]
 const context = { PROGRESS_STAGES, Object, Number, canApprove:{value:canApprove}, approvalSaving:{value:false}, approverName:{value:'审核人'}, authorityGeneration:1, props:{projectId:4}, report:{value:{project_id:4,report_version_id:9,state_version:2}}, progressModules:{value:rows}, progressEdits:{value:edits}, evidenceRefs:{value:refs}, progressGitRefs:{value:refs}, sameProjectId:(a,b)=>String(a)===String(b), setNotice:()=>{}, localTimezone:()=> 'UTC', localUtcOffsetMinutes:()=>0, createReportApproval:async (...args)=>{calls.push(args);return {status:409,ok:false,body:{}}}, authorityRequestIsCurrent:()=>true,detailMessage:()=>'', calls }
 await vm.runInNewContext(`${build}; ${confirm}; confirmApproval()`,context)
 return JSON.parse(JSON.stringify(calls))
}
test('actual approval body adds only explicitly valid corrections and never sends invalid or approved edits',async()=>{
 const plain = await approve({})
 assert.equal(plain.length,1)
 assert.equal(Object.hasOwn(plain[0][2],'progress_corrections'),false)
 const edits={A:{enabled:true,stage:'开发中',reason:'代码回退',evidence_ids:['e1']}}
 const changed=await approve(edits)
 assert.deepEqual(changed[0][2].progress_corrections,[{module_id:'A',stage:'开发中',reason:'代码回退',evidence_ids:['e1']}])
 assert.deepEqual(await approve({A:{...edits.A,reason:''}}),[])
 assert.deepEqual(await approve(edits,false),[])
})

test('preview code refs are authoritative; generic legacy refs never become selectable',()=>{
 const block=source.match(/function selectProgressGitRefs\([^]*?\n\}/)[0]
 const get=(preview,legacyRefs)=>JSON.parse(JSON.stringify(vm.runInNewContext(`${block}; selectProgressGitRefs(preview,legacyRefs)`,{preview,legacyRefs})))
 assert.deepEqual(get({evidence_refs:[{evidence_id:'new',type:'git_file_fact'}]},refs),[{evidence_id:'new',type:'git_file_fact'}])
 assert.deepEqual(get({evidence_refs:[]},refs),[])
 assert.deepEqual(get(undefined,[{evidence_id:'x',type:'frozen_reference'}]),[])
})
