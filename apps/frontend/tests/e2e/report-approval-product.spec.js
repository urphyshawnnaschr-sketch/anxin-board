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

test.beforeEach(async ({ page }) => {
  // Every unmatched API request is local fixture data, never a real backend call.
  await page.route('**/api/**', route => new URL(route.request().url()).pathname.startsWith('/api/')
    ? json(route, { detail: { code: 'TEST_FIXTURE_NOT_PREPARED' } }, 404) : route.continue())
  await page.route('**/api/settings/mail-transport', route => json(route, {
    schema_version: 'mail_transport_settings_v1', configured: false, credential_reference_bound: false, profile: null
  }))
  await page.route('**/api/projects/*/recipient-config', route => json(route, {
    schema_version: 'recipient_config_settings_v1', configured: false, version_no: null, to_recipients: []
  }))
})

function formalBoard(approval) {
  return {
    schema_version: 'anxin_board_report_v3',
    title: '安心看板',
    motto: '非己所安，不加于物',
    project_name: project.name,
    report_date: '2026-09-03',
    profile_id: 13,
    profile_version_no: 4,
    profile_content_hash: '5'.repeat(64),
    source_prd_id: 7,
    module_count: 2,
    completed_module_count: 1,
    active_module_count: 1,
    unknown_module_count: 0,
    overall_message: '本版报告已经由项目经理正式确认，安心看板只展示该批准版本绑定的研发事实。',
    modules: [
      {
        module_id: 'task_execution',
        name: '任务执行',
        stage: '已完成',
        display_stage: '已完成',
        tone: 'done',
        summary: 'AI 调用、结果保存与报告版本生成链路已闭合。',
        next_step: '保持当前链路稳定。',
        client_stage_summary_hash: '6'.repeat(64)
      },
      {
        module_id: 'report_review',
        name: '报告审阅',
        stage: '开发中',
        display_stage: '开发中',
        tone: 'active',
        summary: '最终确认已完成，正在核对正式安心看板。',
        next_step: '完成真机与截图验收。',
        client_stage_summary_hash: '7'.repeat(64)
      }
    ],
    daily_change: {
      display_state: 'ready',
      headline: '最终确认已闭合并生成正式安心看板',
      summary: '本轮完成最终确认和正式看板准入闭环。',
      highlights: [],
      scope_note: '仅展示已批准报告绑定的正式数据。',
      plain_language_change_summary_hash: '8'.repeat(64)
    },
    manager_supplement: '项目经理已核对本版报告。',
    approval_snapshot_id: 31,
    approval_snapshot_hash: '9'.repeat(64),
    report_version_id: 3,
    report_version_no: 1,
    report_content_hash: '1'.repeat(64),
    validation_result_id: 23,
    validation_result_hash: 'f'.repeat(64),
    evidence_snapshot_id: 11,
    evidence_snapshot_hash: '2'.repeat(64),
    git_snapshot_id: 29,
    git_facts_hash: 'a'.repeat(64),
    git_branch: 'main',
    git_from_commit: 'a'.repeat(40),
    git_to_commit: 'b'.repeat(40),
    prd_id: 7,
    prd_source_hash: 'b'.repeat(64),
    prd_parsed_hash: 'c'.repeat(64),
    prd_structured_hash: 'd'.repeat(64),
    prd_document_fingerprint: 'e'.repeat(64),
    model_execution_result_id: 17,
    execution_result_hash: '3'.repeat(64),
    model_call_id: 19,
    call_identity_hash: '4'.repeat(64),
    provider: 'deepseek',
    model_id: 'deepseek-flash',
    model_version: 'DeepSeek-V4.1-Flash',
    actual_model: 'deepseek-flash',
    provider_runtime_fingerprint: 'deepseek-runtime-cn-v1',
    rule_version: 'report-rule-v1',
    output_schema_version: 'daily-report-output-v1',
    benchmark_sample_pack_version: 'benchmark-pack-v1',
    qualification_hash: 'c'.repeat(64),
    authorization_hash: 'd'.repeat(64),
    supplement_version_id: null,
    supplement_content_hash: null,
    supplement_provided_by: null,
    supplement_provided_at: null,
    supplement_provided_timezone: null,
    supplement_source_type: null,
    confirmed_by: approval.confirmed_by,
    confirmed_at: approval.confirmed_at,
    confirmed_timezone: approval.confirmed_timezone,
    confirmed_utc_offset_minutes: approval.confirmed_utc_offset_minutes,
    human_acknowledged: true,
    anxin_board_report_hash: 'e'.repeat(64)
  }
}

