// @ts-check
import { test as base, expect } from '@playwright/test'

/**
 * ============================================================================
 *  projectProfileApi.js — Strict mode mock fixture for project profile E2E tests
 *
 *  Design:
 *  1. Each test gets INDEPENDENT state (createInitialData returns fresh clone)
 *  2. Catch-all /api/** handler — any unregistered API makes the test fail
 *  3. Per-test request ledger: ONE record per request, outcome updated on completion
 *  4. GET/POST/PUT/DELETE counts, per-endpoint call tracking
 *  5. Browser anomaly monitoring: pageerror, console.error, unhandledrejection,
 *     requestfailed
 *  6. Deferred/pending request support with manual release (removed from pending
 *     only AFTER resolution)
 *  7. Error injection: 409, 500, route.abort / network failure (all failures
 *     go through the public handler via routeOverrides and update the SAME
 *     ledger entry — no standalone page.route() aborts in tests)
 *  8. Per-path failure whitelist is fail-closed: entries may be precise to
 *     method + path + failureText; when failureText is declared, an actual
 *     missing or mismatching errorText makes the test FAIL
 *  9. Test-end verifier: strict per-scenario write policy (exact method +
 *     endpoint + allowed count), unexpected API, pending requests, browser
 *     anomalies
 * ============================================================================
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function urlPath(url) {
  try { return new URL(url).pathname } catch { return url }
}

async function parseBody(request) {
  const ct = request.headers()['content-type'] || ''
  if (ct.includes('json')) {
    try { return JSON.parse((await request.postDataBuffer())?.toString() || '{}') } catch { return {} }
  }
  return {}
}

// ---------------------------------------------------------------------------
// Default data (returned fresh from createInitialData)
// ---------------------------------------------------------------------------

const BASE_CONTENT = {
  project_summary: '',
  modules: [],
  domain_glossary: [],
  exclude_patterns: [],
  notes: ''
}

const MODULE_A = {
  client_id: 'mod-a',
  name: '用户管理',
  description: '用户注册、登录、权限管理',
  prd_refs: ['PRD-001'],
  requirements: ['必须支持 OAuth2'],
  paths: [
    { type: 'frontend', pattern: 'src/pages/login/**', required: true, note: '登录页' },
    { type: 'backend', pattern: 'app/auth/**', required: true, note: '' }
  ],
  exclusions: []
}

const TERM_A = { term: 'OAuth2', definition: '开放授权协议', aliases: ['OAuth'] }

const CANDIDATE_CONTENT = {
  schema_version: 'project_profile_manual_v1',
  project_summary: '这是一个测试项目，用于验证 E2E 核心流程。',
  modules: [MODULE_A],
  domain_glossary: [TERM_A],
  exclude_patterns: ['*.log', 'node_modules/'],
  notes: '初版候选'
}

const DEFAULT_PROJECTS = {
  1: { id: 1, name: 'E2E 测试项目 1', status: 'active', version: 5, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-07-01T10:00:00Z', git_url: '', branch: '' },
  2: { id: 2, name: 'E2E 测试项目 2', status: 'active', version: 3, created_at: '2026-02-01T00:00:00Z', updated_at: '2026-06-15T08:00:00Z', git_url: '', branch: '' },
  3: { id: 3, name: 'E2E 测试项目 3（空）', status: 'active', version: 1, created_at: '2026-03-01T00:00:00Z', updated_at: '2026-03-01T00:00:00Z', git_url: '', branch: '' },
  4: { id: 4, name: 'E2E 多版本项目', status: 'active', version: 10, created_at: '2026-04-01T00:00:00Z', updated_at: '2026-07-05T14:00:00Z', git_url: '', branch: '' }
}

/**
 * Create a fresh independent set of mock data. Each test gets its own copy.
 * Profile IDs start at 100; project 4 has 3 records (100-102).
 */
