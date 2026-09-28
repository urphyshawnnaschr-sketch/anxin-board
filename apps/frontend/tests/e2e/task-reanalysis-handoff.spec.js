import { test, expect } from '@playwright/test'
const hash = 'a'.repeat(64)
const project = { id: 1, name: 'Mock project', status: 'active', version: 1 }
async function mock(page, { malformed = false, executeFailure = false, restoreFailure = false, normal = false, cancelled = false } = {}) {
  const counts = { prepare: 0, preview: 0, authorize: 0, execute: 0, normal: 0 }
  let state = cancelled ? 'voided' : 'queued'
  await page.addInitScript(({ malformed, normal }) => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'mock-session')
    sessionStorage.setItem('rd-agent:project:1:evidence-snapshot-id', '11')
    if (!normal) {
      sessionStorage.setItem('rd-agent:project:1:report-generation-task-id', 'replacement-3')
      if (!malformed) {
        sessionStorage.setItem('rd-agent:project:1:report-reanalysis-source-report-version-id', '3')
        sessionStorage.setItem('rd-agent:project:1:report-reanalysis-task-id', 'replacement-3')
      }
    }
  }, { malformed, normal })
  await page.route('**/*', async route => {
    const p = new URL(route.request().url()).pathname
    if (!p.startsWith('/api/')) return route.continue()
    const respond = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (p === '/api/projects') return respond([project])
    if (p === '/api/projects/1') return respond(project)
    if (p.endsWith('/report-generation-tasks/active')) return respond({ task: null })
    if (p.includes('/report-generation-tasks')) {
      counts.normal++
      return respond({ local_task_id: route.request().postDataJSON()?.local_task_id, state: 'queued', evidence_snapshot_id: 11 })
    }
    if (p.endsWith('/reanalysis')) return respond(restoreFailure ? { detail: { message: 'mock restore failed' } } : {
      replacement_task: { local_task_id: 'replacement-3', task_type: 'daily_report_regenerate', evidence_snapshot_id: 11, state },
      source_report_version: { report_version_id: 3, evidence_snapshot_id: 11 },
      reanalysis_request: { replacement_local_task_id: 'replacement-3' },
      cancelled, cancellation: cancelled ? { state: 'cancelled_before_send' } : null
    }, restoreFailure ? 500 : 200)
    if (p.endsWith('/prepare')) { counts.prepare++; return respond({ preparation_state: 'prepared', provider_send_state: 'not_attempted', model_call_id: 9, local_request_readiness_state: 'ready_for_gateway_evaluation', next_gate: 'exact_human_send_authorization_required' }) }
    if (p.endsWith('/send-authorization-preview')) { counts.preview++; return respond({ authorization_state: 'awaiting_human_confirmation', model_call_id: 9, data_scope_hash: hash }) }
    if (p.endsWith('/authorize-send')) {
      counts.authorize++
      expect(route.request().postDataJSON()).toEqual({ model_call_id: 9, data_scope_hash: hash, human_confirmed: true })
      return respond({ authorization_state: 'authorized_once', provider_send_state: 'not_attempted', data_scope_hash: hash })
    }
    if (p.endsWith('/execute')) {
      counts.execute++
      expect(route.request().postDataJSON()).toEqual({ model_call_id: 9, data_scope_hash: hash })
      state = executeFailure ? 'unknown' : 'succeeded'
      return respond(executeFailure ? { detail: { message: 'mock execution unknown; no retry' } } : { task_state: 'succeeded' }, executeFailure ? 503 : 200)
    }
    return respond({}, 404)
  })
  await page.goto(normal ? '/#/projects/1/task' : '/#/projects/1/task?reanalysis=3')
  return counts
}
test('cancelled reanalysis stays closed after reload and returns to review', async ({ page }) => {
  const counts = await mock(page, { cancelled: true })
  await expect(page.getByRole('button', { name: '返回原报告审阅 →', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('button', { name: '返回原报告审阅 →', exact: true })).toBeVisible()
  expect(counts).toEqual({ prepare: 0, preview: 0, authorize: 0, execute: 0, normal: 0 })
})
for (const failure of [false, true]) test(`replacement restore, exact authorization and single execution; failure=${failure}`, async ({ page }) => {
  const counts = await mock(page, { executeFailure: failure })
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toBeEnabled()
  expect(counts).toEqual({ prepare: 0, preview: 0, authorize: 0, execute: 0, normal: 0 })
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await expect(page.locator('.send-confirm')).toBeVisible()
  expect(counts).toEqual({ prepare: 1, preview: 1, authorize: 0, execute: 0, normal: 0 })
  // Reviewing the exact scope is followed by a distinct explicit authorization;
  // that authorization itself still must not execute the provider request.
  await page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true }).click()
  await expect(page.getByText('已授权一次', { exact: true })).toBeVisible()
  expect(counts.execute).toBe(0)
  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true }).click()
  await expect(page.locator('.message')).toContainText(failure ? 'mock execution unknown; no retry' : 'AI 分析已经完成')
  await expect(page.getByRole('button', { name: /开始 AI 分析/ })).toHaveCount(0)
  expect(counts).toEqual({ prepare: 1, preview: 1, authorize: 1, execute: 1, normal: 0 })
})
for (const kind of ['malformed', 'restoreFailure']) test(`${kind} blocks fallback task creation`, async ({ page }) => {
  const counts = await mock(page, { [kind]: true })
  await expect(page.locator('.message')).toBeVisible()
  await expect(page.getByRole('button', { name: '准备本次分析', exact: true })).toBeDisabled()
  expect(counts.normal).toBe(0)
  expect(counts.execute).toBe(0)
})
test('normal initial task creation remains available', async ({ page }) => {
  const counts = await mock(page, { normal: true })
  await page.getByRole('button', { name: '准备本次分析', exact: true }).click()
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toBeEnabled()
  expect(counts.normal).toBe(1)
  expect(counts.prepare + counts.preview + counts.authorize + counts.execute).toBe(0)
})
test('late preparation from the previous project cannot reopen the send gate', async ({ page }) => {
  await mock(page)
  let release
  let received
  const pending = new Promise(resolve => { received = resolve })
  await page.route('**/api/projects/1/reports/3/reanalysis/prepare', async route => {
    received()
    await new Promise(resolve => { release = resolve })
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ preparation_state: 'prepared', provider_send_state: 'not_attempted', model_call_id: 991, local_request_readiness_state: 'ready_for_gateway_evaluation', next_gate: 'exact_human_send_authorization_required' }) })
  })
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  await pending
  await page.evaluate(() => { window.location.hash = '#/projects/2/task' })
  await expect(page).toHaveURL(/projects\/2\/task$/)
  release()
  await expect(page.getByRole('button', { name: '先选择研发范围 →', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认本次发送范围 →', exact: true })).toHaveCount(0)
  await expect(page.locator('.message')).toHaveCount(0)
})
