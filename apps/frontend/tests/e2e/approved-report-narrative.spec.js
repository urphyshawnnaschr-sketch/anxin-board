import {test,expect} from '@playwright/test'
import {narrativeDigest} from '../../src/api/approvedReportNarrative.js'
const project={id:1,name:'合成日报项目',status:'active',version:1,git_url:'https://example.invalid/demo.git',branch:'main'}
function formalBoard(approval) {
  return {
    schema_version: 'anxin_board_report_v3',
    title: '安心看板',
    motto: '非己所安，不加于物',
    project_name: project.name,
    report_date: '2026-09-03',
    profile_id: 13,
    profile_version_no: 4,
    profile_content_hash: '5'.repeat(64),
    source_prd_id: 7,
    module_count: 2,
    completed_module_count: 1,
    active_module_count: 1,
    unknown_module_count: 0,
    overall_message: '本版报告已经由项目经理正式确认，安心看板只展示该批准版本绑定的研发事实。',
    modules: [
      {
        module_id: 'task_execution',
        name: '任务执行',
        stage: '已完成',
        display_stage: '已完成',
        tone: 'done',
        summary: 'AI 调用、结果保存与报告版本生成链路已闭合。',
        next_step: '保持当前链路稳定。',
        client_stage_summary_hash: '6'.repeat(64)
      },
      {
        module_id: 'report_review',
        name: '报告审阅',
        stage: '开发中',
        display_stage: '开发中',
        tone: 'active',
        summary: '最终确认已完成，正在核对正式安心看板。',
        next_step: '完成真机与截图验收。',
        client_stage_summary_hash: '7'.repeat(64)
      }
    ],
    daily_change: {
      display_state: 'ready',
      headline: '最终确认已闭合并生成正式安心看板',
      summary: '本轮完成最终确认和正式看板准入闭环。',
      highlights: [],
      scope_note: '仅展示已批准报告绑定的正式数据。',
      plain_language_change_summary_hash: '8'.repeat(64)
    },
    manager_supplement: '项目经理已核对本版报告。',
    approval_snapshot_id: 31,
    approval_snapshot_hash: '9'.repeat(64),
    report_version_id: 3,
    report_version_no: 1,
    report_content_hash: '1'.repeat(64),
    validation_result_id: 23,
    validation_result_hash: 'f'.repeat(64),
    evidence_snapshot_id: 11,
    evidence_snapshot_hash: '2'.repeat(64),
    git_snapshot_id: 29,
    git_facts_hash: 'a'.repeat(64),
    git_branch: 'main',
    git_from_commit: 'a'.repeat(40),
    git_to_commit: 'b'.repeat(40),
    prd_id: 7,
    prd_source_hash: 'b'.repeat(64),
    prd_parsed_hash: 'c'.repeat(64),
    prd_structured_hash: 'd'.repeat(64),
    prd_document_fingerprint: 'e'.repeat(64),
    model_execution_result_id: 17,
    execution_result_hash: '3'.repeat(64),
    model_call_id: 19,
    call_identity_hash: '4'.repeat(64),
    provider: 'deepseek',
    model_id: 'deepseek-flash',
    model_version: 'DeepSeek-V4.1-Flash',
    actual_model: 'deepseek-flash',
    provider_runtime_fingerprint: 'deepseek-runtime-cn-v1',
    rule_version: 'report-rule-v1',
    output_schema_version: 'daily-report-output-v1',
    benchmark_sample_pack_version: 'benchmark-pack-v1',
    qualification_hash: 'c'.repeat(64),
    authorization_hash: 'd'.repeat(64),
    supplement_version_id: null,
    supplement_content_hash: null,
    supplement_provided_by: null,
    supplement_provided_at: null,
    supplement_provided_timezone: null,
    supplement_source_type: null,
    confirmed_by: approval.confirmed_by,
    confirmed_at: approval.confirmed_at,
    confirmed_timezone: approval.confirmed_timezone,
    confirmed_utc_offset_minutes: approval.confirmed_utc_offset_minutes,
    human_acknowledged: true,
    anxin_board_report_hash: 'e'.repeat(64)
  }
}