function createInitialData() {
  let idCounter = 100
  function nextId() { return idCounter++ }

  const p4Superseded = {
    id: nextId(), project_id: 4, status: 'superseded', version_no: 1, edit_version: 2,
    content: {
      project_summary: '多版本项目的历史版',
      modules: [{ ...MODULE_A, client_id: 'mod-p4s', name: '旧核心' }],
      domain_glossary: [], exclude_patterns: [], notes: 'v1 superseded'
    },
    created_at: '2026-05-01T09:00:00Z', updated_at: '2026-05-01T09:00:00Z',
    confirmed_at: '2026-05-01T09:00:00Z', confirmed_by: 'admin', active: false
  }
  const p4Confirmed = {
    id: nextId(), project_id: 4, status: 'confirmed', version_no: 2, edit_version: 4,
    content: {
      project_summary: '多版本项目的确认版',
      modules: [{ ...MODULE_A, client_id: 'mod-p4c', name: '核心模块' }],
      domain_glossary: [], exclude_patterns: ['temp/'], notes: 'v1 确认'
    },
    created_at: '2026-06-01T10:00:00Z', updated_at: '2026-06-10T12:00:00Z',
    confirmed_at: '2026-06-10T12:00:00Z', confirmed_by: 'local', active: true
  }
  const p4Candidate = {
    id: nextId(), project_id: 4, status: 'candidate', version_no: 3, edit_version: 1,
    content: {
      project_summary: '多版本项目的候选版',
      modules: [
        { ...MODULE_A, client_id: 'mod-p4c', name: '核心模块' },
        { ...MODULE_A, client_id: 'mod-p4n', name: '新增模块' }
      ],
      domain_glossary: [], exclude_patterns: ['temp/', 'build/'], notes: 'v2 候选（未确认）'
    },
    created_at: '2026-07-05T14:00:00Z', updated_at: '2026-07-05T14:00:00Z',
    confirmed_at: null, confirmed_by: null, active: false
  }

  return {
    projects: {
      1: { ...DEFAULT_PROJECTS[1] },
      2: { ...DEFAULT_PROJECTS[2] },
      3: { ...DEFAULT_PROJECTS[3] },
      4: { ...DEFAULT_PROJECTS[4] }
    },
    profiles: {
      1: [{ ...CANDIDATE_CONTENT, id: nextId(), project_id: 1, status: 'candidate', version_no: 1, edit_version: 1,
            content: { ...CANDIDATE_CONTENT }, created_at: '2026-07-01T10:00:00Z', updated_at: '2026-07-01T10:00:00Z',
            confirmed_at: null, confirmed_by: null, active: false }],
      2: [{ id: nextId(), project_id: 2, status: 'confirmed', version_no: 1, edit_version: 3,
            content: {
              project_summary: '已有确认档案的项目',
              modules: [{ ...MODULE_A, client_id: 'mod-b', name: '角色管理' }],
              domain_glossary: [], exclude_patterns: [], notes: '已确认版本'
            },
            created_at: '2026-06-15T08:00:00Z', updated_at: '2026-06-15T08:00:00Z',
            confirmed_at: '2026-06-15T08:00:00Z', confirmed_by: 'local', active: true }],
      3: [],
      4: [p4Superseded, p4Confirmed, p4Candidate]
    },
    profileIdCounter: idCounter
  }
}

// Known API endpoint patterns used for early matching
const KNOWN_PATTERNS = [
  /^\/api\/projects\/(\d+)\/project-state-baseline\/(?:status|atlas\/tasks\/latest|atlas\/results\/\d+)$/,
  /^\/api\/projects\/(\d+)$/,
  /^\/api\/projects\/(\d+)\/git\/status$/,
  /^\/api\/projects\/(\d+)\/analysis-lineage$/,
  /^\/api\/projects\/(\d+)\/prd-versions$/,
  /^\/api\/projects\/(\d+)\/profiles$/,
  /^\/api\/projects\/(\d+)\/profile-candidates$/,
  /^\/api\/profile-candidates\/(\d+)$/,
  /^\/api\/profile-candidates\/(\d+)\/confirm-with-state-baseline$/,
  // AppShell-mounted ModelQuickSetup card probes the saved model status on every page.
  /^\/api\/settings\/deepseek-status$/
]

