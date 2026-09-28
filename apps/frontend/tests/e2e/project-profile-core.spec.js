// @ts-check
import { expect } from '@playwright/test'
import { test, registerEndOfTestAssertions, isFailureAllowed } from './fixtures/projectProfileApi.js'

function profilePanel(page) {
  return page.locator('section[aria-labelledby="profile-panel-title"]')
}

async function openModules(page, projectId) {
  await page.goto(`/#/projects/${projectId}/modules`)
  const panel = profilePanel(page)
  await expect(panel.locator('#profile-panel-title')).toHaveText('代码盘点')
  return panel
}

async function expandAdvancedProfile(panel) {
  const details = panel.locator('details.profile-advanced')
  await expect(details).toBeVisible()
  await details.locator('summary').click()
  await expect(panel.getByLabel('项目概述')).toBeVisible()
}

test.describe('功能模块确认核心流程', () => {
  registerEndOfTestAssertions()

  test('E2E-01 空项目保持零写且未授权时不得调用真实 AI', async ({ page, requestLedger, mockApi }) => {
    mockApi.writePolicy = {}

    const panel = await openModules(page, 3)
    await expect(panel.getByText('先让 AI 按 PRD 整理计划功能', { exact: true })).toBeVisible()
    const consent = panel.getByRole('checkbox').last()
    await expect(consent).not.toBeChecked()
    await expect(panel.getByRole('button', { name: '生成第一版计划功能' })).toBeDisabled()
    await expect(panel.getByRole('button', { name: '创建空白候选' })).toHaveCount(0)

    expect(requestLedger.getWrites(), 'E2E-01: 未经用户授权不得产生写请求').toBe(0)
    expect(requestLedger.getEndpointCount('POST', '/api/projects/3/profile-candidates'), 'E2E-01: 不得恢复旧 blank-candidate POST').toBe(0)
    expect(requestLedger.getEndpointCount('POST', '/api/projects/3/profile-candidates/generate'), 'E2E-01: 未勾选真实模型授权时不得调用 generate').toBe(0)
  })

  test('E2E-02 已确认项目进入只读模式且零写请求', async ({ page, requestLedger, mockApi }) => {
    mockApi.writePolicy = {}

    const panel = await openModules(page, 2)
    await expect(panel.getByText('功能档案第 1 版 · 已确认', { exact: true })).toBeVisible()
    await expect(panel.getByText('已确认的功能模块 · V1', { exact: true })).toBeVisible()
    if (await panel.locator('details.profile-plan-details').getAttribute('open') === null) await panel.locator('details.profile-plan-details > summary').click()
    await expect(panel.getByText('角色管理', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: '保存修改' })).toHaveCount(0)
    await expect(panel.getByRole('button', { name: /确认并使用 V/ })).toHaveCount(0)
    await expect(panel.getByRole('button', { name: '需要修改这版' })).toBeVisible()
    await expect(panel.getByRole('button', { name: '创建 V2 功能档案候选', exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: '下一步：选择研发范围 →' })).toHaveCount(0)

    expect(requestLedger.getWrites(), 'E2E-02: 只读模式下写请求应为 0').toBe(0)
  })

  test('E2E-03 编辑、保存、确认完整流程且 PUT/POST 精确计数', async ({ page, requestLedger, mockApi }) => {
    mockApi.writePolicy = {}
    const panel = await openModules(page, 1)
    await expandAdvancedProfile(panel)

    const detailGet = requestLedger.getEntries().find(e => e.method === 'GET' && e.path.startsWith('/api/profile-candidates/'))
    const profileId = detailGet ? parseInt(detailGet.path.split('/').pop(), 10) : 103
    const profilePath = `/api/profile-candidates/${profileId}`
    const confirmPath = `/api/profile-candidates/${profileId}/confirm-with-state-baseline`
    mockApi.writePolicy = {
      [`PUT:${profilePath}`]: 1,
      [`POST:${confirmPath}`]: 1
    }

    await panel.getByLabel('项目概述').fill('E2E-03 测试项目概述已更新')
    await panel.getByRole('button', { name: '保存修改' }).click()
    await expect(panel.getByRole('status')).toContainText('保存成功')
    expect(requestLedger.getEndpointCount('PUT', profilePath), 'E2E-03: 保存应产生恰好 1 次 PUT').toBe(1)

    await panel.getByRole('button', { name: /确认并使用 V/ }).click()
    await expect(panel.getByRole('status')).toContainText('项目档案已确认生效')
    expect(requestLedger.getEndpointCount('POST', confirmPath), 'E2E-03: 确认应产生恰好 1 次 POST').toBe(1)

    await expect(panel.getByText('功能档案第 1 版 · 已确认', { exact: true })).toBeVisible()
    await expect(panel.getByText('已确认的功能模块 · V1', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: '创建 V2 功能档案候选', exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: '下一步：选择研发范围 →' })).toHaveCount(0)
    await expect(panel.getByRole('button', { name: '保存修改' })).toHaveCount(0)
    expect(requestLedger.getWrites(), 'E2E-03: 总写请求应为 2（PUT + POST）').toBe(2)
  })

  test('E2E-04 并发冲突时本地输入保留且重载无需额外 GET', async ({ page, requestLedger, mockApi }) => {
    mockApi.writePolicy = {}
    const panel = await openModules(page, 1)
    await expandAdvancedProfile(panel)

    mockApi._allowedConsoleErrors.push({ textPattern: '^{}$' }, { textPattern: '409 \\(Conflict\\)' })

    const detailGet = requestLedger.getEntries().find(e => e.method === 'GET' && e.path.startsWith('/api/profile-candidates/'))
    const profileId = detailGet ? parseInt(detailGet.path.split('/').pop(), 10) : 103
    const profilePath = `/api/profile-candidates/${profileId}`
    mockApi.writePolicy = {
      [`PUT:${profilePath}`]: 2
    }

    const bumpResult = await page.evaluate(async ({ profileIdParam }) => {
      try {
        const res = await fetch(`/api/profile-candidates/${profileIdParam}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            edit_version: 1,
            content: {
              project_summary: '并发写入',
              modules: [],
              domain_glossary: [],
              exclude_patterns: [],
              notes: ''
            }
          })
        })
        return { status: res.status, ok: res.ok }
      } catch (e) {
        return { status: 0, ok: false, error: e.message }
      }
    }, { profileIdParam: profileId })
    expect(bumpResult.ok, `E2E-04: 并发写入应返回 200 (err: ${bumpResult.error || 'n/a'}, status: ${bumpResult.status})`).toBe(true)

    await panel.getByLabel('项目概述').fill('尝试保存但会遇到冲突')
    await panel.getByRole('button', { name: '保存修改' }).click()

    await expect(panel.getByText('候选已被其他操作更新', { exact: false })).toBeVisible()
    await expect(panel.getByRole('button', { name: '重新加载最新版本' })).toBeVisible()
    expect(await panel.getByLabel('项目概述').inputValue(), 'E2E-04: 冲突后本地输入应保留').toBe('尝试保存但会遇到冲突')

    const getDetailBefore = requestLedger.getEndpointCount('GET', profilePath)
    await panel.getByRole('button', { name: '重新加载最新版本' }).click()
    await expect(panel.getByRole('status')).toContainText('已重新加载服务器最新版本')
    const getDetailAfter = requestLedger.getEndpointCount('GET', profilePath)
    expect(getDetailAfter - getDetailBefore, 'E2E-04: conflictCurrent.content 重载不应产生额外 GET').toBe(0)
  })
})

test.describe('fixture 白名单正反验证', () => {
  test('failureText 白名单 fail-closed：正确文本允许，错误或缺失文本拒绝', async () => {
    const whitelist = [
      { method: 'GET', pathPattern: '/api/profile-candidates/100', failureText: 'net::ERR_INTERNET_DISCONNECTED' }
    ]
    expect(isFailureAllowed(whitelist, 'GET', '/api/profile-candidates/100', 'net::ERR_INTERNET_DISCONNECTED')).toBe(true)
    expect(isFailureAllowed(whitelist, 'GET', '/api/profile-candidates/100', 'net::ERR_FAILED')).toBe(false)
    expect(isFailureAllowed(whitelist, 'GET', '/api/profile-candidates/100', null)).toBe(false)
    expect(isFailureAllowed(whitelist, 'GET', '/api/profile-candidates/100', '')).toBe(false)
    expect(isFailureAllowed(whitelist, 'GET', '/api/profile-candidates/101', 'net::ERR_INTERNET_DISCONNECTED')).toBe(false)
    expect(isFailureAllowed(whitelist, 'PUT', '/api/profile-candidates/100', 'net::ERR_INTERNET_DISCONNECTED')).toBe(false)
  })
})
