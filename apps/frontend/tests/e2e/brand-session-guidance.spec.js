import { test, expect } from '@playwright/test'

function profileMetadata({ content, ...metadata }) { return metadata }

// Every API response is synthetic; unexpected writes never reach a server.
async function openFixture(page, established = true) {
  const requests = [], errors = []
  const profile = { id: 8, project_id: 3, version_no: 3, edit_version: 1,
    status: established ? 'confirmed' : 'candidate', content_hash: 'a'.repeat(64),
    confirmed_at: established ? '2026-09-24T00:00:00Z' : null,
    content: { schema_version: 'project_profile_v2', project_summary: '合成项目',
      planned_modules: [{ client_id: 'm1', name: '合成功能', description: '', requirements: ['合成需求'], prd_refs: [], exclusions: [] }],
      implementation_mappings: [], unplanned_code_features: [], domain_glossary: [], exclude_patterns: [], notes: '' } }
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    // Vite serves source modules under /src/api/; these are assets, not API calls.
    if (!path.startsWith('/api/')) return route.continue()
    requests.push({ method: request.method(), path })
    if (request.method() !== 'GET') return route.abort()
    const bodies = {
      '/api/projects/3': { id: 3, name: '品牌导航合成项目', branch: 'main', git_url: 'https://example.invalid/repo.git', status: 'active' },
      '/api/projects/3/profiles': [profile, ...(established ? [] : [{ ...profile, id: 2, status: 'confirmed' }])].map(profileMetadata),
      '/api/profile-candidates/8': profile,
      '/api/profile-candidates/2': { ...profile, id: 2, status: 'confirmed' },
      '/api/projects/3/prd-versions': [],
      '/api/projects/3/project-state-baseline/status': { status: established ? 'established' : 'candidate_pending', has_confirmed_baseline: established },
      '/api/projects/3/project-state-baseline/atlas/tasks/latest': { task: null },
      '/api/settings/deepseek-status': { configured: false, connected: false, selected_model: null },
      '/api/projects/3/git/status': { status: 'not_checked' },
      '/api/projects/3/analysis-lineage': { lineage: null }
    }
    if (!(path in bodies)) { errors.push('Unexpected API: ' + path); return route.abort() }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(bodies[path]) })
  })
  await page.goto('/#/projects/3/modules')
  const skip = page.getByRole('button', { name: '跳过引导', exact: true })
  if (await skip.isVisible()) await skip.click()
  await expect(page.locator('.reconciliation-item')).toHaveCount(1)
  return { requests, errors }
}