test('Page07 final approval immediately exposes the formal ApprovalSnapshot-bound Page08 V3 board', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'approval-test-session')
  })

  let approved = false
  let approval = null
  let approvalPosts = 0
  let reviewGets = 0
  let postApprovalReviewRequested = false
  let releasePostApprovalReview
  const postApprovalReviewGate = new Promise((resolve) => { releasePostApprovalReview = resolve })
  const validation = {
    validation_result_id: 23,
    report_version_id: 3,
    report_state_version: 1,
    report_content_hash: '1'.repeat(64),
    state: 'passed',
    blockers: [],
    result_hash: 'f'.repeat(64)
  }
  const reviewBody = () => ({
    schema_version: 'report_review_bundle_v1',
    project_id: 1,
    report_version: {
      report_version_id: 3,
      project_id: 1,
      version_no: 1,
      lifecycle: approved ? 'approved' : 'pending_review',
      state_version: approved ? 2 : 1,
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
        plain_summary: '本轮已完成任务执行主链与安全授权闭环，最终报告等待项目经理确认。',
        feature_progress: [
          { feature: '任务执行', stage: '已完成', summary: 'AI 调用、结果保存与报告版本生成链路已闭合。', evidence_ids: ['E-101'] },
          { feature: '报告审阅', stage: '开发中', summary: '正在完成最终确认与安心看板衔接。', evidence_ids: ['E-102'] }
        ]
      }
    },
    evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) },
    git_facts: { branch: 'main', from_commit: 'a'.repeat(40), to_commit: 'b'.repeat(40), git_snapshot_id: 29 },
    evidence_refs: [
      { evidence_id: 'E-101', source_type: 'git_fact', bound_object: '任务执行' },
      { evidence_id: 'E-102', source_type: 'git_fact', bound_object: '报告审阅' }
    ],
    current_supplement: null,
    latest_validation_result: validation
  })

  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-review/current') {
      reviewGets += 1
      if (approved && reviewGets > 1) {
        postApprovalReviewRequested = true
        await postApprovalReviewGate
      }
      return json(route, reviewBody())
    }
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/3/approval') return json(route, { approval_snapshot: approval })
    if (request.method() === 'GET' && pathname === '/api/projects/1/anxin-board/latest') {
      if (approved && approval) return json(route, formalBoard(approval))
      return json(route, { detail: { code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', message: '尚无正式安心看板。' } }, 404)
    }
    if (request.method() === 'POST' && pathname === '/api/projects/1/reports/3/approval') {
      approvalPosts += 1
      const headers = request.headers()
      expect(headers['x-anxin-session']).toBe('approval-test-session')
      expect(headers['x-request-id']).toBeTruthy()
      expect(headers['local-idempotency-key']).toBeTruthy()
      const body = request.postDataJSON()
      expect(body.human_confirmed).toBe(true)
      expect(body.confirmed_by).toBe('张经理')
      expect(body.expected_report_state_version).toBe(1)
      approved = true
      approval = {
        approval_snapshot_id: 31,
        approval_snapshot_hash: '9'.repeat(64),
        project_id: 1,
        report_version_id: 3,
        report_state_version_before: 1,
        report_state_version_after: 2,
        confirmed_by: '张经理',
        confirmed_at: '2026-09-03T13:00:00Z',
        confirmed_timezone: body.confirmed_timezone,
        confirmed_utc_offset_minutes: body.confirmed_utc_offset_minutes
      }
      return json(route, { approval_snapshot: approval, created: true })
    }
    return json(route, { detail: { message: '该验收场景没有准备这项数据。' } }, 404)
  })

  const outDir = path.resolve('test-results/visual-product-acceptance')
  await fs.mkdir(outDir, { recursive: true })
  await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
  await expect(page.locator('.app-shell')).toBeVisible()
  await expect(page.getByRole('heading', { name: '报告审阅' })).toBeVisible()
  await expect(page.locator('.gate-card .status.passed')).toHaveText('通过')
  const confirmButton = page.getByRole('button', { name: '确认本版报告' })
  await expect(confirmButton).toBeEnabled()
  await page.screenshot({ path: path.join(outDir, '12-review-ready-to-approve.png'), fullPage: true, animations: 'disabled' })

  await confirmButton.click()
  const approvalDialog = page.getByRole('dialog', { name: '确认本版报告' })
  await expect(approvalDialog).toBeVisible()
  await expect(approvalDialog.getByText('下一步', { exact: true })).toBeVisible()
  await expect(approvalDialog.getByText('安心看板', { exact: true })).toBeVisible()
  await expect(page.getByText(/V5\.5/)).toHaveCount(0)
  await page.getByLabel('最终确认人').fill('张经理')
  await expect(page.getByRole('button', { name: '正式确认本版报告' })).toBeEnabled()
  await page.getByRole('button', { name: '正式确认本版报告' }).click()

  await expect(page.getByText('本版已经正式确认')).toBeVisible({ timeout: 2000 })
  await expect(page.getByText(/张经理/).first()).toBeVisible()
  await expect(page.getByText(/V5\.5/)).toHaveCount(0)
  await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toBeVisible()
  expect(approvalPosts).toBe(1)
  expect(reviewGets).toBe(1)
  expect(postApprovalReviewRequested).toBe(false)
  releasePostApprovalReview()
  await page.screenshot({ path: path.join(outDir, '13-report-approved.png'), fullPage: true, animations: 'disabled' })

  await page.getByRole('button', { name: '下一步：查看安心看板 →' }).click()
  await expect(page).toHaveURL(/#\/projects\/1\/board$/)
  await expect(page.getByRole('heading', { name: '安心看板' })).toBeVisible()
  const formalBoardView = page.getByTestId('anxin-board')
  await expect(formalBoardView).toBeVisible()
  await expect(formalBoardView.getByText('正式确认版 · 张经理 已确认', { exact: true })).toBeVisible()
  await expect(formalBoardView.getByText('审核人', { exact: true })).toBeVisible()
  await expect(formalBoardView.getByText('张经理', { exact: true })).toBeVisible()
  await expect(formalBoardView.getByText('V1', { exact: true })).toBeVisible()
  await expect(formalBoardView.getByText(/ApprovalSnapshot #31/)).toHaveCount(0)
  await expect(formalBoardView.getByText('任务执行', { exact: true })).toBeVisible()
  await expect(formalBoardView.getByText('报告审阅', { exact: true })).toBeVisible()
  await expect(page.getByTestId('anxin-board-template-preview')).toHaveCount(0)
  await page.screenshot({ path: path.join(outDir, '14-board-after-approval.png'), fullPage: true, animations: 'disabled' })

  approval = null
  await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
  await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '正式确认凭据不可用' })).toBeDisabled()
  await expect(page.getByText(/系统保持关闭，不允许进入正式安心看板/)).toBeVisible()
  await page.screenshot({ path: path.join(outDir, '15-review-approval-authority-broken.png'), fullPage: true, animations: 'disabled' })
})

