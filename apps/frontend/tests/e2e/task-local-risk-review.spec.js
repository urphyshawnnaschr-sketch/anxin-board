import { test, expect } from '@playwright/test'

test('local exclusions are visible and require review; cancel never sends', async ({ page }) => {
  let authorizations = 0
  let executions = 0
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'mock-session')
    sessionStorage.setItem('rd-agent:project:1:evidence-snapshot-id', '11')
    sessionStorage.setItem('rd-agent:project:1:report-generation-task-id', 'normal-task')
  })
  await page.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const reply = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    const project = { id: 1, name: 'Local risk fixture', status: 'active', version: 1 }
    if (path === '/api/projects') return reply([project])
    if (path === '/api/projects/1') return reply(project)
    if (path.endsWith('/prepare-model-call')) return reply({
      model_call_id: 9, preparation_state: 'prepared', provider_send_state: 'not_attempted',
      local_request_readiness_state: 'ready_for_gateway_evaluation', denied_target_count: 1,
      context_findings: [{ target: 'git:1', path: 'src/style.scss', line_start: 4, line_end: 4,
        range_kind: 'diff_lines', rule_id: 'R3', risk_level: 'warning', action: 'isolated',
        safe_snippet: '[REDACTED:CREDENTIAL]' }]
    })
    if (path.endsWith('/send-authorization-preview')) return reply({
      authorization_state: 'awaiting_human_confirmation', data_scope_hash: 'a'.repeat(64), admitted_target_count: 2
    })
    if (path.endsWith('/authorize-send')) {
      authorizations++
      return reply({ authorization_state: 'authorized_once', provider_send_state: 'not_attempted', data_scope_hash: 'a'.repeat(64) })
    }
    if (path.endsWith('/execute-authorized')) { executions++; return reply({ task_state: 'succeeded' }) }
    if (path.includes('/report-generation-tasks/')) return reply({ local_task_id: 'normal-task', task_type: 'daily_report_generate', state: 'queued', evidence_snapshot_id: 11 })
    return reply({})
  })
  await page.goto('/#/projects/1/task')
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  const risks = page.getByRole('region', { name: '本地内容风险处理' })
  await expect(risks).toContainText('src/style.scss')
  await expect(risks).toContainText('Diff 第 4–4 行')
  await expect(risks).toContainText('[REDACTED:CREDENTIAL]')
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  const authorize = page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true })
  await expect(authorize).toBeEnabled()
  await expect(risks).toContainText('不需要额外确认')
  await risks.getByRole('button', { name: '取消本次发送' }).click()
  await expect(page.getByRole('region', { name: '真实 AI 发送授权' })).toHaveCount(0)
  expect(authorizations).toBe(0)
  expect(executions).toBe(0)
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await expect(authorize).toBeEnabled()
  await authorize.click()
  expect(authorizations).toBe(1)
  expect(executions).toBe(0)
  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true }).click()
  expect(executions).toBe(1)
})

for (const outcome of ['succeeded', 'failed', 'unknown']) {
test(`pending execution settles ${outcome} without duplicate send`, async ({ page }) => {
  let state = 'queued'
  let release
  const pending = new Promise(resolve => { release = resolve })
  let authorizations = 0
  let executions = 0
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'mock-session')
    sessionStorage.setItem('rd-agent:project:1:evidence-snapshot-id', '11')
    sessionStorage.setItem('rd-agent:project:1:report-generation-task-id', 'normal-task')
  })
  await page.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const reply = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    const project = { id: 1, name: 'Local risk fixture', status: 'active', version: 1 }
    if (path === '/api/projects') return reply([project])
    if (path === '/api/projects/1') return reply(project)
    if (path.endsWith('/prepare-model-call')) return reply({
      model_call_id: 9, preparation_state: 'prepared', provider_send_state: 'not_attempted',
      local_request_readiness_state: 'ready_for_gateway_evaluation', denied_target_count: 1,
      context_findings: [{ target: 'git:1', path: 'src/style.scss', line_start: 4, line_end: 4,
        range_kind: 'diff_lines', rule_id: 'R3', risk_level: 'warning', action: 'isolated',
        safe_snippet: '[REDACTED:CREDENTIAL]' }]
    })
    if (path.endsWith('/send-authorization-preview')) return reply({
      authorization_state: 'awaiting_human_confirmation', data_scope_hash: 'a'.repeat(64), admitted_target_count: 2
    })
    if (path.endsWith('/authorize-send')) {
      authorizations++
      return reply({ authorization_state: 'authorized_once', provider_send_state: 'not_attempted', data_scope_hash: 'a'.repeat(64) })
    }
    if (path.endsWith('/execute-authorized')) { executions++; await pending; state = outcome; if (outcome === 'unknown') return route.abort('failed'); return reply({ task_state: outcome }) }
    if (path.includes('/report-generation-tasks/')) return reply({ local_task_id: 'normal-task', task_type: 'daily_report_generate', state, evidence_snapshot_id: 11 })
    return reply({})
  })
  await page.goto('/#/projects/1/task')
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  const risks = page.getByRole('region', { name: '本地内容风险处理' })
  await expect(risks).toContainText('src/style.scss')
  await expect(risks).toContainText('Diff 第 4–4 行')
  await expect(risks).toContainText('[REDACTED:CREDENTIAL]')
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  const authorize = page.getByRole('button', { name: '授权本次发送（尚不调用 AI）', exact: true })
  await expect(authorize).toBeEnabled()
  await expect(risks).toContainText('不需要额外确认')
  await risks.getByRole('button', { name: '取消本次发送' }).click()
  await expect(page.getByRole('region', { name: '真实 AI 发送授权' })).toHaveCount(0)
  expect(authorizations).toBe(0)
  expect(executions).toBe(0)
  await page.getByRole('button', { name: '确认本次发送范围 →', exact: true }).click()
  await expect(authorize).toBeEnabled()
  await authorize.click()
  expect(authorizations).toBe(1)
  expect(executions).toBe(0)
  await page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true }).click()
  try {
    await expect(page.getByText('已提交执行请求，正在等待分析结果', { exact: true }).first()).toBeVisible()
    await expect(page.getByText(/范围已授权，AI 尚未被调用|等待你单独点击开始|已授权 · 尚未发送|待发送资料尚未发送/)).toHaveCount(0)
    for (const button of await page.getByRole('button', { name: '正在调用 AI…', exact: true }).all()) await expect(button).toBeDisabled()
    expect(executions).toBe(1)
  } finally { release() }
  const label = { succeeded: '分析完成', failed: '分析失败', unknown: '结果暂时无法确认' }[outcome]
  await expect(page.getByText(label, { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '开始 AI 分析 · 会调用模型', exact: true })).toHaveCount(0)
  expect(executions).toBe(1)
})

}
