import { expect, test } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'

const STORAGE_KEY = 'rd-agent:uiux-v2-1:onboarding-complete'
const REPLAY_EVENT = 'rd-agent:uiux-v2-1:onboarding-replay'
const project = { id: 1, name: '研发进度 AI Agent', status: 'active', version: 4, created_at: '2026-09-01T09:00:00Z', updated_at: '2026-09-05T09:30:00Z', git_url: 'https://github.com/example/rd-agent.git', branch: 'main' }
function json(route, body, status = 200) { return route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) }) }
async function mockProductApi(page) { await page.route('**/*', async (route) => { const request = route.request(); const pathname = new URL(request.url()).pathname; if (!pathname.startsWith('/api/')) return route.continue(); if (request.method() === 'GET' && pathname === '/api/projects') return json(route, [project]); if (request.method() === 'GET' && pathname === '/api/projects/1') return json(route, project); if (request.method() === 'GET' && pathname === '/api/projects/1/prd-versions') return json(route, []); if (request.method() === 'GET' && pathname === '/api/projects/1/profiles') return json(route, []); if (request.method() === 'GET' && pathname === '/api/health') return json(route, { status: 'ok' }); return json(route, { detail: { message: 'Lane D onboarding fixture intentionally leaves this business endpoint unavailable.' } }, 404) }) }
async function replay(page) { await page.evaluate((eventName) => window.dispatchEvent(new CustomEvent(eventName)), REPLAY_EVENT); await expect(page.getByRole('dialog')).toBeVisible() }
async function capture(page, outDir, captures, label, filename) { const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth); expect(overflow).toBeLessThanOrEqual(1); const dialog = page.getByRole('dialog'); await expect(dialog).toBeVisible(); await expect.poll(async () => { const b = await dialog.boundingBox(); const v = page.viewportSize(); return !!b && b.x >= 0 && b.y >= 0 && b.x + b.width <= v.width && b.y + b.height <= v.height }).toBe(true); const box = await dialog.boundingBox(); expect(box).not.toBeNull(); const viewport = await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight })); expect(box.x).toBeGreaterThanOrEqual(0); expect(box.y).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(viewport.width); expect(box.y + box.height).toBeLessThanOrEqual(viewport.height); const file = path.join(outDir, filename); await page.screenshot({ path: file, fullPage: true, animations: 'disabled' }); captures.push({ file, label }) }
async function writeContactSheet(page, outDir, captures) { const cards = []; for (const capture of captures) { const bytes = await fs.readFile(capture.file); cards.push(`<figure><figcaption>${capture.label}</figcaption><img src="data:image/png;base64,${bytes.toString('base64')}" /></figure>`) } await page.setViewportSize({ width: 1600, height: 1000 }); await page.setContent(`<!doctype html><meta charset="utf-8"><style>*{box-sizing:border-box}body{margin:0;padding:22px;background:#eef2f7;font:14px system-ui;color:#172033}h1{margin:0 0 16px;font-size:23px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}figure{margin:0;padding:9px;border:1px solid #dce3ec;border-radius:12px;background:#fff}figcaption{padding:2px 2px 8px;font-size:12px;font-weight:800}img{display:block;width:100%;height:410px;object-fit:contain;object-position:top;background:#f8fafc;border:1px solid #edf0f4;border-radius:8px}</style><h1>UI/UX V2.1 · Lane D onboarding contact sheet</h1><div class="grid">${cards.join('')}</div>`); await page.screenshot({ path: path.join(outDir, 'lane-d-onboarding-contact-sheet.png'), fullPage: true, animations: 'disabled' }) }
async function targetGeometry(page, selectors) {
  return page.evaluate((candidateSelectors) => {
    const renderable = (element) => {
      if (!element) return false
      const rect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'
    }
    const target = candidateSelectors.flatMap((selector) => [...document.querySelectorAll(selector)]).find(renderable)
    const card = document.querySelector('[role="dialog"]')
    const highlight = document.querySelector('.onboarding-highlight')
    if (!target || !card || !highlight) return { targetFound: false, targetVisible: false, cardOverlapsTarget: true, highlightTouchesTarget: false }
    const targetRect = target.getBoundingClientRect()
    const cardRect = card.getBoundingClientRect()
    const highlightRect = highlight.getBoundingClientRect()
    const targetVisible = targetRect.bottom > 8 && targetRect.right > 8 && targetRect.top < innerHeight - 8 && targetRect.left < innerWidth - 8
    const cardOverlapsTarget = !(cardRect.right <= targetRect.left || cardRect.left >= targetRect.right || cardRect.bottom <= targetRect.top || cardRect.top >= targetRect.bottom)
    const highlightTouchesTarget = !(highlightRect.right < targetRect.left || highlightRect.left > targetRect.right || highlightRect.bottom < targetRect.top || highlightRect.top > targetRect.bottom)
    return { targetFound: true, targetVisible, cardOverlapsTarget, highlightTouchesTarget, scrollY }
  }, selectors)
}
async function expectTargetVisibleAndSeparated(page, selectors) {
  await expect.poll(async () => {
    const result = await targetGeometry(page, selectors)
    return result.targetFound && result.targetVisible && !result.cardOverlapsTarget && result.highlightTouchesTarget
  }, { timeout: 5000 }).toBe(true)
  return targetGeometry(page, selectors)
}