test('Page07 never carries project A approval authority into project B failed review states', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'stale-authority-session'))
  const projects = [{ ...project, id: 1, name: '项目 A' }, { ...project, id: 2, name: '项目 B' }]
  const approvalA = { approval_snapshot_id: 91, approval_snapshot_hash: '9'.repeat(64), project_id: 1, report_version_id: 31, report_state_version_before: 1, report_state_version_after: 2, confirmed_by: 'A 经理', confirmed_at: '2026-09-03T13:00:00Z', confirmed_timezone: 'Asia/Shanghai', confirmed_utc_offset_minutes: 480 }
  const approvedReviewA = {
    schema_version: 'report_review_bundle_v1', project_id: 1,
    report_version: { report_version_id: 31, project_id: 1, version_no: 1, lifecycle: 'approved', state_version: 2, report_content_hash: '1'.repeat(64), evidence_snapshot_id: 11, evidence_snapshot_hash: '2'.repeat(64), model_execution_result_id: 17, execution_result_hash: '3'.repeat(64), model_call_id: 19, call_identity_hash: '4'.repeat(64), created_at: '2026-09-03T10:00:00Z' },
    ai_raw: { provider: 'deepseek', model_id: 'deepseek-flash', model_version: 'v', actual_model: 'deepseek-flash', validated_result_hash: '1'.repeat(64), content: { plain_summary: 'A approved', feature_progress: [] } },
    evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) }, git_facts: {}, evidence_refs: [], current_supplement: null,
    latest_validation_result: { validation_result_id: 1, report_version_id: 31, report_state_version: 1, report_content_hash: '1'.repeat(64), state: 'passed', blockers: [], result_hash: 'f'.repeat(64) }
  }
  let bMode = '404'
  await page.route('**/*', async (route) => {
    const request = route.request(); const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, projects)
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, projects[0])
    if (request.method() === 'GET' && pathname === '/api/projects/2') return json(route, projects[1])
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-review/current') return json(route, approvedReviewA)
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/31/approval') return json(route, { approval_snapshot: approvalA })
    if (request.method() === 'GET' && pathname === '/api/projects/2/report-review/current') {
      if (bMode === '404') return json(route, { detail: { message: 'B 无当前报告' } }, 404)
      if (bMode === '409') return json(route, { detail: { message: 'B authority conflict' } }, 409)
      if (bMode === '500') return json(route, { detail: { message: 'B read failed' } }, 500)
      return json(route, { schema_version: 'report_review_bundle_v1', project_id: 2, report_version: null, ai_raw: {} })
    }
    return json(route, { detail: { message: 'unexpected' } }, 404)
  })
  for (const mode of ['404', '409', '500', 'malformed']) {
    await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
    await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toBeVisible()
    await expect(page.getByText('本版已经正式确认')).toBeVisible()
    bMode = mode
    await page.goto('/#/projects/2/review', { waitUntil: 'networkidle' })
    await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toHaveCount(0)
    await expect(page.getByText('本版已经正式确认')).toHaveCount(0)
  }
})

