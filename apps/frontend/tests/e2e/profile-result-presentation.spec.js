import { test, expect } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

// Synthetic project only. Every API is intercepted; unexpected writes fail.
const hash = 'a'.repeat(64)
const head = 'b'.repeat(40)
const longText = 'LongEvidenceReference'.repeat(45)
function fixture(status = 'succeeded', matching = true) {
  const modules = [
    { client_id: 'm-login', name: '账号管理', requirements: ['用户能够登录', '用户能够修改密码'] },
    { client_id: 'm-audit', name: '操作审计', requirements: ['能够查询操作记录'] },
    { client_id: 'm-report', name: '统计报告', requirements: ['能够导出报告'] }
  ].map(m => ({ ...m, description: '测试计划', prd_refs: [], exclusions: [] }))
  const content = {
    schema_version: 'project_profile_v2', project_summary: '合成项目', planned_modules: modules,
    implementation_mappings: modules.map((m, i) => ({ planned_module_id: m.client_id, status: ['partial', 'unknown', 'implemented'][i],
      rationale: 'Requirement evidence summary: static evidence only.', exact_head: head,
      paths: i === 1 ? [] : [{ type: 'backend', pattern: 'src/' + longText + '.ts', required: true, note: '' }], evidence_ids: i === 1 ? [] : ['repo-code-' + longText] })),
    unplanned_code_features: [], domain_glossary: [], exclude_patterns: [], notes: ''
  }
  const candidate = { id: 8, project_id: 3, status: 'candidate', version_no: 3, edit_version: 1,
    content_hash: hash, content, confirmed_at: null, created_at: '2026-09-20T00:00:00Z', updated_at: '2026-09-20T00:00:00Z' }
  const task = { task_id: 'synthetic-ui-task', status, profile_id: 8, generated_content_hash: matching ? hash : 'c'.repeat(64),
    stage_count: 7, completed_stages: status === 'running' ? 3 : 7, max_calls: 12, current_stage: 'verify/1/internal-stage',
    resume_available: false, stale_scope: false, module_requirements: {
      'm-login': [{ requirement_index: 0, status: 'implemented', rationale: longText, evidence_ids: ['repo-code-' + longText] },
        { requirement_index: 1, status: 'partial', rationale: 'Only part of the requirement is evidenced.', evidence_ids: ['ev-2'] }],
      'm-audit': [{ requirement_index: 0, status: 'unknown', rationale: 'Insufficient evidence.', evidence_ids: ['repo-code-background'] }],
      'm-report': [{ requirement_index: 0, status: 'implemented', rationale: 'Export route is evidenced.', evidence_ids: ['ev-3'] }]
    } }
  return { candidate, task }
}

async function open(page, { status = 'succeeded', matching = true, confirmed = false, notStarted = false, allUnknown = false, errorCode = null, failureDiagnostic = null, latestStatus = null, resultFailure = false } = {}) {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session'))
  const data = fixture(status, matching), unexpected = [], errors = []
  data.candidate.generated_baseline_result = status === 'succeeded'
  if (errorCode) data.task.error_code = errorCode
  if (failureDiagnostic) data.task.failure_diagnostic = failureDiagnostic
  let queryFailure = false
  if (confirmed) data.candidate.status = 'confirmed'
  if (notStarted) { data.task = null; data.candidate.content.implementation_mappings = [] }
  if (allUnknown) {
    data.task = null
    data.candidate.content.implementation_mappings.forEach(mapping => {
      mapping.status = 'unknown'; mapping.evidence_ids = []; mapping.paths = []
    })
  }
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') { unexpected.push(request.method() + ' ' + url.pathname); return route.abort() }
    let body
    if (url.pathname === '/api/projects/3') body = { id: 3, name: '界面测试项目', git_url: 'https://example.invalid/project.git', branch: 'main', status: 'active' }
    else if (url.pathname === '/api/projects/3/profiles') {
      const { content, ...metadata } = data.candidate
      body = confirmed ? [metadata] : [metadata, { ...metadata, id: 2, status: 'confirmed' }]
    }
    else if (url.pathname === '/api/profile-candidates/8') body = data.candidate
    else if (url.pathname === '/api/profile-candidates/2') body = { ...data.candidate, id: 2, status: 'confirmed' }
    else if (url.pathname === '/api/projects/3/prd-versions') body = []
    else if (url.pathname === '/api/projects/3/project-state-baseline/status') body = { status: notStarted ? 'missing' : confirmed ? 'established' : 'candidate_pending', profile_id: data.candidate.id, git_branch: 'synthetic-test-branch', exact_head: head }
    else if (url.pathname === '/api/projects/3/project-state-baseline/atlas/results/8') {
      if (resultFailure) return route.fulfill({ status: 409, json: { detail: { code: 'BROWNFIELD_RESULT_IDENTITY_INVALID' } } })
      body = { task: data.task?.status === 'succeeded' ? data.task : null }
    }
    else if (url.pathname.startsWith('/api/projects/3/project-state-baseline/atlas/tasks/')) {
      if (queryFailure) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
      body = { task: latestStatus ? { ...data.task, task_id: 'new-task', status: latestStatus, profile_id: null, error_code: 'PROFILE_GENERATION_STRICT_WIRE_INVALID' } : data.task }
    }
    else if (url.pathname === '/api/settings/deepseek-status') body = { configured: false, connected: false, selected_model: null }
    else { unexpected.push(request.method() + ' ' + url.pathname); return route.abort() }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.goto('/#/projects/3/modules')
  const skip = page.getByRole('button', { name: '跳过引导', exact: true })
  if (await skip.isVisible()) await skip.click()
  await expect(page.locator('.reconciliation-item')).toHaveCount(3)
  if (status === 'succeeded' && matching && !confirmed && !notStarted && !allUnknown && !resultFailure && !['running', 'unknown'].includes(latestStatus)) {
    await expect(page.getByText('分析已完成 · 待你审核', { exact: true })).toBeVisible()
  }
  return { unexpected, errors, failQueries: () => { queryFailure = true } }
}