async function fixture(version=3) {
 const report=formalBoard({confirmed_by:'审核人',confirmed_at:'2026-09-26T10:00:00Z',confirmed_timezone:'UTC',confirmed_utc_offset_minutes:0})
 report.report_version_id=version
 report.anxin_board_report_hash=String(version).repeat(64)
 const source_result={plain_summary:`版本${version}：增加输入校验，运行验证仍待进行。`,feature_progress:[],code_change_summary:[{content:'补充输入校验测试代码。',implementation_scope:'测试',source_type:'git_fact',evidence_ids:['evidence-original-1']}],test_evidence:[],risks:[{content:'外部联调条件仍需确认。',source_type:'ai_analysis',risk_level:'suspected',evidence_ids:['evidence-original-1']}],unknown_items:[],source_warnings:[]}
 report.report_content_hash=await narrativeDigest(source_result)
 const {feature_progress,...content}=source_result
 const keys=['anxin_board_report_hash','approval_snapshot_id','approval_snapshot_hash','report_version_id','report_content_hash','model_execution_result_id','execution_result_hash']
 const dto={schema_version:'approved_report_narrative_v1',project_id:1,...Object.fromEntries(keys.map(k=>[k,report[k]])),source_task_type:'daily_report_generate',source_result,content}
 dto.narrative_hash=await narrativeDigest(dto)
 const gitMetrics=await gitMetricsProjection(report)
 return {report,dto,gitMetrics}
}
async function gitMetricsProjection(report,counts={added_lines:58,deleted_lines:2,changed_file_count:3}) {
 const source_git_facts={branch:report.git_branch,from_commit:report.git_from_commit,to_commit:report.git_to_commit,
  commits:[report.git_to_commit],commit_count:1,...counts,diff_bytes:256}
 report.git_facts_hash=await narrativeDigest(source_git_facts)
 const value={schema_version:'approved_report_git_metrics_v1',project_id:1,
  ...Object.fromEntries(['report_version_id','anxin_board_report_hash','approval_snapshot_id','approval_snapshot_hash','git_snapshot_id','git_facts_hash'].map(k=>[k,report[k]])),
  source_git_facts,metrics:{...counts,commit_count:1,net_added_lines:counts.added_lines-counts.deleted_lines,line_change_total:counts.added_lines+counts.deleted_lines}}
 value.metrics_hash=await narrativeDigest(value)
 return value
}
async function setup(page, latest, historical, response) {
 await page.addInitScript(()=>sessionStorage.setItem('anxinboard:local-browser-session:v1','synthetic-read-session'))
 await page.route('**/api/**',async route=>{
  const url=new URL(route.request().url()),p=url.pathname
  if(!p.startsWith('/api/')) return route.continue()
  if(route.request().method()!=='GET') return route.abort()
  if(p==='/api/projects/1') return route.fulfill({json:project})
  if(p==='/api/projects/1/anxin-board/latest') return route.fulfill({json:latest.report})
  if(p==='/api/projects/1/anxin-board/history') return route.fulfill({json:[{id:9,project_id:1,schema_version:historical.report.schema_version,report_date:historical.report.report_date,report_hash:historical.report.anxin_board_report_hash,created_at:'2026-09-26T00:00:00Z',report:historical.report}]})
  if(p.endsWith('/module-narrative')) return route.fulfill({json:null})
  if(p.endsWith('/git-metrics')) return route.fulfill({json:(p.includes(`/reports/${latest.report.report_version_id}/`)?latest:historical).gitMetrics})
  if(p.endsWith('/narrative')) return response(route,url)
  return route.fulfill({status:404,json:{detail:{code:'SYNTHETIC_NOT_PROVIDED'}}})
 })
}