test('first-run onboarding is optional, persists locally, and does not interrupt deep links or final board', async ({ page }) => { await mockProductApi(page); await page.goto('/#/projects', { waitUntil: 'networkidle' }); const dialog = page.getByRole('dialog'); await expect(dialog).toBeVisible(); await expect(dialog.getByText('1 / 5', { exact: true })).toBeVisible(); await expect(page.locator('.app-shell')).toHaveAttribute('inert', ''); expect(await page.evaluate((key) => localStorage.getItem(key), STORAGE_KEY)).toBeNull(); await dialog.getByRole('button', { name: '跳过引导' }).click(); await expect(dialog).toHaveCount(0); expect(await page.evaluate((key) => localStorage.getItem(key), STORAGE_KEY)).toBe('1'); await page.reload({ waitUntil: 'networkidle' }); await expect(page.getByRole('dialog')).toHaveCount(0); await page.goto('/#/projects/1/home', { waitUntil: 'networkidle' }); await expect(page.getByRole('dialog')).toHaveCount(0); await page.evaluate((key) => localStorage.removeItem(key), STORAGE_KEY); await page.goto('/#/projects/1/board', { waitUntil: 'networkidle' }); await expect(page.getByRole('dialog')).toHaveCount(0) })

test('missing targets fall forward safely and focus stays inside the modal until Escape', async ({ page }) => { await mockProductApi(page); await page.goto('/#/projects', { waitUntil: 'networkidle' }); const dialog = page.getByRole('dialog'); await expect(dialog.getByText('1 / 5', { exact: true })).toBeVisible(); await dialog.getByRole('button', { name: '下一步' }).click(); await expect(dialog.getByText('2 / 5', { exact: true })).toBeVisible(); await dialog.getByRole('button', { name: '下一步' }).click(); await expect(dialog.getByText('4 / 5', { exact: true })).toBeVisible(); for (let index = 0; index < 8; index += 1) { await page.keyboard.press('Tab'); expect(await page.evaluate(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement))).toBe(true) } for (let index = 0; index < 4; index += 1) { await page.keyboard.press('Shift+Tab'); expect(await page.evaluate(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement))).toBe(true) } await page.keyboard.press('Escape'); await expect(page.getByRole('dialog')).toHaveCount(0); await expect(page.locator('.app-shell')).not.toHaveAttribute('inert', ''); expect(await page.evaluate((key) => localStorage.getItem(key), STORAGE_KEY)).toBe('1') })

