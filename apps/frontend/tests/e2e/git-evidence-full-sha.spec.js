import { expect, test } from '@playwright/test'

const project = {
  id: 1,
  name: '研发进度 AI Agent',
  status: 'active',
  version: 4,
  git_url: 'https://github.com/example/rd-agent.git',
  branch: 'main'
}

const prd = {
  id: 11,
  project_id: 1,
  version_no: 3,
  status: 'parse_confirmed',
  preview: '已确认 PRD'
}

const profile = {
  id: 21,
  project_id: 1,
  version_no: 2,
  status: 'confirmed',
  content: { modules: [] }
}

const lineage = {
  id: 77,
  project_id: 1,
  branch: 'main',
  baseline_commit: '1111111111111111111111111111111111111111',
  sequence_no: 3
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

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body)
  })
}

test('Git diagnostics keep full from/to/commit SHA available while the first layer stays short', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()

    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/git/status') {
      return json(route, { status: 'connected', remote_head: candidate.remote_head, last_checked_at: '2026-09-05T09:25:00Z' })
    }
    if (request.method() === 'GET' && pathname === '/api/projects/1/analysis-lineage') return json(route, { status: 'active', lineage })
    if (request.method() === 'GET' && pathname === '/api/projects/1/prd-versions') return json(route, [prd])
    if (request.method() === 'GET' && pathname === '/api/projects/1/profiles') return json(route, [profile])
    if (request.method() === 'POST' && pathname === '/api/projects/1/git/range-candidate') return json(route, { status: 'continuous', candidate })

    return json(route, { detail: { message: 'Focused full-SHA fixture does not provide this request.' } }, 404)
  })

  await page.goto('/#/projects/1/git', { waitUntil: 'networkidle' })

  const firstLayer = page.locator('.commits-card')
  await expect(firstLayer.getByText('22222222', { exact: true })).toBeVisible()
  await expect(firstLayer.getByText(candidate.commits[0], { exact: true })).toHaveCount(0)

  const details = page.locator('.technical-details')
  await details.locator('summary').click()
  const technicalGrid = details.locator('.technical-grid')
  const fullCommitList = details.getByLabel('本轮完整提交 SHA')
  await expect(technicalGrid.getByText(lineage.baseline_commit, { exact: true })).toBeVisible()
  await expect(technicalGrid.getByText(candidate.remote_head, { exact: true })).toBeVisible()
  for (const commit of candidate.commits) {
    await expect(fullCommitList.getByText(commit, { exact: true })).toBeVisible()
  }
})