async function moduleProjection(report) {
 const dto={schema_version:'approved_module_narrative_v1',project_id:1,
  ...Object.fromEntries(['report_version_id','report_content_hash','anxin_board_report_hash','approval_snapshot_id','approval_snapshot_hash','profile_id','profile_content_hash'].map(k=>[k,report[k]])),
  source:{project_id:2,profile_id:9,profile_content_hash:'a'.repeat(64),task_id:'baseline-synthetic',task_identity_hash:'b'.repeat(64),output_hash:'c'.repeat(64),exact_head:'d'.repeat(40)},
  attribution:'已确认的模块说明',confirmed_by:'模块说明核对人',confirmed_at:'2026-09-28T12:00:00Z',cross_project_reuse_ack:true,historical_baseline:true,approved_git_head:report.git_to_commit,
  modules:report.modules.map((module,index)=>({module_id:module.module_id,summary:`已找到${module.name}的记录保存与查看能力。`,remaining:`${module.name}的实际运行验证仍待完成。`,requirement_refs:[0],source_requirements:[{requirement_index:0,requirement_text:`${module.name}支持保存与查看。`,status:index===0?'implemented':'partial',rationale:`Saved original rationale ${index}.`,evidence_ids:[`repo-code-source-${index}`]}]})),
  idempotency_key:'synthetic-module-note'}
 dto.module_narrative_hash=await narrativeDigest(dto)
 return dto
}

async function readyMail(page) {
 await page.route('**/api/settings/mail-transport',route=>route.fulfill({json:{configured:true,profile:{from_identity:'sender@example.test'}}}))
 await page.route('**/api/projects/1/recipient-config',route=>route.fulfill({json:{configured:true,version_no:1,to_recipients:['recipient@example.test']}}))
}

test('analysis model follows the frozen selected report and never reads current model settings',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let modelSettingReads=0
 Object.assign(old.report,{provider:'historic-vendor',model_id:'requested-old-model',actual_model:'actual-old-model',model_version:'Archived-Version-2'})
 await setup(page,latest,old,(route,url)=>route.fulfill({json:url.pathname.includes('/reports/3/')?latest.dto:old.dto}))
 await page.route('**/api/settings/deepseek-*',route=>{modelSettingReads++;return route.fulfill({json:{selected_model:'wrong-current-model'}})})
 await page.goto('/#/projects/1/board')
 const model=page.getByTestId('analysis-model')
 await expect(model.getByText('AI 分析模型',{exact:true})).toBeVisible()
 await expect(model.getByTestId('analysis-model-name')).toHaveText('DeepSeek · deepseek-flash')
 await expect(model.getByTestId('analysis-model-version')).toHaveText('版本记录：DeepSeek-V4.1-Flash')
 await page.getByRole('button',{name:'查看历史版本'}).click();await page.locator('.history-record').click()
 await expect(model.getByTestId('analysis-model-name')).toHaveText('historic-vendor · actual-old-model')
 await expect(model.getByTestId('analysis-model-version')).toHaveText('版本记录：Archived-Version-2')
 await expect(model).not.toContainText('requested-old-model')
 expect(modelSettingReads).toBe(0)
})

for(const counts of [{added_lines:58,deleted_lines:2,changed_file_count:3},{added_lines:0,deleted_lines:0,changed_file_count:0}]) {
test(`approved Git cards use exact saved counts ${counts.added_lines}/${counts.deleted_lines}/${counts.changed_file_count}`,async({page})=>{
 const latest=await fixture(),old=await fixture(2)
 latest.gitMetrics=await gitMetricsProjection(latest.report,counts)
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}))
 await page.goto('/#/projects/1/board')
 const cards=page.getByTestId('anxin-board-git-metrics')
 await expect(cards).toBeVisible()
 for(const key of ['added_lines','deleted_lines','changed_file_count']) await expect(cards.locator(`[data-metric="${key}"] strong`)).toHaveText(String(counts[key]))
 await expect(cards.locator('.today-item')).toHaveCount(3)
 await expect(page.getByTestId('anxin-board-module-metrics')).toHaveCount(0)
 await expect(page.getByText('当前并行主线',{exact:true})).toHaveCount(0)
 await expect(page.getByTestId('git-metrics-range')).not.toBeVisible()
 await page.getByTestId('git-metrics-details').locator('summary').click()
 await expect(page.getByTestId('git-metrics-range')).toContainText(latest.report.git_from_commit)
})
}