test('candidate report preview is sandboxed read-only and download requires confirmation', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session')
    window.__reportUrls = { created: [], revoked: [] }
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL)
    URL.createObjectURL = value => { const url = create(value); window.__reportUrls.created.push(url); return url }
    URL.revokeObjectURL = url => { window.__reportUrls.revoked.push(url); revoke(url) }
  })
  const log = await open(page)
  let reads = 0
  await page.route('**/review-html', route => {
    reads++; expect(route.request().method()).toBe('GET')
    expect(route.request().headers()['x-anxin-session']).toBe('synthetic-session')
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><p>合成安心看板</p><script>parent.__unsafeReport=true</script>' })
  })
  await page.getByRole('button', { name: '预览安心看板', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '安心看板预览' })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('iframe')).toHaveAttribute('sandbox', '')
  await expect(page.frameLocator('iframe[title="安心看板只读预览"]').getByText('合成安心看板')).toBeVisible()
  expect(await page.evaluate(() => window.__unsafeReport)).toBeUndefined()
  await expect(page.getByRole('button', { name: '下载 HTML', exact: true })).toBeDisabled()
  await expect(page.getByText('下载前请先人工确认本次结果。', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '关闭预览' }).click()
  await expect(dialog).toHaveCount(0)
  expect(await page.evaluate(() => window.__reportUrls.created.every(url => window.__reportUrls.revoked.includes(url)))).toBe(true)
  expect(reads).toBe(1); expect(log.unexpected).toEqual([])
})

test('late report response cannot open a dialog after leaving the profile', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session'))
  await open(page)
  let release, requested
  const gate = new Promise(resolve => { release = resolve }), started = new Promise(resolve => { requested = resolve })
  await page.route('**/review-html', async route => {
    requested(); await gate
    await route.fulfill({ contentType: 'text/html', body: '<!doctype html><meta charset="utf-8"><p>迟到合成报告</p>' })
  })
  await page.getByRole('button', { name: '预览安心看板', exact: true }).click()
  await started
  await page.evaluate(() => { location.hash = '#/projects/3/home' })
  await expect(page.locator('.home-overview')).toBeVisible()
  const response = page.waitForResponse(response => response.url().endsWith('/review-html'))
  release(); await (await response).finished()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('iframe[title="安心看板只读预览"]')).toHaveCount(0)
})

test('confirmed report downloads locally and stale hash disables both actions', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session'))
  const log = await open(page, { confirmed: true })
  await page.locator('.confirmed-task-record > summary').click()
  await page.route('**/export-html', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><p>合成已确认结果</p>' }))
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 HTML', exact: true }).click()
  expect((await download).suggestedFilename()).toBe('anxin-board-baseline-3-8.html')
  expect(log.unexpected).toEqual([])
  await page.goto('about:blank')
  await open(page, { matching: false })
  await expect(page.getByRole('button', { name: '预览安心看板', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '下载 HTML', exact: true })).toHaveCount(0)
})

test('report read errors stay visible without affecting analysis and duplicate conflicts use plain language', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session'))
  await open(page)
  await page.route('**/review-html', route => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'BROWNFIELD_REPORT_IDENTITY_INVALID' } }) }))
  await page.getByRole('button', { name: '预览安心看板', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: '结果版本已变化' })).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.goto('about:blank')
  await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_RESPONSES_CONTENT_JSON_DUPLICATE_KEY_INVALID' })
  await expect(page.getByText('AI 返回了重复字段，旧版本未能解析；已保存成功步骤，不会自动重发。', { exact: false })).toBeVisible()
  await page.goto('about:blank')
  await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_ARGUMENT_CONFLICT' })
  await expect(page.getByText('AI 返回了相互冲突的字段，本次已停止；不会自动重发。', { exact: false })).toBeVisible()
})

