// @ts-check
import { expect } from '@playwright/test'
import { test, registerEndOfTestAssertions } from './fixtures/projectProfileApi.js'

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

async function expandHistory(panel) {
  const details = panel.locator('details.history-details')
  await expect(details).toBeVisible()
  await details.locator('summary').click()
  return details
}

test.describe('功能模块确认容错场景', () => {
  registerEndOfTestAssertions()

  test('E2E-05 快速切换项目应丢弃前一个项目的响应', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    const deferred = mockApi.setDeferred('GET:/api/projects/1/profiles')

    try {
      await page.goto('/#/projects/1/modules')
      await expect(profilePanel(page).locator('#profile-panel-title')).toHaveText('代码盘点')

      await page.evaluate(() => { window.location.hash = '#/projects/2/modules' })
      const panel = profilePanel(page)
      await expect(panel.getByText('已确认的功能模块 · V1', { exact: true })).toBeVisible()
      if (await panel.locator('details.profile-plan-details').getAttribute('open') === null) await panel.locator('details.profile-plan-details > summary').click()
      await expect(panel.getByText('角色管理', { exact: true })).toBeVisible()

      deferred.resolve()

      await expect.poll(
        () => {
          const entries = requestLedger.getEntries().filter(
            e => e.method === 'GET' && e.path === '/api/projects/1/profiles'
          )
          return entries.length > 0 && entries.every(e => e.outcome === '200 ok')
        },
        { message: 'E2E-05: 项目 A 的延迟 profiles 请求应在释放后全部完成', timeout: 10_000 }
      ).toBe(true)

      await expect(panel.getByText('已确认的功能模块 · V1', { exact: true })).toBeVisible()
      if (await panel.locator('details.profile-plan-details').getAttribute('open') === null) await panel.locator('details.profile-plan-details > summary').click()
      await expect(panel.getByText('角色管理', { exact: true })).toBeVisible()
      await expect(panel.getByText('用户管理', { exact: true })).toHaveCount(0)
    } finally {
      deferred.resolve()
    }
  })

  test('E2E-06 加载失败后显示 fail-visible 状态并可重新加载', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    mockApi._allowedRequestFailures = [
      { method: 'GET', pathPattern: '/api/projects/3/profiles', failureText: 'net::ERR_INTERNET_DISCONNECTED' }
    ]
    mockApi._allowedConsoleErrors.push(
      { textPattern: '^{}$' },
      { textPattern: 'Failed to load resource: net::ERR_INTERNET_DISCONNECTED' }
    )
    mockApi.routeOverrides = {
      'GET:/api/projects/3/profiles': async (route, request, ledger, entry) => {
        entry.outcome = 'aborted (internetdisconnected)'
        route.abort('internetdisconnected')
      }
    }

    const panel = await openModules(page, 3)
    await expect(panel.getByText('功能模块加载失败', { exact: true })).toBeVisible()
    await expect(panel.getByText('当前结果暂不可用，请重新加载。', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: '重新加载' })).toBeVisible()

    const failedGets = requestLedger.getEntries().filter(
      e => e.method === 'GET' && e.path === '/api/projects/3/profiles'
    )
    expect(failedGets.length, 'E2E-06: 至少一个 profiles GET 应进入公共 ledger').toBeGreaterThan(0)
    expect(failedGets.every(e => e.outcome === 'aborted (internetdisconnected)'), 'E2E-06: profiles GET 应记录为 aborted').toBe(true)
    expect(requestLedger.getWrites(), 'E2E-06: 写请求应为 0').toBe(0)
  })

  test('E2E-07 保存失败后保留未保存修改并禁止确认', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    const panel = await openModules(page, 1)
    await expandAdvancedProfile(panel)

    const detailGet = requestLedger.getEntries().find(e => e.method === 'GET' && e.path.startsWith('/api/profile-candidates/'))
    const profileId = detailGet ? parseInt(detailGet.path.split('/').pop(), 10) : 103
    const profilePath = `/api/profile-candidates/${profileId}`
    mockApi.writePolicy = { [`PUT:${profilePath}`]: 1 }
    mockApi._allowedConsoleErrors.push(
      { textPattern: '^{}$' },
      { textPattern: 'Failed to load resource: net::ERR_INTERNET_DISCONNECTED' }
    )
    mockApi._allowedRequestFailures = [
      { method: 'PUT', pathPattern: profilePath, failureText: 'net::ERR_INTERNET_DISCONNECTED' }
    ]
    mockApi.routeOverrides = {
      [`PUT:${profilePath}`]: async (route, request, ledger, entry) => {
        entry.outcome = 'aborted (internetdisconnected)'
        route.abort('internetdisconnected')
      }
    }

    await panel.getByLabel('项目概述').fill('保存会失败的内容')
    await panel.getByRole('button', { name: '保存修改' }).click()

    await expect(panel.getByRole('status')).toContainText('保存失败，原数据未改变')
    await expect(panel.getByText('还有未保存的修改，请先保存再确认。', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: /确认并使用 V/ })).toBeDisabled()

    const putEntries = requestLedger.getEntries().filter(e => e.method === 'PUT' && e.path === profilePath)
    expect(putEntries, 'E2E-07: 失败 PUT 应精确为 1 条 ledger 记录').toHaveLength(1)
    expect(putEntries[0].outcome, 'E2E-07: 失败 PUT 应记录为 aborted').toBe('aborted (internetdisconnected)')
  })

  test('E2E-A 保存请求 pending 期间锁定 UI 且释放后保存成功', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    const panel = await openModules(page, 1)
    await expandAdvancedProfile(panel)
    const detailGet = requestLedger.getEntries().find(e => e.method === 'GET' && e.path.startsWith('/api/profile-candidates/'))
    const profileId = detailGet ? parseInt(detailGet.path.split('/').pop(), 10) : 103
    const profilePath = `/api/profile-candidates/${profileId}`
    mockApi.writePolicy = { [`PUT:${profilePath}`]: 1 }

    await panel.getByLabel('项目概述').fill('E2E-A pending 保存测试')
    const putDeferred = mockApi.setDeferred(`PUT:${profilePath}`)
    try {
      await panel.getByRole('button', { name: '保存修改' }).click()
      const savingButton = panel.getByRole('button', { name: '保存中...' })
      await expect(savingButton).toBeDisabled()
      await expect(panel.getByRole('button', { name: /确认并使用 V/ })).toBeDisabled()
      await expect(panel.getByLabel('项目概述')).toBeDisabled()
      await expect(panel.getByText('还有未保存的修改，请先保存再确认。', { exact: true })).toHaveCount(0)

      const putCountBefore = requestLedger.getEndpointCount('PUT', profilePath)
      await savingButton.click({ force: true }).catch(() => {})
      await page.waitForTimeout(200)
      expect(requestLedger.getEndpointCount('PUT', profilePath) - putCountBefore, 'E2E-A: 重复点击不应产生额外 PUT').toBe(0)

      putDeferred.resolve()
      await expect(panel.getByRole('status')).toContainText('保存成功')
      await expect(panel.getByRole('button', { name: '保存修改' })).toBeEnabled()
      expect(requestLedger.getEndpointCount('PUT', profilePath), 'E2E-A: 应产生恰好 1 次 PUT').toBe(1)
    } finally {
      putDeferred.resolve()
    }
  })

  test('E2E-B 确认请求 pending 期间锁定 UI 且释放后进入只读', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    const panel = await openModules(page, 1)
    const detailGet = requestLedger.getEntries().find(e => e.method === 'GET' && e.path.startsWith('/api/profile-candidates/'))
    const profileId = detailGet ? parseInt(detailGet.path.split('/').pop(), 10) : 103
    const confirmPath = `/api/profile-candidates/${profileId}/confirm-with-state-baseline`
    mockApi.writePolicy = { [`POST:${confirmPath}`]: 1 }

    const confirmDeferred = mockApi.setDeferred(`POST:${confirmPath}`)
    try {
      await panel.getByRole('button', { name: /确认并使用 V/ }).click()
      const confirmingButton = panel.getByRole('button', { name: '确认中...' })
      await expect(confirmingButton).toBeDisabled()
      await expect(panel.getByRole('button', { name: '保存修改' })).toBeDisabled()
      await expect(panel.getByLabel('项目概述')).toBeDisabled()

      const confirmCountBefore = requestLedger.getEndpointCount('POST', confirmPath)
      await confirmingButton.click({ force: true }).catch(() => {})
      await page.waitForTimeout(200)
      expect(requestLedger.getEndpointCount('POST', confirmPath) - confirmCountBefore, 'E2E-B: 重复点击不应产生额外 POST').toBe(0)

      confirmDeferred.resolve()
      await expect(panel.getByText('已确认的功能模块 · V1', { exact: true })).toBeVisible()
      await expect(panel.getByRole('status')).toContainText('项目档案已确认生效')
      await expect(panel.getByRole('button', { name: '创建 V2 功能档案候选', exact: true })).toBeVisible()
      await expect(panel.getByRole('button', { name: '下一步：选择研发范围 →' })).toHaveCount(0)
      await expect(panel.getByRole('button', { name: '保存修改' })).toHaveCount(0)
      expect(requestLedger.getEndpointCount('POST', confirmPath), 'E2E-B: 应产生恰好 1 次 POST 确认').toBe(1)
    } finally {
      confirmDeferred.resolve()
    }
  })

  test('E2E-C 查看历史版本网络失败时主页面不变且有错误提示', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    mockApi._allowedConsoleErrors.push({ textPattern: '^{}$' })

    const panel = await openModules(page, 4)
    await expect(panel.getByText('历史版本 · 3 个', { exact: true })).toBeVisible()
    await expect(panel.getByText('2 个待确认模块', { exact: true })).toBeVisible()
    await expandAdvancedProfile(panel)
    const historyDetails = await expandHistory(panel)

    const summaryField = panel.getByLabel('项目概述')
    await expect(summaryField).toBeEnabled()
    const summaryBefore = await summaryField.inputValue()
    expect(summaryBefore, 'E2E-C: 失败前项目概述应为候选内容').toBe('多版本项目的候选版')

    mockApi._allowedConsoleErrors.push(
      { textPattern: 'Failed to load resource: net::ERR_INTERNET_DISCONNECTED' },
      { textPattern: 'Failed to load resource: net::ERR_FAILED' }
    )
    mockApi._allowedRequestFailures = [
      { method: 'GET', pathPattern: '/api/profile-candidates/100', failureText: 'net::ERR_INTERNET_DISCONNECTED' }
    ]
    mockApi.routeOverrides = {
      'GET:/api/profile-candidates/100': async (route, request, ledger, entry) => {
        entry.outcome = 'aborted (internetdisconnected)'
        route.abort('internetdisconnected')
      }
    }

    await historyDetails.getByRole('button', { name: '查看', exact: true }).first().click()
    await expect.poll(
      () => requestLedger.getEntries().find(
        e => e.method === 'GET' && e.path === '/api/profile-candidates/100' && e.outcome === 'aborted (internetdisconnected)'
      )?.outcome || null,
      { message: 'E2E-C: 历史详情 GET 应被触发并完成', timeout: 10_000 }
    ).toBe('aborted (internetdisconnected)')

    expect(await summaryField.inputValue(), 'E2E-C: 历史查看失败后项目概述必须不变').toBe(summaryBefore)
    await expect(summaryField).toBeEnabled()
    await expect(panel.getByRole('status')).toContainText('网络请求失败')
    expect(requestLedger.getWrites(), 'E2E-C: 写请求应为 0').toBe(0)
  })

  test('E2E-D 冲突后重载请求网络失败时本地输入和重载入口保留', async ({ page, mockApi, requestLedger }) => {
    mockApi.writePolicy = {}
    mockApi._allowedConsoleErrors.push(
      { textPattern: '^{}$' },
      { textPattern: '409 \\(Conflict\\)' },
      { textPattern: 'Failed to load resource: net::ERR_INTERNET_DISCONNECTED' }
    )

    const panel = await openModules(page, 1)
    await expandAdvancedProfile(panel)
    const detailGet = requestLedger.getEntries().find(e => e.method === 'GET' && e.path.startsWith('/api/profile-candidates/'))
    const profileId = detailGet ? parseInt(detailGet.path.split('/').pop(), 10) : 103
    const profilePath = `/api/profile-candidates/${profileId}`
    mockApi.writePolicy = { [`PUT:${profilePath}`]: 2 }

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
    expect(bumpResult.ok, `E2E-D: 并发写入应返回 200 (err: ${bumpResult.error || 'n/a'}, status: ${bumpResult.status})`).toBe(true)

    await panel.getByLabel('项目概述').fill('E2E-D 冲突后重载网络失败测试')
    mockApi._forceReloadFetch = true
    await panel.getByRole('button', { name: '保存修改' }).click()
    await expect(panel.getByText('候选已被其他操作更新', { exact: false })).toBeVisible()
    await expect(panel.getByRole('button', { name: '重新加载最新版本' })).toBeVisible()
    expect(await panel.getByLabel('项目概述').inputValue(), 'E2E-D: 冲突后本地输入应保留').toBe('E2E-D 冲突后重载网络失败测试')

    mockApi._allowedRequestFailures = [
      { method: 'GET', pathPattern: profilePath, failureText: 'net::ERR_INTERNET_DISCONNECTED' }
    ]
    mockApi.routeOverrides = {
      [`GET:${profilePath}`]: async (route, request, ledger, entry) => {
        entry.outcome = 'aborted (internetdisconnected)'
        route.abort('internetdisconnected')
      }
    }

    const writesBeforeReload = requestLedger.getWrites()
    await panel.getByRole('button', { name: '重新加载最新版本' }).click()
    await expect.poll(
      () => requestLedger.getEntries().find(
        e => e.method === 'GET' && e.path === profilePath && e.outcome === 'aborted (internetdisconnected)'
      )?.outcome || null,
      { message: 'E2E-D: 重载 GET 应被触发并完成', timeout: 10_000 }
    ).toBe('aborted (internetdisconnected)')

    await expect(panel.getByText('候选已被其他操作更新', { exact: false })).toHaveCount(0)
    await expect(panel.getByRole('button', { name: '重新加载最新版本' })).toBeVisible()
    expect(await panel.getByLabel('项目概述').inputValue(), 'E2E-D: 网络失败后本地输入应仍保留').toBe('E2E-D 冲突后重载网络失败测试')
    await expect(panel.getByRole('status')).toContainText('网络请求失败')
    expect(requestLedger.getWrites() - writesBeforeReload, 'E2E-D: 重载失败不得产生额外写请求').toBe(0)
  })
})