test('Page07 discards a delayed project A review response after project B navigation', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'generation-session'))
  let releaseA; let markAStarted
  const aStarted = new Promise((resolve) => { markAStarted = resolve })
  const aGate = new Promise((resolve) => { releaseA = resolve })
  let aApprovalGets = 0
  const reviewA = {
    schema_version: 'report_review_bundle_v1', project_id: 1,
    report_version: { report_version_id: 41, project_id: 1, version_no: 1, lifecycle: 'approved', state_version: 2, report_content_hash: '1'.repeat(64), evidence_snapshot_id: 11, evidence_snapshot_hash: '2'.repeat(64), model_execution_result_id: 17, execution_result_hash: '3'.repeat(64), model_call_id: 19, call_identity_hash: '4'.repeat(64), created_at: '2026-09-03T10:00:00Z' },
    ai_raw: { provider: 'deepseek', model_id: 'm', model_version: 'v', actual_model: 'm', validated_result_hash: '1'.repeat(64), content: { plain_summary: 'late A', feature_progress: [] } }, evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) }, git_facts: {}, evidence_refs: [], current_supplement: null, latest_validation_result: null
  }
  await page.route('**/*', async (route) => {
    const request = route.request(); const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [{ ...project, id: 1 }, { ...project, id: 2 }])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, { ...project, id: 1 })
    if (request.method() === 'GET' && pathname === '/api/projects/2') return json(route, { ...project, id: 2 })
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-review/current') { markAStarted(); await aGate; return json(route, reviewA) }
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/41/approval') { aApprovalGets += 1; return json(route, { approval_snapshot: null }) }
    if (request.method() === 'GET' && pathname === '/api/projects/2/report-review/current') return json(route, { detail: { message: 'B 无报告' } }, 404)
    return json(route, { detail: { message: 'unexpected' } }, 404)
  })
  await page.goto('/#/projects/2/review', { waitUntil: 'networkidle' })
  await page.evaluate(() => { window.location.hash = '#/projects/1/review' })
  await aStarted
  await page.evaluate(() => { window.location.hash = '#/projects/2/review' })
  await expect(page.getByText('暂无可审阅报告')).toBeVisible()
  releaseA(); await page.waitForTimeout(150)
  await expect(page).toHaveURL(/#\/projects\/2\/review$/)
  await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toHaveCount(0)
  await expect(page.getByText('本版已经正式确认')).toHaveCount(0)
  expect(aApprovalGets).toBe(0)
})