test('collector failure explains stopped analysis and shows only numeric diagnostics without retry', async ({ page }) => {
  const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_TOOL_COUNT_INVALID',
    failureDiagnostic: { expected_row_count: 3, tool_call_count: 1, raw_body: 'private-diagnostic-marker' } })
  await expect(page.getByText('AI 未按本批要求返回结果，本次分析已停止；已完成步骤仍保留，不会自动重发。', { exact: false })).toBeVisible()
  const details = page.locator('.atlas-technical-details')
  await details.locator('summary').click()
  await expect(details).toContainText('每份完整结果要求 3 条记录；AI 返回 1 份结果。')
  await expect(page.getByText('private-diagnostic-marker')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '继续原分析任务', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '查询已有任务', exact: true }).click()
  expect(log.unexpected).toEqual([])
})

test('collector conflict does not expose untrusted diagnostic strings', async ({ page }) => {
  const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_BATCH_CONFLICT',
    failureDiagnostic: { expected_row_count: true, tool_call_count: 'private-diagnostic-marker' } })
  await expect(page.getByText('AI 对同一批返回了相互矛盾的结果，本次已停止；不会自动选择其中一份或重发。', { exact: false })).toBeVisible()
  await page.locator('.atlas-technical-details > summary').click()
  await expect(page.getByText('private-diagnostic-marker')).toHaveCount(0)
  await expect(page.getByText('每份完整结果要求', { exact: false })).toHaveCount(0)
  expect(log.unexpected).toEqual([])
})

test('wire failure shows its safe cause and location without opening technical details', async ({ page }) => {
  const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_WIRE_INVALID',
    failureDiagnostic: { wire_reason: 'INDEX_INVALID', tool_call_ordinal: 1, row_ordinal: 0,
      slot_ordinal: 3, raw_key: 'private-diagnostic-marker', raw_value: 'private-diagnostic-marker' } })
  await expect(page.getByText('具体原因：代码引用编号类型不对或超出本批范围（第 2 份结果，第 1 条记录，第 4 个引用位置）。', { exact: true })).toBeVisible()
  await expect(page.getByText('private-diagnostic-marker')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '继续原分析任务', exact: true })).toHaveCount(0)
  expect(log.unexpected).toEqual([])
})

for (const [reason, rule, explanation] of [
  ['INDEX_INVALID', 'INTEGER_TYPE', '代码引用编号的类型不是整数'],
  ['INDEX_INVALID', 'INTEGER_RANGE', '代码引用编号超出本批范围'],
  ['RATIONALE_INVALID', 'NONBLANK_STRING', '分析说明为空或只有空白字符'],
  ['RATIONALE_INVALID', 'STRING_LENGTH', '分析说明超过长度限制']
]) {
  test(`wire failure distinguishes the precise rule ${rule}`, async ({ page }) => {
    const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_WIRE_INVALID',
      failureDiagnostic: { wire_reason: reason, wire_rule: rule, raw_value: 'private-diagnostic-marker' } })
    await expect(page.getByText(`具体原因：${explanation}。`, { exact: true })).toBeVisible()
    await expect(page.getByText('private-diagnostic-marker')).toHaveCount(0)
    expect(log.unexpected).toEqual([])
  })
}

for (const reason of ['private-diagnostic-marker', 'constructor', ['INDEX_INVALID'], { reason: 'INDEX_INVALID' }]) {
  test(`wire failure rejects untrusted reason ${JSON.stringify(reason)}`, async ({ page }) => {
    const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_WIRE_INVALID',
      failureDiagnostic: { wire_reason: reason, tool_call_ordinal: true, row_ordinal: 'private-diagnostic-marker' } })
    await expect(page.getByText('这条记录未保存具体校验原因，暂时无法进一步定位；不会自动重发。', { exact: true })).toBeVisible()
    await expect(page.getByText('private-diagnostic-marker')).toHaveCount(0)
    await expect(page.getByText('第 true 份结果', { exact: false })).toHaveCount(0)
    expect(log.unexpected).toEqual([])
  })
}

