// @ts-check
import { test, expect } from '@playwright/test'

const project = {
  id: 1,
  name: '甲方演示项目',
  status: 'draft',
  version: 1,
  git_url: 'https://example.invalid/demo.git',
  branch: 'main',
  created_at: '2026-08-21T00:00:00Z',
  updated_at: '2026-08-21T00:00:00Z'
}

const formalStages = new Set([
  '开发中',
  '等待联调',
  '等待测试',
  '测试中',
  '已完成',
  '暂时无法确认'
])

const stageRows = [
  ['local_app', 'Windows 本地软件', '开发中', 'active'],
  ['project_management', '项目管理', '开发中', 'active'],
  ['git', '代码版本读取', '已完成', 'done'],
  ['prd', '需求文档读取', '已完成', 'done'],
  ['project_profile', '项目基础资料', '开发中', 'active'],
  ['anxin_board', '安心看板自动生成', '测试中', 'checking'],
  ['ai_correction', 'AI 结果纠正', '开发中', 'active'],
  ['formal_report', '正式 AI 报告', '等待测试', 'waiting'],
  ['email', '邮件发送', '暂时无法确认', 'unknown'],
  ['report_evidence', '报告依据与留痕', '开发中', 'active'],
  ['history', '历史报告查看', '开发中', 'active']
]

const formalReport = {
  schema_version: 'anxin_board_report_v1',
  title: '安心看板',
  motto: '非己所安，不加于物',
  project_name: '甲方演示项目',
  report_date: '2026-08-21',
  module_count: 11,
  completed_module_count: 2,
  active_module_count: 8,
  unknown_module_count: 1,
  overall_message: '当前有 8 项工作正在推进或等待后续检查；有 2 项已经完成当前约定范围内的开发和检查；另有 1 项暂时缺少足够依据，暂不下结论。',
  modules: stageRows.map(([module_id, name, stage, tone], index) => ({
    module_id,
    name,
    stage,
    display_stage: stage,
    tone,
    summary: `这是第 ${index + 1} 项功能当前可以确认的状态说明。`,
    next_step: `继续处理第 ${index + 1} 项功能的下一步工作。`,
    client_stage_summary_hash: 'a'.repeat(64)
  })),
  daily_change: {
    display_state: 'ready',
    headline: '今天项目有明确进展。',
    summary: '今天完成了可核实的研发变化，并已经整理成甲方可以直接阅读的说明。',
    highlights: [
      { label: '今天完成的关键工作', value: 4, unit: '项' },
      { label: '今天有变化的功能', value: 6, unit: '项' },
      { label: '进入检查的功能', value: 2, unit: '项' },
      { label: '当前继续推进的工作', value: 8, unit: '项' }
    ],
    scope_note: '这些数字只说明今天确实发生了多少研发变化，不代表项目完成度，也不用于评价个人绩效。',
    plain_language_change_summary_hash: 'b'.repeat(64)
  },
  manager_supplement: '同意大模型的日报。',
  anxin_board_report_hash: 'c'.repeat(64)
}

function makeReport({
  reportDate = '2026-08-20',
  supplement = '历史版本补充。',
  hash = 'd'.repeat(64)
} = {}) {
  return {
    ...formalReport,
    report_date: reportDate,
    manager_supplement: supplement,
    anxin_board_report_hash: hash
  }
}

function makeV2Report(moduleCount, {
  reportDate = '2026-08-22',
  supplement = 'V2 项目经理补充。',
  hash = '9'.repeat(64)
} = {}) {
  const modules = Array.from({ length: moduleCount }, (_, index) => ({
    module_id: `profile_module_${index + 1}`,
    name: `确认模块 ${index + 1}`,
    stage: '开发中',
    display_stage: '开发中',
    tone: 'active',
    summary: `确认模块 ${index + 1} 正在按计划推进。`,
    next_step: `继续确认模块 ${index + 1} 的下一步。`,
    client_stage_summary_hash: 'a'.repeat(64)
  }))
  return {
    schema_version: 'anxin_board_report_v2',
    title: '安心看板',
    motto: '非己所安，不加于物',
    project_name: '甲方演示项目',
    report_date: reportDate,
    profile_id: 31,
    profile_version_no: 4,
    profile_content_hash: '8'.repeat(64),
    source_prd_id: 17,
    module_count: moduleCount,
    completed_module_count: 0,
    active_module_count: moduleCount,
    unknown_module_count: 0,
    overall_message: `当前有 ${moduleCount} 项工作正在推进或等待后续检查。`,
    modules,
    daily_change: structuredClone(formalReport.daily_change),
    manager_supplement: supplement,
    anxin_board_report_hash: hash
  }
}

