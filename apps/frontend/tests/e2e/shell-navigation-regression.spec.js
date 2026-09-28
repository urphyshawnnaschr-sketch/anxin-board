import { expect, test } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const project = {
  id: 1,
  name: 'Lane A 壳层验收项目',
  status: 'active',
  version: 1,
  created_at: '2026-09-05T00:00:00Z',
  updated_at: '2026-09-05T00:00:00Z',
  git_url: 'https://github.com/example/lane-a-shell.git',
  branch: 'main'
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body)
  })
}

async function mockShellApi(page) {
  await page.route('**/*', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project])
    if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project)
    if (request.method() === 'GET' && pathname === '/api/projects/1/prd-versions') return json(route, [])
    if (request.method() === 'GET' && pathname === '/api/projects/1/profiles') return json(route, [])
    return json(route, { detail: { message: 'Lane A shell test intentionally leaves business data unavailable.' } }, 404)
  })
}

test('shared shell separates fast navigation from the single numbered workflow indicator', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 })
  await mockShellApi(page)
  await page.goto('/#/projects/1/task', { waitUntil: 'networkidle' })

  const sideNavigation = page.getByRole('navigation', { name: '项目导航' })
  await expect(sideNavigation).toBeVisible()
  await expect(sideNavigation.getByText('项目导航', { exact: true })).toBeVisible()
  await expect(sideNavigation.getByText('工具', { exact: true })).toBeVisible()
  await expect(sideNavigation.getByText('现在：生成本次研发报告', { exact: true })).toHaveCount(0)

  const icons = await sideNavigation.locator('.navigation-icon').allTextContents()
  expect(icons).not.toContain('1')
  expect(icons).not.toContain('2')
  expect(icons).not.toContain('3')
  expect(icons).not.toContain('4')
  expect(icons).not.toContain('5')

  const workflow = page.getByRole('region', { name: '项目主流程' })
  await expect(workflow).toBeVisible()
  await expect(workflow.locator('[aria-current=step]')).toHaveText('3研发分析')
  await expect(workflow.getByText('工作流程', { exact: true })).toBeVisible()
  expect((await workflow.boundingBox()).height).toBeLessThan(70)
  await expect(workflow.locator('.complete')).toHaveCount(0)
  await expect(workflow.locator('.guided-step')).toHaveCount(5)
  await expect(sideNavigation.getByRole('button', { name: '研发分析', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByText('本地运行', { exact: true })).toBeVisible()
  await expect(page.locator('.shell-main > .content-frame > .quick-model')).toHaveCount(0)
  const modelSettings = page.locator('.top-actions .quick-model')
  await modelSettings.locator('summary').click()
  await expect(modelSettings.getByRole('heading', { name: '分析使用的模型' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(modelSettings).not.toHaveAttribute('open')
  await expect(modelSettings.locator('summary')).toBeFocused()
  expect(await sideNavigation.locator('.navigation-label').first().evaluate(el => getComputedStyle(el).fontSize)).toBe('14px')
})

test('mobile drawer keeps Escape close and focus restoration semantics', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockShellApi(page)
  await page.goto('/#/projects/1/home', { waitUntil: 'networkidle' })

  const menu = page.getByRole('button', { name: '打开主菜单', exact: true })
  const drawer = page.locator('#primary-navigation')
  await expect(menu).toHaveAttribute('aria-expanded', 'false')
  await expect(drawer).toHaveAttribute('aria-hidden', 'true')

  await menu.click()
  await expect(menu).toHaveAttribute('aria-expanded', 'true')
  await expect(drawer).not.toHaveAttribute('aria-hidden', 'true')
  await expect(drawer.getByRole('button', { name: '关闭主菜单', exact: true })).toBeFocused({ timeout: 1500 })

  await page.keyboard.press('Escape')
  await expect(menu).toHaveAttribute('aria-expanded', 'false')
  await expect(drawer).toHaveAttribute('aria-hidden', 'true')
  await expect(menu).toBeFocused({ timeout: 1500 })

  await menu.click()
  await drawer.getByRole('button', { name: '准备项目', exact: true }).click()
  await expect(page).toHaveURL(/#\/projects\/1\/setup$/)
  await expect(menu).toHaveAttribute('aria-expanded', 'false')
})

test('late page polish layers cannot override shared shell authority', async () => {
  // Inspect source directly: retired styles are deliberately absent from Vite's module graph.
  const laterSources = await Promise.all([
    'prototype-pages-v12.css', 'prototype-pages-v12-pass2.css',
    'prototype-pages-v12-pass3.css', 'product-polish.css'
  ].map((name) => fs.readFile(path.resolve('src', name), 'utf8')))

  const protectedSelectors = [
    '.shell-sidebar',
    '.shell-topbar',
    '.shell-main',
    '.navigation-item',
    '.navigation-icon',
    '.project-switch',
    '.sidebar-foot',
    '.breadcrumb',
    '.runtime'
  ]

  for (const source of laterSources) {
    for (const selector of protectedSelectors) {
      expect(source).not.toContain(selector)
    }
  }
})

test('model settings stays readable and inside the mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockShellApi(page)
  await page.goto('/#/projects/1/modules', { waitUntil: 'networkidle' })
  const disclosure = page.locator('.top-actions .quick-model')
  await disclosure.locator('summary').click()
  const box = await disclosure.locator('.quick-body').boundingBox()
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(390)
  expect(box.y + box.height).toBeLessThanOrEqual(844)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.keyboard.press('Escape')
  await expect(disclosure).not.toHaveAttribute('open')
})

test('Lane A representative shell matrix captures required viewports and contact sheet', async ({ page }) => {
  await mockShellApi(page)
  const outDir = path.resolve(process.env.ANXINBOARD_UI_ARTIFACTS || 'test-results/lane-a-shell')
  await fs.mkdir(outDir, { recursive: true })

  const viewports = [
    ['1440x900', 1440, 900],
    ['1920x1080', 1920, 1080],
    ['1366x768', 1366, 768],
    ['390x844', 390, 844]
  ]
  const representativePages = [
    ['home', '/#/projects/1/home'],
    ['setup', '/#/projects/1/setup'],
    ['analysis', '/#/projects/1/git']
  ]
  const captures = []

  for (const [viewportName, width, height] of viewports) {
    await page.setViewportSize({ width, height })
    for (const [pageName, route] of representativePages) {
      await page.goto(route, { waitUntil: 'networkidle' })
      await expect(page.locator('.app-shell')).toBeVisible()
      const filename = `${viewportName}-${pageName}.png`
      await page.screenshot({
        path: path.join(outDir, filename),
        fullPage: true,
        animations: 'disabled'
      })
      captures.push({ viewportName, pageName, filename })
    }
  }

  const cards = []
  for (const capture of captures) {
    const bytes = await fs.readFile(path.join(outDir, capture.filename))
    cards.push(`
      <figure>
        <figcaption>${capture.viewportName} · ${capture.pageName}</figcaption>
        <img src="data:image/png;base64,${bytes.toString('base64')}" alt="${capture.viewportName} ${capture.pageName}">
      </figure>
    `)
  }

  await page.setViewportSize({ width: 1600, height: 1000 })
  await page.setContent(`
    <!doctype html>
    <meta charset="utf-8">
    <style>
      *{box-sizing:border-box}body{margin:0;padding:24px;background:#eef2f7;font:14px system-ui;color:#172033}
      h1{margin:0 0 18px;font-size:22px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
      figure{margin:0;padding:10px;border:1px solid #dce3ec;border-radius:12px;background:#fff;box-shadow:0 4px 14px rgba(30,45,70,.06)}
      figcaption{margin:0 0 8px;font-weight:800}img{display:block;width:100%;height:280px;object-fit:cover;object-position:top;border:1px solid #edf0f4;border-radius:8px}
    </style>
    <h1>UI/UX V2.1 · Lane A shared shell contact sheet</h1>
    <div class="grid">${cards.join('')}</div>
  `)
  await page.screenshot({
    path: path.join(outDir, 'shell-contact-sheet.png'),
    fullPage: true,
    animations: 'disabled'
  })
})


test('healthy synthetic home keeps the shared theme and keyboard focus without a session warning', async ({ page }) => {
  await mockShellApi(page)
  await page.addInitScript(() => sessionStorage.setItem('anxinboard:local-browser-session:v1', 'synthetic-shell-session'))
  const profile = { id: 10, project_id: 1, version_no: 1, status: 'confirmed', content_hash: 'a'.repeat(64), content: {
    schema_version: 'project_profile_v2', planned_modules: [{ client_id: 'accounts', name: '账户管理', requirements: ['管理账户'] }],
    implementation_mappings: [{ planned_module_id: 'accounts', status: 'partial' }]
  } }
  await page.route('**/api/projects/1/prd-versions', route => json(route, [{ id: 2, version_no: 1, status: 'parse_confirmed' }]))
  await page.route('**/api/projects/1/profiles', route => json(route, [profile]))
  await page.route('**/api/profile-candidates/10', route => json(route, profile))
  await page.route('**/api/projects/1/project-state-baseline/status', route => json(route, { status: 'established', has_confirmed_baseline: true }))
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/projects/1/home', { waitUntil: 'networkidle' })
  await expect(page.locator('.local-session-notice')).toHaveCount(0)
  await expect(page.getByText('准备情况暂不可用', { exact: true })).toHaveCount(0)
  await expect(page.locator('.shell-sidebar')).toBeVisible()
  expect(await page.locator('.app-shell').evaluate(el => getComputedStyle(el).backgroundColor)).toBe('rgb(245, 245, 247)')
  const nav = page.getByRole('navigation', { name: '项目导航' }).getByRole('button', { name: '项目概览', exact: true })
  await nav.focus()
  await page.keyboard.press('Tab')
  const focused = await page.evaluate(() => { const el = document.activeElement; const style = getComputedStyle(el); return { tag: el.tagName, outline: style.outlineStyle, width: style.outlineWidth } })
  expect(focused.tag).toBe('BUTTON')
  expect(focused.outline).toBe('solid')
  expect(parseFloat(focused.width)).toBeGreaterThanOrEqual(2)
  await page.screenshot({ path: path.join(path.resolve(process.env.ANXINBOARD_UI_ARTIFACTS || 'test-results/lane-a-shell'), '1440x900-home-healthy.png'), fullPage: true, animations: 'disabled' })
})
