import { expect, test } from '@playwright/test'

const project = {
  id: 1,
  name: 'Page07 AI 实测',
  status: 'active',
  version: 1,
  created_at: '2026-09-08T00:00:00Z',
  updated_at: '2026-09-08T00:00:00Z',
  git_url: 'https://github.com/example/page07-ai-test.git',
  branch: 'main'
}

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
}

function baseReview({ lifecycle = 'pending_review', version = 1, reportVersionId = 3, supplement = null } = {}) {
  return {
    schema_version: 'report_review_bundle_v1',
    project_id: 1,
    report_version: {
      report_version_id: reportVersionId,
      project_id: 1,
      version_no: version,
      lifecycle,
      state_version: lifecycle === 'superseded' ? 2 : 1,
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
      provider: 'deepseek', model_id: 'deepseek-flash', model_version: 'DeepSeek-V4.1-Flash',
      actual_model: 'deepseek-flash', validated_result_hash: '1'.repeat(64),
      content: { plain_summary: '当前报告等待项目经理核对。', feature_progress: [] }
    },
    evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) },
    git_facts: { branch: 'main', from_commit: 'a'.repeat(40), to_commit: 'b'.repeat(40), git_snapshot_id: 29 },
    evidence_refs: [],
    current_supplement: supplement,
    latest_validation_result: null
  }
}

async function openPage(page) {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'page07-model-send-session'))
  await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
  await expect(page.getByRole('heading', { name: '报告审阅' })).toBeVisible()
}

