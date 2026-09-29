import { test, expect } from '@playwright/test'
import { createHash } from 'node:crypto'

const settings = { configured: true, version_no: 1, gateway_url: 'http://127.0.0.1:18789', account_id: 'synthetic-account', target: 'synthetic@im.wechat', recipient_label: '验收客户', session_key: 'main', token_configured: true }
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')
const preview = { preview_id: 'preview-test', report_version_id: 7, report_label: '合成报告 #7', config_version_no: 1, recipient_label: '验收客户', created_at: '2026-09-29T00:00:00Z', pages: [1, 2].map(index => ({ index, width: 1, height: 1, sha256: createHash('sha256').update(png).digest('hex'), url: `/api/projects/1/wechat-preview/preview-test/images/${index}` })) }

async function mount(page, overrides = {}, initialSettings = settings) {
  const calls = []
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session')
    localStorage.setItem('anxinboard:onboarding:v1', 'done')
  })
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/wechat-settings')) {
      if (request.method() === 'POST') calls.push({ kind: 'settings', body: request.postDataJSON(), headers: request.headers() })
      return route.fulfill({ json: request.method() === 'POST' ? settings : initialSettings })
    }
    if (path.endsWith('/wechat-history')) return route.fulfill({ json: [] })
    if (path.endsWith('/wechat-binding')) return route.fulfill({ json: { transport: 'openclaw', binding_state: 'unbound', version_no: initialSettings.version_no, account_id: '', target: '', recipient_label: '', context_ready: false } })
    if (path.endsWith('/wechat-preview')) {
      calls.push({ kind: 'preview', body: request.postDataJSON(), headers: request.headers() })
      return route.fulfill({ json: preview })
    }
    if (path.includes('/wechat-preview/')) {
      calls.push({ kind: 'image', headers: request.headers() })
      return route.fulfill({ contentType: 'image/png', body: png })
    }
    if (path.endsWith('/wechat-send')) {
      calls.push({ kind: 'send', body: request.postDataJSON(), headers: request.headers() })
      return route.fulfill({ json: { attempt_id: 'attempt-test', state: 'accepted', report_version_id: 7, recipient_label: '验收客户', pages: [{ index: 1, state: 'accepted' }, { index: 2, state: 'accepted' }] } })
    }
    return route.fulfill({ status: 404, json: {} })
  })
  await page.addInitScript(props => { window.wechatInitialProps = props }, { projectId: 1, displayedReportVersionId: 7, displayedReportHash: 'b'.repeat(64), moduleNarrativeState: 'loaded', displayedModuleNarrativeHash: 'c'.repeat(64), displayedGitMetricsState: 'loaded', ...overrides })
  await page.goto('/tests/e2e/fixtures/wechat-panel.html')
  await expect(page.getByTestId('wechat-panel')).toBeVisible()
  return calls
}

test('配置保存清空令牌并且不触发发送', async ({ page }) => {
  const calls = await mount(page, {}, { configured: false, version_no: 0, session_key: '' })
  await page.getByText('高级接入：使用已有 OpenClaw', { exact: true }).click()
  await expect(page.getByLabel('OpenClaw 会话')).toHaveValue('main')
  await page.getByLabel('OpenClaw 网关地址').fill(settings.gateway_url)
  await page.getByLabel('绑定账号标识').fill(settings.account_id)
  await page.getByLabel('微信接收标识').fill(settings.target)
  await page.getByLabel('接收人名称').fill(settings.recipient_label)
  await page.getByLabel('访问令牌').fill('synthetic-credential-only')
  await page.getByRole('button', { name: '保存微信配置', exact: true }).click()
  await expect(page.getByLabel('访问令牌')).toHaveValue('')
  await expect(page.getByTestId('wechat-status')).toContainText('配置已保存')
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
  const saved = calls.find(c => c.kind === 'settings')
  expect(saved.body.expected_version_no).toBe(0)
  expect(saved.headers['x-anxin-session']).toBe('synthetic-session')
  expect(await page.getByTestId('wechat-panel').innerText()).not.toContain('synthetic-credential-only')
})

test('正式截图预览携带精确版本，图片受保护，确认后只发一次', async ({ page }) => {
  const calls = await mount(page)
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByTestId('wechat-preview').getByRole('img')).toHaveCount(2)
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeEnabled()
  expect(calls.find(c => c.kind === 'preview').body).toEqual({ expected_report_version_id: 7, expected_report_hash: 'b'.repeat(64), expected_module_narrative_hash: 'c'.repeat(64), expected_config_version_no: 1 })
  expect(calls.filter(c => c.kind === 'image').every(c => c.headers['x-anxin-session'] === 'synthetic-session')).toBe(true)
  page.on('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '确认发送图片', exact: true }).click()
  await expect(page.getByTestId('wechat-status')).toContainText('已提交')
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeDisabled()
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(1)
  expect(calls.find(c => c.kind === 'send').body).toEqual({ preview_id: 'preview-test', human_confirmed: true })
})

test('尚无可见正式报告时不能预览或发送', async ({ page }) => {
  const calls = await mount(page, { displayedReportVersionId: null, displayedReportHash: null })
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeDisabled()
  expect(calls.filter(c => ['preview', 'send'].includes(c.kind))).toHaveLength(0)
})