test('Page07 discards project A approval POST completion when navigation switches to project B', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'approval-switch-session'))
  let releaseApproval; let markApprovalStarted; let releaseBReview; let markBReviewStarted
  const approvalStarted = new Promise((resolve) => { markApprovalStarted = resolve })
  const approvalGate = new Promise((resolve) => { releaseApproval = resolve })
  const bReviewStarted = new Promise((resolve) => { markBReviewStarted = resolve })
  const bReviewGate = new Promise((resolve) => { releaseBReview = resolve })
  const pendingA = {
    schema_version: 'report_review_bundle_v1', project_id: 1,
    report_version: { report_version_id: 51, project_id: 1, version_no: 1, lifecycle: 'pending_review', state_version: 1, report_content_hash: '1'.repeat(64), evidence_snapshot_id: 11, evidence_snapshot_hash: '2'.repeat(64), model_execution_result_id: 17, execution_result_hash: '3'.repeat(64), model_call_id: 19, call_identity_hash: '4'.repeat(64), created_at: '2026-09-03T10:00:00Z' },
    ai_raw: { provider: 'deepseek', model_id: 'm', model_version: 'v', actual_model: 'm', validated_result_hash: '1'.repeat(64), content: { plain_summary: 'pending A', feature_progress: [] } }, evidence_snapshot: { snapshot_id: 11, snapshot_hash: '2'.repeat(64) }, git_facts: {}, evidence_refs: [], current_supplement: null,
    latest_validation_result: { validation_result_id: 5, report_version_id: 51, report_state_version: 1, report_content_hash: '1'.repeat(64), state: 'passed', blockers: [], result_hash: 'f'.repeat(64) }
  }
  await page.route('**/*', async (route) => {
    const request = route.request(); const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [{ ...project, id: 1 }, { ...project, id: 2 }])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, { ...project, id: 1 })
    if (request.method() === 'GET' && pathname === '/api/projects/2') return json(route, { ...project, id: 2 })
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-review/current') return json(route, pendingA)
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/51/approval') return json(route, { approval_snapshot: null })
    if (request.method() === 'GET' && pathname === '/api/projects/2/report-review/current') { markBReviewStarted(); await bReviewGate; return json(route, { detail: { message: 'B 无报告' } }, 404) }
    if (request.method() === 'POST' && pathname === '/api/projects/1/reports/51/approval') {
      markApprovalStarted(); const body = request.postDataJSON(); await approvalGate
      return json(route, { approval_snapshot: { approval_snapshot_id: 101, approval_snapshot_hash: '9'.repeat(64), project_id: 1, report_version_id: 51, report_state_version_before: 1, report_state_version_after: 2, confirmed_by: 'A 经理', confirmed_at: '2026-09-03T13:00:00Z', confirmed_timezone: body.confirmed_timezone, confirmed_utc_offset_minutes: body.confirmed_utc_offset_minutes }, created: true })
    }
    return json(route, { detail: { message: 'unexpected' } }, 404)
  })
  await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '确认本版报告' }).click()
  await page.getByLabel('最终确认人').fill('A 经理')
  await expect(page.getByRole('button', { name: '正式确认本版报告' })).toBeEnabled()
  await page.getByRole('button', { name: '正式确认本版报告' }).click()
  await approvalStarted
  await page.evaluate(() => { window.location.hash = '#/projects/2/review' })
  await bReviewStarted
  releaseApproval(); await page.waitForTimeout(100); releaseBReview()
  await expect(page.getByText('暂无可审阅报告')).toBeVisible()
  await expect(page).toHaveURL(/#\/projects\/2\/review$/)
  await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toHaveCount(0)
  await expect(page.getByText('本版已经正式确认')).toHaveCount(0)
})

