import { expect, test } from '@playwright/test'

test('deadline integration real page SFCs compile through Vite', async ({ page }) => {
  await page.goto('/')

  const loaded = await page.evaluate(async () => {
    try {
      await Promise.all([
        import('/src/views/ProjectListView.vue'),
        import('/src/views/ProjectDetailView.vue'),
        import('/src/views/GitEvidenceView.vue'),
        import('/src/views/TaskExecutionView.vue'),
        import('/src/views/ReportReviewView.vue'),
        import('/src/views/AnxinBoardView.vue'),
        import('/src/views/HistoryExceptionsView.vue'),
        import('/src/views/SettingsBackupView.vue')
      ])
      return { ok: true, message: '' }
    } catch (error) {
      return { ok: false, message: String(error) }
    }
  })

  expect(loaded.ok, loaded.message).toBe(true)
})

test('deadline integration router maps 01-10 without PrototypePlaceholder', async ({ page }) => {
  await page.goto('/')

  const source = await page.evaluate(async () => {
    const sourceModule = await import('/src/App.vue?raw')
    return sourceModule.default
  })

  expect(source).not.toContain('PrototypePlaceholder')

  for (const component of [
    'ProjectListView',
    'ProjectDetailView',
    'GitEvidenceView',
    'TaskExecutionView',
    'ReportReviewView',
    'AnxinBoardView',
    'HistoryExceptionsView',
    'SettingsBackupView'
  ]) {
    expect(source).toContain(component)
  }

  for (const pageKey of ['home', 'setup', 'modules', 'git', 'task', 'review', 'board', 'history', 'settings']) {
    expect(source).toContain(pageKey)
  }

  expect(source).toContain(':section="currentView"')
})