test('unavailable Git metrics never substitute module counters and block sending',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let release,reads=0,posts=0
 const gate=new Promise(resolve=>{release=resolve})
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}));await readyMail(page)
 await page.route('**/git-metrics?*',async route=>{reads++;await gate;return route.fulfill({status:409,json:{detail:{code:'GIT_METRICS_SOURCE_INVALID'}}})})
 await page.route('**/api/projects/1/mail-send',route=>{posts++;return route.abort()})
 await page.goto('/#/projects/1/board');await expect.poll(()=>reads).toBe(1)
 await expect(page.getByTestId('git-metrics-loading')).toBeVisible()
 await expect(page.getByTestId('mail-send-button')).toBeDisabled()
 release()
 await expect(page.getByTestId('git-metrics-error')).toBeVisible()
 await expect(page.getByTestId('anxin-board-git-metrics')).toHaveCount(0)
 await expect(page.getByTestId('anxin-board-module-metrics')).toHaveCount(0)
 await expect(page.getByTestId('mail-send-button')).toBeDisabled()
 expect(posts).toBe(0)
})

test('Git metrics reject altered source with a recomputed DTO hash',async({page})=>{
 const latest=await fixture(),old=await fixture(2),bad=structuredClone(latest.gitMetrics)
 bad.source_git_facts.added_lines=999
 const {metrics_hash,...unsigned}=bad;bad.metrics_hash=await narrativeDigest(unsigned)
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}));await readyMail(page)
 await page.route('**/git-metrics?*',route=>route.fulfill({json:bad}))
 await page.goto('/#/projects/1/board')
 await expect(page.getByTestId('git-metrics-error')).toBeVisible()
 await expect(page.getByTestId('anxin-board-git-metrics')).toHaveCount(0)
 await expect(page.getByTestId('mail-send-button')).toBeDisabled()
})

test('late Git metrics never replace the selected historical report',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let release,requested=false,finished=false
 old.gitMetrics=await gitMetricsProjection(old.report,{added_lines:9,deleted_lines:4,changed_file_count:2})
 const gate=new Promise(resolve=>{release=resolve})
 await setup(page,latest,old,(route,url)=>route.fulfill({json:url.pathname.includes('/reports/3/')?latest.dto:old.dto}))
 await page.route('**/git-metrics?*',async route=>{
  if(route.request().url().includes('/reports/3/')) {requested=true;await gate;await route.fulfill({json:latest.gitMetrics});finished=true;return}
  return route.fulfill({json:old.gitMetrics})
 })
 await page.goto('/#/projects/1/board');await expect.poll(()=>requested).toBe(true)
 await page.getByRole('button',{name:'查看历史版本'}).click();await page.locator('.history-record').click()
 const cards=page.getByTestId('anxin-board-git-metrics')
 await expect(cards.locator('.today-value strong')).toHaveText(['9','4','2'])
 release();await expect.poll(()=>finished).toBe(true)
 await expect(cards.locator('.today-value strong')).toHaveText(['9','4','2'])
})

test('mail submits only the displayed module hash and stale annotation is blocked without refetch',async({page})=>{
 const latest=await fixture(),old=await fixture(2),projection=await moduleProjection(latest.report)
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}));await readyMail(page)
 let reads=0,posted=null
 await page.route('**/module-narrative?*',route=>{reads++;return route.fulfill({json:projection})})
 await page.route('**/api/projects/1/mail-send',async route=>{
  posted=route.request().postDataJSON()
  return route.fulfill({status:409,json:{detail:{code:'MAIL_SEND_MODULE_NARRATIVE_DRIFT',message:'模块说明与已展示版本不一致，请重新查看。'}}})
 })
 await page.goto('/#/projects/1/board')
 await expect(page.getByText(projection.modules[0].summary,{exact:true})).toBeVisible()
 const button=page.getByTestId('mail-send-button');await expect(button).toBeEnabled()
 page.once('dialog',async dialog=>{expect(dialog.message()).toContain('包含已确认的模块说明修订');await dialog.accept()})
 await button.click()
 await expect(page.getByTestId('mail-send-result')).toContainText('模块说明与已展示版本不一致')
 expect(posted.expected_module_narrative_hash).toBe(projection.module_narrative_hash)
 expect(posted.expected_report_version_id).toBe(latest.report.report_version_id)
 expect(reads).toBe(1)
 await expect(button).toBeDisabled()
})

