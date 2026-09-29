// @ts-check
import { test, expect } from '@playwright/test'

const project = {
  id: 1,
  name: '甲方演示项目',
  status: 'draft',
  version: 1,
  git_url: 'https://example.invalid/demo.git',
  branch: 'main',
  created_at: '2026-09-20T00:00:00Z',
  updated_at: '2026-09-20T00:00:00Z'
}

const stageRows = [
  ['local_app', 'Windows 本地软件', '开发中', 'active'],
  ['project_management', '项目管理', '开发中', 'active'],
  ['git', '代码版本读取', '已完成', 'done'],
  ['prd', '需求文档读取', '已完成', 'done'],
  ['project_profile', '项目基础资料', '开发中', 'active'],
  ['anxin_board', '安心看板自动生成', '测试中', 'checking'],
  ['ai_correction', 'AI 结果纠正', '开发中', 'active'],
  ['formal_report', '正式 AI 报告', '等待测试', 'waiting'],
  ['email', '邮件发送', '暂时无法确认', 'unknown'],
  ['report_evidence', '报告依据与留痕', '开发中', 'active'],
  ['history', '历史报告查看', '开发中', 'active']
]

const reportV1 = {
  schema_version: 'anxin_board_report_v1',
  title: '安心看板',
  motto: '非己所安，不加于物',
  project_name: '甲方演示项目',
  report_date: '2026-09-20',
  module_count: 11,
  completed_module_count: 2,
  active_module_count: 8,
  unknown_module_count: 1,
  overall_message: '项目按计划推进。',
  modules: stageRows.map(([module_id, name, stage, tone]) => ({
    module_id,
    name,
    stage,
    display_stage: stage,
    tone,
    summary: `${name}当前状态。`,
    next_step: `${name}下一步。`,
    client_stage_summary_hash: 'a'.repeat(64)
  })),
  daily_change: {
    display_state: 'ready',
    headline: '今天项目有明确进展。',
    summary: '今天完成了可核实的研发变化。',
    highlights: [],
    scope_note: '这些数字只说明研发变化，不代表项目完成度，也不用于评价个人绩效。',
    plain_language_change_summary_hash: 'b'.repeat(64)
  },
  manager_supplement: '项目经理已确认。',
  anxin_board_report_hash: 'c'.repeat(64)
}

async function routeCommon(page, reportBody) {
  await page.route('**/api/projects/1/wechat-settings', route => route.fulfill({
    json: { configured: false, version_no: 0, token_configured: false }
  }))
  await page.route('**/api/projects/1/wechat-history', route => route.fulfill({ json: [] }))
  await page.route('**/api/projects/1/wechat-binding', route => route.fulfill({ json: { transport: 'direct', binding_state: 'unbound', version_no: 0, context_ready: false } }))
  await page.route('**/api/projects/1', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(project)
  }))
  await page.route('**/api/projects/1/anxin-board/latest', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(reportBody)
  }))
  await page.route('**/api/settings/mail-transport', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schema_version: 'mail_transport_settings_v1',
      configured: true,
      credential_reference_bound: true,
      profile: {
        version_no: 1,
        host: 'smtp.qq.com',
        port: 465,
        security: 'implicit_tls',
        username: 'sender@example.test',
        from_identity: 'sender@example.test',
        timeout_seconds: 30,
        configured_by: 'test',
        created_at: '2026-09-20T00:00:00Z'
      }
    })
  }))
  await page.route('**/api/projects/1/recipient-config', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schema_version: 'recipient_config_settings_v1',
      configured: true,
      version_no: 1,
      to_recipients: ['recipient@example.test']
    })
  }))
}

test('正式安心看板不向甲方展示系统说明和技术占位文案', async ({ page }) => {
  await routeCommon(page, reportV1)
  await page.goto('/#/projects/1/board')

  const board = page.getByTestId('anxin-board')
  await expect(board).toBeVisible()
  await expect(board.getByRole('heading', { name: '01 本次工作与进展', exact: true })).toBeVisible()
  await expect(board.getByRole('heading', { name: '02 当前功能状态', exact: true })).toBeVisible()
  await expect(board.getByRole('heading', { name: '03 项目经理补充与下一步', exact: true })).toBeVisible()
  await expect(board.getByRole('heading', { name: '04 报告信息', exact: true })).toBeVisible()

  const visible = await board.innerText()
  for (const forbidden of [
    '代码行数只说明本次 Git 范围的客观变化量',
    '这些数字只说明研发变化',
    '只说甲方能直接理解的话',
    '模型与来源信息',
    '输入 Token',
    '输出 Token',
    'Git / AI / PM-human 来源边界',
    '责任边界',
    '固定责任说明暂不可用',
    '安心看板 · 简洁书法版 · 2026-09-20'
  ]) {
    expect(visible).not.toContain(forbidden)
  }
})