test('dynamic requirement results are readable and all disclosure interactions stay read-only', async ({ page }, testInfo) => {
  const log = await open(page)
  const summary = page.getByRole('region', { name: '需求实现概览', exact: true })
  await expect(summary).toBeVisible()
  await expect(summary).toContainText('4')
  await expect(summary).toContainText('已实现')
  const result = page.getByRole('region', { name: '计划与实现对账', exact: true })
  await expect(result.getByRole('region', { name: '需求实现概览', exact: true })).toBeVisible()
  await expect(page.getByText('只有人工确认的结果会用于后续分析；证据不足的需求保持待核实。', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('profile-result-desktop.png'), fullPage: true })
  const evidence = page.locator('.evidence-details').first()
  await evidence.locator(':scope > summary').click()
  const row = evidence.locator('.requirement-evidence').first()
  await expect(row).toContainText('用户能够登录')
  const source = evidence.locator('.module-evidence-source')
  await expect(source.locator('code').first()).not.toBeVisible()
  await source.locator(':scope > summary').click()
  await expect(source.locator('code').first()).toBeVisible()
  const references = row.locator('.evidence-references')
  await expect(references.locator('code').first()).not.toBeVisible()
  await references.locator(':scope > summary').click()
  await expect(references.locator('code').first()).toBeVisible()
  const analysis = row.locator('.requirement-analysis')
  await expect(analysis.locator('p')).not.toBeVisible()
  await analysis.locator('summary').click()
  await expect(analysis).toContainText(longText)
  const overflow = await row.evaluate(el => el.scrollWidth > el.clientWidth + 1)
  expect(overflow).toBe(false)
  await expect(page.getByRole('button', { name: /确认并使用 V3/ })).toBeVisible()
  expect(log.unexpected).toEqual([])
  expect(log.errors).toEqual([])
})

test('a result from another content hash never becomes this candidate summary', async ({ page }) => {
  const log = await open(page, { matching: false })
  await expect(page.getByRole('region', { name: '需求实现概览', exact: true }).locator('dl')).toHaveCount(0)
  await expect(page.locator('.requirement-evidence')).toHaveCount(0)
  await expect(page.getByText('逐条需求统计暂不可用，请查看模块结果。', { exact: true })).toHaveCount(1)
  expect(log.unexpected).toEqual([])
})

test('UNKNOWN stays blocked while technical details remain available', async ({ page }) => {
  const log = await open(page, { status: 'unknown' })
  await expect(page.getByText('模型执行结果无法确认，系统不会自动重发。', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '继续原分析任务', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始 AI 对账', exact: true })).toHaveCount(0)
  expect(log.unexpected).toEqual([])
})

test('confirmed result shows the next step and keeps reanalysis collapsed', async ({ page }) => {
  const log = await open(page, { confirmed: true })
  await expect(page.getByRole('button', { name: '下一步：选择研发范围 →', exact: true })).toBeVisible()
  await expect(page.locator('.reanalysis-disclosure')).not.toHaveAttribute('open', '')
  await expect(page.getByRole('button', { name: /检查当前代码并估算分析上限/ })).not.toBeVisible()
  await expect(page.getByRole('region', { name: '计划与实现对账', exact: true }).locator('.requirement-totals')).toBeVisible()
  const record = page.locator('details.confirmed-task-record')
  await expect(record.locator(':scope > summary')).toHaveText('本次分析记录')
  await expect(record).not.toHaveAttribute('open', '')
  await expect(page.getByText('本次分析结果已人工确认，可继续查看需求和代码依据。', { exact: true })).not.toBeVisible()
  await expect(record.getByRole('button', { name: '查询已有任务', exact: true })).not.toBeVisible()
  await record.locator(':scope > summary').click()
  await expect(record.getByRole('button', { name: '查询已有任务', exact: true })).toBeVisible()
  await expect(record.locator('.atlas-technical-details > summary')).toBeVisible()
  expect(log.unexpected).toEqual([])
})

test('mobile result rows and explicit confirmation remain readable without writes', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const log = await open(page)
  await page.locator('.evidence-details').first().locator(':scope > summary').click()
  const row = page.locator('.requirement-evidence').first()
  await row.locator('.requirement-analysis > summary').click()
  expect(await row.evaluate(el => el.scrollWidth > el.clientWidth + 1)).toBe(false)
  await expect(page.getByRole('button', { name: /确认并使用 V3/ })).toBeVisible()
  expect(log.unexpected).toEqual([])
  expect(log.errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('profile-result-mobile.png'), fullPage: true })
})

for (const options of [{ confirmed: true, matching: false }, ...['running', 'unknown', 'failed_after_send'].map(status => ({ confirmed: true, status }))]) {
  test(`confirmed task is not folded when unbound or non-successful ${JSON.stringify(options)}`, async ({ page }) => {
    const log = await open(page, options)
    await expect(page.locator('details.confirmed-task-record')).toHaveCount(0)
    await expect(page.getByRole('region', { name: '代码分析任务', exact: true })).toBeVisible()
    expect(log.unexpected).toEqual([])
  })
}