test('mail is disabled while module notes load or fail and legacy null is explicitly bound',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let release,posted=null
 const gate=new Promise(resolve=>{release=resolve})
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}));await readyMail(page)
 let reads=0
 await page.route('**/module-narrative?*',async route=>{
  reads++;if(reads===1){await gate;return route.fulfill({status:503,json:{}})}
  return route.fulfill({json:null})
 })
 await page.route('**/api/projects/1/mail-send',route=>{
  posted=route.request().postDataJSON()
  const headers=route.request().headers()
  expect(headers['x-anxin-session']).toBe('synthetic-read-session')
  expect(headers['local-idempotency-key']).toBeTruthy()
  return route.fulfill({json:{state:'sent'}})
 })
 await page.goto('/#/projects/1/board');await expect.poll(()=>reads).toBe(1)
 const button=page.getByTestId('mail-send-button')
 await expect(button).toBeDisabled();release()
 await expect(page.getByTestId('module-narrative-error')).toBeVisible()
 await expect(button).toBeDisabled();expect(posted).toBeNull()
 await page.getByRole('button',{name:'重新读取模块说明'}).click()
 await expect(button).toBeEnabled()
 page.once('dialog',dialog=>dialog.accept());await button.click()
 await expect(page.getByTestId('mail-send-result')).toContainText('SMTP 服务器已接受')
 expect(posted.expected_module_narrative_hash).toBeNull()
 expect(posted.expected_report_version_id).toBe(latest.report.report_version_id)
 expect(posted.expected_recipient_config_version_no).toBe(1)
 expect(posted.human_confirmed).toBe(true)
 expect(posted).not.toHaveProperty('password');expect(posted).not.toHaveProperty('to_recipients')
})

test('mail stays blocked when the displayed historical report differs from the latest send target',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let posts=0
 await setup(page,latest,old,(route,url)=>route.fulfill({json:url.pathname.includes('/reports/3/')?latest.dto:old.dto}))
 await readyMail(page)
 await page.route('**/api/projects/1/mail-send',route=>{posts++;return route.abort()})
 await page.goto('/#/projects/1/board')
 const button=page.getByTestId('mail-send-button');await expect(button).toBeEnabled()
 await page.getByRole('button',{name:'查看历史版本'}).click()
 await page.locator('.history-record').click()
 await expect(page.getByTestId('approved-report-narrative')).toContainText(old.dto.content.plain_summary)
 await expect(button).toBeDisabled()
 await expect(page.getByTestId('mail-display-binding')).toContainText('请先查看当前最新的完整报告')
 expect(posts).toBe(0)
})

