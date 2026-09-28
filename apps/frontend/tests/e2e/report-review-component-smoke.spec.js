import { expect, test } from '@playwright/test'

test('Page07 ReportReviewView compiles as a standalone Vite module', async ({ page }) => {
  // Compile source without contacting a developer's local backend.
  await page.route(url => url.pathname.startsWith('/api/'), route => route.abort())
  await page.goto('/')

  const compiled = await page.evaluate(async () => {
    const module = await import('/src/views/ReportReviewView.vue')
    return {
      hasDefaultExport: Boolean(module.default),
      hasRender: typeof module.default?.render === 'function'
    }
  })

  expect(compiled.hasDefaultExport).toBe(true)
  expect(compiled.hasRender).toBe(true)
})