test('a new preparation error remains visible outside the successful record', async ({ page }) => {
  const log = await open(page, { confirmed: true })
  await page.evaluate(() => sessionStorage.removeItem('anxinboard:local-browser-session:v1'))
  await page.locator('.reanalysis-disclosure > summary').click()
  await page.getByRole('button', { name: '检查当前代码并估算分析上限', exact: true }).click()
  await expect(page.locator('.lane-profile > .message.error')).toBeVisible()
  await expect(page.locator('details.confirmed-task-record')).not.toHaveAttribute('open', '')
  expect(log.unexpected).toEqual([])
})

test('a failed query cannot hide its warning behind an old success message type', async ({ page }) => {
  const log = await open(page, { confirmed: true })
  await page.locator('.confirmed-task-record > summary').click()
  log.failQueries()
  await page.getByRole('button', { name: '查询已有任务', exact: true }).click()
  await expect(page.getByText('暂时无法核对已有分析任务，请先恢复任务查询。', { exact: true })).toBeVisible()
  await expect(page.locator('details.confirmed-task-record')).toHaveCount(0)
  expect(log.unexpected).toEqual([])
})

test('not-started plan exposes preparation without presenting a completed analysis', async ({ page }) => {
  const log = await open(page, { confirmed: true, notStarted: true })
  await expect(page.getByRole('button', { name: '检查当前代码并估算分析上限', exact: true })).toBeVisible()
  await expect(page.locator('.requirement-totals')).toHaveCount(0)
  await expect(page.getByText('尚无完整代码分析结果，以下为已整理的需求。', { exact: true })).toBeVisible()
  expect(log.unexpected).toEqual([])
})

test('confirmed all-unknown legacy result exposes full analysis and explains missing evidence', async ({ page }, testInfo) => {
  const log = await open(page, { confirmed: true, allUnknown: true })
  const notice = page.getByRole('region', { name: '实现状态待核实', exact: true })
  await expect(notice).toContainText('3 个模块都缺少足够实现证据')
  await expect(notice).toContainText('synthetic-test-branch')
  await expect(page.getByText('功能与当前实现已确认', { exact: true })).toHaveCount(0)
  await expect(page.getByText('尚无完整代码分析结果，以下为已整理的需求。', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '检查当前代码并估算分析上限', exact: true })).toBeVisible()
  await expect(page.locator('.reanalysis-disclosure')).toHaveCount(0)
  await expect(page.locator('.requirement-totals')).toHaveCount(0)
  await expect(page.getByText('已读取 3 个模块结论，均为暂无法确认；这里显示的是模块状态，不是逐条需求统计。', { exact: true })).toBeVisible()
  await expect(page.locator('.reconciliation-item .mapping-pill').filter({ hasText: '暂时无法确认' })).toHaveCount(3)
  expect(log.unexpected).toEqual([])
  expect(log.errors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('confirmed-all-unknown.png'), fullPage: true })
})

for (const [status, label] of [['running', '正在核对代码证据'], ['failed_after_send', '分析已停止']]) {
  test(`${status} task status stays visible with no fabricated requirement totals`, async ({ page }) => {
    const log = await open(page, { status })
    await expect(page.getByRole('region', { name: '代码分析任务', exact: true }).getByText(label, { exact: true })).toBeVisible()
    await expect(page.locator('.requirement-totals')).toHaveCount(0)
    expect(log.unexpected).toEqual([])
  })
}

for (const latestStatus of ['failed_after_send', 'running']) {
  test(`saved confirmed evidence remains readable with a newer ${latestStatus} task`, async ({ page }) => {
    const log = await open(page, { confirmed: true, latestStatus })
    await expect(page.locator('.requirement-totals')).toContainText('4')
    await expect(page.getByRole('button', { name: '预览安心看板', exact: true })).toBeEnabled()
    await expect(page.getByRole('button', { name: '下载 HTML', exact: true })).toBeEnabled()
    await page.locator('.evidence-details').first().locator(':scope > summary').click()
    await expect(page.locator('.requirement-evidence').first()).toContainText('用户能够登录')
    expect(log.unexpected).toEqual([])
    expect(log.errors).toEqual([])
  })
}

