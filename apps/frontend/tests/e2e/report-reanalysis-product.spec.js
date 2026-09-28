import { expect, test } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const project = {
  id: 1,
  name: '真实项目验收',
  status: 'active',
  version: 1,
  created_at: '2026-09-03T00:00:00Z',
  updated_at: '2026-09-03T00:00:00Z',
  git_url: 'https://github.com/example/real-project.git',
  branch: 'main'
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body)
  })
}

test('Page07 reanalysis keeps old report, queues replacement, and never calls AI on submit', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'reanalyze-test-session')
  })

  let superseded = false
  let reanalysisPosts = 0
  const writeRequests = []
  let providerExecutionCalls = 0
  let reanalysisBundle = null

  const reviewBody = () => ({
    schema_version: 'report_review_bundle_v1',
    project_id: 1,
    report_version: {
      report_version_id: 3,
      project_id: 1,
      version_no: 1,
      lifecycle: superseded ? 'superseded' : 'pending_review',
      state_version: superseded ? 2 : 1,
      report_content_hash: '1'.repeat(64),
      evidence_snapshot_id: 11,
      evidence_snapshot_hash: '2'.repeat(64),
      model_execution_result_id: 17,
      execution_result_hash: '3'.repeat(64),
      model_call_id: 19,
      call_identity_hash: '4'.repeat(64),
      created_at: '2026-09-03T10:00:00Z'
    },
    ai_raw: {
      provider: 'deepseek',
      model_id: 'deepseek-flash',
      model_version: 'DeepSeek-V4.1-Flash',
      actual_model: 'deepseek-flash',
      validated_result_hash: '1'.repeat(64),
      content: {
        plain_summary: '支付模块测试结论需要项目经理复核。',
        feature_progress: [
          { feature: '支付模块', stage: '已完成', summary: '旧报告把测试状态写成已完成。', evidence_ids: ['E-201'] }
        ]
      }
    },
    evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) },
    git_facts: { branch: 'main', from_commit: 'a'.repeat(40), to_commit: 'b'.repeat(40), git_snapshot_id: 29 },
    evidence_refs: [{ evidence_id: 'E-201', source_type: 'git_fact', bound_object: '支付模块' }],
    current_supplement: null,
    latest_validation_result: {
      validation_result_id: 23,
      report_version_id: 3,
      report_state_version: 1,
      report_content_hash: '1'.repeat(64),
      state: 'blocked',
      blockers: [{ code: 'REPORT_STAGE_EVIDENCE_NOT_CLOSED', message: '测试状态需要更正。' }],
      result_hash: 'f'.repeat(64)
    }
  })

  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') writeRequests.push({ method: request.method(), pathname })

    if (pathname.includes('/execute-authorized') || pathname.includes('/authorize-send')) {
      providerExecutionCalls += 1
      return json(route, { detail: { message: '本验收禁止调用执行链。' } }, 500)
    }
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-review/current') return json(route, reviewBody())
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/3/approval') return json(route, { approval_snapshot: null })
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/3/reanalysis') {
      return reanalysisBundle
        ? json(route, reanalysisBundle)
        : json(route, { detail: { message: '尚未退回。' } }, 404)
    }
    if (request.method() === 'POST' && pathname === '/api/projects/1/reports/3/reanalysis') {
      reanalysisPosts += 1
      const headers = request.headers()
      expect(headers['x-anxin-session']).toBe('reanalyze-test-session')
      expect(headers['x-request-id']).toBeTruthy()
      expect(headers['local-idempotency-key']).toBeTruthy()
      const body = request.postDataJSON()
      expect(body.expected_report_state_version).toBe(1)
      expect(body.error_location).toBe('支付模块 / 测试状态')
      expect(body.corrected_truth).toBe('测试结论尚未形成，不能写成已完成。')
      expect(body.requested_by).toBe('张经理')

      superseded = true
      reanalysisBundle = {
        created: true,
        reanalysis_request: {
          reanalysis_request_id: 8,
          report_version_id: 3,
          replacement_local_task_id: 'report-reanalysis-3-demo'
        },
        replacement_task: {
          id: 9,
          local_task_id: 'report-reanalysis-3-demo',
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
      return json(route, reanalysisBundle, 201)
    }
    return json(route, { detail: { message: '该验收场景没有准备这项数据。' } }, 404)
  })

  const outDir = path.resolve('test-results/visual-product-acceptance')
  await fs.mkdir(outDir, { recursive: true })
  await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
  await expect(page.getByRole('heading', { name: '报告审阅' })).toBeVisible()

  await page.getByRole('button', { name: '退回重分析' }).click()
  const reanalysisDialog = page.getByRole('dialog', { name: '退回重分析' })
  await expect(reanalysisDialog).toBeVisible()
  await expect(reanalysisDialog.getByRole('heading', { name: '退回重分析' })).toBeVisible()
  await page.getByLabel('错误位置').fill('支付模块 / 测试状态')
  await page.getByLabel('正确事实').fill('测试结论尚未形成，不能写成已完成。')
  await page.getByLabel('更正依据').fill('项目经理核对冻结证据后确认。')
  await page.getByLabel('更正来源').fill('项目经理人工核对')
  await page.getByLabel('申请人').fill('张经理')
  await expect(reanalysisDialog.getByRole('button', { name: '退回并创建重分析任务' })).toBeEnabled()
  await page.screenshot({ path: path.join(outDir, '14-review-reanalysis-confirm.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '退回并创建重分析任务' }).click()

  await expect(page).toHaveURL(/#\/projects\/1\/task\?reanalysis=3$/)
  await expect(page.getByRole('heading', { name: '生成研发报告' })).toBeVisible()
  // Preparation is a separate explicit local action, never performed by this handoff.
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toBeEnabled()
  expect(reanalysisPosts).toBe(1)
  expect(writeRequests).toEqual([{ method: 'POST', pathname: '/api/projects/1/reports/3/reanalysis' }])
  expect(providerExecutionCalls).toBe(0)
  const handoff = await page.evaluate(() => ({
    task: sessionStorage.getItem('rd-agent:project:1:report-generation-task-id'),
    source: sessionStorage.getItem('rd-agent:project:1:report-reanalysis-source-report-version-id'),
    evidence: sessionStorage.getItem('rd-agent:project:1:evidence-snapshot-id')
  }))
  expect(handoff).toEqual({ task: 'report-reanalysis-3-demo', source: '3', evidence: '11' })
  await page.goto('/#/projects/1/review')
  await expect(page.getByText('本版已经退回重分析')).toBeVisible()
  await expect(page.getByText('本版已退回', { exact: true })).toBeVisible()
  expect(writeRequests).toEqual([{ method: 'POST', pathname: '/api/projects/1/reports/3/reanalysis' }])
  expect(providerExecutionCalls).toBe(0)
})