// ---------------------------------------------------------------------------
// Deferred request helper (for pending/race-condition scenarios)
// ---------------------------------------------------------------------------

function createDeferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

// ---------------------------------------------------------------------------
// Request Ledger — ONE record per request, outcome updated on completion
// ---------------------------------------------------------------------------

function createLedger() {
  const entries = []
  const methodCounts = { GET: 0, POST: 0, PUT: 0, DELETE: 0 }
  const endpointCounts = {}

  /**
   * Create a ledger entry for an incoming request.
   * Returns an object with updateOutcome(outcome) to record the final result.
   */
  function record(method, path, body) {
    const entry = { method, path, body, outcome: 'pending' }
    entries.push(entry)
    methodCounts[method] = (methodCounts[method] || 0) + 1
    const key = `${method}:${path}`
    endpointCounts[key] = (endpointCounts[key] || 0) + 1
    return entry
  }

  function getCount(method) { return methodCounts[method] || 0 }
  function getEndpointCount(method, path) { return endpointCounts[`${method}:${path}`] || 0 }
  function getWrites() { return methodCounts.POST + methodCounts.PUT + methodCounts.DELETE }
  function getEntries() { return entries.slice() }
  function getPendingRequests() { return entries.filter(e => e.outcome === 'pending').length }
  function getBlocked() { return entries.filter(e => e.outcome && (e.outcome.startsWith('blocked') || e.outcome.startsWith('unhandled'))) }

  return { record, getCount, getEndpointCount, getWrites, getEntries, getPendingRequests, getBlocked }
}

// ---------------------------------------------------------------------------
// Browser anomaly monitor
// ---------------------------------------------------------------------------

function createAnomalyMonitor(page) {
  const errors = []
  const consoleErrors = []
  const rejections = []
  const requestFailures = []

  function onPageError(err) { errors.push({ message: err.message, stack: err.stack }) }
  function onConsoleError(msg) { consoleErrors.push({ text: msg.text(), location: msg.location }) }
  function onRequestFailed(failure) {
    const errorText = failure.failure()?.errorText ?? null
    requestFailures.push({ url: failure.url(), failureText: errorText, method: failure.method() })
  }

  async function attach() {
    page.on('pageerror', onPageError)
    page.on('console', msg => { if (msg.type() === 'error') onConsoleError(msg) })
    page.on('requestfailed', onRequestFailed)
    await page.addInitScript(() => {
      window.__pw_rejections = []
      window.addEventListener('unhandledrejection', event => {
        window.__pw_rejections.push({
          message: event.reason?.message || String(event.reason),
          stack: event.reason?.stack
        })
      })
    })
  }

  async function detach() {
    page.off('pageerror', onPageError)
    page.off('console', onConsoleError)
    page.off('requestfailed', onRequestFailed)
  }

  function getErrors() { return errors.slice() }
  function getConsoleErrors() { return consoleErrors.slice() }
  async function getRejections() {
    try {
      return await page.evaluate(() => window.__pw_rejections || []).catch(() => [])
    } catch { return [] }
  }
  function getRequestFailures() { return requestFailures.slice() }

  return { attach, detach, getErrors, getConsoleErrors, getRejections, getRequestFailures }
}

// ---------------------------------------------------------------------------
// Per-path allowed failure whitelist
// ---------------------------------------------------------------------------

/**
 * Check if a request failure is in the allowed whitelist.
 * Whitelist entries: { method, pathPattern (regex string), failureText? }
 *
 * FAIL-CLOSED: when an entry declares failureText, the actual failureText
 * must exist AND contain the declared text; a missing or mismatching actual
 * errorText is NOT allowed.
 */
function isFailureAllowed(whitelist, method, url, failureText) {
  const path = urlPath(url)
  return whitelist.some(entry => {
    if (entry.method && entry.method !== method) return false
    if (entry.pathPattern) {
      try {
        if (!new RegExp(entry.pathPattern).test(path)) return false
      } catch { return false }
    }
    if (entry.failureText) {
      if (!failureText) return false
      if (!failureText.includes(entry.failureText)) return false
    }
    return true
  })
}