test('generated candidate remains immutable when result lookup fails, while manual confirmation remains available', async ({ page }) => {
  const log = await open(page, { resultFailure: true })
  await expect(page.locator('.result-read-error')).toContainText('不一致')
  await expect(page.getByRole('button', { name: '重新读取分析结果', exact: true })).toBeVisible()
  await expect(page.locator('.requirement-totals')).toHaveCount(0)
  await page.locator('.candidate-plan-editor > summary').click()
  await expect(page.getByRole('textbox', { name: '模块名称', exact: true }).first()).toBeDisabled()
  await expect(page.getByRole('button', { name: '保存修改', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认并使用 V3', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '查看范围', exact: true }).first().click()
  await expect(page.locator('.lane-module-detail').first()).toBeVisible()
  expect(log.unexpected).toEqual([])
})

test('Apple presentation preview preserves candidate confirmation and read-only evidence', async ({ page }) => {
  const log = await open(page)
  const directory = path.resolve(process.cwd(), '../../../../artifacts/apple-ui-20260926')
  await fs.mkdir(directory, { recursive: true })
  await expect(page.locator('.requirement-totals')).toBeVisible()
  await expect(page.getByText('分析结果保留原文。修改计划请返回准备项目；核对完成后仍可人工确认本版结果。')).toBeVisible()
  await expect(page.getByRole('button', { name: '保存修改', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认并使用 V3', exact: true })).toBeEnabled()
  await page.screenshot({ path: path.join(directory, 'lane-b-candidate-desktop.png'), fullPage: true })
  await page.locator('.evidence-details').first().locator(':scope > summary').click()
  await page.screenshot({ path: path.join(directory, 'lane-b-evidence-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.requirement-evidence').first()).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
  await page.screenshot({ path: path.join(directory, 'lane-b-candidate-mobile.png'), fullPage: true })
  expect(log.unexpected).toEqual([])
  expect(log.errors).toEqual([])
})

test('Apple project list and workbench show one primary action with real fixture readiness', async ({ page }) => {
  const log = await open(page, { confirmed: true })
  const directory = path.resolve(process.cwd(), '../../../../artifacts/apple-ui-20260926')
  await fs.mkdir(directory, { recursive: true })
  await page.route('**/api/projects/3/prd-versions', route => route.fulfill({ json: [{ id: 1, version_no: 1, status: 'parse_confirmed' }] }))
  await page.goto('/#/projects/3/home')
  await page.reload()
  const home = page.locator('.home-overview')
  await expect(home.locator('.primary-button')).toHaveCount(1)
  await expect(home.locator('.home-readiness-row')).toHaveCount(4)
  await expect(home.locator('.home-readiness-status.ready')).toHaveCount(4)
  await expect(home.locator('.lane-page-head h2')).toHaveCSS('font-size', '24px')
  await expect(home.locator('.home-preparation-head h3')).toHaveCSS('font-size', '16px')
  await page.screenshot({ path: path.join(directory, 'lane-b-workbench-desktop.png'), fullPage: true })
  const projects = [
    { id: 3, name: '客户服务平台', branch: 'main', git_url: 'https://example.invalid/customer-service.git', status: 'active', updated_at: '2026-09-26T08:00:00Z' },
    { id: 4, name: '仓储管理系统', branch: 'main', git_url: 'https://example.invalid/warehouse.git', status: 'draft', updated_at: '2026-09-25T08:00:00Z' }
  ]
  await page.route('**/api/projects', route => route.fulfill({ json: projects }))
  await page.goto('/#/projects')
  const skip = page.getByRole('button', { name: '跳过引导', exact: true })
  if (await skip.isVisible()) await skip.click()
  await expect(page.locator('.lane-table tbody tr')).toHaveCount(2)
  await expect(page.locator('.lane-page .primary-button')).toHaveCount(1)
  await page.screenshot({ path: path.join(directory, 'lane-b-projects-desktop.png'), fullPage: true })
  await page.route('**/api/projects', route => route.fulfill({ json: [] }))
  await page.reload()
  if (await skip.isVisible()) await skip.click()
  await expect(page.locator('.welcome-card')).toBeVisible()
  await page.screenshot({ path: path.join(directory, 'lane-b-first-project-desktop.png'), fullPage: true })
  expect(log.unexpected).toEqual([])
  expect(log.errors).toEqual([])
})

test('lost confirmation can recover by read-only reload after its first reconciliation fails', async ({ page }) => {
  const log = await open(page)
  let posts = 0, recoveryReads = 0
  const confirmed = { ...fixture().candidate, generated_baseline_result: true, status: 'confirmed', edit_version: 2, confirmed_at: '2026-09-26T00:00:00Z', confirmed_by: 'local' }
  await page.route('**/api/profile-candidates/8', async route => {
    expect(route.request().method()).toBe('GET')
    if (!posts) return route.fulfill({ json: { ...fixture().candidate, generated_baseline_result: true } })
    recoveryReads++
    if (recoveryReads === 1) return route.fulfill({ status: 503, json: { detail: { code: 'TEST_READ_UNAVAILABLE' } } })
    return route.fulfill({ json: confirmed })
  })
  await page.route('**/api/profile-candidates/8/confirm-with-state-baseline', async route => {
    expect(route.request().method()).toBe('POST')
    posts++
    await route.abort('failed')
  })
  await page.route('**/api/projects/3/project-state-baseline/status', route => route.fulfill({ json: { status: recoveryReads >= 2 ? 'established' : 'candidate_pending', profile_id: 8 } }))
  const confirm = page.getByRole('button', { name: '确认并使用 V3', exact: true })
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect(page.getByText('确认结果尚未核实，已暂停再次确认。请重新加载服务器版本核对；不会自动重复提交。', { exact: true })).toBeVisible()
  await expect(confirm).toBeDisabled()
  const reload = page.getByRole('button', { name: '重新加载最新版本', exact: true })
  await expect(reload).toBeEnabled()
  await reload.click()
  await expect(page.getByText('已核实：项目档案已确认生效。没有重复发送确认请求。', { exact: true })).toBeVisible()
  await expect(page.getByText('已确认的功能模块 · V3', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '下一步：选择研发范围 →', exact: true })).toBeVisible()
  expect(posts).toBe(1)
  expect(recoveryReads).toBe(2)
  expect(log.unexpected).toEqual([])
  expect(log.errors).toEqual([])
})

for (const status of ['failed_after_send', 'failed_pre_send', 'succeeded']) {
  test(`ready preflight distinguishes existing ${status} record without changing send boundaries`, async ({ page }) => {
    const log = await open(page, { confirmed: true, status, errorCode: 'PROFILE_GENERATION_STRICT_WIRE_INVALID' })
    let preflights = 0
    await page.route('**/api/projects/3/project-state-baseline/atlas/preflight', async route => {
      expect(route.request().method()).toBe('POST')
      expect(route.request().postDataJSON()).toEqual({ plan_profile_id: 8 })
      preflights++
      return route.fulfill({ json: { status: 'ready', preflight: { preflight_identity_hash: 'd'.repeat(64), tracked_files: 1881, safe_text_bytes: 7465104, module_count: 3, max_calls: 288, exact_head: head } } })
    })
    const original = { failed_after_send: '分析已停止', failed_pre_send: '分析准备未完成', succeeded: '分析已完成' }[status]
    if (status === 'succeeded') await page.locator('.confirmed-task-record > summary').click()
    await expect(page.locator('.atlas-task-heading b')).toHaveText(original)
    await page.locator('.reanalysis-disclosure > summary').click()
    await page.getByRole('button', { name: '检查当前代码并估算分析上限', exact: true }).click()
    await expect(page.getByText('代码检查完成。请核对文件范围和分析上限；确认后才会将本次资料发送给 AI。', { exact: true })).toBeVisible()
    const previous = page.locator('details.previous-task-record')
    await expect(previous).toHaveCount(1)
    await expect(previous).not.toHaveAttribute('open', '')
    await expect(previous.locator(':scope > summary')).toHaveText('上次' + original + ' · 查看记录')
    await expect(previous).toHaveClass(new RegExp(status === 'succeeded' ? 'success' : 'error'))
    await expect(previous.getByRole('button', { name: '查询已有任务', exact: true })).not.toBeVisible()
    await previous.locator(':scope > summary').click()
    await expect(previous.getByRole('button', { name: '查询已有任务', exact: true })).toBeVisible()
    await previous.locator('.atlas-technical-details > summary').click()
    await expect(previous.locator('.atlas-technical-details code').filter({ hasText: /^PROFILE_GENERATION_STRICT_WIRE_INVALID$/ })).toBeVisible()
    if (status === 'failed_after_send') await expect(previous.getByText('上次分析记录：这条记录未保存具体校验原因，暂时无法进一步定位；不会自动重发。', { exact: true })).toBeVisible()
    await expect(page.locator('.atlas-task-heading b')).toHaveText('上次' + original)
    await expect(page.locator('.baseline-ready-strip').getByText('代码版本', { exact: true })).toBeVisible()
    await page.locator('.baseline-tech-details > summary').click()
    await expect(page.locator('.baseline-tech-details code')).toHaveText(head)
    await expect(page.locator('.baseline-go-button')).toBeDisabled()
    if (status === 'failed_after_send') {
      for (const finalStatus of ['failed_after_send', 'unknown']) {
        let newStatus = 'queued'
        await page.route('**/api/projects/3/project-state-baseline/atlas/tasks/latest', route => route.fulfill({ json: { task: { ...fixture(newStatus).task, task_id: 'new-task-' + finalStatus, profile_id: null } } }))
        await page.getByRole('button', { name: '查询已有任务', exact: true }).click()
        await expect(page.locator('.atlas-task-heading b')).toHaveText('等待分析')
        newStatus = 'running'
        await page.getByRole('button', { name: '查询已有任务', exact: true }).click()
        await expect(page.locator('.atlas-task-heading b')).toHaveText('正在核对代码证据')
        newStatus = finalStatus
        await page.getByRole('button', { name: '查询已有任务', exact: true }).click()
        await expect(page.locator('.atlas-task-heading b')).toHaveText(finalStatus === 'unknown' ? '分析结果暂无法确认' : '分析已停止')
        await expect(page.locator('details.previous-task-record')).toHaveCount(0)
        await expect(page.locator('.atlas-task-heading b')).toBeVisible()
        if (finalStatus === 'unknown') {
          await expect(page.locator('.unknown-retry-panel')).toBeVisible()
          await expect(page.getByRole('button', { name: '重新扫描', exact: true })).toBeDisabled()
          await expect(page.locator('.baseline-go-button')).toBeDisabled()
        }
      }
    }
    expect(preflights).toBe(1)
    expect(log.unexpected).toEqual([])
    expect(log.errors).toEqual([])
  })
}

test('row mismatch exposes exact safe counts only in technical details and never resends', async ({ page }) => {
  const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_WIRE_INVALID',
    failureDiagnostic: { wire_reason: 'ROWS_INVALID', wire_rule: 'EXACT_KEYS', expected_row_count: 3,
      tool_call_count: 2, tool_call_ordinal: 1, missing_row_count: 3, extra_row_count: 1, raw_key: 'private-diagnostic-marker' } })
  await expect(page.getByText('具体原因：AI 返回的内容未能对应本批分析范围；具体数量可展开分析详情查看（第 2 份结果）。', { exact: true })).toBeVisible()
  const details = page.locator('.atlas-technical-details')
  await expect(details).not.toHaveAttribute('open', '')
  await details.locator('summary').click()
  await expect(details).toContainText('第 2 份结果含 1 个批外记录键，缺少 3 个本批记录键')
  await expect(page.getByText('private-diagnostic-marker')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '继续原分析任务', exact: true })).toHaveCount(0)
  expect(log.unexpected).toEqual([])
})

test('row mismatch with invalid counts keeps fallback without unsafe ordinal', async ({ page }) => {
  const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_STRICT_WIRE_INVALID',
    failureDiagnostic: { wire_reason: 'ROWS_INVALID', wire_rule: 'EXACT_KEYS', expected_row_count: 3,
      tool_call_count: 2, tool_call_ordinal: 2, missing_row_count: '3', extra_row_count: -1 } })
  await expect(page.getByText('具体原因：返回的记录不完整，或包含本批之外的记录。', { exact: true })).toBeVisible()
  await page.locator('.atlas-technical-details > summary').click()
  await expect(page.getByText('批外记录键', { exact: false })).toHaveCount(0)
  expect(log.unexpected).toEqual([])
})


