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

const viewports = [
  ['1920x1080', 1920, 1080],
  ['1440x900', 1440, 900],
  ['1366x768', 1366, 768],
  ['390x844', 390, 844]
]

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) })
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
  await page.setContent(`<!doctype html><meta charset="utf-8"><style>body{margin:0;padding:20px;font-family:Arial,sans-serif;background:#eef1f5;color:#172033}h1{margin:0 0 16px;font-size:24px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}figure{margin:0;padding:8px;background:#fff;border:1px solid #dfe4ec;border-radius:10px}figcaption{padding:4px 2px 8px;font-size:12px;font-weight:700}img{display:block;width:100%;height:360px;object-fit:contain;object-position:top;background:#f8fafc;border:1px solid #edf0f4}</style><h1>Lane C · Sensitive Flows · exact-head visual matrix</h1><div class="grid">${tiles.join('')}</div>`)
  await page.screenshot({ path: path.join(outDir, 'lane-c-contact-sheet.png'), fullPage: true, animations: 'disabled' })
}

test('Lane C covers all required Human-send and report-review visual states across required viewports', async ({ page }) => {
  test.setTimeout(180_000)

  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'lane-c-visual-session')
  })

  const sends = { prepare: 0, preview: 0, authorize: 0, execute: 0 }
  let taskState = 'queued'
  let reviewMode = 'pending'

  const taskBody = () => ({
    id: 5,
    project_id: 1,
    local_task_id: 'tsk-demo',
    evidence_snapshot_id: 13,
    task_type: 'daily_report_generate',
    state: taskState,
    created_at: '2026-09-05T09:00:00Z',
    updated_at: '2026-09-05T09:00:00Z'
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
  const passedValidation = {
    validation_result_id: 23,
    report_version_id: 3,
    report_state_version: 1,
    report_content_hash: '4'.repeat(64),
    state: 'passed',
    blockers: [],
    result_hash: '5'.repeat(64)
  }
  const blockedValidation = {
    ...passedValidation,
    validation_result_id: 24,
    state: 'blocked',
    blockers: [{ code: 'REPORT_STAGE_EVIDENCE_NOT_CLOSED', message: '测试状态需要项目经理复核。' }]
  }
  const approval = {
    approval_snapshot_id: 31,
    approval_snapshot_hash: '9'.repeat(64),
    project_id: 1,
    report_version_id: 3,
    report_state_version_before: 1,
    report_state_version_after: 2,
    confirmed_by: '张经理',
    confirmed_at: '2026-09-05T09:45:00Z',
    confirmed_timezone: 'Asia/Shanghai',
    confirmed_utc_offset_minutes: 480
  }
  const reviewBody = () => {
    const approved = reviewMode === 'approved'
    const blocked = reviewMode === 'blocked' || reviewMode === 'correction'
    return {
      schema_version: 'report_review_bundle_v1',
      project_id: 1,
      report_version: {
        report_version_id: 3,
        project_id: 1,
        version_no: 1,
        lifecycle: approved ? 'approved' : blocked ? 'blocked' : 'pending_review',
        state_version: approved ? 2 : 1,
        report_content_hash: '4'.repeat(64),
        evidence_snapshot_id: 13,
        evidence_snapshot_hash: '6'.repeat(64),
        model_execution_result_id: 17,
        execution_result_hash: '7'.repeat(64),
        model_call_id: 41,
        call_identity_hash: '8'.repeat(64),
        created_at: '2026-09-05T09:30:00Z'
      },
      ai_raw: {
        provider: 'deepseek',
        model_id: 'deepseek-flash',
        model_version: 'DeepSeek-V4.1-Flash',
        actual_model: 'deepseek-flash',
        validated_result_hash: '4'.repeat(64),
        content: {
          plain_summary: '本轮共享导航、核心页面和敏感流程都已经进入 UI/UX 收口阶段，关键安全边界保持不变。',
          feature_progress: [
            { feature: '共享导航', stage: '已完成', summary: '左侧负责跳转，顶部负责显示流程位置。', evidence_ids: ['E-101'] },
            { feature: 'AI 发送', stage: blocked ? '等待复核' : '开发中', summary: '发送授权与真正模型调用保持两个独立动作。', evidence_ids: ['E-102'] }
          ]
        }
      },
      evidence_snapshot: { snapshot_id: 13, snapshot_hash: '6'.repeat(64) },
      git_facts: { branch: 'main', from_commit: '1'.repeat(40), to_commit: '4'.repeat(40), git_snapshot_id: 29 },
      evidence_refs: [
        { evidence_id: 'E-101', source_type: 'git_fact', bound_object: '共享导航' },
        { evidence_id: 'E-102', source_type: 'git_fact', bound_object: 'AI 发送' }
      ],
      current_supplement: null,
      latest_validation_result: reviewMode === 'ready' || approved
        ? passedValidation
        : blocked
          ? blockedValidation
          : null
    }
  }

  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()

    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-generation-tasks/active') {
      // Durable recovery preflight: no snapshot means "no task yet", otherwise the
      // fixture returns the same durable task the page would restore from storage.
      const snapshot = new URL(request.url()).searchParams.get('evidence_snapshot_id')
      return json(route, { task: snapshot ? taskBody() : null })
    }
    if (request.method() === 'GET' && pathname === '/api/projects/1/report-generation-tasks/tsk-demo') return json(route, taskBody())
    if (request.method() === 'POST' && pathname.endsWith('/prepare-model-call')) { sends.prepare++; return json(route, preparation) }
    if (request.method() === 'POST' && pathname.endsWith('/send-authorization-preview')) { sends.preview++; return json(route, preview) }
    if (request.method() === 'POST' && pathname.endsWith('/authorize-send')) { sends.authorize++; expect(request.postDataJSON()).toMatchObject({ human_confirmed: true, data_scope_hash: preview.data_scope_hash }); return json(route, { ...preview, authorization_state: 'authorized_once', permit_ttl_seconds: 300 }) }
    if (pathname.includes('/execute-authorized')) { sends.execute++; return json(route, { detail: { message: 'Lane C visual fixture forbids provider execution.' } }, 500) }

    if (request.method() === 'GET' && pathname === '/api/projects/1/report-review/current') return json(route, reviewBody())
    if (request.method() === 'GET' && pathname === '/api/projects/1/reports/3/approval') return json(route, { approval_snapshot: reviewMode === 'approved' ? approval : null })

    return json(route, { detail: { message: 'Lane C visual fixture does not provide this request.' } }, 404)
  })

  const outDir = path.resolve('test-results/lane-c-sensitive-flows')
  await fs.mkdir(outDir, { recursive: true })
  const captures = []

  for (const [viewportName, width, height] of viewports) {
    await page.setViewportSize({ width, height })

    taskState = 'queued'
    await page.goto('/#/projects/1/task', { waitUntil: 'networkidle' })
    await page.evaluate(() => {
      sessionStorage.removeItem('rd-agent:project:1:report-generation-task-id')
      sessionStorage.removeItem('rd-agent:project:1:evidence-snapshot-id')
    })
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.getByRole('heading', { name: '还没开始' })).toBeVisible()
    await capture(page, outDir, captures, `${viewportName} · task · not started`, `${viewportName}-task-not-started.png`)

    await page.evaluate(() => {
      sessionStorage.setItem('rd-agent:project:1:report-generation-task-id', 'tsk-demo')
      sessionStorage.setItem('rd-agent:project:1:evidence-snapshot-id', '13')
    })
    await page.reload({ waitUntil: 'networkidle' })
    const beforePrepare = { ...sends }
    await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: '准备 AI 调用' }).click()
    await expect(page.getByRole('button', { name: '确认本次发送范围 →' })).toBeVisible()
    expect(sends).toEqual({ ...beforePrepare, prepare: beforePrepare.prepare + 1 })
    await capture(page, outDir, captures, `${viewportName} · task · prepared`, `${viewportName}-task-prepared.png`)

    await page.getByRole('button', { name: '确认本次发送范围 →' }).click()
    await expect(page.getByRole('heading', { name: '确认这次要交给 AI 的资料' })).toBeVisible()
    await expect(page.getByText('点击下方按钮即授权这个精确范围', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '授权本次发送（尚不调用 AI）' })).toBeEnabled()
    expect(sends.authorize).toBe(beforePrepare.authorize)
    expect(sends.execute).toBe(0)
    await page.getByRole('button', { name: '授权本次发送（尚不调用 AI）' }).click()
    await expect(page.getByText('本次授权已经记录，待发送资料尚未发送')).toBeVisible()
    await expect(page.getByRole('button', { name: '开始 AI 分析 · 会调用模型' })).toBeVisible()
    expect(sends).toEqual({ prepare: beforePrepare.prepare + 1, preview: beforePrepare.preview + 1, authorize: beforePrepare.authorize + 1, execute: 0 })
    await capture(page, outDir, captures, `${viewportName} · task · authorized / not sent`, `${viewportName}-task-authorized-not-sent.png`)

    taskState = 'succeeded'
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.getByRole('heading', { name: '分析完成' })).toBeVisible()
    await capture(page, outDir, captures, `${viewportName} · task · completed`, `${viewportName}-task-completed.png`)

    taskState = 'unknown'
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.getByRole('heading', { name: '结果暂时无法确认' })).toBeVisible()
    await expect(page.getByText(/原调用不会自动重试/)).toBeVisible()
    expect(sends.execute).toBe(0)
    await capture(page, outDir, captures, `${viewportName} · task · unknown / no auto retry`, `${viewportName}-task-unknown.png`)

    reviewMode = 'pending'
    await page.goto('/#/projects/1/review', { waitUntil: 'networkidle' })
    await expect(page.getByRole('heading', { name: '报告审阅' })).toBeVisible()
    await expect(page.locator('.version-strip').getByText('确认时自动检查', { exact: true })).toBeVisible()
    await capture(page, outDir, captures, `${viewportName} · report · pending review`, `${viewportName}-report-pending.png`)

    reviewMode = 'blocked'
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.getByText('测试状态需要项目经理复核。', { exact: true })).toBeVisible()
    await capture(page, outDir, captures, `${viewportName} · report · validation blocked`, `${viewportName}-report-blocked.png`)

    reviewMode = 'ready'
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.getByRole('button', { name: '确认本版报告' })).toBeEnabled()
    await capture(page, outDir, captures, `${viewportName} · report · approval ready`, `${viewportName}-report-approval-ready.png`)

    reviewMode = 'approved'
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.getByText('本版已经正式确认')).toBeVisible()
    await expect(page.getByRole('button', { name: '下一步：查看安心看板 →' })).toBeVisible()
    await expect(page.getByText(/V5\.5/)).toHaveCount(0)
    await capture(page, outDir, captures, `${viewportName} · report · approved read-only`, `${viewportName}-report-approved.png`)

    reviewMode = 'correction'
    await page.reload({ waitUntil: 'networkidle' })
    await page.getByRole('button', { name: '退回重分析' }).click()
    const correctionDialog = page.getByRole('dialog', { name: '退回重分析' })
    await expect(correctionDialog).toBeVisible()
    await expect(correctionDialog.getByText(/旧报告会保留并标记为已被替代/)).toBeVisible()
    await capture(page, outDir, captures, `${viewportName} · report · correction / reanalysis entry`, `${viewportName}-report-reanalysis-entry.png`)
    await correctionDialog.getByRole('button', { name: '关闭' }).click()
  }

  await writeContactSheet(page, outDir, captures)
})