for(const moduleCount of [2,11]) {
test(`confirmed module notes replace repeated stage copy for ${moduleCount} modules and keep source facts collapsed`,async({page})=>{
 const latest=await fixture(),old=await fixture(2)
 while(latest.report.modules.length<moduleCount) {
  const index=latest.report.modules.length
  latest.report.modules.push({...latest.report.modules[1],module_id:`module_${index}`,name:`合成模块${index}`})
 }
 latest.report.module_count=moduleCount
 latest.report.active_module_count=moduleCount-1
 const projection=await moduleProjection(latest.report)
 // The saved projection carries every original requirement, even when the concise note cites a subset.
 projection.modules[0].source_requirements.push({requirement_index:1,requirement_text:'额外待核实事项。',status:'unknown',rationale:'Unverified original requirement.',evidence_ids:[]})
 if(moduleCount===11) {
  const requirementCounts=[6,7,5,7,9,7,4,7,7,5,6]
  for(const [index,row] of projection.modules.entries()) {
   while(row.source_requirements.length<requirementCounts[index]) {
    const requirement_index=row.source_requirements.length
    row.source_requirements.push({requirement_index,requirement_text:`原始需求 ${index}-${requirement_index}`,status:'partial',rationale:`Original rationale ${index}-${requirement_index}`,evidence_ids:[`repo-code-${index}-${requirement_index}`]})
   }
  }
 }
 const {module_narrative_hash,...unsignedProjection}=projection
 projection.module_narrative_hash=await narrativeDigest(unsignedProjection)
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}))
 await page.route('**/module-narrative?*',route=>route.fulfill({json:projection}))
 await page.goto('/#/projects/1/board')
 await expect(page.getByRole('columnheader',{name:'具体进展与待完善事项'})).toBeVisible()
 const notes=page.getByTestId('module-narrative-provenance')
 await expect(notes).toContainText('历史代码盘点')
 await expect(notes).toContainText('模块说明核对人')
 await expect(notes).toContainText('独立确认')
 await expect(page.locator('.module-original article')).toHaveCount(moduleCount===11?70:3)
 for(const row of projection.modules){
  await expect(page.getByText(row.summary,{exact:true})).toBeVisible()
  await expect(page.getByText(row.remaining,{exact:true})).toBeVisible()
 }
 await expect(page.getByText('Saved original rationale 0.',{exact:true})).not.toBeVisible()
 await page.getByTestId('module-original-task_execution').locator('summary').click()
 await expect(page.getByText('Saved original rationale 0.',{exact:true})).toBeVisible()
 await expect(page.getByText('repo-code-source-0',{exact:true})).toBeVisible()
 await expect(page.getByText('Unverified original requirement.',{exact:true})).toBeVisible()
 await expect(page.getByText(latest.report.modules[1].summary,{exact:true})).toHaveCount(0)
})
}

test('late module notes never replace the selected historical report',async({page})=>{
 const latest=await fixture(),old=await fixture(2)
 const latestNotes=await moduleProjection(latest.report),oldNotes=await moduleProjection(old.report)
 latestNotes.modules[0].summary='最新版本的独立模块说明。'
 oldNotes.modules[0].summary='历史版本的独立模块说明。'
 for(const dto of [latestNotes,oldNotes]) {
  const {module_narrative_hash,...unsigned}=dto
  dto.module_narrative_hash=await narrativeDigest(unsigned)
 }
 let release,requested=false
 const gate=new Promise(resolve=>{release=resolve})
 await setup(page,latest,old,(route,url)=>route.fulfill({json:url.pathname.includes('/reports/3/')?latest.dto:old.dto}))
 await page.route('**/module-narrative?*',async route=>{
  if(route.request().url().includes('/reports/3/')) { requested=true;await gate;return route.fulfill({json:latestNotes}) }
  return route.fulfill({json:oldNotes})
 })
 await page.goto('/#/projects/1/board');await expect.poll(()=>requested).toBe(true)
 await page.getByRole('button',{name:'查看历史版本'}).click()
 await page.locator('.history-record').click()
 await expect(page.getByText(oldNotes.modules[0].summary,{exact:true})).toBeVisible()
 release()
 await expect(page.getByText(latestNotes.modules[0].summary,{exact:true})).toHaveCount(0)
})

