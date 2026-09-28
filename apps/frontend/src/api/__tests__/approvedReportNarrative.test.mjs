import test from 'node:test'
import assert from 'node:assert/strict'
import { createNarrativeLoader, narrativeSourceLabel, narrativeScopeLabel, validateNarrative } from '../approvedReportNarrative.js'
const report = {schema_version:'anxin_board_report_v3',anxin_board_report_hash:'a'.repeat(64),approval_snapshot_id:1,approval_snapshot_hash:'b'.repeat(64),report_version_id:2,report_content_hash:'c'.repeat(64),model_execution_result_id:3,execution_result_hash:'d'.repeat(64)}
test('mismatched binding is rejected before content can display', async()=>{
 await assert.rejects(validateNarrative({project_id:2},1,report))
})
test('switching report discards a late response and surfaces failure rather than empty success',async()=>{
 let finish; const writes=[]
 const loader=createNarrativeLoader(()=>new Promise(r=>{finish=r}),x=>writes.push(x))
 const first=loader.load(1,report)
 await loader.load(2,{schema_version:'anxin_board_report_v2'})
 finish({ok:false}); await first
 assert.equal(writes.at(-1).state,'legacy')
 await loader.load(1,null)
 assert.equal(writes.at(-1).state,'idle')
 const failing=createNarrativeLoader(async()=>({ok:false}),x=>writes.push(x))
 await failing.load(1,report)
 assert.equal(writes.at(-1).state,'failed'); assert.equal(writes.at(-1).content,null)
})
test('scope and source labels do not claim testing passed',()=>{
 assert.equal(narrativeSourceLabel('ai_analysis'),'AI 分析')
 assert.equal(narrativeScopeLabel('测试'),'测试相关代码')
 assert.equal(narrativeScopeLabel('暂时无法确认'),'涉及范围待核实')
})

async function fixture(regenerate=false) {
 const {narrativeDigest}=await import('../approvedReportNarrative.js')
 const source={plain_summary:'保留原文：本次增加校验，运行仍待核实。',feature_progress:[],code_change_summary:[{content:'增加测试代码，未提供运行结果。',source_type:'git_fact',implementation_scope:'测试',evidence_ids:['ev-1']}],test_evidence:[],risks:[],unknown_items:[],source_warnings:[]}
 const source_result=regenerate?{new_report:source,correction_trace:[]}:source
 const selected={...report,report_content_hash:await narrativeDigest(source_result)}
 const {feature_progress,...content}=source
 const dto={schema_version:'approved_report_narrative_v1',project_id:1,...Object.fromEntries(Object.entries(selected).filter(([k])=>k!=='schema_version')),source_task_type:regenerate?'daily_report_regenerate':'daily_report_generate',source_result,content}
 dto.narrative_hash=await narrativeDigest(dto)
 return {dto,selected}
}
for(const regenerate of [false,true]) test(`approved ${regenerate?'regenerated':'generated'} source binds and keeps original content`,async()=>{
 const {dto,selected}=await fixture(regenerate)
 assert.deepEqual(await validateNarrative(dto,1,selected),dto.content)
 const {narrativeDigest}=await import('../approvedReportNarrative.js')
 const forged=structuredClone(dto);forged.content.plain_summary='改成了未经批准的结论'
 const {narrative_hash,...unsigned}=forged;forged.narrative_hash=await narrativeDigest(unsigned)
 await assert.rejects(validateNarrative(forged,1,selected))
 await assert.rejects(validateNarrative(dto,2,selected))
 await assert.rejects(validateNarrative(dto,1,{...selected,report_version_id:99}))
})
test('new selected report wins over an old successful response; disposal prevents publication',async()=>{
 const {dto,selected}=await fixture();const pending=[];const writes=[]
 const loader=createNarrativeLoader((...args)=>new Promise(resolve=>pending.push({args,resolve})),x=>writes.push(x))
 const first=loader.load(1,selected),second=loader.load(1,{...selected,report_version_id:9})
 pending[1].resolve({ok:false});await second
 pending[0].resolve({ok:true,body:dto});await first
 assert.equal(writes.at(-1).state,'failed')
 assert.deepEqual(pending[0].args,[1,2,selected.anxin_board_report_hash])
 const third=loader.load(1,selected);loader.cancel();const count=writes.length
 pending[2].resolve({ok:true,body:dto});await third
 assert.equal(writes.length,count)
})

test('only unique exact feature associations disappear; suffix, duplicates and regenerate stay original',async()=>{
 const {unassociatedNarrativeFeatures}=await import('../approvedReportNarrative.js')
 const row=(feature)=>({feature,stage:'开发中',source_type:'git_fact',implementation_scope:'前端',evidence_ids:['ev<1>']})
 const rows=[row('设备在线状态展示（三态）'),row('设备在线状态展示'),row('设备在线状态展示'),row('精确匹配')]
 const value={source_task_type:'daily_report_generate',source_result:{feature_progress:rows}}
 const selected={modules:[{name:'设备在线状态展示'},{name:'精确匹配'}]}
 assert.deepEqual(unassociatedNarrativeFeatures(value,selected),rows.slice(0,3))
 const regen={source_task_type:'daily_report_regenerate',source_result:{new_report:value.source_result}}
 assert.deepEqual(unassociatedNarrativeFeatures(regen,selected),rows.slice(0,3))
 assert.deepEqual(unassociatedNarrativeFeatures(value,{modules:[{name:'精确匹配'},{name:'精确匹配'}]}),rows)
})

test('loader exposes unassociated rows only after exact original-source hash verification',async()=>{
 const {dto,selected}=await fixture()
 const {narrativeDigest}=await import('../approvedReportNarrative.js')
 const row={feature:'设备在线状态展示（三态）<script>',stage:'开发中',source_type:'git_fact',implementation_scope:'前端',evidence_ids:['ev-original']}
 dto.source_result.feature_progress=[row]
 dto.report_content_hash=selected.report_content_hash=await narrativeDigest(dto.source_result)
 selected.modules=[{name:'设备在线状态展示',display_stage:'暂时无法确认'}]
 const {narrative_hash,...unsigned}=dto;dto.narrative_hash=await narrativeDigest(unsigned)
 const writes=[];const loader=createNarrativeLoader(async()=>({ok:true,body:dto}),x=>writes.push(x))
 await loader.load(1,selected)
 assert.equal(writes.at(-1).state,'loaded')
 assert.deepEqual(writes.at(-1).unassociatedFeatures,[row])
 assert.equal(selected.modules[0].display_stage,'暂时无法确认')
 dto.source_result.feature_progress[0].feature='改写原始功能'
 await loader.load(1,selected)
 assert.equal(writes.at(-1).state,'failed')
 assert.equal(writes.at(-1).unassociatedFeatures,undefined)
})
