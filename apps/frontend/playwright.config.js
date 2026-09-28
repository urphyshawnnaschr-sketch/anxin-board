import { defineConfig, devices } from '@playwright/test'

const isCI = !!process.env.CI

const resolveE2EPort = () => {
  if (!isCI) {
    return 5173
  }

  const rawPort = process.env.ANXINBOARD_E2E_PORT ?? ''
  if (!/^\d+$/.test(rawPort)) {
    throw new Error('ANXINBOARD_E2E_PORT must be a numeric port in CI')
  }

  const port = Number.parseInt(rawPort, 10)
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('ANXINBOARD_E2E_PORT must be between 1 and 65535')
  }

  return port
}

const e2ePort = resolveE2EPort()
const baseURL = `http://127.0.0.1:${e2ePort}`

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  fullyParallel: false,
  forbidOnly: isCI,
  retries: 0,
  workers: isCI ? 1 : undefined,
  reporter: isCI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off'
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ],
  webServer: {
    command: isCI
      ? `npm run dev -- --host 127.0.0.1 --port ${e2ePort} --strictPort`
      : 'npm run dev -- --host 127.0.0.1',
    url: baseURL,
    reuseExistingServer: !isCI,
    timeout: 120_000
  }
})