test('completed users can replay from the actual narrow Settings button with reachable targets visible and unobscured', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.addInitScript((key) => localStorage.setItem(key, '1'), STORAGE_KEY)
  await mockProductApi(page)
  await page.goto('/#/settings', { waitUntil: 'networkidle' })
  await expect(page.getByRole('dialog')).toHaveCount(0)
  const replayButton = page.getByRole('button', { name: '重新查看 5 步引导' })
  const initialReplayTop = await replayButton.evaluate((element) => element.getBoundingClientRect().top)
  expect(initialReplayTop).toBeGreaterThan(844)
  await replayButton.click()
  expect(await page.evaluate(() => scrollY)).toBeGreaterThan(0)

  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('1 / 5', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '下一步' }).click()
  await expect(dialog.getByText('2 / 5', { exact: true })).toBeVisible()
  await expectTargetVisibleAndSeparated(page, ['[data-tour="project-context"]'])
  await page.screenshot({ path: 'test-results/lane-d-onboarding/390x844-settings-replay-step-2.png', fullPage: true, animations: 'disabled' })
  await expect(dialog.getByText('2 / 5', { exact: true })).toBeVisible()

  await dialog.getByRole('button', { name: '下一步' }).click()
  await expect(dialog.getByText('4 / 5', { exact: true })).toBeVisible()
  await expect(dialog.getByText('3 / 5', { exact: true })).toHaveCount(0)
  await expectTargetVisibleAndSeparated(page, ['[data-tour="primary-action"]', '.content-frame .primary', '.content-frame .primary-button', '.content-frame .guided-start'])
  await page.screenshot({ path: 'test-results/lane-d-onboarding/390x844-settings-replay-step-4.png', fullPage: true, animations: 'disabled' })
  await expect(dialog.getByText('4 / 5', { exact: true })).toBeVisible()

  await dialog.getByRole('button', { name: '下一步' }).click()
  await expect(dialog.getByText('5 / 5', { exact: true })).toBeVisible()
  await expect(dialog.getByText('软件不会自动替你调用 AI，也不会替你正式确认报告。')).toBeVisible()
  await dialog.getByRole('button', { name: '完成' }).click()
  await expect(dialog).toHaveCount(0)
  expect(await page.evaluate((key) => localStorage.getItem(key), STORAGE_KEY)).toBe('1')
})

test('home skips its absent workflow target and setup exposes all five steps at both widths', async ({ page }) => {
  await page.addInitScript(key => localStorage.setItem(key, '1'), STORAGE_KEY)
  await mockProductApi(page)
  const outDir = path.resolve('test-results/lane-d-onboarding')
  await fs.mkdir(outDir, { recursive: true })
  const captures = []
  for (const [viewportName, width, height] of [['1440x900', 1440, 900], ['390x844', 390, 844]]) {
    await page.setViewportSize({ width, height })
    for (const [view, steps] of [['home', [1, 2, 4, 5]], ['setup', [1, 2, 3, 4, 5]]]) {
      await page.goto('/#/projects/1/' + view, { waitUntil: 'networkidle' })
      await expect(page.locator('[data-tour="main-workflow"]')).toHaveCount(view === 'home' ? 0 : 1)
      await replay(page)
      for (const step of steps) {
        const dialog = page.getByRole('dialog')
        await expect(dialog.getByText(step + ' / 5', { exact: true })).toBeVisible()
        if (step === 2) await expectTargetVisibleAndSeparated(page, ['[data-tour="project-context"]'])
        if (step === 3) await expectTargetVisibleAndSeparated(page, ['[data-tour="main-workflow"]'])
        if (step === 4) {
          if (view === 'home') await expect(dialog.getByText('3 / 5', { exact: true })).toHaveCount(0)
          await expectTargetVisibleAndSeparated(page, ['[data-tour="primary-action"]', '.content-frame .primary', '.content-frame .primary-button', '.content-frame .guided-start'])
        }
        await capture(page, outDir, captures, viewportName + ' ' + view + ' step ' + step, viewportName + '-' + view + '-step-' + step + '.png')
        await dialog.getByRole('button', { name: step === 5 ? '完成' : '下一步', exact: true }).click()
      }
      await expect(page.getByRole('dialog')).toHaveCount(0)
    }
  }
  await writeContactSheet(page, outDir, captures)
})