function historyRecord(id, report, createdAt) {
  return {
    id,
    project_id: 1,
    schema_version: report.schema_version,
    report_date: report.report_date,
    report_hash: report.anxin_board_report_hash,
    created_at: createdAt,
    report
  }
}

async function routeProject(page) {
  await page.route('**/api/**', route => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    const reads = {
      '/api/projects/1/prd-versions': [], '/api/projects/1/profiles': [],
      '/api/projects/1/project-state-baseline/status': { status: 'missing', has_confirmed_baseline: false }
    }
    if (route.request().method() === 'GET' && Object.hasOwn(reads, pathname)) return route.fulfill({ status: 200, json: reads[pathname] })
    return route.fulfill({ status: 404, json: { detail: { code: 'SYNTHETIC_ENDPOINT_NOT_PROVIDED' } } })
  })
  // Narrow ancillary GET fixtures: unknown business requests are not accepted.
  await page.route('**/api/settings/mail-transport', route => route.request().method() === 'GET'
    ? route.fulfill({ status: 200, json: { configured: false, profile_version: 0 } }) : route.abort())
  await page.route('**/api/projects/1/recipient-config', route => route.request().method() === 'GET'
    ? route.fulfill({ status: 200, json: { configured: false, version_no: 0, to_recipients: [] } }) : route.abort())
  await page.route('**/api/projects/1', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(project)
    })
  })
}

async function routeReport(page, { status = 200, body = formalReport } = {}) {
  await page.route('**/api/projects/1/anxin-board/latest', async (route) => {
    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body)
    })
  })
}

test('历史报告保留书法与模块说明且缺少Git统计时不编造数字', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await routeProject(page)
  await routeReport(page)
  await page.goto('/#/projects/1/board')

  const board = page.getByTestId('anxin-board')
  await expect(board).toBeVisible()
  await expect(board.getByRole('heading', { name: '安心看板', exact: true })).toBeVisible()
  await expect(board.getByAltText('非己所安，不加于物')).toBeVisible()
  await expect(board.getByText('甲方演示项目', { exact: true }).first()).toBeVisible()
  await expect(board.getByText('报告日期：2026-08-21')).toBeVisible()
  await expect(board.getByText('本次代码变化')).toBeVisible()
  await expect(board.getByTestId('git-metrics-absent')).toBeVisible()
  await expect(board.getByTestId('analysis-model')).toContainText('本版未记录')
  await expect(board.getByTestId('analysis-model-version')).toHaveCount(0)
  await expect(board.getByText('今天项目有明确进展。')).toBeVisible()
  await expect(board.getByText('同意大模型的日报。')).toBeVisible()
  await expect(board.getByText('这些数字只说明今天确实发生了多少研发变化，不代表项目完成度，也不用于评价个人绩效。')).toHaveCount(1)
  await expect(board.getByText('这些数字只说明今天确实发生了多少研发变化，不代表项目完成度，也不用于评价个人绩效。')).toBeHidden()

  await expect(board.getByTestId('anxin-board-module-metrics')).toHaveCount(0)
  await expect(board.getByTestId('anxin-board-git-metrics')).toHaveCount(0)
  for (const section of ['01 本次工作与进展', '02 当前功能状态', '03 项目经理补充与下一步', '04 报告信息']) {
    await expect(board.getByRole('heading', { name: section, exact: true })).toBeVisible()
  }
  for (const phrase of ['简洁书法版', 'ApprovalSnapshot', 'EvidenceSnapshot', '模型厂商', 'Token', 'V1/V2']) {
    expect(await board.innerText()).not.toContain(phrase)
  }
  for (const index of [3, 4]) {
    await expect(board.getByText(`这是第 ${index} 项功能当前可以确认的状态说明。`, { exact: true })).toBeVisible()
  }

  const displayedStages = await board.locator('.status').allTextContents()
  expect(displayedStages).toHaveLength(11)
  for (const stage of displayedStages) {
    expect(formalStages.has(stage)).toBeTruthy()
  }

  await page.screenshot({ path: testInfo.outputPath('customer-board-desktop.png'), fullPage: false })
  const visibleText = await board.innerText()
  expect(visibleText).not.toContain('开发样本')
  expect(visibleText).not.toContain('约 1,600')
  expect(visibleText).not.toContain(formalReport.anxin_board_report_hash)
  expect(visibleText).not.toContain('完成当前约定范围内的开发和检查')
  for (const forbidden of ['SHA', 'Framing', 'Pull Request', 'commit hash']) {
    expect(visibleText).not.toContain(forbidden)
  }
})

