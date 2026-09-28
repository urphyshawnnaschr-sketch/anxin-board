import { expect, test } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const project = {
  id: 1,
  name: '新用户验收项目',
  status: 'active',
  version: 1,
  created_at: '2026-09-03T00:00:00Z',
  updated_at: '2026-09-03T00:00:00Z',
  git_url: 'https://github.com/example/new-user-acceptance.git',
  branch: 'main'
}

const pages = [
  ['01-projects', '/#/projects'],
  ['02-home', '/#/projects/1/home'],
  ['03-setup', '/#/projects/1/setup'],
  ['04-modules', '/#/projects/1/modules'],
  ['05-analysis-range', '/#/projects/1/git'],
  ['06-analysis-report', '/#/projects/1/task'],
  ['07-review', '/#/projects/1/review'],
  ['08-board', '/#/projects/1/board'],
  ['09-settings', '/#/projects/1/settings']
]

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body)
  })
}

async function mockNewUserApi(page) {
  const writes = []
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname

    // Vite serves source files from paths such as /src/api/projects.js.
    // Only intercept actual backend API requests; never swallow frontend modules.
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') writes.push(pathname)

    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/prd-versions') return json(route, [])
    if (request.method() === 'GET' && pathname === '/api/projects/1/profiles') return json(route, [])
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-generation-tasks/active') return json(route, { task: null })
    if (request.method() === 'GET' && pathname === '/api/projects/1/project-state-baseline/status') return json(route, { status: 'missing', has_confirmed_baseline: false })

    return json(route, { detail: { message: '当前验收场景尚未准备这项数据。' } }, 404)
  })
  return { writes }
}

test('new-user visual acceptance — mounted product pages at 1440x900', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockNewUserApi(page)

  const outDir = path.resolve('test-results/visual-product-acceptance')
  await fs.mkdir(outDir, { recursive: true })

  for (const [name, route] of pages) {
    await page.goto(route, { waitUntil: 'networkidle' })
    await expect(page.locator('.app-shell')).toBeVisible()
    await page.screenshot({
      path: path.join(outDir, `${name}.png`),
      fullPage: true,
      animations: 'disabled'
    })
  }
})

test('new user can follow the visible next action without knowing internal routes', async ({ page }) => {
  const log = await mockNewUserApi(page)
  await page.goto('/#/projects/1/home', { waitUntil: 'networkidle' })

  const next = page.getByRole('button', { name: '下一步：准备项目 →' })
  await expect(next).toBeVisible()
  await next.click()
  await expect(page).toHaveURL(/#\/projects\/1\/setup$/)
  await expect(page.getByRole('heading', { name: '准备项目' })).toBeVisible()

  await page.goto('/#/projects/1/task', { waitUntil: 'networkidle' })
  const analysisNav = page.getByRole('button', { name: '研发分析' })
  await expect(analysisNav).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('heading', { name: '还没开始', exact: true })).toBeVisible()
  expect(log.writes).toEqual([])
})

