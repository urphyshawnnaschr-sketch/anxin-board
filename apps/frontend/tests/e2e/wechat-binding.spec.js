import { test, expect } from '@playwright/test'
import { createHash } from 'node:crypto'

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')
const unbound = { transport: 'direct', binding_state: 'unbound', version_no: 0, account_id: '', target: '', recipient_label: '', context_ready: false }
const ready = { ...unbound, binding_state: 'ready', version_no: 2, account_id: 'synthetic-account', target: 'synthetic@im.wechat', recipient_label: '扫码绑定的微信', context_ready: true }
const expiry = () => new Date(Date.now() + 120_000).toISOString()

async function mount(page, { binding = unbound, statuses = ['wait'], expiresAt = expiry(), start, poll } = {}) {
  const calls = []
  let currentBinding = binding
  let polls = 0
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session')
    window.wechatInitialProps = { projectId: 1, displayedReportVersionId: 7, displayedReportHash: 'b'.repeat(64), moduleNarrativeState: 'absent', displayedModuleNarrativeHash: null, displayedGitMetricsState: 'loaded' }
    window.revokedWechatUrls = []
    const revoke = URL.revokeObjectURL.bind(URL)
    URL.revokeObjectURL = value => { window.revokedWechatUrls.push(value); revoke(value) }
  })
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const body = request.method() === 'POST' ? request.postDataJSON() : null
    calls.push({ path, method: request.method(), body, headers: request.headers() })
    if (path.endsWith('/wechat-settings')) return route.fulfill({ json: { ...currentBinding, configured: ['ready', 'awaiting_message'].includes(currentBinding.binding_state), token_configured: ['ready', 'awaiting_message'].includes(currentBinding.binding_state), gateway_url: '', session_key: '' } })
    if (path.endsWith('/wechat-history')) return route.fulfill({ json: [] })
    if (path.endsWith('/wechat-binding')) return route.fulfill({ json: currentBinding })
    if (path.endsWith('/wechat-login/start')) {
      if (start) return start(route)
      return route.fulfill({ json: { flow_id: 'synthetic-flow', status: 'wait', expires_at: expiresAt, qr_url: 'https://must-never-fetch.example/private-login' } })
    }
    if (path.endsWith('/qr.png')) return route.fulfill({ contentType: 'image/png', body: png })
    if (path.endsWith('/wechat-login/poll')) {
      if (poll) return poll(route)
      const status = statuses[Math.min(polls++, statuses.length - 1)]
      if (status === 'awaiting_message') currentBinding = { ...ready, binding_state: 'awaiting_message', context_ready: false, version_no: 1 }
      if (status === 'ready') currentBinding = ready
      return route.fulfill({ json: { flow_id: 'synthetic-flow', status, expires_at: expiresAt, binding: ['awaiting_message', 'ready'].includes(status) ? currentBinding : null } })
    }
    if (path.endsWith('/wechat-login/cancel')) return route.fulfill({ json: { cancelled: true, binding: currentBinding } })
    if (path.endsWith('/wechat-binding/refresh')) { currentBinding = ready; return route.fulfill({ json: ready }) }
    if (path.endsWith('/wechat-binding/disconnect')) { currentBinding = { ...unbound, version_no: 3, binding_state: 'disconnected' }; return route.fulfill({ json: currentBinding }) }
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/tests/e2e/fixtures/wechat-panel.html')
  await expect(page.getByTestId('wechat-binding')).toBeVisible()
  await expect(page.getByRole('button', { name: ['ready', 'awaiting_message'].includes(binding.binding_state) ? '重新绑定微信' : '绑定微信', exact: true })).toBeEnabled()
  return calls
}

test('默认只需绑定微信，高级技术配置折叠且不自动创建二维码', async ({ page }) => {
  const calls = await mount(page)
  await expect(page.getByLabel('OpenClaw 网关地址')).not.toBeVisible()
  await expect(page.getByText('若这个微信已在其他工具使用 ClawBot，重新绑定可能影响原连接；可改用高级接入。')).toBeVisible()
  expect(calls.filter(c => c.method === 'POST')).toHaveLength(0)
})

test('扫码确认后无需首条消息即可预览，二维码仅从受保护本地图片读取', async ({ page }) => {
  const calls = await mount(page, { statuses: ['scaned', 'awaiting_message'] })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  const qr = page.getByRole('img', { name: '微信绑定二维码' })
  await expect(qr).toBeVisible()
  expect(await qr.getAttribute('src')).toMatch(/^blob:/)
  await expect(page.getByTestId('wechat-binding')).toContainText('请在微信中确认', { timeout: 7000 })
  await expect(page.getByTestId('wechat-binding')).toContainText('微信已绑定，可直接生成图片预览并确认发送', { timeout: 7000 })
  await expect(page.getByTestId('wechat-binding')).not.toContainText('给 ClawBot 发一句话')
  await expect(qr).toHaveCount(0)
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  expect(calls.find(c => c.path.endsWith('/qr.png')).headers['x-anxin-session']).toBe('synthetic-session')
  expect(calls.filter(c => c.path.includes('/wechat-login/')).filter(c => c.method === 'POST').every(c => c.headers['local-idempotency-key'])).toBe(true)
  expect(calls.filter(c => c.path.endsWith('/wechat-send'))).toHaveLength(0)
})