test('旧通用完成说明不冒充具体进展而保留具体事实', async ({ page }) => {
  await routeProject(page)
  const body = structuredClone(formalReport)
  body.modules[2].summary = '当前约定范围已完成。'
  body.modules[3].summary = '这项工作已经完成当前约定范围内的开发和检查，可以作为已完成内容展示。'
  await routeReport(page, { body })
  await page.goto('/#/projects/1/board')
  const board = page.getByTestId('anxin-board')
  await expect(board.getByText('约定功能已完成开发。', { exact: true })).toHaveCount(0)
  await expect(board.getByText(formalReport.modules[0].summary, { exact: true })).toBeVisible()
  await expect(board.getByText(body.modules[3].summary, { exact: true })).toHaveCount(0)
})

test('正式报告尚未生成时明确显示空状态且不回退到演示数据', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await routeProject(page)
  await routeReport(page, {
    status: 404,
    body: {
      detail: {
        code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE',
        message: '当前还没有可展示的正式安心看板'
      }
    }
  })
  await page.goto('/#/projects/1/board')

  const empty = page.getByTestId('anxin-board-empty')
  await expect(empty).toBeVisible()
  await expect(empty.getByText('当前还没有可展示的正式安心看板。')).toBeVisible()
  await expect(empty.getByRole('button', { name: '前往项目工作台' })).toBeVisible()
  await expect(page.getByTestId('anxin-board')).toHaveCount(0)
  await expect(page.getByTestId('anxin-board-template-preview')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('customer-board-empty-mobile.png'), fullPage: false })
  await empty.getByRole('button', { name: '前往项目工作台' }).click()
  await expect(page).toHaveURL(/#\/projects\/1$/)
  await expect(page.getByText('约 1,600')).toHaveCount(0)
})

test('正式报告结构异常时 fail closed 不渲染看板', async ({ page }) => {
  await routeProject(page)
  await routeReport(page, {
    body: { ...formalReport, schema_version: 'unexpected_schema' }
  })
  await page.goto('/#/projects/1/board')

  await expect(page.getByText('安心看板数据无法确认，请稍后重试。')).toBeVisible()
  await expect(page.getByTestId('anxin-board')).toHaveCount(0)
})

test('侧栏可进入安心看板并可返回项目工作台', async ({ page }) => {
  await routeProject(page)
  await routeReport(page)
  await page.goto('/#/projects/1')
  await page
    .getByRole('navigation', { name: '项目导航' })
    .getByRole('button', { name: '安心看板', exact: true })
    .click()
  await expect(page).toHaveURL(/#\/projects\/1\/board$/)
  await expect(page.getByTestId('anxin-board')).toBeVisible()

  await page.getByRole('button', { name: '← 返回项目工作台' }).click()
  await expect(page).toHaveURL(/#\/projects\/1$/)
})

test('历史只在显式点击后读取，首屏保留 latest 且 first page 仅带 limit=10', async ({ page }) => {
  const historyRequests = []
  const older = makeReport()
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    historyRequests.push({ method: route.request().method(), url: route.request().url() })
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        historyRecord(10, older, '2026-08-20T10:00:00+00:00')
      ])
    })
  })

  await page.goto('/#/projects/1/board')
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
  expect(historyRequests).toHaveLength(0)

  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-list')).toBeVisible()
  expect(historyRequests).toHaveLength(1)
  expect(historyRequests[0].method).toBe('GET')
  const firstUrl = new URL(historyRequests[0].url)
  expect(firstUrl.searchParams.get('limit')).toBe('10')
  expect(firstUrl.searchParams.has('before_id')).toBeFalsy()
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
})