test('Page08 报告 identity 不完整时保留邮件入口但阻止发送', async ({ page }) => {
  const reportForSend = {
    schema_version: 'anxin_board_report_v3',
    report_version_id: 19,
    report_date: '2026-09-20'
  }
  await routeCommon(page, reportForSend)
  await page.addInitScript(() => {
    sessionStorage.setItem('anxinboard:local-browser-session:v1', 'test-local-session')
  })

  /** @type {any} */
  let posted = null
  await page.route('**/api/projects/1/mail-send', async route => {
    const request = route.request()
    posted = request.postDataJSON()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'mail_send_action_v1',
        send_attempt_id: 'mail-test',
        project_id: 1,
        report_version_id: 19,
        state: 'sent',
        terminal: true,
        from_identity: 'sender@example.test',
        to_recipients: ['recipient@example.test'],
        subject: '甲方演示项目｜2026-09-20 安心看板',
        terminal_code: 'MAIL_SEND_SENT',
        terminal_summary: 'accepted',
        recipient_results: []
      })
    })
  })

  await page.goto('/#/projects/1/board')

  const panel = page.getByTestId('mail-send-panel')
  await expect(panel).toBeVisible()
  await expect(panel.getByText('sender@example.test', { exact: true })).toBeVisible()
  await expect(panel.getByText('recipient@example.test', { exact: true })).toBeVisible()
  const sendButton = page.getByTestId('mail-send-button')
  await expect(sendButton).toBeDisabled()
  await expect(panel.getByTestId('mail-display-binding')).toContainText('请先查看当前最新的完整报告')
  expect(posted).toBeNull()
})

for (const scenario of [
  { name: '无正式报告且配置正常', status: 404, code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', empty: true },
  { name: '报告500仍为读取失败', status: 500, code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', empty: false },
  { name: '其他404不可当无报告', status: 404, code: 'OTHER_NOT_FOUND', empty: false },
  { name: '无报告但发件配置失败仍拒绝', status: 404, code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', configFailure: 'mail-transport', empty: false },
  { name: '无报告但收件配置失败仍拒绝', status: 404, code: 'ANXIN_BOARD_REPORT_NOT_AVAILABLE', configFailure: 'recipient-config', empty: false }
]) {
  test(`邮件准备：${scenario.name}且零POST`, async ({ page }) => {
    let posts = 0
    page.on('request', request => { if (request.method() === 'POST') posts += 1 })
    await page.route('**/api/**', route => new URL(route.request().url()).pathname.startsWith('/api/')
      ? route.fulfill({ status: 404, json: { detail: { code: 'SYNTHETIC_UNPROVIDED' } } }) : route.continue())
    await routeCommon(page, reportV1)
    await page.route('**/api/projects/1/anxin-board/latest', route => route.fulfill({
      status: scenario.status, json: { detail: { code: scenario.code } }
    }))
    if (scenario.configFailure) {
      await page.route(`**/api/${scenario.configFailure === 'mail-transport' ? 'settings/mail-transport' : 'projects/1/recipient-config'}`,
        route => route.fulfill({ status: 500, json: { detail: { code: 'SYNTHETIC_READ_FAILED' } } }))
    }
    await page.goto('/#/projects/1/board')
    const panel = page.getByTestId('mail-send-panel')
    if (scenario.empty) {
      await expect(panel.getByText('尚无已确认报告，请先审阅并确认', { exact: true })).toBeVisible()
      await expect(panel.getByTestId('mail-send-from')).toHaveText('sender@example.test')
      await expect(panel.locator('.error')).toHaveCount(0)
    } else {
      await expect(panel.getByText('发送准备信息读取失败，请刷新后重试。', { exact: true })).toBeVisible()
    }
    await expect(panel.getByTestId('mail-send-button')).toBeDisabled()
    expect(posts).toBe(0)
  })
}