test('unknown references remain background checks rather than implementation evidence', async ({ page }) => {
  const log = await open(page)
  const module = page.locator('.reconciliation-item').nth(1)
  await module.locator('.evidence-details > summary').click()
  const row = module.locator('.requirement-evidence')
  await expect(row.locator('.mapping-pill')).toHaveText('暂时无法确认')
  const references = row.locator('.evidence-references')
  await expect(references.locator('summary')).toContainText('已检查的相关代码（仍待核实）')
  await references.locator('summary').click()
  await expect(references).toContainText('不代表已经实现')
  await expect(references.locator('code')).toHaveText('repo-code-background')
  expect(log.unexpected).toEqual([])
})


for (const [status, message] of [[402, '模型账户余额不足'], [422, '模型请求参数无效'], [503, '模型服务暂时不可用']]) {
  test(`HTTP rejection ${status} explains the fixed cause without leaking content or retrying`, async ({ page }) => {
    const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_PROVIDER_HTTP_FAILED',
      failureDiagnostic: { provider_http_status: status, provider_http_class: 'private-marker', raw_body: 'private-marker' } })
    await expect(page.getByRole('status').filter({ hasText: message })).toContainText(`HTTP ${status}`)
    await expect(page.getByRole('status').filter({ hasText: message })).toContainText('不会自动重发')
    await expect(page.locator('body')).not.toContainText('private-marker')
    expect(log.unexpected).toEqual([])
  })
}
for (const value of [true, '402', -1, 600]) {
  test(`invalid HTTP diagnostic ${JSON.stringify(value)} cannot fabricate a status message`, async ({ page }) => {
    const log = await open(page, { status: 'failed_after_send', errorCode: 'PROFILE_GENERATION_PROVIDER_HTTP_FAILED',
      failureDiagnostic: { provider_http_status: value, provider_http_class: 'payment', raw_body: 'private-marker' } })
    await expect(page.locator('body')).not.toContainText('模型账户余额不足')
    await expect(page.locator('body')).not.toContainText('private-marker')
    expect(log.unexpected).toEqual([])
  })
}