test('取消会清理二维码并拒绝迟到的已绑定响应', async ({ page }) => {
  let release
  let pendingPoll = false
  const pending = new Promise(resolve => { release = resolve })
  const calls = await mount(page, { poll: async route => { pendingPoll = true; await pending; await route.fulfill({ json: { flow_id: 'synthetic-flow', status: 'ready', expires_at: expiry(), binding: ready } }).catch(() => {}) } })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toBeVisible()
  await expect.poll(() => pendingPoll).toBe(true)
  await page.getByRole('button', { name: '取消绑定', exact: true }).click()
  release()
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toHaveCount(0)
  await expect(page.getByTestId('wechat-binding')).not.toContainText('微信已绑定，可直接生成图片预览并确认发送')
  expect(calls.filter(c => c.path.endsWith('/wechat-login/cancel'))).toHaveLength(1)
  expect(await page.evaluate(() => window.revokedWechatUrls.length)).toBeGreaterThan(0)
})

test('项目切换丢弃迟到的开始响应并取消旧项目二维码', async ({ page }) => {
  let release
  let requested = false
  const pending = new Promise(resolve => { release = resolve })
  const calls = await mount(page, { start: async route => { requested = true; await pending; await route.fulfill({ json: { flow_id: 'old-flow', status: 'wait', expires_at: expiry(), qr_url: '/ignored' } }) } })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect.poll(() => requested).toBe(true)
  await page.evaluate(() => { window.wechatTestProps.projectId = 2 })
  release()
  await expect.poll(() => calls.some(c => c.path === '/api/projects/1/wechat-login/cancel' && c.body.flow_id === 'old-flow')).toBe(true)
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toHaveCount(0)
  expect(calls.filter(c => c.path.endsWith('/qr.png'))).toHaveLength(0)
})

test('过期后清理二维码并停止自动轮询', async ({ page }) => {
  const calls = await mount(page, { expiresAt: new Date(Date.now() + 1600).toISOString() })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByTestId('wechat-binding')).toContainText('二维码已过期', { timeout: 6000 })
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toHaveCount(0)
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(0)
  await page.route('**/api/projects/1/wechat-login/start', route => route.fulfill({ json: { flow_id: 'synthetic-fresh-flow', status: 'wait', expires_at: expiry(), qr_url: '/ignored' } }))
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toBeVisible()
  await expect(page.getByTestId('wechat-binding')).not.toContainText('二维码已过期')
})

test('绑定凭据失效只展示固定重新绑定提示，不回显上游响应', async ({ page }) => {
  await mount(page, { poll: route => route.fulfill({ status: 409, json: { detail: { code: 'ILINK_SESSION_EXPIRED', message: 'private-provider-text-must-not-render' } } }) })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByTestId('wechat-binding')).toContainText('微信会话已失效，请重新绑定微信', { timeout: 7000 })
  await expect(page.getByTestId('wechat-binding')).not.toContainText('private-provider-text')
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '绑定微信', exact: true })).toBeEnabled()
})

test('需要验证码时只接受人工输入并在提交后清空', async ({ page }) => {
  const calls = await mount(page, { statuses: ['need_verifycode', 'awaiting_message'] })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  const field = page.getByLabel('微信验证码')
  await expect(field).toBeVisible({ timeout: 7000 })
  await expect(field).toHaveAttribute('type', 'password')
  await expect(field).toHaveAttribute('maxlength', '12')
  const beforeCode = calls.filter(c => c.path.endsWith('/wechat-login/poll')).length
  await field.fill('abc123')
  await expect(page.getByRole('button', { name: '提交验证码', exact: true })).toBeDisabled()
  await field.fill('123')
  await expect(page.getByRole('button', { name: '提交验证码', exact: true })).toBeDisabled()
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(beforeCode)
  await field.fill('123456')
  await page.getByRole('button', { name: '提交验证码', exact: true }).click()
  await expect(field).toHaveCount(0)
  expect(calls.filter(c => c.body?.verification_code === '123456')).toHaveLength(1)
  await expect(page.getByTestId('wechat-binding')).not.toContainText('123456')
})

test('重新打开无会话绑定可直接预览，仅人工选择时刷新会话', async ({ page }) => {
  const calls = await mount(page, { binding: { ...ready, binding_state: 'awaiting_message', context_ready: false, version_no: 1 } })
  await expect(page.getByTestId('wechat-binding')).toContainText('微信已绑定，可直接生成图片预览并确认发送')
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  expect(calls.filter(c => c.method === 'POST')).toHaveLength(0)
  await page.getByRole('button', { name: '刷新会话（可选）', exact: true }).click()
  await expect(page.getByTestId('wechat-binding')).toContainText('微信已绑定，可直接生成图片预览并确认发送')
  expect(calls.find(c => c.path.endsWith('/wechat-binding/refresh')).body).toEqual({ expected_version_no: 1 })
})

