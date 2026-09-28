import { test, expect } from '@playwright/test'

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

async function openFixture(page, { delayShellRead = false } = {}) {
  const original = { id: 3, name: '原项目', branch: 'main', version: 1,
    git_url: 'https://example.invalid/three.git', status: 'active' }
  const other = { ...original, id: 4, name: '另一个项目', branch: 'release',
    git_url: 'https://example.invalid/four.git' }
  const saved = { ...original, name: '服务器确认的新名称', branch: 'feature/saved', version: 2 }
  const saveStarted = deferred(), saveResponse = deferred(), shellRead = deferred()
  const writes = [], errors = []
  let reads = 0
  page.on('pageerror', error => errors.push(error.message))
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-session'))
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const respond = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (request.method() === 'PUT' && path === '/api/projects/3') {
      writes.push({ method: 'PUT', path, body: request.postDataJSON() })
      saveStarted.resolve()
      const response = await saveResponse.promise
      return respond(response.body, response.status)
    }
    if (request.method() !== 'GET') {
      errors.push(`Unexpected write: ${request.method()} ${path}`)
      return route.abort()
    }
    if (path === '/api/projects/3') {
      // The shell starts its immediate watcher before the detail mounts.
      if (++reads === 1 && delayShellRead) await shellRead.promise
      return respond(original)
    }
    if (path === '/api/projects/4') return respond(other)
    if (/^\/api\/projects\/[34]\/(prd-versions|profiles)$/.test(path)) return respond([])
    if (/^\/api\/projects\/[34]\/project-state-baseline\/status$/.test(path)) {
      return respond({ status: 'missing', has_confirmed_baseline: false })
    }
    if (/^\/api\/projects\/[34]\/git\/status$/.test(path)) return respond({ status: 'not_tested' })
    errors.push(`Unexpected read: ${path}`)
    return route.abort()
  })
  await page.goto('/#/projects/3/setup', { waitUntil: 'domcontentloaded' })
  await expect(page.getByLabel('项目名称', { exact: true })).toHaveValue(original.name)
  return { original, other, saved, saveStarted, saveResponse, shellRead, writes, errors }
}

async function editAndSave(page) {
  await page.getByLabel('项目名称', { exact: true }).fill('提交的新名称')
  await page.getByLabel('当前研发分支', { exact: true }).fill('feature/draft')
  await page.getByRole('button', { name: '保存项目资料', exact: true }).click()
}

async function expectProjectContext(page, project) {
  await expect(page.locator('.project-switch strong')).toHaveText(project.name)
  await expect(page.locator('.project-switch .branch')).toHaveText(project.branch)
  await expect(page.locator('.breadcrumb > span').first()).toHaveText(project.name)
  await expect(page.locator('.lane-project-name')).toHaveText(project.name)
}

async function finishSave(page, fixture, response = { status: 200, body: { changed: true, project: fixture.saved } }) {
  const received = page.waitForResponse(result => result.request().method() === 'PUT'
    && new URL(result.url()).pathname === '/api/projects/3')
  fixture.saveResponse.resolve(response)
  await (await received).finished()
  await page.waitForLoadState('networkidle')
}

test('saved name and branch immediately update the sidebar and topbar from the server response', async ({ page }) => {
  const fixture = await openFixture(page)
  await expectProjectContext(page, fixture.original)
  await editAndSave(page)
  await fixture.saveStarted.promise
  await expect(page.getByRole('button', { name: '保存中...', exact: true })).toBeDisabled()
  await expectProjectContext(page, fixture.original)
  await finishSave(page, fixture)
  await expect(page.getByText('项目配置已保存', { exact: true })).toBeVisible()
  await expectProjectContext(page, fixture.saved)
  await expect(page.getByLabel('项目名称', { exact: true })).toHaveValue(fixture.saved.name)
  await expect(page.getByLabel('当前研发分支', { exact: true })).toHaveValue(fixture.saved.branch)
  expect(fixture.writes).toEqual([{ method: 'PUT', path: '/api/projects/3', body: {
    name: '提交的新名称', branch: 'feature/draft', git_url: fixture.original.git_url, version: 1
  } }])
  expect(fixture.errors).toEqual([])
})

test('a failed save leaves the sidebar and topbar on the last saved project', async ({ page }) => {
  const fixture = await openFixture(page)
  await expectProjectContext(page, fixture.original)
  await editAndSave(page)
  await fixture.saveStarted.promise
  await finishSave(page, fixture, { status: 503, body: { detail: { message: '合成保存失败' } } })
  await expect(page.getByText('合成保存失败', { exact: true })).toBeVisible()
  await expectProjectContext(page, fixture.original)
  expect(fixture.writes).toHaveLength(1)
  expect(fixture.errors).toEqual([])
})

test('a late save response cannot replace the project selected during saving', async ({ page }) => {
  const fixture = await openFixture(page)
  await editAndSave(page)
  await fixture.saveStarted.promise
  await page.evaluate(() => { window.location.hash = '#/projects/4/setup' })
  await expectProjectContext(page, fixture.other)
  await finishSave(page, fixture)
  await expectProjectContext(page, fixture.other)
  await expect(page.getByLabel('项目名称', { exact: true })).toHaveValue(fixture.other.name)
  await expect(page.getByText('项目配置已保存', { exact: true })).toHaveCount(0)
  expect(fixture.writes).toHaveLength(1)
  expect(fixture.errors).toEqual([])
})

test('a shell read started before saving cannot overwrite the saved project context', async ({ page }) => {
  const fixture = await openFixture(page, { delayShellRead: true })
  await editAndSave(page)
  await fixture.saveStarted.promise
  // The shell GET is deliberately still pending, so networkidle is not applicable yet.
  fixture.saveResponse.resolve({ status: 200, body: { changed: true, project: fixture.saved } })
  await expect(page.getByText('项目配置已保存', { exact: true })).toBeVisible()
  await expectProjectContext(page, fixture.saved)
  const received = page.waitForResponse(result => result.request().method() === 'GET'
    && new URL(result.url()).pathname === '/api/projects/3')
  fixture.shellRead.resolve()
  await (await received).finished()
  await page.waitForLoadState('networkidle')
  await expectProjectContext(page, fixture.saved)
  expect(fixture.writes).toHaveLength(1)
  expect(fixture.errors).toEqual([])
})
