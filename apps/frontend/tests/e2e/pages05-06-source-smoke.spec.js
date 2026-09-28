import { expect, test } from '@playwright/test'

test.describe('Page 05 / Page 06 source smoke', () => {
  test('both isolated page SFCs compile through the Vite dev server', async ({ page }) => {
    // This is a compiler check, never an integration with a developer's backend.
    await page.route(url => url.pathname.startsWith('/api/'), route => route.abort())
    await page.goto('/')

    const result = await page.evaluate(async () => {
      const gitModule = await import('/src/views/GitEvidenceView.vue')
      const taskModule = await import('/src/views/TaskExecutionView.vue')
      return {
        gitEvidence: Boolean(gitModule?.default),
        taskExecution: Boolean(taskModule?.default)
      }
    })

    expect(result).toEqual({ gitEvidence: true, taskExecution: true })
  })
})