test('历史 loading 可稳定观察且不会清空或伪装当前 latest', async ({ page }) => {
  let releaseHistory
  const gate = new Promise((resolve) => {
    releaseHistory = resolve
  })
  const older = makeReport()
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await gate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        historyRecord(9, older, '2026-08-20T09:00:00+00:00')
      ])
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-loading')).toHaveText('正在加载历史版本...')
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
  await expect(page.getByText('历史正式报告', { exact: true })).toHaveCount(0)

  releaseHistory()
  await expect(page.getByTestId('history-list')).toBeVisible()
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
})

test('同日修订不去重，选择历史版本把 exact record.report 送入同一 renderer', async ({ page }) => {
  const revisionA = makeReport({
    reportDate: '2026-08-20',
    supplement: '同日较新修订。',
    hash: 'd'.repeat(64)
  })
  const revisionB = makeReport({
    reportDate: '2026-08-20',
    supplement: '同日较早修订。',
    hash: 'e'.repeat(64)
  })
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        historyRecord(12, revisionA, '2026-08-20T12:00:00+00:00'),
        historyRecord(11, revisionB, '2026-08-20T11:00:00+00:00')
      ])
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  const list = page.getByTestId('history-list')
  await expect(list.getByRole('button')).toHaveCount(2)
  await expect(list).toContainText('修订 #12')
  await expect(list).toContainText('修订 #11')

  await list.getByRole('button', { name: /修订 #11/ }).click()
  const board = page.getByTestId('anxin-board')
  await expect(board).toContainText('同日较早修订。')
  await expect(board).toContainText('报告日期：2026-08-20')
  await expect(page.getByText('历史正式报告', { exact: true })).toBeVisible()
  await expect(page.locator('[data-testid="anxin-board"]')).toHaveCount(1)
})

test('更早一页使用末条 id exclusive cursor，上一页使用内存 cursor stack', async ({ page }) => {
  const seen = []
  const firstA = makeReport({ supplement: '第一页 A。', hash: 'd'.repeat(64) })
  const firstB = makeReport({ supplement: '第一页 B。', hash: 'e'.repeat(64) })
  const secondA = makeReport({ reportDate: '2026-08-19', supplement: '第二页 A。', hash: 'f'.repeat(64) })
  const secondB = makeReport({ reportDate: '2026-08-19', supplement: '第二页 B。', hash: '1'.repeat(64) })

  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    const url = new URL(route.request().url())
    seen.push(url.search)
    const beforeId = url.searchParams.get('before_id')
    const body = beforeId === '11'
      ? [
          historyRecord(10, secondA, '2026-08-19T10:00:00+00:00'),
          historyRecord(9, secondB, '2026-08-19T09:00:00+00:00')
        ]
      : [
          historyRecord(12, firstA, '2026-08-20T12:00:00+00:00'),
          historyRecord(11, firstB, '2026-08-20T11:00:00+00:00')
        ]
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-list')).toContainText('修订 #11')
  await page.getByRole('button', { name: '更早一页' }).click()
  await expect(page.getByTestId('history-list')).toContainText('修订 #10')
  expect(new URLSearchParams(seen[1]).get('before_id')).toBe('11')
  expect(new URLSearchParams(seen[1]).get('limit')).toBe('10')

  await page.getByRole('button', { name: '返回上一页' }).click()
  await expect(page.getByTestId('history-list')).toContainText('修订 #12')
  expect(new URLSearchParams(seen[2]).has('before_id')).toBeFalsy()
  expect(new URLSearchParams(seen[2]).get('limit')).toBe('10')
})