test('established profile gives a read-only next step and the original brand artwork', async ({ page }) => {
  const log = await openFixture(page)
  const brand = page.locator('.side-navigation .brand-block')
  await expect(brand.locator('.brand-title')).toHaveText('安心看板')
  await expect(brand).not.toContainText('项目经理工作台')
  await expect(brand).not.toContainText('研发进度 AI Agent')
  const image = brand.locator('img')
  await expect(image).toHaveAttribute('alt', '非己所安，不加于物')
  await expect(image).toHaveAttribute('src', /anxin-board-calligraphy.*\.png/)
  await expect.poll(() => image.evaluate(el => el.complete && el.naturalWidth > 0)).toBe(true)
  // The light sidebar displays the original black calligraphy without inversion.
  await expect(image).toHaveCSS('filter', 'none')
  await expect(page.getByText('当前窗口仅供查看', { exact: true })).toBeVisible()
  const next = page.getByRole('region', { name: '下一步', exact: true })
  await expect(next).toContainText('可以选择本次研发范围，进行后续增量分析。各功能状态以证据结果为准。')
  await expect(page.locator('.reanalysis-disclosure')).not.toHaveAttribute('open', '')
  await expect(page.getByRole('button', { name: '检查当前代码并估算分析上限', exact: true })).not.toBeVisible()
  await next.getByRole('button', { name: '下一步：选择研发范围 →', exact: true }).click()
  await expect(page).toHaveURL(/#\/projects\/3\/git$/)
  expect(log.requests.filter(request => request.method !== 'GET')).toEqual([])
  expect(log.errors).toEqual([])
})

test('unconfirmed candidate does not receive the established next-step instruction', async ({ page }) => {
  const log = await openFixture(page, false)
  await expect(page.getByRole('region', { name: '下一步', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认并使用 V3', exact: true })).toBeVisible()
  expect(log.requests.filter(request => request.method !== 'GET')).toEqual([])
  expect(log.errors).toEqual([])
})

test('project overview has one main action and does not claim to be workflow step one', async ({ page }) => {
  const log = await openFixture(page)
  await page.route('**/api/projects/3/prd-versions', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify([{ id: 1, version_no: 1, status: 'parse_confirmed' }])
  }))
  await page.goto('/#/projects/3/home')
  await page.reload()
  const home = page.locator('.home-overview')
  await expect(home).toBeVisible()
  await expect(page.getByRole('region', { name: '项目主流程' })).toHaveCount(0)
  await expect(home.locator('.primary-button')).toHaveCount(1)
  await expect(home.locator('.lane-page-head h2')).toHaveCSS('font-size', '24px')
  await expect(home.locator('.primary-button')).toHaveCSS('font-size', '14px')
  await expect(home.locator('.primary-button')).toHaveCSS('font-weight', '600')
  await expect(home.getByRole('region', { name: '项目准备情况' })).toBeVisible()
  await expect(home.locator('.home-readiness-row')).toHaveCount(4)
  await expect(home.locator('.home-readiness-status.ready')).toHaveCount(4)
  await expect(home.locator('.home-project-meta')).toContainText('管理状态')
  await expect(home.locator('.primary-button')).toContainText('开始研发分析')
  await expect(home.locator('.lane-kpi-card')).toHaveCount(0)

  await page.route('**/api/projects/3/project-state-baseline/status', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'unknown' })
  }))
  await page.reload()
  await expect(home.locator('.home-next-action')).toContainText('查看代码分析状态')
  await expect(home.locator('.home-readiness-row').filter({ hasText: '当前代码分析' })).toContainText('暂不可用')
  await expect(home.locator('.primary-button')).toHaveText('查看分析记录 →')

  // A failed read must not be presented as a missing or confirmed document.
  await page.route('**/api/projects/3/prd-versions', route => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'unavailable' })
  }))
  await page.reload()
  await expect(home.locator('.home-next-action')).toContainText('准备情况暂不可用')
  await expect(home.locator('.home-readiness-row').filter({ hasText: '需求文档' })).toContainText('暂不可用')
  await expect(home.locator('.primary-button')).toHaveText('重新读取准备情况')

  // A slow summary from project 3 must not overwrite project 4 after navigation.
  let releaseOldSummary, oldSummaryRequested
  const oldSummaryGate = new Promise(resolve => { releaseOldSummary = resolve })
  const oldSummaryStarted = new Promise(resolve => { oldSummaryRequested = resolve })
  await page.route('**/api/projects/3/prd-versions', async route => {
    oldSummaryRequested()
    await oldSummaryGate
    await route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify([{ id: 1, version_no: 1, status: 'parse_confirmed' }]) })
  })
  await page.route('**/api/projects/4**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/projects/4') return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ id: 4, name: '项目四读取失败', branch: 'main', git_url: 'https://example.invalid/four.git', status: 'active' }) })
    return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'unavailable' }) })
  })
  await home.locator('.primary-button').click()
  await oldSummaryStarted
  await page.evaluate(() => { window.location.hash = '#/projects/4/home' })
  await expect(home.locator('.lane-project-name')).toHaveText('项目四读取失败')
  await expect(home.locator('.primary-button')).toHaveText('重新读取准备情况')
  const oldResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/projects/3/prd-versions')
  releaseOldSummary()
  await (await oldResponse).finished()
  await page.waitForLoadState('networkidle')
  await expect.poll(() => home.locator('.home-readiness-status.ready').count()).toBe(1)
  await expect(home.locator('.home-next-action')).toContainText('准备情况暂不可用')
  await expect(home.locator('.lane-project-name')).toHaveText('项目四读取失败')

  expect(await home.evaluate(el => el.scrollWidth > el.clientWidth + 1)).toBe(false)
  expect(log.requests.filter(request => request.method !== 'GET')).toEqual([])
  expect(log.errors).toEqual([])
})