/**
 * Check if a console.error is in the allowed whitelist.
 * Whitelist entries: { textPattern (string or regex string) }
 */
function isConsoleErrorAllowed(whitelist, text) {
  return whitelist.some(entry => {
    if (!text) return false
    if (entry.textPattern) {
      try {
        return new RegExp(entry.textPattern).test(text)
      } catch {
        return text.includes(entry.textPattern)
      }
    }
    return false
  })
}

// ---------------------------------------------------------------------------
// End-of-test verifier
// ---------------------------------------------------------------------------

/**
 * Verify no browser anomalies, no blocked API calls, no pending deferreds,
 * no unexpected writes, and no pending requests at the end of each test.
 */
async function verifyEndOfTest({ requestLedger, anomalyMonitor, mockApi }) {
  const monitor = anomalyMonitor.current
  if (!monitor) return

  // 1. No page errors
  const pageErrors = monitor.getErrors()
  expect(pageErrors, `Unexpected page errors: ${JSON.stringify(pageErrors)}`).toHaveLength(0)

  // 2. Console errors checked against per-path whitelist
  const allowedConsole = mockApi._allowedConsoleErrors || []
  const consoleErrors = monitor.getConsoleErrors()
  const unexpectedConsole = consoleErrors.filter(
    e => !isConsoleErrorAllowed(allowedConsole, e.text)
  )
  expect(unexpectedConsole, `Unexpected console.error: ${JSON.stringify(unexpectedConsole)}`).toHaveLength(0)

  // 3. No unhandled rejections
  const rejections = await monitor.getRejections()
  expect(rejections, `Unhandled promise rejections: ${JSON.stringify(rejections)}`).toHaveLength(0)

  // 4. Request failures checked against per-path whitelist
  const allowedFailures = mockApi._allowedRequestFailures || []
  const reqFailures = monitor.getRequestFailures()
  const unexpectedFailures = reqFailures.filter(
    f => !isFailureAllowed(allowedFailures, f.method || 'GET', f.url, f.failureText)
  )
  expect(unexpectedFailures, `Unexpected request failures: ${JSON.stringify(unexpectedFailures)}`).toHaveLength(0)

  // 5. No pending deferred requests
  const pendingDeferreds = mockApi.getPendingDeferreds()
  expect(pendingDeferreds, `Pending deferred requests not released: ${JSON.stringify(pendingDeferreds)}`).toHaveLength(0)

  // 6. No blocked / unexpected API calls
  const blocked = requestLedger.getBlocked()
  expect(blocked, `Unexpected API calls blocked: ${JSON.stringify(blocked)}`).toHaveLength(0)

  // 7. No pending requests in ledger (all must have completed outcomes)
  expect(requestLedger.getPendingRequests(), 'There are still pending in-flight requests').toBe(0)

  // 8. Enforce per-scenario write request policy (STRICT, exact counts)
  //    mockApi.writePolicy: { 'METHOD:endpointPath': allowedCount }
  //    - undefined / null -> policy NOT declared -> test FAILS (must be explicit)
  //    - {} (empty object) -> strict zero writes: ANY write fails
  //    - declared key -> exact allowed count; duplicate or missing writes fail
  //    - any write (POST/PUT/DELETE) not declared in the policy -> FAILS
  const writePolicy = mockApi.writePolicy
  expect(writePolicy !== undefined && writePolicy !== null,
    '每个场景必须显式声明 writePolicy；空对象 {} 表示严格零写，不允许未声明写策略').toBe(true)
  const writes = requestLedger.getEntries().filter(e => ['POST', 'PUT', 'DELETE'].includes(e.method))
  const actualWriteCounts = {}
  writes.forEach(w => {
    const key = `${w.method}:${w.path}`
    actualWriteCounts[key] = (actualWriteCounts[key] || 0) + 1
  })
  for (const [key, count] of Object.entries(actualWriteCounts)) {
    expect(Object.prototype.hasOwnProperty.call(writePolicy, key),
      `未声明的写请求 ${key} 出现 ${count} 次（写策略: ${JSON.stringify(writePolicy)}）`).toBe(true)
  }
  for (const [key, allowed] of Object.entries(writePolicy)) {
    expect(typeof allowed === 'number' && allowed >= 0,
      `写策略 ${key} 必须声明精确允许次数（非负整数），实际: ${allowed}`).toBe(true)
    const actual = actualWriteCounts[key] || 0
    expect(actual, `写请求 ${key} 应精确发生 ${allowed} 次，实际 ${actual} 次（重复或缺失写请求必须失败）`).toBe(allowed)
  }
}

