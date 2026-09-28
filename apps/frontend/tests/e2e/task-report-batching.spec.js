import { test, expect } from '@playwright/test'

async function fixture(page, { state = 'queued', completed = 0, uncertain = false, holdExecution = false, executionError = null } = {}) {
  const seen = { prepares: 0, authorizes: 0, executes: 0 }
  let releaseExecution
  const executionGate = new Promise(resolve => { releaseExecution = resolve })
  seen.releaseExecution = releaseExecution
  const plan = { plan_hash: 'b'.repeat(64), batch_count: 3, completed_batch_count: completed,
    pending_batch_count: 3 - completed, state: uncertain ? 'unknown' : 'pending', can_resume: !uncertain,
    batches: [1, 2, 3].map(ordinal => ({ ordinal, model_call_id: 10 + ordinal,
      state: ordinal <= completed ? 'succeeded' : uncertain && ordinal === completed + 1 ? 'unknown' : 'pending',
      estimated_input_tokens: 18000 + ordinal })) }
  const task = { project_id: 1, local_task_id: 'batch-task', evidence_snapshot_id: 11,
    task_type: 'daily_report_generate', state, batch_plan: plan }
  const scope = { authorization_state: 'awaiting_human_confirmation', data_scope_hash: 'a'.repeat(64),
    admitted_target_count: 150, batch_plan: plan }
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'fixture-session')
    sessionStorage.setItem('rd-agent:project:1:evidence-snapshot-id', '11')
  })
  await page.route('**/*', async route => {
    const p = new URL(route.request().url()).pathname
    if (!p.startsWith('/api/')) return route.continue()
    const reply = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    const project = { id: 1, name: 'Batch report fixture', status: 'active', version: 1 }
    if (p === '/api/projects') return reply([project])
    if (p === '/api/projects/1') return reply(project)
    if (p.endsWith('/active')) return reply({ task })
    if (p.endsWith('/prepare-model-call')) {
      seen.prepares++
      return reply({ model_call_id: 9, preparation_state: 'prepared', provider_send_state: 'not_attempted',
        local_request_readiness_state: 'ready_for_gateway_evaluation',
        next_gate: 'exact_human_send_authorization_required', batch_plan: plan })
    }
    if (p.endsWith('/send-authorization-preview')) return reply(scope)
    if (p.endsWith('/authorize-send')) {
      seen.authorizes++
      return reply({ ...scope, authorization_state: 'authorized_once', provider_send_state: 'not_attempted' })
    }
    if (p.endsWith('/execute-authorized')) {
      seen.executes++
      if (holdExecution) await executionGate
      if (executionError) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: executionError }) })
      task.state = 'succeeded'
      return reply({ task_state: 'succeeded', report_version_id: 21 })
    }
    if (p.endsWith('/batch-task')) return reply(task)
    return reply({})
  })
  await page.goto('/#/projects/1/task')
  return seen
}

test('large report shows bounded batches and count before authorization, sends only on click', async ({ page }) => {
  const seen = await fixture(page)
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  const batches = page.getByRole('region', { name: '分批分析进度' })
  await expect(batches).toContainText('共 3 批')
  await expect(batches).toContainText('已完成 0 批')
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await expect(page.locator('.send-confirm')).toContainText('最多 3 次模型调用')
  expect(seen.executes).toBe(0)
  await page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true }).click()
  expect(seen.executes).toBe(0)
  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true }).click()
  await expect(page.getByRole('button', { name: '下一步：审阅报告 →' })).toBeVisible()
  expect(seen.executes).toBe(1)
})

test('new session restores batch progress and permits pending batches only after fresh authorization', async ({ page }) => {
  const seen = await fixture(page, { state: 'running', completed: 1 })
  await expect(page.getByRole('region', { name: '分批分析进度' })).toContainText('已完成 1 批')
  expect(seen.prepares + seen.authorizes + seen.executes).toBe(0)
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await expect(page.locator('.send-confirm')).toContainText('最多 2 次模型调用')
  await expect(page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true })).toBeEnabled()
  expect(seen.executes).toBe(0)
})

test('unknown batch cannot be continued even if there are pending batches', async ({ page }) => {
  const seen = await fixture(page, { state: 'unknown', completed: 1, uncertain: true })
  await expect(page.getByRole('region', { name: '分批分析进度' })).toContainText('结果不确定')
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toHaveCount(0)
  expect(seen.prepares + seen.authorizes + seen.executes).toBe(0)
})


test('refresh cannot reset an in-flight batch execution', async ({ page }) => {
  const seen = await fixture(page, { holdExecution: true })
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true }).click()
  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true }).click()
  await expect.poll(() => seen.executes).toBe(1)
  await expect(page.getByRole('button', { name: '刷新状态', exact: true })).toBeDisabled()
  seen.releaseExecution()
  await expect(page.getByRole('button', { name: '下一步：审阅报告 →' })).toBeVisible()
  expect(seen.executes).toBe(1)
})


test('pre-send failure keeps actionable reason and error identity without automatic retry', async ({ page }) => {
  const seen = await fixture(page, { executionError: {
    code: 'MODEL_EXECUTION_GATEWAY_NOT_READY',
    cause_code: 'DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE',
    stage: 'current_authority',
    message: '无法读取已配置的 DeepSeek 凭据，请检查模型设置。'
  } })
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true }).click()
  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true }).click()
  await expect(page.locator('.message')).toContainText('无法读取已配置的 DeepSeek 凭据')
  await expect(page.locator('.message')).toContainText('DEEPSEEK_AUTHORITY_CREDENTIAL_UNAVAILABLE')
  await expect(page.locator('.message')).toContainText('MODEL_EXECUTION_GATEWAY_NOT_READY')
  expect(seen.executes).toBe(1)
  await expect(page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true })).toHaveCount(0)
})