test('home routes bound all-unknown confirmed history to full analysis without changing its status', async ({ page }) => {
  const log = await openFixture(page)
  const profile = { id: 8, project_id: 3, status: 'confirmed', version_no: 4, edit_version: 1,
    content_hash: 'a'.repeat(64), content: { schema_version: 'project_profile_v2', project_summary: '合成历史记录',
      planned_modules: [{ client_id: 'm1', name: '合成功能', requirements: ['合成需求'], description: '', prd_refs: [], exclusions: [] }],
      implementation_mappings: [{ planned_module_id: 'm1', status: 'unknown', rationale: '尚待核实', evidence_refs: [] }],
      unplanned_code_features: [], domain_glossary: [], exclude_patterns: [], notes: '' } }
  await page.route('**/api/profile-candidates/8', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(profile) }))
  await page.route('**/api/projects/3/profiles', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([profileMetadata(profile)]) }))
  await page.route('**/api/projects/3/prd-versions', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 1, version_no: 1, status: 'parse_confirmed' }]) }))
  await page.route('**/api/projects/3/project-state-baseline/status', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'established', profile_id: 8, has_confirmed_baseline: true }) }))
  await page.goto('/#/projects/3/home')
  await page.reload()
  const home = page.locator('.home-overview')
  await expect(home.locator('.home-next-action')).toContainText('历史记录已确认，但所有模块的实现情况仍待核实')
  await expect(home.locator('.home-readiness-row').filter({ hasText: '当前代码分析' })).toContainText('全部待核实')
  await home.getByRole('button', { name: '核对代码范围并全量分析 →', exact: true }).click()
  await expect(page).toHaveURL(/#\/projects\/3\/modules$/)
  expect(profile.content.implementation_mappings[0].status).toBe('unknown')

  profile.content.implementation_mappings[0].status = 'partial'
  await page.goto('/#/projects/3/home')
  await page.reload()
  await expect(home.locator('.primary-button')).toContainText('开始研发分析')
  expect(log.requests.filter(request => request.method !== 'GET')).toEqual([])
  expect(log.errors).toEqual([])
})


test('home refuses failed or mismatched confirmed-profile details and permits a read-only refresh', async ({ page }) => {
  const log = await openFixture(page)
  await page.goto('/#/projects/3/home')
  const home = page.locator('.home-overview')
  const valid = { id: 8, project_id: 3, status: 'confirmed', content_hash: 'a'.repeat(64),
    content: { schema_version: 'project_profile_v2', planned_modules: [], implementation_mappings: [] } }
  let detailReads = 0
  for (const [status, body] of [
    [503, { detail: 'unavailable' }], [200, { ...valid, id: 99 }],
    [200, { ...valid, project_id: 4 }], [200, { ...valid, status: 'candidate' }],
    [200, { ...valid, content_hash: 'b'.repeat(64) }]
  ]) {
    await page.route('**/api/profile-candidates/8', route => {
      detailReads++
      expect(route.request().method()).toBe('GET')
      return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.reload()
    await expect(home.locator('.primary-button')).toHaveText('重新读取准备情况')
    await expect(home.locator('.home-readiness-row').filter({ hasText: '功能模块' })).toContainText('暂不可用')
    await expect(home.locator('.home-readiness-row').filter({ hasText: '当前代码分析' })).toContainText('暂不可用')
  }
  const before = detailReads
  await home.locator('.primary-button').click()
  await expect.poll(() => detailReads).toBe(before + 1)
  await expect(home.locator('.primary-button')).toHaveText('重新读取准备情况')
  expect(log.requests.filter(request => request.method !== 'GET')).toEqual([])
  expect(log.errors).toEqual([])
})

test('late confirmed-profile detail cannot replace a different project summary', async ({ page }) => {
  const log = await openFixture(page)
  let releaseDetail, markRequested
  const gate = new Promise(resolve => { releaseDetail = resolve })
  const requested = new Promise(resolve => { markRequested = resolve })
  await page.route('**/api/profile-candidates/8', async route => {
    markRequested()
    await gate
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      id: 8, project_id: 3, status: 'confirmed', content_hash: 'a'.repeat(64),
      content: { schema_version: 'project_profile_v2', planned_modules: [{ client_id: 'm1' }],
        implementation_mappings: [{ planned_module_id: 'm1', status: 'unknown' }] }
    }) })
  })
  await page.route('**/api/projects/4**', route => {
    if (new URL(route.request().url()).pathname === '/api/projects/4') return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ id: 4, name: '详情迟到隔离项目', branch: 'main', git_url: 'https://example.invalid/four.git', status: 'active' }) })
    return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
  })
  await page.goto('/#/projects/3/home')
  await page.reload()
  await requested
  await page.evaluate(() => { window.location.hash = '#/projects/4/home' })
  const home = page.locator('.home-overview')
  await expect(home.locator('.lane-project-name')).toHaveText('详情迟到隔离项目')
  await expect(home.locator('.primary-button')).toHaveText('重新读取准备情况')
  const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/profile-candidates/8')
  releaseDetail()
  await (await response).finished()
  await page.waitForLoadState('networkidle')
  await expect(home.locator('.lane-project-name')).toHaveText('详情迟到隔离项目')
  await expect(home.locator('.home-readiness-status.ready')).toHaveCount(1)
  await expect(home.locator('.primary-button')).toHaveText('重新读取准备情况')
  expect(log.errors).toEqual([])
})