test('返回最新版本恢复原 latest 报告并退出历史模式', async ({ page }) => {
  const older = makeReport({ supplement: '历史选中内容。' })
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([historyRecord(8, older, '2026-08-20T08:00:00+00:00')])
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await page.getByRole('button', { name: /修订 #8/ }).click()
  await expect(page.getByTestId('anxin-board')).toContainText('历史选中内容。')

  await page.getByRole('button', { name: '返回最新版本' }).click()
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
  await expect(page.getByRole('button', { name: '查看历史版本' })).toBeVisible()
  await expect(page.getByTestId('history-list')).toHaveCount(0)
})

test('history 200 空页明确显示无更早版本且 latest 保持可见', async ({ page }) => {
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-empty')).toHaveText('暂无更早历史版本')
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
})

test('history 5xx fail closed 且不会覆盖当前 latest', async ({ page }) => {
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: { code: 'ANXIN_BOARD_REPORT_STORED_INVALID' } })
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-error')).toHaveText('历史报告数据无法确认')
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
  await expect(page.getByTestId('history-list')).toHaveCount(0)
})

test('任一历史 record 绑定或 report 非法时整页 fail closed，不显示部分可信列表', async ({ page }) => {
  const valid = makeReport({ supplement: '本来有效的历史项。', hash: 'd'.repeat(64) })
  const invalid = makeReport({ supplement: '损坏项。', hash: 'e'.repeat(64) })
  const badRecord = historyRecord(6, invalid, '2026-08-20T06:00:00+00:00')
  badRecord.report_hash = 'f'.repeat(64)

  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        historyRecord(7, valid, '2026-08-20T07:00:00+00:00'),
        badRecord
      ])
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-error')).toHaveText('历史报告数据无法确认')
  await expect(page.getByTestId('history-list')).toHaveCount(0)
  await expect(page.getByText('本来有效的历史项。')).toHaveCount(0)
  await expect(page.getByTestId('anxin-board')).toContainText('同意大模型的日报。')
})

test('历史浏览没有时间轴、搜索、过滤、对比或写请求入口', async ({ page }) => {
  const older = makeReport()
  const methods = []
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    methods.push(route.request().method())
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([historyRecord(5, older, '2026-08-20T05:00:00+00:00')])
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-list')).toBeVisible()
  expect(methods).toEqual(['GET'])
  const text = await page.getByTestId('anxin-board-history-controls').innerText()
  for (const forbidden of ['时间轴', '搜索', '筛选', '对比', '编辑']) {
    expect(text).not.toContain(forbidden)
  }
})

for (const moduleCount of [3, 17]) {
  test(`V2 confirmed Profile ${moduleCount} modules renders exactly ${moduleCount}`, async ({ page }) => {
    const v2 = makeV2Report(moduleCount)
    await routeProject(page)
    await routeReport(page, { body: v2 })
    await page.goto('/#/projects/1/board')

    const board = page.getByTestId('anxin-board')
    await expect(board).toBeVisible()
    await expect(board.locator('tbody tr')).toHaveCount(moduleCount)
    await expect(board.locator('.status')).toHaveCount(moduleCount)
    await expect(board.getByText(`确认模块 ${moduleCount}`, { exact: true })).toBeVisible()
    await expect(board.getByText('Windows 本地软件')).toHaveCount(0)
  })
}

test('V2 module count or identity shape mismatch fails closed instead of falling back to fixed 11', async ({ page }) => {
  const invalid = makeV2Report(3)
  invalid.module_count = 11
  await routeProject(page)
  await routeReport(page, { body: invalid })
  await page.goto('/#/projects/1/board')

  await expect(page.getByText('安心看板数据无法确认，请稍后重试。')).toBeVisible()
  await expect(page.getByTestId('anxin-board')).toHaveCount(0)
})

test('mixed V1 V2 history dispatches by exact schema and one renderer shows V2 dynamic modules', async ({ page }) => {
  const v2 = makeV2Report(3, { supplement: '历史 V2 三模块。', hash: '7'.repeat(64) })
  const v1 = makeReport({ supplement: '历史 V1 十一模块。', hash: '6'.repeat(64) })
  await routeProject(page)
  await routeReport(page)
  await page.route('**/api/projects/1/anxin-board/history?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        historyRecord(22, v2, '2026-08-22T12:00:00+00:00'),
        historyRecord(21, v1, '2026-08-20T11:00:00+00:00')
      ])
    })
  })

  await page.goto('/#/projects/1/board')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('history-list').getByRole('button')).toHaveCount(2)
  await page.getByRole('button', { name: /修订 #22/ }).click()
  const board = page.getByTestId('anxin-board')
  await expect(board).toContainText('历史 V2 三模块。')
  await expect(board.locator('tbody tr')).toHaveCount(3)
})