test('可选刷新未收到消息仍可直接预览，不要求先发消息', async ({ page }) => {
  const calls = await mount(page, { binding: { ...ready, binding_state: 'awaiting_message', context_ready: false, version_no: 1 } })
  await page.route('**/api/projects/1/wechat-binding/refresh', route => route.fulfill({ json: { ...ready, binding_state: 'awaiting_message', context_ready: false, version_no: 1 } }))
  await page.getByRole('button', { name: '刷新会话（可选）', exact: true }).click()
  await expect(page.getByTestId('wechat-binding')).toContainText('尚未收到新的会话消息，仍可直接生成预览并确认发送')
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  expect(calls.filter(c => c.path.endsWith('/wechat-send'))).toHaveLength(0)
})

test('轮询失败停止并只展示安全提示', async ({ page }) => {
  const calls = await mount(page, { poll: route => route.fulfill({ status: 502, json: { detail: { code: 'WECHAT_PROVIDER_UNAVAILABLE', message: 'private-upstream-text-must-not-render' } } }) })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByTestId('wechat-binding')).toContainText('暂时无法确认绑定状态', { timeout: 7000 })
  await expect(page.getByTestId('wechat-binding')).not.toContainText('private-upstream')
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toHaveCount(0)
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(1)
})

test('轮询始终串行，等待响应期间不会叠加下一次请求', async ({ page }) => {
  await page.clock.install()
  let release
  const pending = new Promise(resolve => { release = resolve })
  const calls = await mount(page, { poll: async route => { await pending; await route.fulfill({ json: { flow_id: 'synthetic-flow', status: 'wait', expires_at: expiry(), binding: null } }) } })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toBeVisible()
  await page.clock.fastForward(2100)
  await expect.poll(() => calls.filter(c => c.path.endsWith('/wechat-login/poll')).length).toBe(1)
  await page.clock.fastForward(8000)
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(1)
  release()
  await page.getByRole('button', { name: '取消绑定', exact: true }).click()
})

test('离开组件停止扫码并清理图片，不请求新二维码', async ({ page }) => {
  await page.clock.install()
  const calls = await mount(page)
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toBeVisible()
  await page.evaluate(() => window.unmountWechatTestPanel())
  await expect.poll(() => calls.filter(c => c.path.endsWith('/wechat-login/cancel')).length).toBe(1)
  await page.clock.fastForward(10_000)
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(0)
  expect(calls.filter(c => c.path.endsWith('/wechat-login/start'))).toHaveLength(1)
  expect(await page.evaluate(() => window.revokedWechatUrls.length)).toBe(1)
})

test('扫码已绑定就结束轮询，旧二维码到期不取消有效绑定', async ({ page }) => {
  await page.clock.install()
  const calls = await mount(page, { statuses: ['awaiting_message'] })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByRole('img', { name: '微信绑定二维码' })).toBeVisible()
  await page.clock.fastForward(2100)
  await expect(page.getByRole('button', { name: '取消绑定', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  await page.clock.fastForward(125_000)
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(1)
  expect(calls.filter(c => c.path.endsWith('/wechat-login/cancel'))).toHaveLength(0)
  expect(calls.filter(c => c.path.endsWith('/wechat-binding/refresh'))).toHaveLength(0)
  expect(calls.filter(c => c.path.endsWith('/wechat-binding/disconnect'))).toHaveLength(0)
})

test('扫码响应缺少有效绑定时不能把无会话状态当作成功', async ({ page }) => {
  const calls = await mount(page, { poll: route => route.fulfill({ json: { flow_id: 'synthetic-flow', status: 'awaiting_message', expires_at: expiry(), binding: null } }) })
  await page.getByRole('button', { name: '绑定微信', exact: true }).click()
  await expect(page.getByTestId('wechat-binding')).toContainText('暂时无法确认绑定状态', { timeout: 7000 })
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeDisabled()
  expect(calls.filter(c => c.path.endsWith('/wechat-login/poll'))).toHaveLength(1)
})

test('进入扫码前清除已有正式图片预览', async ({ page }) => {
  const calls = await mount(page, { binding: ready })
  await page.route('**/api/projects/1/wechat-preview', route => route.fulfill({ json: {
    preview_id: 'synthetic-preview', report_version_id: 7, report_label: '合成报告', config_version_no: 2,
    recipient_label: '扫码绑定的微信', pages: [{ index: 1, width: 1, height: 1, sha256: createHash('sha256').update(png).digest('hex') }]
  } }))
  await page.route('**/api/projects/1/wechat-preview/synthetic-preview/images/1', route => route.fulfill({ contentType: 'image/png', body: png }))
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '生成图片预览', exact: true }).click()
  await expect(page.getByTestId('wechat-preview')).toBeVisible()
  await page.getByRole('button', { name: '重新绑定微信', exact: true }).click()
  await expect(page.getByRole('button', { name: '生成图片预览', exact: true })).toBeDisabled()
  await expect(page.getByTestId('wechat-preview')).toHaveCount(0)
  await expect(page.getByTestId('wechat-binding')).not.toContainText('微信已绑定，可直接生成图片预览并确认发送')
  expect(calls.filter(c => c.path.endsWith('/wechat-send'))).toHaveLength(0)
})