for(const mutation of ['hash','profile','module']) {
test(`module notes reject mismatched ${mutation} without substituting generic prose`,async({page})=>{
 const latest=await fixture(),old=await fixture(2),projection=await moduleProjection(latest.report)
 if(mutation==='hash') projection.module_narrative_hash='0'.repeat(64)
 if(mutation==='profile') projection.profile_id+=1
 if(mutation==='module') projection.modules[0].module_id='foreign-module'
 if(mutation!=='hash') {
  const {module_narrative_hash,...unsigned}=projection
  projection.module_narrative_hash=await narrativeDigest(unsigned)
 }
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}))
 await page.route('**/module-narrative?*',route=>route.fulfill({json:projection}))
 await page.goto('/#/projects/1/board')
 await expect(page.getByTestId('module-narrative-error')).toBeVisible()
 await expect(page.getByText(projection.modules[0].summary,{exact:true})).toHaveCount(0)
 await expect(page.getByText(latest.report.modules[0].summary,{exact:true})).toHaveCount(0)
})
}
test('approved original work is prominent, scope honest, evidence collapsed, failure explicit',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let reads=0
 await setup(page,latest,old,(route,url)=>{
  expect(url.searchParams.get('report_hash')).toBe(latest.report.anxin_board_report_hash)
  reads++;return route.fulfill(reads===1?{status:503,json:{}}:{json:latest.dto})
 })
 await page.goto('/#/projects/1/board')
 await expect(page.getByRole('alert')).toContainText('本次工作内容未能载入')
 await page.getByRole('button',{name:'重新读取工作内容'}).click()
 const panel=page.getByTestId('approved-report-narrative')
 await expect(panel).toContainText(latest.dto.content.plain_summary)
 await expect(panel).toContainText('测试相关代码')
 await expect(panel).toContainText('尚待验证的分析判断')
 await expect(panel.getByText('evidence-original-1').first()).not.toBeVisible()
 await panel.locator('details').first().locator('summary').click()
 await expect(panel.getByText('evidence-original-1').first()).toBeVisible()
 await expect(page.getByText('本报告记录的全项目状态，不代表本次新增成果')).toBeVisible()
})

for (const moduleCount of [2,11]) {
test(`manager fact leads ${moduleCount} dynamic modules while complete AI original stays available and client actions stay visible`,async({page})=>{
 const latest=await fixture(),old=await fixture(2)
 while(latest.report.modules.length<moduleCount) {
  const index=latest.report.modules.length
  latest.report.modules.push({...latest.report.modules[1],module_id:`synthetic_${index}`,name:`合成功能${index}`})
 }
 latest.report.module_count=moduleCount
 latest.report.active_module_count=moduleCount-1
 Object.assign(latest.report, {
  manager_supplement:'模拟验收：四项检查已运行；尚未真实联调，并非生产发布。',
  supplement_version_id:6,supplement_content_hash:'a'.repeat(64),supplement_source_type:'pm_external_fact',
  supplement_provided_by:'合成验收人',supplement_provided_at:'2026-09-28T10:00:00Z',supplement_provided_timezone:'UTC'
 })
 latest.dto.content.risks.push({content:'请甲方提供联调窗口。',source_type:'pm_external_fact',risk_level:'needs_client_action',evidence_ids:['synthetic-action']})
 latest.dto.content.source_warnings.push({content:'只分析本次选定范围。',source_type:'fixed_disclaimer',evidence_ids:['synthetic-scope']})
 latest.report.report_content_hash=latest.dto.report_content_hash=await narrativeDigest(latest.dto.source_result)
 const {narrative_hash,...unsigned}=latest.dto; latest.dto.narrative_hash=await narrativeDigest(unsigned)
 const before=JSON.stringify(latest)
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}))
 await page.goto('/#/projects/1/board')
 const lead=page.getByRole('region',{name:'本次工作与进展',exact:true})
 await expect(lead.getByRole('heading',{name:'项目经理说明',exact:true})).toBeVisible()
 await expect(lead.getByText(latest.report.manager_supplement,{exact:true})).toBeVisible()
 await expect(lead).toContainText('项目经理提供')
 await expect(lead).toContainText('合成验收人')
 await expect(lead.getByText('请甲方提供联调窗口。',{exact:true})).toBeVisible()
 await expect(lead).not.toContainText(latest.dto.content.plain_summary)
 const board=page.getByTestId('anxin-board')
 await expect(board.locator('table tbody tr')).toHaveCount(moduleCount)
 await expect(board.locator('.today-value strong')).toHaveText(['58','2','3'])
 const original=page.getByTestId('approved-report-narrative')
 await expect(original.getByText(latest.dto.content.plain_summary,{exact:true})).not.toBeVisible()
 await page.getByText('查看完整 AI 原文与依据',{exact:true}).click()
 await expect(original.getByText(latest.dto.content.plain_summary,{exact:true})).toBeVisible()
 await expect(original.getByText('只分析本次选定范围。',{exact:true})).toBeVisible()
 await expect(original).toContainText('尚待验证的分析判断')
 const headings=await board.locator(':scope > section > .section-head h2').allTextContents()
 expect(headings.map(text=>text.replace(/^\d+/,''))).toEqual(['本次工作与进展','当前功能状态','AI 分析原文与依据','报告信息'])
 await page.reload()
 await expect(lead.getByText(latest.report.manager_supplement,{exact:true})).toBeVisible()
 await expect(lead.getByText('请甲方提供联调窗口。',{exact:true})).toBeVisible()
 await expect(original.getByText(latest.dto.content.plain_summary,{exact:true})).not.toBeVisible()
 await expect(board.locator('table tbody tr')).toHaveCount(moduleCount)
 expect(JSON.stringify(latest)).toBe(before)
})
}
test('late latest narrative never replaces the selected historical report',async({page})=>{
 const latest=await fixture(),old=await fixture(2);let release;let requested=false
 const gate=new Promise(resolve=>{release=resolve})
 await setup(page,latest,old,async(route,url)=>{
  if(url.pathname.includes('/reports/3/')){requested=true;await gate;return route.fulfill({json:latest.dto})}
  expect(url.searchParams.get('report_hash')).toBe(old.report.anxin_board_report_hash)
  return route.fulfill({json:old.dto})
 })
 await page.goto('/#/projects/1/board');await expect.poll(()=>requested).toBe(true)
 await page.getByRole('button',{name:'查看历史版本'}).click()
 await page.locator('.history-record').click()
 await expect(page.getByTestId('approved-report-narrative')).toContainText(old.dto.content.plain_summary)
 release()
 await expect(page.getByTestId('approved-report-narrative')).not.toContainText(latest.dto.content.plain_summary)
})


