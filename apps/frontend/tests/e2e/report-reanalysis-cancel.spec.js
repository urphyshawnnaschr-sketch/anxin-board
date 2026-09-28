import { test, expect } from '@playwright/test'

async function fixture(page, { refused = false, cancelledInitially = false } = {}) {
  let cancelled = cancelledInitially
  const writes = []
  const hash = 'a'.repeat(64)
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'mock-session'))
  await page.route('**/api/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const report = { project_id: 6, report_version_id: 7, state_version: cancelled ? 3 : 2,
      version_no: 1, lifecycle: cancelled ? 'pending_review' : 'superseded', report_content_hash: hash, evidence_snapshot_id: 5 }
    const bundle = { source_report_version: report,
      reanalysis_request: { request_hash: hash, replacement_local_task_id: 'replacement-7' },
      replacement_task: { id: 12, local_task_id: 'replacement-7', task_type: 'daily_report_regenerate', identity_hash: hash,
        evidence_snapshot_id: 5, state: cancelled ? 'voided' : 'queued' },
      cancelled, cancellation: cancelled ? { state: 'cancelled_before_send', cancelled_by: '验收人', cancelled_at: '2026-09-28T10:00:00Z' } : null }
    if (req.method() !== 'GET') {
      writes.push({ path, payload: req.postDataJSON(), headers: req.headers() })
      if (path.endsWith('/reanalysis/cancel')) {
        if (refused) return route.fulfill({ status: 409, json: { detail: { code: 'REANALYSIS_CANCEL_UNSAFE', message: '任务已经开始执行，不能撤销。' } } })
        cancelled = true
        return route.fulfill({ json: { ...bundle, cancelled: true, cancellation: { state: 'cancelled_before_send' } } })
      }
      return route.fulfill({ status: 409, json: { detail: { code: 'UNEXPECTED_WRITE' } } })
    }
    if (path === '/api/projects/6') return route.fulfill({ json: { id: 6, name: '模拟验收', status: 'active', version: 1 } })
    if (path.endsWith('/report-review/current')) return route.fulfill({ json: { project_id: 6, report_version: report, ai_raw: { content: { plain_summary: '原始分析保留不变。' } } } })
    if (path.endsWith('/reanalysis')) return route.fulfill({ json: bundle })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/#/projects/6/review')
  return writes
}

test('cancel unsent reanalysis restores review across reload without provider calls', async ({ page }) => {
  const writes = await fixture(page)
  await page.getByRole('button', { name: '撤销重分析并恢复审阅', exact: true }).click()
  await page.getByLabel('撤销执行者').fill('Codex（用户授权验收）')
  await page.getByLabel('撤销原因').fill('保留已有分析，继续报告审阅。')
  await page.getByRole('button', { name: '确认撤销并恢复审阅', exact: true }).click()
  await expect(page.getByText('未发送的重分析已撤销，原报告已恢复审阅。', { exact: true })).toBeVisible()
  await expect(page.getByText('原始分析保留不变。')).toBeVisible()
  expect(writes).toHaveLength(1)
  expect(writes[0].path).toBe('/api/projects/6/reports/7/reanalysis/cancel')
  expect(writes[0].payload).toMatchObject({ expected_report_state_version: 2,
    expected_reanalysis_request_hash: 'a'.repeat(64), expected_replacement_task_identity_hash: 'a'.repeat(64),
    cancelled_by: 'Codex（用户授权验收）', cancellation_reason: '保留已有分析，继续报告审阅。' })
  expect(writes[0].payload.idempotency_key).toBeTruthy()
  expect(writes[0].headers['local-idempotency-key']).toBeTruthy()
  await page.reload()
  await expect(page.getByText('已撤销未发送的重分析', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '准备 AI 重分析发送范围', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '退回重分析', exact: true })).toBeDisabled()
  expect(writes).toHaveLength(1)
})

test('refused cancellation keeps superseded report and error visible', async ({ page }) => {
  const writes = await fixture(page, { refused: true })
  await page.getByRole('button', { name: '撤销重分析并恢复审阅', exact: true }).click()
  await page.getByLabel('撤销执行者').fill('验收人')
  await page.getByLabel('撤销原因').fill('恢复审阅')
  await page.getByRole('button', { name: '确认撤销并恢复审阅', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('任务已经开始执行')
  await expect(page.getByText('本版已经退回重分析', { exact: true })).toBeVisible()
  expect(writes).toHaveLength(1)
})