// ---------------------------------------------------------------------------
// Route handler factory
// ---------------------------------------------------------------------------

/**
 * Create the strict route handler for one test.
 */
function createHandler(options = {}) {
  const data = options.initialData || createInitialData()
  const profiles = data.profiles
  const projectData = data.projects
  let profileIdCounter = data.profileIdCounter
  function nextId() { return profileIdCounter++ }

const mockApi = options.mockApi
    const delays = options.delays || {}

  // Deferred control: a map of `method:path` -> deferred objects
  const deferredMap = {}
  function setDeferred(key) {
    const d = createDeferred()
    deferredMap[key] = d
    return d
  }
  async function waitDeferred(key) {
    const d = deferredMap[key]
    if (d) {
      await d.promise
      // Only remove from pending map AFTER resolution
      delete deferredMap[key]
    }
  }
  function getPendingDeferreds() {
    return Object.keys(deferredMap)
  }

  function applyDelay(path) {
    const ms = delays[path]
    if (ms) return new Promise(resolve => setTimeout(resolve, ms))
  }

  return {
    handler: async (route) => {
      const request = route.request()
      const method = request.method()
      const path = urlPath(request.url())
      const body = await parseBody(request).catch(() => ({}))
      const ledger = options._ledger

// Record ONE entry with outcome='pending' initially
        const entry = ledger.record(method, path, body)

        // Check overrides first — read dynamically from mockApi
        // so tests can set routeOverrides after navigation
        const routeOverrides = mockApi?.routeOverrides || {}
        const overrideKey = `${method}:${path}`
        if (routeOverrides[overrideKey]) {
          return routeOverrides[overrideKey](route, request, ledger, entry)
        }

      await applyDelay(path)

      // Check deferred — await before responding
      await waitDeferred(overrideKey)

      // ---- Catch-all: any unknown /api/** path fails the test ----
      const isKnown = KNOWN_PATTERNS.some(p => p.test(path))
      if (!isKnown) {
        entry.outcome = `blocked (unknown path: ${path})`
        route.abort('blockedbyclient')
        return
      }

      // Read-only Atlas requests are handled locally, never forwarded to the backend.
      const baselineMatch = path.match(/^\/api\/projects\/(\d+)\/project-state-baseline\/(status|atlas\/tasks\/latest|atlas\/results\/\d+)$/)
      if (baselineMatch && method === 'GET') {
        const rows = profiles[Number(baselineMatch[1])] || []
        const confirmed = rows.find(profile => profile.status === 'confirmed')
        const response = baselineMatch[2] === 'status'
          ? { status: confirmed ? 'upgrade_required' : 'profile_required', has_confirmed_baseline: false, profile_id: confirmed?.id ?? null }
          : { task: null }
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, json: response })
      }

      // ---- GET /api/projects/{id} ----
      const projectMatch = path.match(/^\/api\/projects\/(\d+)$/)
      if (projectMatch && method === 'GET') {
        const pid = parseInt(projectMatch[1], 10)
        const p = projectData[pid]
        if (!p) {
          entry.outcome = '404 not found'
          return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { message: 'Not found' } }) })
        }
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(p) })
      }

      // ---- GET /api/settings/deepseek-status (AppShell ModelQuickSetup mount) ----
      const statusMatch = path.match(/^\/api\/settings\/deepseek-status$/)
      if (statusMatch && method === 'GET') {
        entry.outcome = '200 ok'
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ provider: 'deepseek', configured: false, connected: false, selected_model: null })
        })
      }

      // ---- GET /api/projects/{id}/git/status ----
      const gitMatch = path.match(/^\/api\/projects\/(\d+)\/git\/status$/)
      if (gitMatch && method === 'GET') {
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ clean: true, ahead: 0, behind: 0 }) })
      }

      // ---- GET /api/projects/{id}/analysis-lineage (git-baseline panel mount) ----
      const lineageMatch = path.match(/^\/api\/projects\/(\d+)\/analysis-lineage$/)
      if (lineageMatch && method === 'GET') {
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'no_lineage', lineage: null }) })
      }

      // ---- GET /api/projects/{id}/prd-versions ----
      const prdMatch = path.match(/^\/api\/projects\/(\d+)\/prd-versions$/)
      if (prdMatch && method === 'GET') {
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
      }

      // ---- GET /api/projects/{id}/profiles (list) ----
      const listMatch = path.match(/^\/api\/projects\/(\d+)\/profiles$/)
      if (listMatch && method === 'GET') {
        const pid = parseInt(listMatch[1], 10)
        const list = profiles[pid] || []
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(list) })
      }

      // ---- POST /api/projects/{id}/profile-candidates (create) ----
      const createMatch = path.match(/^\/api\/projects\/(\d+)\/profile-candidates$/)
      if (createMatch && method === 'POST') {
        const pid = parseInt(createMatch[1], 10)
        const now = new Date().toISOString()
        const newProfile = {
          id: nextId(),
          project_id: pid,
          status: 'candidate',
          version_no: 1,
          edit_version: 1,
          content: body.content || body,
          created_at: now,
          updated_at: now,
          confirmed_at: null,
          confirmed_by: null,
          active: false
        }
        if (!profiles[pid]) profiles[pid] = []
        profiles[pid].push(newProfile)
        entry.outcome = '200 ok'
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(newProfile) })
      }

      // ---- GET /api/profile-candidates/{id} (detail) ----
      const getMatch = path.match(/^\/api\/profile-candidates\/(\d+)$/)
      if (getMatch && method === 'GET') {
        const profileId = parseInt(getMatch[1], 10)
        for (const pid of Object.keys(profiles)) {
          const found = profiles[pid].find(p => p.id === profileId)
          if (found) {
            entry.outcome = '200 ok'
            return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(found) })
          }
        }
        entry.outcome = '404 not found'
        return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { message: 'Not found' } }) })
      }

      // ---- PUT /api/profile-candidates/{id} (update) ----
      const putMatch = path.match(/^\/api\/profile-candidates\/(\d+)$/)
      if (putMatch && method === 'PUT') {
        const profileId = parseInt(putMatch[1], 10)
        for (const pid of Object.keys(profiles)) {
          const idx = profiles[pid].findIndex(p => p.id === profileId)
          if (idx !== -1) {
            const existing = profiles[pid][idx]
            if (typeof body.edit_version === 'number' && body.edit_version !== existing.edit_version) {
              const bumpedVersion = existing.edit_version + 1
              const conflictProfile = {
                ...existing,
                edit_version: bumpedVersion,
                content: { ...existing.content, project_summary: existing.content.project_summary + ' (server updated)' },
                updated_at: new Date().toISOString()
              }
              profiles[pid][idx] = conflictProfile
              entry.outcome = '409 conflict'
              // When _forceReloadFetch is set, return current without content so
              // the product code's reloadServerVersion() falls through to a real GET.
              const currentBody = mockApi?._forceReloadFetch
                ? { ...conflictProfile, content: null }
                : conflictProfile
              return route.fulfill({
                status: 409,
                contentType: 'application/json',
                body: JSON.stringify({
                  detail: {
                    code: 'PROJECT_PROFILE_VERSION_CONFLICT',
                    message: '候选已被其他操作更新，请重新加载最新内容',
                    current: currentBody
                  }
                })
              })
            }
            const now = new Date().toISOString()
            const updated = {
              ...existing,
              content: body.content || existing.content,
              edit_version: existing.edit_version + 1,
              updated_at: now
            }
            profiles[pid][idx] = updated
            entry.outcome = '200 ok'
            return route.fulfill({
              status: 200,
              contentType: 'application/json',
              body: JSON.stringify({ changed: true, profile: updated })
            })
          }
        }
        entry.outcome = '404 not found'
        return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { message: 'Not found' } }) })
      }

      // ---- POST /api/profile-candidates/{id}/confirm-with-state-baseline ----
      const confirmMatch = path.match(/^\/api\/profile-candidates\/(\d+)\/confirm-with-state-baseline$/)
      if (confirmMatch && method === 'POST') {
        const profileId = parseInt(confirmMatch[1], 10)
        for (const pid of Object.keys(profiles)) {
          const idx = profiles[pid].findIndex(p => p.id === profileId)
          if (idx !== -1) {
            const existing = profiles[pid][idx]
            const now = new Date().toISOString()
            const confirmed = {
              ...existing,
              status: 'confirmed',
              edit_version: existing.edit_version + 1,
              confirmed_at: now,
              confirmed_by: 'local',
              active: true,
              updated_at: now
            }
            profiles[pid][idx] = confirmed
            profiles[pid].forEach((p, i) => {
              if (i !== idx && p.status === 'confirmed') {
                profiles[pid][i] = { ...p, status: 'superseded', active: false }
              }
            })
            entry.outcome = '200 ok'
            return route.fulfill({
              status: 200,
              contentType: 'application/json',
              body: JSON.stringify(confirmed)
            })
          }
        }
        entry.outcome = '404 not found'
        return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { message: 'Not found' } }) })
      }

      // Should never reach here — all known patterns are handled above
      entry.outcome = `unhandled known path: ${path}`
      route.abort('blockedbyclient')
    },
    setDeferred,
    waitDeferred,
    getPendingDeferreds
  }
}