test('actual board preserves suffix and duplicate feature judgments, escapes text and leaves module stages unchanged',async({page})=>{
 const latest=await fixture(),old=await fixture(2)
 const rows=[
  {feature:'任务执行（局部工作）<img src=x onerror=alert(1)>',stage:'开发中',source_type:'git_fact',implementation_scope:'前端',evidence_ids:['ref<suffix>']},
  {feature:'任务执行',stage:'开发中',source_type:'ai_analysis',implementation_scope:'前端',evidence_ids:['ref-duplicate-a']},
  {feature:'任务执行',stage:'暂时无法确认',source_type:'ai_analysis',implementation_scope:'测试',evidence_ids:['ref-duplicate-b']},
  {feature:'报告审阅',stage:'开发中',source_type:'git_fact',implementation_scope:'后端',evidence_ids:['ref-exact-hidden']}
 ]
 latest.dto.source_result.feature_progress=rows
 latest.report.report_content_hash=latest.dto.report_content_hash=await narrativeDigest(latest.dto.source_result)
 const {narrative_hash,...unsigned}=latest.dto;latest.dto.narrative_hash=await narrativeDigest(unsigned)
 await setup(page,latest,old,route=>route.fulfill({json:latest.dto}))
 await page.goto('/#/projects/1/board')
 const area=page.getByTestId('unassociated-features')
 await expect(area.getByRole('heading',{name:'尚未关联到已确认功能模块',exact:true})).toBeVisible()
 await expect(area.locator('article')).toHaveCount(3)
 await expect(area).toContainText(rows[0].feature)
 await expect(area.locator('img')).toHaveCount(0)
 await expect(area).not.toContainText('报告审阅')
 await expect(area.getByText('ref<suffix>',{exact:true})).not.toBeVisible()
 await area.locator('details').first().locator('summary').click()
 await expect(area.getByText('ref<suffix>',{exact:true})).toBeVisible()
 const table=page.getByTestId('anxin-board').locator('table').first()
 await expect(table.locator('tr').filter({hasText:'任务执行'})).toContainText('已完成')
 await expect(table.locator('tr').filter({hasText:'报告审阅'})).toContainText('开发中')
})
