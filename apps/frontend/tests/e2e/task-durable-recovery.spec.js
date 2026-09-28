import { test, expect } from '@playwright/test'

const original = { project_id: 1, local_task_id: 'durable-original', evidence_snapshot_id: 11,
  task_type: 'daily_report_generate', state: 'queued', current_attempt: { sequence_no: 1 } }

async function setup(page, { snapshot = true, task = original, status = 200, conflict = false, stale = false, oldSnapshot = false } = {}) {
  const seen = { lookups: [], creates: 0, prepares: 0, preparedTaskIds: [], sends: 0 }
  await page.addInitScript(({ snapshot, stale }) => {
    if (snapshot) sessionStorage.setItem('rd-agent:project:1:evidence-snapshot-id', '11')
    if (stale) sessionStorage.setItem('rd-agent:project:1:report-generation-task-id', 'stale-cache')
  }, { snapshot, stale })
  await page.route('**/*', async route => {
    const req = route.request()
    const url = new URL(req.url())
    const p = url.pathname
    if (!p.startsWith('/api/')) return route.continue()
    const reply = (body, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })
    const project = { id: 1, name: 'Recovery fixture', status: 'active', version: 1 }
    if (p === '/api/projects') return reply([project])
    if (p === '/api/projects/1') return reply(project)
    if (p.endsWith('/report-generation-tasks/active')) {
      seen.lookups.push(url.searchParams.get('evidence_snapshot_id'))
      if (status !== 200) return reply({ detail: { message: '持久化状态异常：存在多条 active task' } }, status)
      return reply({ task: conflict && !seen.creates ? null : task })
    }
    if (p.endsWith('/report-generation-tasks') && req.method() === 'POST') {
      seen.creates++
      if (conflict) return reply({ detail: { code: 'REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT' } }, 409)
      return reply({ ...original, local_task_id: req.postDataJSON().local_task_id })
    }
    if (p.endsWith('/stale-cache')) return oldSnapshot
      ? reply({ ...original, local_task_id: 'stale-cache', evidence_snapshot_id: 10 })
      : reply({ detail: { message: 'not found' } }, 404)
    if (p.endsWith('/prepare-model-call')) {
      seen.prepares++
      seen.preparedTaskIds.push(p.split('/').at(-2))
      return reply({})
    }
    if (p.includes('/execute') || p.includes('/authorize-send')) { seen.sends++; return reply({}) }
    if (p.endsWith('/durable-original')) return reply(original)
    return reply({})
  })
  await page.goto('/#/projects/1/task')
  return seen
}

for (const snapshot of [true, false]) test(`restores durable queued task with cached snapshot=${snapshot}`, async ({ page }) => {
  const seen = await setup(page, { snapshot })
  await expect(page.getByText('已恢复原有分析任务。没有创建新任务，也没有调用 AI。')).toBeVisible()
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toBeEnabled()
  expect(seen.lookups).toEqual([snapshot ? '11' : null])
  expect(seen.creates).toBe(0)
  expect(seen.prepares + seen.sends).toBe(0)
  expect(await page.evaluate(() => sessionStorage.getItem('rd-agent:project:1:report-generation-task-id'))).toBe('durable-original')
  await page.reload()
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toBeEnabled()
  expect(seen.creates).toBe(0)
  // Restoring never prepares/sends on its own; an explicit local preparation
  // continues the original durable chain, without admitting another task.
  expect(seen.prepares + seen.sends).toBe(0)
  await page.getByRole('button', { name: '准备 AI 调用', exact: true }).click()
  await expect.poll(() => seen.prepares).toBe(1)
  expect(seen.preparedTaskIds).toEqual(['durable-original'])
  expect(seen.creates + seen.sends).toBe(0)
})

test('zero durable active tasks permits initial creation', async ({ page }) => {
  const seen = await setup(page, { task: null })
  await expect(page.getByRole('button', { name: '准备本次分析', exact: true })).toBeEnabled()
  expect(seen.creates).toBe(0)
})

for (const status of [409, 503]) test(`ambiguous or unavailable durable state blocks creation (${status})`, async ({ page }) => {
  const seen = await setup(page, { status })
  await expect(page.getByRole('heading', { name: '任务状态无法确认' })).toBeVisible()
  await expect(page.getByRole('button', { name: '准备本次分析', exact: true })).toBeDisabled()
  expect(seen.creates + seen.sends).toBe(0)
})

for (const state of ['running', 'unknown']) test(`restores ${state} without retry`, async ({ page }) => {
  const seen = await setup(page, { task: { ...original, state } })
  await expect(page.getByText('已恢复原有分析任务。没有创建新任务，也没有调用 AI。')).toBeVisible()
  await expect(page.getByRole('button', { name: '准备 AI 调用', exact: true })).toHaveCount(0)
  expect(seen.creates + seen.prepares + seen.sends).toBe(0)
})

test('create race recovers winner instead of creating a second chain', async ({ page }) => {
  const seen = await setup(page, { conflict: true })
  await page.getByRole('button', { name: '准备本次分析', exact: true }).click()
  await expect(page.getByText('已恢复原有分析任务。没有创建新任务，也没有调用 AI。')).toBeVisible()
  expect(seen.creates).toBe(1)
  expect(seen.lookups).toHaveLength(2)
  expect(seen.prepares + seen.sends).toBe(0)
})

test('stale missing local id recovers durable identity', async ({ page }) => {
  const seen = await setup(page, { stale: true })
  await expect(page.getByText('已恢复原有分析任务。没有创建新任务，也没有调用 AI。')).toBeVisible()
  expect(seen.creates).toBe(0)
})

test('valid old task cache cannot replace the currently selected snapshot', async ({ page }) => {
  const seen = await setup(page, { stale: true, oldSnapshot: true })
  await expect(page.getByText('已恢复原有分析任务。没有创建新任务，也没有调用 AI。')).toBeVisible()
  expect(seen.lookups).toEqual(['11'])
  expect(await page.evaluate(() => sessionStorage.getItem('rd-agent:project:1:evidence-snapshot-id'))).toBe('11')
  expect(await page.evaluate(() => sessionStorage.getItem('rd-agent:project:1:report-generation-task-id'))).toBe('durable-original')
  expect(seen.creates).toBe(0)
})