// ---------------------------------------------------------------------------
// Test fixture with strict mock, ledger, anomaly monitoring
// ---------------------------------------------------------------------------

export const test = base.extend({
  /**
   * projectId: parameterizable project ID (default 3 = empty project)
   */
  projectId: [3, { option: true }],

  /**
   * requestLedger: per-test independent request log
   */
  requestLedger: async ({}, use) => {
    const ledger = createLedger()
    await use(ledger)
  },

  /**
   * anomalyMonitor: per-test browser anomaly collector
   */
  anomalyMonitor: async ({}, use) => {
    const monitor = { current: null }
    await use(monitor)
  },

  /**
   * mockApi: exposes helpers for deferred control, route overrides, etc.
   */
  mockApi: async ({}, use) => {
    const api = { deferred: {} }
    await use(api)
  },

  /**
   * Override page fixture to install strict mock + monitoring.
   * Uses a single catch-all route for ALL /api/ paths.
   */
  page: async ({ page, projectId, requestLedger, anomalyMonitor, mockApi }, use) => {
    const state = createInitialData()

// Support routeOverrides and delays set before navigation
      const delays = mockApi.delays || {}

      const factory = createHandler({
        initialData: state,
        mockApi,
        delays,
        _ledger: requestLedger
      })

    // Install catch-all /api/** route (remove any previous first)
    await page.unroute('/api/**')
    await page.route('/api/**', factory.handler)

    // Attach anomaly monitoring
    const monitor = createAnomalyMonitor(page)
    await monitor.attach()
    anomalyMonitor.current = monitor

    // Expose deferred helpers and pending-deferred query to tests
    mockApi.setDeferred = factory.setDeferred
    mockApi.waitDeferred = factory.waitDeferred
    mockApi.getPendingDeferreds = factory.getPendingDeferreds

    // Per-path whitelists (replaces global boolean gates)
    mockApi._allowedRequestFailures = []
    mockApi._allowedConsoleErrors = []

    await use(page)

    await monitor.detach()
  }
})

/**
 * Register a global afterEach hook that verifies end-of-test invariants.
 */
export function registerEndOfTestAssertions() {
  test.afterEach(async ({ requestLedger, anomalyMonitor, mockApi }) => {
    await verifyEndOfTest({ requestLedger, anomalyMonitor, mockApi })
  })
}

export { expect } from '@playwright/test'

export { isFailureAllowed }