test('Page07 reanalysis requires visible exact scope and explicit one-shot Human confirmation', async ({ page }) => {
  let executed = false
  const sequence = []
  const replacement = {
    created: false,
    reanalysis_request: { reanalysis_request_id: 8, report_version_id: 3, replacement_local_task_id: 'report-reanalysis-3-demo' },
    replacement_task: { id: 9, local_task_id: 'report-reanalysis-3-demo', task_type: 'daily_report_regenerate', state: 'queued', evidence_snapshot_id: 11 },
    source_report_version: { report_version_id: 3, lifecycle: 'superseded', state_version: 2, evidence_snapshot_id: 11 }
  }

  await page.route('**/*', async (route) => {
    const req = route.request(); const pathname = new URL(req.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (req.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (req.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (req.method() === 'GET' && pathname === '/api/projects/1/report-review/current') {
      return json(route, executed ? baseReview({ version: 2, reportVersionId: 4 }) : baseReview({ lifecycle: 'superseded' }))
    }
    if (req.method() === 'GET' && /^\/api\/projects\/1\/reports\/\d+\/approval$/.test(pathname)) return json(route, { approval_snapshot: null })
    if (req.method() === 'GET' && pathname === '/api/projects/1/reports/3/reanalysis') return json(route, replacement)
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis/prepare') {
      sequence.push('prepare')
      return json(route, { model_call_id: 77, preparation_state: 'prepared', provider_send_state: 'not_attempted' }, 201)
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis/send-authorization-preview') {
      sequence.push('preview')
      return json(route, {
        model_call_id: 77, provider: 'deepseek', model_id: 'deepseek-flash', model_version: 'DeepSeek-V4.1-Flash',
        data_scope_hash: 'd'.repeat(64), request_envelope_hash: 'e'.repeat(64), call_identity_hash: 'c'.repeat(64),
        authorization_state: 'awaiting_human_confirmation', provider_send_state: 'not_attempted'
      })
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis/authorize-send') {
      sequence.push('authorize')
      const body = req.postDataJSON()
      expect(body).toEqual({ model_call_id: 77, data_scope_hash: 'd'.repeat(64), human_confirmed: true })
      return json(route, { ...body, authorization_state: 'authorized_once', provider_send_state: 'not_attempted' })
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis/execute') {
      sequence.push('execute')
      expect(req.postDataJSON()).toEqual({ model_call_id: 77, data_scope_hash: 'd'.repeat(64) })
      executed = true
      return json(route, { task_state: 'succeeded', replacement_report_version_id: 4, provider_retry_state: 'not_retried' }, 201)
    }
    return json(route, { detail: { message: `unhandled ${req.method()} ${pathname}` } }, 404)
  })

  await openPage(page)
  await expect(page.getByRole('heading', { name: '重分析任务等待处理', exact: true })).toBeVisible()
  expect(sequence).toEqual([])
  await page.getByRole('button', { name: '准备 AI 重分析发送范围' }).click()
  const dialog = page.getByRole('dialog', { name: '确认 AI 重分析 的发送范围' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/report-reanalysis-3-demo/)).toBeVisible()
  await expect(dialog.getByText(/deepseek-flash/)).toBeVisible()
  expect(sequence).toEqual(['prepare', 'preview'])
  await dialog.getByRole('checkbox').check()
  await dialog.getByRole('button', { name: '确认范围并只发送一次' }).click()
  await expect(page.getByText(/AI 重分析已完成并形成新的报告版本/)).toBeVisible()
  expect(sequence).toEqual(['prepare', 'preview', 'authorize', 'execute'])
})

test('Page07 contradiction check also requires the same visible one-shot send gate', async ({ page }) => {
  const sequence = []
  const supplement = {
    supplement_version_id: 31,
    version_no: 2,
    content_hash: '6'.repeat(64),
    content: '支付模块仍在等待测试确认。',
    source_type: 'pm_external_fact',
    provided_by: '张经理',
    provided_at: '2026-09-08T11:00:00Z'
  }
  await page.route('**/*', async (route) => {
    const req = route.request(); const pathname = new URL(req.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (req.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (req.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (req.method() === 'GET' && pathname === '/api/projects/1/report-review/current') return json(route, baseReview({ supplement }))
    if (req.method() === 'GET' && pathname === '/api/projects/1/reports/3/approval') return json(route, { approval_snapshot: null })
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/contradiction/prepare') {
      sequence.push('prepare')
      return json(route, { model_call_id: 88, preparation_state: 'prepared', provider_send_state: 'not_attempted' }, 201)
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/contradiction/send-authorization-preview') {
      sequence.push('preview')
      return json(route, {
        model_call_id: 88, provider: 'deepseek', model_id: 'deepseek-flash', model_version: 'DeepSeek-V4.1-Flash',
        data_scope_hash: '7'.repeat(64), request_envelope_hash: '8'.repeat(64), call_identity_hash: '9'.repeat(64),
        authorization_state: 'awaiting_human_confirmation', provider_send_state: 'not_attempted'
      })
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/contradiction/authorize-send') {
      sequence.push('authorize')
      const body = req.postDataJSON()
      expect(body).toEqual({ model_call_id: 88, data_scope_hash: '7'.repeat(64), human_confirmed: true })
      return json(route, { ...body, authorization_state: 'authorized_once', provider_send_state: 'not_attempted' })
    }
    if (req.method() === 'POST' && pathname === '/api/projects/1/reports/3/contradiction/execute') {
      sequence.push('execute')
      expect(req.postDataJSON()).toEqual({ model_call_id: 88, data_scope_hash: '7'.repeat(64) })
      return json(route, { model_result_id: 90, task_type: 'report_contradiction_check' }, 201)
    }
    return json(route, { detail: { message: `unhandled ${req.method()} ${pathname}` } }, 404)
  })

  await openPage(page)
  await page.getByRole('button', { name: '可选：让 AI 检查补充冲突' }).click()
  const dialog = page.getByRole('dialog', { name: '确认 AI 冲突检查 的发送范围' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/当前人工补充 V2/)).toBeVisible()
  expect(sequence).toEqual(['prepare', 'preview'])
  await dialog.getByRole('checkbox').check()
  await dialog.getByRole('button', { name: '确认范围并只发送一次' }).click()
  await expect(page.getByText(/AI 冲突检查已完成并持久化验证结果/)).toBeVisible()
  expect(sequence).toEqual(['prepare', 'preview', 'authorize', 'execute'])
})
