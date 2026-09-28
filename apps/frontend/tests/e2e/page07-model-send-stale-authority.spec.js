import { expect, test } from '@playwright/test'

const project = {
  id: 1,
  name: 'Page07 stale authority',
  status: 'active',
  version: 1,
  created_at: '2026-09-08T00:00:00Z',
  updated_at: '2026-09-08T00:00:00Z',
  git_url: 'https://github.com/example/page07-stale-authority.git',
  branch: 'main'
}

const replacement = {
  created: false,
  reanalysis_request: {
    reanalysis_request_id: 8,
    report_version_id: 3,
    replacement_local_task_id: 'report-reanalysis-3-stale'
  },
  replacement_task: {
    id: 9,
    local_task_id: 'report-reanalysis-3-stale',
    task_type: 'daily_report_regenerate',
    state: 'queued',
    evidence_snapshot_id: 11
  },
  source_report_version: {
    report_version_id: 3,
    lifecycle: 'superseded',
    state_version: 2,
    evidence_snapshot_id: 11
  }
}

function reviewBundle() {
  return {
    schema_version: 'report_review_bundle_v1',
    project_id: 1,
    report_version: {
      report_version_id: 3,
      project_id: 1,
      version_no: 1,
      lifecycle: 'superseded',
      state_version: 2,
      report_content_hash: '1'.repeat(64),
      evidence_snapshot_id: 11,
      evidence_snapshot_hash: '2'.repeat(64),
      model_execution_result_id: 17,
      execution_result_hash: '3'.repeat(64),
      model_call_id: 19,
      call_identity_hash: '4'.repeat(64),
      created_at: '2026-09-08T10:00:00Z'
    },
    ai_raw: {
      provider: 'deepseek',
      model_id: 'deepseek-flash',
      model_version: 'DeepSeek-V4.1-Flash',
      actual_model: 'deepseek-flash',
      validated_result_hash: '5'.repeat(64),
      content: { plain_summary: '等待重分析。', feature_progress: [] }
    },
    evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) },
    git_facts: {
      branch: 'main',
      from_commit: 'a'.repeat(40),
      to_commit: 'b'.repeat(40),
      git_snapshot_id: 29
    },
    evidence_refs: [],
    current_supplement: null,
    latest_validation_result: null
  }
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body)
  })
}

test('Page07 authority reload invalidates an in-flight send-scope preview before the Human gate opens', async ({ page }) => {
  let releasePreview
  const previewGate = new Promise((resolve) => { releasePreview = resolve })
  const sequence = []

  await page.route('**/*', async (route) => {
    const req = route.request()
    const pathname = new URL(req.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (req.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (req.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (req.method() === 'GET' && pathname === '/api/projects/1/report-review/current') return json(route, reviewBundle())
    if (req.method() === 'GET' && pathname === '/api/projects/1/reports/3/approval') return json(route, { approval_snapshot: null })
    if (req.method() === 'GET' && pathname === '/api/projects/1/reports/3/reanalysis') return json(route, replacement)
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis/prepare') {
      sequence.push('prepare')
      return json(route, { model_call_id: 77, preparation_state: 'prepared', provider_send_state: 'not_attempted' }, 201)
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis/send-authorization-preview') {
      sequence.push('preview')
      await previewGate
      try {
        await json(route, {
          model_call_id: 77,
          provider: 'deepseek',
          model_id: 'deepseek-flash',
          model_version: 'DeepSeek-V4.1-Flash',
          data_scope_hash: 'd'.repeat(64),
          request_envelope_hash: 'e'.repeat(64),
          call_identity_hash: 'c'.repeat(64),
          authorization_state: 'awaiting_human_confirmation',
          provider_send_state: 'not_attempted'
        })
      } catch {
        // Expected when the browser-side AbortController invalidates this stale request.
      }
      return
    }
    if (pathname.includes('/authorize-send')) sequence.push('authorize')
    if (pathname.endsWith('/execute')) sequence.push('execute')
    return json(route, { detail: { message: `unhandled ${req.method()} ${pathname}` } }, 404)
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'page07-stale-authority-session'))
  await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
  await expect(page.getByRole('heading', { name: '重分析任务等待处理', exact: true })).toBeVisible()
  expect(sequence).toEqual([])

  await page.getByRole('button', { name: '准备 AI 重分析发送范围' }).click()
  await expect.poll(() => sequence.includes('preview')).toBe(true)

  await page.getByRole('button', { name: '刷新状态' }).click()
  await expect(page.getByRole('heading', { name: '重分析任务等待处理', exact: true })).toBeVisible()
  releasePreview()

  await expect(page.getByRole('dialog', { name: '确认 AI 重分析 的发送范围' })).toHaveCount(0)
  await expect.poll(() => sequence.includes('authorize') || sequence.includes('execute')).toBe(false)
})
