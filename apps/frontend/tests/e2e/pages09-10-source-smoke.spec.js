import { expect, test } from '@playwright/test'

test.describe('Page 09 / Page 10 source smoke', () => {
  test('both isolated page SFCs compile through the Vite dev server', async ({ page }) => {
    // This is a compiler check, never an integration with a developer's backend.
    await page.route(url => url.pathname.startsWith('/api/'), route => route.abort())
    await page.goto('/')

    const result = await page.evaluate(async () => {
      const historyModule = await import('/src/views/HistoryExceptionsView.vue')
      const settingsModule = await import('/src/views/SettingsBackupView.vue')
      return {
        history: Boolean(historyModule?.default),
        settings: Boolean(settingsModule?.default)
      }
    })

    expect(result).toEqual({ history: true, settings: true })
  })
})