test('Page08 discards delayed project A formal V3 report after navigation to project B', async ({ page }) => {
  let releaseAReport
  let markAReportStarted
  const aReportStarted = new Promise((resolve) => { markAReportStarted = resolve })
  const aReportGate = new Promise((resolve) => { releaseAReport = resolve })
  const projectA = { ...project, id: 1, name: '项目 A' }
  const projectB = { ...project, id: 2, name: '项目 B' }
  const approvalA = {
    confirmed_by: 'A 经理',
    confirmed_at: '2026-09-05T08:00:00Z',
    confirmed_timezone: 'Asia/Shanghai',
    confirmed_utc_offset_minutes: 480
  }
  const boardA = { ...formalBoard(approvalA), project_name: '项目 A' }

  await page.route('**/api/projects/1', async (route) => json(route, projectA))
  await page.route('**/api/projects/2', async (route) => json(route, projectB))
  await page.route('**/api/projects/1/anxin-board/latest', async (route) => {
    markAReportStarted()
    await aReportGate
    await json(route, boardA)
  })
  await page.route('**/api/projects/2/anxin-board/latest', async (route) => json(route, {
    detail: { code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', message: 'B 尚无正式报告' }
  }, 404))

  await page.goto('/#/projects/2/board')
  await expect(page.getByTestId('anxin-board-empty')).toBeVisible()
  await page.evaluate(() => { window.location.hash = '#/projects/1/board' })
  await aReportStarted
  await page.evaluate(() => { window.location.hash = '#/projects/2/board' })
  await expect(page.getByTestId('anxin-board-empty')).toBeVisible()
  releaseAReport()
  await page.waitForTimeout(150)

  await expect(page).toHaveURL(/#\/projects\/2\/board$/)
  await expect(page.getByTestId('anxin-board')).toHaveCount(0)
  await expect(page.getByTestId('anxin-board-empty')).toBeVisible()
  await expect(page.getByText('正式确认版 · A 经理 已确认', { exact: true })).toHaveCount(0)
  await expect(page.getByText('项目 A', { exact: true })).toHaveCount(0)
})

test('Page08 discards delayed project A identity before requesting A board after returning to B', async ({ page }) => {
  let releaseAProject
  let markAProjectStarted
  const aProjectStarted = new Promise((resolve) => { markAProjectStarted = resolve })
  const aProjectGate = new Promise((resolve) => { releaseAProject = resolve })
  const projectA = { ...project, id: 1, name: '项目 A' }
  const projectB = { ...project, id: 2, name: '项目 B' }
  let aBoardGets = 0
  let bBoardGets = 0

  await page.route('**/api/projects/1', async (route) => {
    markAProjectStarted()
    await aProjectGate
    await json(route, projectA)
  })
  await page.route('**/api/projects/2', async (route) => json(route, projectB))
  await page.route('**/api/projects/1/anxin-board/latest', async (route) => {
    aBoardGets += 1
    const approvalA = {
      confirmed_by: 'A 经理',
      confirmed_at: '2026-09-05T08:00:00Z',
      confirmed_timezone: 'Asia/Shanghai',
      confirmed_utc_offset_minutes: 480
    }
    await json(route, { ...formalBoard(approvalA), project_name: '项目 A' })
  })
  await page.route('**/api/projects/2/anxin-board/latest', async (route) => { bBoardGets += 1; return json(route, {
    detail: { code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', message: 'B 尚无正式报告' }
  }, 404) })

  await page.goto('/#/projects/2/board')
  await expect(page.getByTestId('anxin-board-empty')).toBeVisible()
  await page.evaluate(() => { window.location.hash = '#/projects/1/board' })
  await aProjectStarted
  // MailSendPanel performs one independent read; AnxinBoardView must still wait for identity.
  await expect.poll(() => aBoardGets).toBe(1)
  await page.evaluate(() => { window.location.hash = '#/projects/2/board' })
  await expect(page.getByTestId('anxin-board-empty')).toBeVisible()
  // The previous B empty DOM can survive until hashchange renders A/B; wait for the new B load.
  await expect.poll(() => bBoardGets).toBe(4)
  releaseAProject()
  await page.waitForTimeout(150)

  expect(aBoardGets).toBe(1)
  await expect(page).toHaveURL(/#\/projects\/2\/board$/)
  await expect(page.getByTestId('anxin-board')).toHaveCount(0)
  await expect(page.getByText('正式确认版 · A 经理 已确认', { exact: true })).toHaveCount(0)
})
