import { expect, test } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const project = {
  id: 1,
  name: '研发进度 AI Agent',
  status: 'active',
  version: 4,
  created_at: '2026-09-01T09:00:00Z',
  updated_at: '2026-09-05T09:30:00Z',
  git_url: 'https://github.com/example/rd-agent.git',
  branch: 'main'
}

const prd = {
  id: 11,
  project_id: 1,
  version_no: 3,
  original_filename: '研发进度AI_Agent_PRD_V0.5.docx',
  size_bytes: 248320,
  status: 'parse_confirmed',
  source_hash: 'a'.repeat(64),
  parser_version: 'prd-parser-v1',
  preview: '目标：让项目经理快速看懂当前研发进度、风险与下一步。\n\n本版本已完成解析并经过人工确认。',
  preview_truncated: false,
  created_at: '2026-09-04T08:00:00Z'
}

const profile = {
  id: 21,
  project_id: 1,
  version_no: 2,
  status: 'confirmed',
  edit_version: 5,
  confirmed_at: '2026-09-04T09:00:00Z',
  updated_at: '2026-09-04T09:00:00Z',
  created_at: '2026-09-04T08:30:00Z',
  content: {
    project_summary: '研发进度 AI Agent 项目经理工作台',
    domain_glossary: [],
    modules: [
      { client_id: 'module-1', name: '研发分析', description: '分析冻结研发证据并生成报告。', prd_refs: [], requirements: [], exclusions: [], paths: [{ type: 'frontend', pattern: 'apps/frontend/**', required: true }] },
      { client_id: 'module-2', name: '报告审阅', description: '项目经理核对报告并完成最终确认。', prd_refs: [], requirements: [], exclusions: [], paths: [{ type: 'backend', pattern: 'apps/backend/**', required: true }] }
    ]
  }
}

const lineage = {
  id: 77,
  project_id: 1,
  branch: 'main',
  baseline_commit: '1111111111111111111111111111111111111111',
  sequence_no: 3,
  source_git_checked_at: '2026-09-05T09:25:00Z',
  break_reason: null
}

const candidate = {
  continuity: 'continuous',
  capacity: 'within_capacity',
  baseline_commit: lineage.baseline_commit,
  remote_head: '4444444444444444444444444444444444444444',
  commit_count: 3,
  changed_file_count: 8,
  diff_bytes: 24576,
  commits: [
    '2222222222222222222222222222222222222222',
    '3333333333333333333333333333333333333333',
    '4444444444444444444444444444444444444444'
  ]
}

const history = [
  { id: 31, schema_version: 'anxin_board_report_v3', report_date: '2026-09-05', report_hash: 'f'.repeat(64), created_at: '2026-09-05T09:40:00Z', report: { git_range: '11111111…44444444' } },
  { id: 30, schema_version: 'anxin_board_report_v3', report_date: '2026-09-04', report_hash: 'e'.repeat(64), created_at: '2026-09-04T18:10:00Z', report: { git_range: '00000000…11111111' } }
]

const fixture = {
  projects: [project],
  prdVersions: [prd],
  profileVersions: [profile],
  rangeResponse: { status: 'continuous', candidate },
  historyRecords: history,
  historyStatus: 200,
  healthBody: { status: 'ok' },
  healthStatus: 200
}

const viewports = [
  ['1920x1080', 1920, 1080],
  ['1440x900', 1440, 900],
  ['1366x768', 1366, 768],
  ['390x844', 390, 844]
]

const routes = [
  ['projects', '/#/projects'],
  ['home', '/#/projects/1/home'],
  ['setup', '/#/projects/1/setup'],
  ['modules', '/#/projects/1/modules'],
  ['git', '/#/projects/1/git'],
  ['history', '/#/projects/1/history'],
  ['settings', '/#/projects/1/settings']
]

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
}

function resetFixture() {
  fixture.projects = [project]
  fixture.prdVersions = [prd]
  fixture.profileVersions = [profile]
  fixture.rangeResponse = { status: 'continuous', candidate }
  fixture.historyRecords = history
  fixture.historyStatus = 200
  fixture.healthBody = { status: 'ok' }
  fixture.healthStatus = 200
}

async function mockCoreApi(page) {
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname
    if (!pathname.startsWith('/api/')) return route.continue()

    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, fixture.projects)
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/prd-versions') return json(route, fixture.prdVersions)
    if (request.method() === 'GET' && pathname === '/api/prd-versions/11') return json(route, fixture.prdVersions[0] || {}, fixture.prdVersions.length ? 200 : 404)
    if (request.method() === 'GET' && pathname === '/api/projects/1/profiles') return json(route, fixture.profileVersions)
    if (request.method() === 'GET' && pathname === '/api/profile-candidates/21') return json(route, fixture.profileVersions[0] || {}, fixture.profileVersions.length ? 200 : 404)
    if (request.method() === 'GET' && pathname === '/api/projects/1/git/status') return json(route, { status: 'connected', remote_head: candidate.remote_head, last_checked_at: '2026-09-05T09:25:00Z' })
    if (request.method() === 'GET' && pathname === '/api/projects/1/analysis-lineage') return json(route, { status: 'active', lineage })
    if (request.method() === 'POST' && pathname === '/api/projects/1/git/range-candidate') return json(route, fixture.rangeResponse)
    if (request.method() === 'GET' && pathname === '/api/projects/1/anxin-board/history') return json(route, fixture.historyStatus === 200 ? fixture.historyRecords : { detail: { message: '历史服务暂时不可用' } }, fixture.historyStatus)
    if (request.method() === 'GET' && pathname === '/api/health') return json(route, fixture.healthBody, fixture.healthStatus)

    return json(route, { detail: { message: 'Lane B visual fixture does not provide this request.' } }, 404)
  })
}