test('取消确认不向微信提交', async ({ page }) => {
  const calls = await mount(page)
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeEnabled()
  page.on('dialog', dialog => dialog.dismiss())
  await page.getByRole('button', { name: '确认发送图片', exact: true }).click()
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('微信结果未知时不重试，显示核对提示', async ({ page }) => {
  const calls = await mount(page)
  let submissions = 0
  await page.route('**/api/projects/1/wechat-send', route => { submissions++; return route.fulfill({ json: { state: 'unknown', pages: [{ index: 1, state: 'unknown' }] } }) })
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  page.on('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '确认发送图片', exact: true }).click()
  await expect(page.getByTestId('wechat-status')).toContainText('无法确认')
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeDisabled()
  expect(submissions).toBe(1)
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('明确的配置拒绝展示可操作原因，未知网关文字不回显', async ({ page }) => {
  await mount(page)
  await page.route('**/api/projects/1/wechat-send', route => route.fulfill({ json: {
    attempt_id: 'config-rejected', report_version_id: 7, recipient_label: '验收客户', state: 'failed',
    retry_requires_config_change: true, pages: [
      { index: 1, state: 'rejected', code: 'GATEWAY_UPLOADS_DISABLED' },
      { index: 2, state: 'not_sent', code: 'private-provider-text-must-not-render' }
    ]
  } }))
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  page.on('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '确认发送图片', exact: true }).click()
  await expect(page.getByTestId('wechat-status')).toContainText('修正并保存配置')
  await expect(page.getByText('第 1 页：网关尚未开启图片上传，请开启后更新配置。')).toBeVisible()
  await expect(page.getByTestId('wechat-panel')).not.toContainText('private-provider-text')
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeDisabled()
})

test('直连失败提示区分重新绑定和未知投递，并保留旧网关历史', async ({ page }) => {
  const calls = await mount(page)
  await page.route('**/api/projects/1/wechat-history', route => route.fulfill({ json: [{
    attempt_id: 'synthetic-history', report_version_id: 7, recipient_label: '验收客户', state: 'unknown',
    pages: [
      { index: 1, state: 'rejected', code: 'ILINK_SESSION_EXPIRED' },
      { index: 2, state: 'unknown', code: 'ILINK_RESPONSE_UNVERIFIED' },
      { index: 3, state: 'unknown', code: 'ILINK_SEND_UNCERTAIN' },
      { index: 4, state: 'rejected', code: 'GATEWAY_UPLOADS_DISABLED' },
      { index: 5, state: 'unknown', code: 'private-provider-text-must-not-render' }
    ]
  }] }))
  await page.getByRole('button', { name: '刷新发送记录', exact: true }).click()
  await expect(page.getByText('第 1 页：微信会话已失效，请重新绑定微信。')).toBeVisible()
  await expect(page.getByText('第 2 页：微信响应无法确认投递结果，请先核对微信，系统不会自动重发。')).toBeVisible()
  await expect(page.getByText('第 3 页：连接中断或等待超时，图片可能已提交，请先核对微信，系统不会自动重发。')).toBeVisible()
  await expect(page.getByText('第 4 页：网关尚未开启图片上传，请开启后更新配置。')).toBeVisible()
  await expect(page.getByTestId('wechat-panel')).not.toContainText('private-provider-text')
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('发送预览前配置有未保存修改时禁止外发', async ({ page }) => {
  const calls = await mount(page)
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeEnabled()
  await page.getByText('高级接入：使用已有 OpenClaw', { exact: true }).click()
  await page.getByLabel('微信接收标识').fill('another@im.wechat')
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeDisabled()
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('下载图片与冻结预览摘要不一致时阻止发送', async ({ page }) => {
  const calls = await mount(page)
  await page.route('**/api/projects/1/wechat-preview', route => route.fulfill({ json: { ...preview, pages: preview.pages.map(p => ({ ...p, sha256: '0'.repeat(64) })) } }))
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByTestId('wechat-status')).toContainText('没有完整加载')
  await expect(page.getByTestId('wechat-preview')).toHaveCount(0)
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('报告在截图生成途中更新时不展示旧预览', async ({ page }) => {
  const calls = await mount(page)
  let release
  const pending = new Promise(resolve => { release = resolve })
  let requested = false
  await page.route('**/api/projects/1/wechat-preview', async route => { requested = true; await pending; await route.fulfill({ json: preview }) })
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect.poll(() => requested).toBe(true)
  await page.evaluate(() => { window.wechatTestProps.displayedReportVersionId = 8; window.wechatTestProps.displayedReportHash = 'd'.repeat(64) })
  release()
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  await expect(page.getByTestId('wechat-preview')).toHaveCount(0)
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('刷新发送历史只读且项目切换清除旧预览', async ({ page }) => {
  const calls = await mount(page)
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByTestId('wechat-preview')).toBeVisible()
  await page.getByRole('button', { name: '刷新发送记录', exact: true }).click()
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
  await page.evaluate(() => { window.wechatTestProps.projectId = 2 })
  await expect(page.getByTestId('wechat-preview')).toHaveCount(0)
  expect(calls.filter(c => c.kind === 'send')).toHaveLength(0)
})

test('发送前发起的迟到历史查询不会抹掉已完成记录', async ({ page }) => {
  await mount(page)
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认发送图片', exact: true })).toBeEnabled()
  let release
  let requested = false
  const pending = new Promise(resolve => { release = resolve })
  await page.route('**/api/projects/1/wechat-history', async route => {
    requested = true
    await pending
    await route.fulfill({ json: [] })
  })
  await page.getByRole('button', { name: '刷新发送记录', exact: true }).click()
  await expect.poll(() => requested).toBe(true)
  page.on('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '确认发送图片', exact: true }).click()
  await expect(page.getByText('报告 #7 · 验收客户', { exact: true })).toBeVisible()
  release()
  await expect(page.getByText('报告 #7 · 验收客户', { exact: true })).toBeVisible()
  await expect(page.getByText('暂无微信发送记录。', { exact: true })).toHaveCount(0)
})