test('exact Human send gate is two explicit clicks and provider execution stays mocked', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => {
    sessionStorage.setItem('rd-agent:project:1:report-generation-task-id', 'tsk-demo')
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'test-session')
  })

  let taskState = 'queued'
  let executeCalls = 0, prepareCalls = 0, authorizeCalls = 0
  const taskBody = () => ({
    id: 5,
    project_id: 1,
    local_task_id: 'tsk-demo',
    evidence_snapshot_id: 13,
    task_type: 'daily_report_generate',
    state: taskState,
    created_at: '2026-09-03T00:00:00Z',
    updated_at: '2026-09-03T00:00:00Z'
  })
  const preparation = {
    preparation_state: 'prepared',
    provider_send_state: 'not_attempted',
    next_gate: 'exact_human_send_authorization_required',
    local_request_readiness_state: 'ready_for_gateway_evaluation',
    model_call_id: 41,
    budget_profile_hash: '1'.repeat(64),
    final_context_manifest_hash: '2'.repeat(64),
    framed_payload_hash: '3'.repeat(64),
    admitted_target_count: 6,
    denied_target_count: 0
  }
  const preview = {
    authorization_state: 'awaiting_human_confirmation',
    provider_send_state: 'not_attempted',
    model_call_id: 41,
    provider: 'deepseek',
    model_id: 'deepseek-flash',
    model_version: 'DeepSeek-V4.1-Flash',
    purpose_id: 'anxin_board_daily_report_v1',
    data_scope_hash: 'a'.repeat(64),
    final_context_manifest_hash: '2'.repeat(64),
    admitted_target_count: 6,
    denied_target_count: 0
  }

  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-generation-tasks/tsk-demo') return json(route, taskBody())
    if (request.method() === 'POST' && pathname.endsWith('/prepare-model-call')) { prepareCalls++; return json(route, preparation) }
    if (request.method() === 'POST' && pathname.endsWith('/send-authorization-preview')) {
      const headers = request.headers()
      expect(headers['x-anxin-session']).toBe('test-session')
      expect(headers['x-request-id']).toBeTruthy()
      return json(route, preview)
    }
    if (request.method() === 'POST' && pathname.endsWith('/authorize-send')) {
      authorizeCalls++
      const headers = request.headers()
      expect(headers['x-anxin-session']).toBe('test-session')
      expect(headers['local-idempotency-key']).toBeTruthy()
      const body = request.postDataJSON()
      expect(body).toEqual({ model_call_id: 41, data_scope_hash: 'a'.repeat(64), human_confirmed: true })
      return json(route, { ...preview, authorization_state: 'authorized_once', permit_ttl_seconds: 300 })
    }
    if (request.method() === 'POST' && pathname.endsWith('/execute-authorized')) {
      executeCalls += 1
      const headers = request.headers()
      expect(headers['x-anxin-session']).toBe('test-session')
      expect(headers['local-idempotency-key']).toBeTruthy()
      taskState = 'succeeded'
      return json(route, { task_state: 'succeeded', model_result_id: 9, report_version_id: 10 })
    }
    return json(route, { detail: { message: '未准备的测试请求。' } }, 404)
  })

  const outDir = path.resolve('test-results/visual-product-acceptance')
  await fs.mkdir(outDir, { recursive: true })
  await page.goto('/#/projects/1/task', { waitUntil: 'networkidle' })
  await expect(page.locator('.app-shell')).toBeVisible()

  expect(executeCalls).toBe(0)
  expect(prepareCalls).toBe(0)
  await page.getByRole('button', { name: '准备 AI 调用' }).click()
  await expect(page.getByRole('button', { name: '确认本次发送范围 →' })).toBeVisible()
  await page.getByRole('button', { name: '确认本次发送范围 →' }).click()
  await expect(page.getByRole('heading', { name: '确认这次要交给 AI 的资料' })).toBeVisible()
  await expect(page.getByText('6 项研发证据')).toBeVisible()

  expect(prepareCalls).toBe(1)
  expect(authorizeCalls).toBe(0)
  expect(executeCalls).toBe(0)
  await expect(page.getByText('点击下方按钮即授权这个精确范围', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '授权本次发送（尚不调用 AI）' }).click()
  await expect(page.getByText('本次授权已经记录，待发送资料尚未发送')).toBeVisible()
  expect(executeCalls).toBe(0)
  await page.screenshot({ path: path.join(outDir, '10-send-authorized-not-sent.png'), fullPage: true, animations: 'disabled' })

  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型' }).click()
  await expect(page.getByRole('heading', { name: '分析完成' })).toBeVisible()
  expect(executeCalls).toBe(1)
  expect(authorizeCalls).toBe(1)
  expect(prepareCalls).toBe(1)
  await page.screenshot({ path: path.join(outDir, '11-analysis-complete-mocked.png'), fullPage: true, animations: 'disabled' })
})

test('launcher bootstrap stays in URL fragment, is exchanged once, then disappears', async ({ page }) => {
  const documentRequests = []
  let exchangeCalls = 0
  page.on('request', (request) => {
    if (request.resourceType() === 'document') documentRequests.push(request.url())
  })

  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'POST' && pathname === '/api/local-session/exchange') {
      exchangeCalls += 1
      expect(request.postDataJSON()).toEqual({ bootstrap_secret: 'fragment-secret' })
      return json(route, {
        schema_version: 'local_browser_session_v1',
        session_state: 'active',
        session_token: 'session-from-fragment'
      })
    }
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    return json(route, { detail: { message: '未准备的测试请求。' } }, 404)
  })

  await page.goto('/#/projects?anxin_bootstrap=fragment-secret', { waitUntil: 'networkidle' })
  await expect(page.locator('.app-shell')).toBeVisible()
  await expect.poll(() => exchangeCalls).toBe(1)
  await expect.poll(() => page.url()).not.toContain('anxin_bootstrap')
  expect(documentRequests.length).toBeGreaterThan(0)
  expect(documentRequests.every((url) => !url.includes('anxin_bootstrap') && !url.includes('fragment-secret'))).toBe(true)

  const stored = await page.evaluate(() => sessionStorage.getItem('anxinboard:local-browser-session:v1'))
  expect(stored).toBe('session-from-fragment')
})