async function capture(page, outDir, captures, label, fileName) {
  await expect(page.locator('.app-shell')).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  const file = path.join(outDir, fileName)
  await page.screenshot({ path: file, fullPage: true, animations: 'disabled' })
  captures.push({ file, label })
}

async function writeContactSheet(page, outDir, captures) {
  const tiles = []
  for (const capture of captures) {
    const bytes = await fs.readFile(capture.file)
    tiles.push(`<figure><figcaption>${capture.label}</figcaption><img src="data:image/png;base64,${bytes.toString('base64')}" /></figure>`)
  }
  await page.setViewportSize({ width: 1600, height: 1200 })
  await page.setContent(`<!doctype html><meta charset="utf-8"><style>body{margin:0;padding:20px;font-family:Arial,sans-serif;background:#eef1f5;color:#172033}h1{margin:0 0 16px;font-size:24px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}figure{margin:0;padding:8px;background:#fff;border:1px solid #dfe4ec;border-radius:10px}figcaption{padding:4px 2px 8px;font-size:12px;font-weight:700}img{display:block;width:100%;height:360px;object-fit:contain;object-position:top;background:#f8fafc;border:1px solid #edf0f4}</style><h1>Lane B · Core Pages · exact-head visual matrix</h1><div class="grid">${tiles.join('')}</div>`)
  await page.screenshot({ path: path.join(outDir, 'lane-b-contact-sheet.png'), fullPage: true, animations: 'disabled' })
}

test('Lane B core pages stay task-first across required viewports and required states', async ({ page }) => {
  await mockCoreApi(page)
  const outDir = path.resolve('test-results/lane-b-core-pages')
  await fs.mkdir(outDir, { recursive: true })
  const captures = []

  resetFixture()
  for (const [viewportName, width, height] of viewports) {
    await page.setViewportSize({ width, height })
    for (const [pageName, route] of routes) {
      await page.goto(route, { waitUntil: 'networkidle' })
      await capture(page, outDir, captures, `${viewportName} · ${pageName} · normal`, `${viewportName}-${pageName}.png`)
    }
  }

  await page.setViewportSize({ width: 1440, height: 900 })

  fixture.projects = []
  await page.goto('/#/projects', { waitUntil: 'networkidle' })
  await expect(page.getByRole('heading', { name: '从一个真实项目开始' })).toBeVisible()
  await capture(page, outDir, captures, '1440x900 · project list · empty first run', 'state-projects-empty.png')
  resetFixture()

  fixture.prdVersions = [{ ...prd, status: 'parsed', preview: '这是尚待人工确认的 PRD 解析结果。' }]
  await page.goto('/#/projects/1/setup', { waitUntil: 'networkidle' })
  await expect(page.getByText('需要人工确认解析结果')).toBeVisible()
  await capture(page, outDir, captures, '1440x900 · setup · before PRD confirmation', 'state-setup-prd-pending.png')
  resetFixture()

  fixture.profileVersions = [{ ...profile, status: 'candidate', version_no: 3, confirmed_at: null }]
  await page.goto('/#/projects/1/modules', { waitUntil: 'networkidle' })
  await capture(page, outDir, captures, '1440x900 · modules · candidate', 'state-modules-candidate.png')
  resetFixture()

  fixture.rangeResponse = { status: 'no_new_commit', candidate: { ...candidate, continuity: 'no_new_commit', commit_count: 0, changed_file_count: 0, commits: [] } }
  await page.goto('/#/projects/1/git', { waitUntil: 'networkidle' })
  await expect(page.getByLabel('本次研发范围摘要').getByText('没有新提交', { exact: true })).toBeVisible()
  await capture(page, outDir, captures, '1440x900 · Git · no new commit', 'state-git-no-new-commit.png')

  fixture.rangeResponse = { status: 'checkpoint_unreachable', candidate: { ...candidate, continuity: 'checkpoint_unreachable' } }
  await page.reload({ waitUntil: 'networkidle' })
  await expect(page.getByLabel('本次研发范围摘要').getByText('历史发生变化', { exact: true })).toBeVisible()
  await capture(page, outDir, captures, '1440x900 · Git · history changed / blocked', 'state-git-history-changed.png')
  resetFixture()

  fixture.historyRecords = []
  await page.goto('/#/projects/1/history', { waitUntil: 'networkidle' })
  await expect(page.getByText('当前没有可读取的报告历史。')).toBeVisible()
  await capture(page, outDir, captures, '1440x900 · history · empty', 'state-history-empty.png')

  fixture.historyStatus = 500
  await page.reload({ waitUntil: 'networkidle' })
  await expect(page.getByText('历史服务暂时不可用')).toBeVisible()
  await capture(page, outDir, captures, '1440x900 · history · error', 'state-history-error.png')
  resetFixture()

  fixture.healthStatus = 503
  fixture.healthBody = { detail: { message: '本地服务状态无法确认' } }
  await page.goto('/#/projects/1/settings', { waitUntil: 'networkidle' })
  await capture(page, outDir, captures, '1440x900 · settings · local service error', 'state-settings-error.png')
  resetFixture()

  await page.goto('/#/projects/1/home', { waitUntil: 'networkidle' })
  await expect(page.getByText('当前待处理 1', { exact: true })).toHaveCount(0)
  await expect(page.getByText('邮件暂未开放', { exact: true })).toHaveCount(0)
  await expect(page.getByText('AI 模型暂未开放', { exact: true })).toHaveCount(0)

  await writeContactSheet(page, outDir, captures)
})
