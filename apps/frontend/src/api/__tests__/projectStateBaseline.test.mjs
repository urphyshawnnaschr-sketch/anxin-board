import assert from 'node:assert/strict'
import test from 'node:test'

const SESSION_KEY = 'anxinboard:local-browser-session:v1'
const ATTEMPT_KEY = 'anxinboard:project-state-baseline-attempt:v1:9'
const storage = new Map()
Object.defineProperty(globalThis, 'sessionStorage', {
  configurable: true,
  value: {
    getItem(key) { return storage.get(key) ?? null },
    setItem(key, value) { storage.set(key, String(value)) },
    removeItem(key) { storage.delete(key) }
  }
})
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { location: { href: 'http://127.0.0.1:8000/#/projects/9/profile' } }
})
Object.defineProperty(globalThis, 'history', {
  configurable: true,
  value: { state: null, replaceState() {} }
})

const {
  preflightProjectStateBaseline,
  executeProjectStateBaseline,
  formatProjectStateBaselineError,
  getProjectStateBaselineStatus,
  confirmProfileCandidate
} = await import('../projectProfiles.js')

function resetStorage() {
  storage.clear()
  storage.set(SESSION_KEY, 'session-token-for-baseline')
}

function response(body, { ok = true, status = 200 } = {}) {
  return { ok, status, async json() { return body } }
}

test('baseline error formatter exposes only the persisted safe diagnostic code', () => {
  const safe = formatProjectStateBaselineError({ detail: { message: 'known failure', current: { error_code: 'PROVIDER_RESULT_MAPPING_INVALID', provider_body: 'secret-provider-text' } } }, 'fallback')
  assert.equal(safe, 'known failure（诊断码：PROVIDER_RESULT_MAPPING_INVALID）')
  assert.doesNotMatch(safe, /secret-provider-text/)
  const unsafe = formatProjectStateBaselineError({ detail: { message: 'known failure', current: { error_code: 'bad code with spaces', provider_body: 'secret-provider-text' } } }, 'fallback')
  assert.equal(unsafe, 'known failure')
})

test('baseline preflight is a local scan request without provider attempt identity', async () => {
  resetStorage()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({
      status: 'ready',
      preflight: {
        exact_head: 'a'.repeat(40),
        tracked_files: 123,
        safe_text_bytes: 4567,
        batch_count: 4,
        provider_calls: 0,
        credential_read: false
      }
    })
  }

  const result = await preflightProjectStateBaseline(9, 42)

  assert.equal(result.ok, true)
  assert.equal(captured[0], '/api/projects/9/project-state-baseline/preflight')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'session-token-for-baseline')
  assert.equal(captured[1].headers['Local-Idempotency-Key'], undefined)
  assert.deepEqual(JSON.parse(captured[1].body), { plan_profile_id: 42 })
  assert.equal(storage.has(ATTEMPT_KEY), false)
})

test('baseline execution binds one durable browser identity to authorization nonce and clears it on success', async () => {
  resetStorage()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({
      status: 'candidate_ready',
      schema_version: 'project_state_baseline_v1',
      exact_head: 'a'.repeat(40),
      authorization_hash: 'b'.repeat(64),
      batch_count: 4,
      profile: { id: 77, status: 'candidate' },
      auto_confirm: false
    }, { status: 201 })
  }

  const result = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))
  const key = captured[1].headers['Local-Idempotency-Key']

  assert.equal(result.ok, true)
  assert.match(key, /^idem-/)
  assert.deepEqual(JSON.parse(captured[1].body), {
    plan_profile_id: 42,
    authorized: true,
    authorization_nonce: key,
    preflight_identity_hash: 'f'.repeat(64)
  })
  assert.equal(storage.has(ATTEMPT_KEY), false)
})

test('ambiguous baseline transport loss keeps and reuses the exact same authorization identity', async () => {
  resetStorage()
  const seenKeys = []
  const seenNonces = []
  let calls = 0
  globalThis.fetch = async (_url, init) => {
    calls += 1
    seenKeys.push(init.headers['Local-Idempotency-Key'])
    seenNonces.push(JSON.parse(init.body).authorization_nonce)
    if (calls === 1) throw new Error('synthetic browser loss after local dispatch')
    return response(
      { detail: { code: 'PROJECT_STATE_BASELINE_BATCH_UNKNOWN', message: 'still unknown' } },
      { ok: false, status: 409 }
    )
  }

  const first = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))
  const persisted = storage.get(ATTEMPT_KEY)
  const second = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))

  assert.equal(first.ok, false)
  assert.equal(first.body.detail.code, 'PROJECT_STATE_BASELINE_BATCH_UNKNOWN')
  assert.match(persisted, /^idem-/)
  assert.deepEqual(seenKeys, [persisted, persisted])
  assert.deepEqual(seenNonces, [persisted, persisted])
  assert.equal(second.body.detail.code, 'PROJECT_STATE_BASELINE_BATCH_UNKNOWN')
  assert.equal(storage.get(ATTEMPT_KEY), persisted)
})

test('HTTP 2xx with incomplete baseline body remains UNKNOWN and keeps durable identity', async () => {
  resetStorage()
  const seen = []
  globalThis.fetch = async (_url, init) => {
    seen.push(init.headers['Local-Idempotency-Key'])
    return response({ status: 'candidate_ready', batch_count: 2 }, { status: 201 })
  }

  const first = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))
  const persisted = storage.get(ATTEMPT_KEY)
  const second = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))

  assert.equal(first.ok, false)
  assert.equal(first.body.detail.code, 'PROJECT_STATE_BASELINE_BATCH_UNKNOWN')
  assert.match(persisted, /^idem-/)
  assert.deepEqual(seen, [persisted, persisted])
  assert.equal(second.body.detail.code, 'PROJECT_STATE_BASELINE_BATCH_UNKNOWN')
})

test('classified baseline failure closes old action so later explicit click gets a fresh authorization', async () => {
  resetStorage()
  const seen = []
  globalThis.fetch = async (_url, init) => {
    seen.push(init.headers['Local-Idempotency-Key'])
    return response(
      { detail: { code: 'PROJECT_STATE_BASELINE_BATCH_FAILED', message: 'known failure' } },
      { ok: false, status: 409 }
    )
  }

  const first = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))
  const second = await executeProjectStateBaseline(9, 42, 'f'.repeat(64))

  assert.equal(first.body.detail.code, 'PROJECT_STATE_BASELINE_BATCH_FAILED')
  assert.equal(second.body.detail.code, 'PROJECT_STATE_BASELINE_BATCH_FAILED')
  assert.equal(storage.has(ATTEMPT_KEY), false)
  assert.notEqual(seen[0], seen[1])
})

test('profile confirmation always uses baseline-aware product endpoint', async () => {
  resetStorage()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ id: 77, status: 'confirmed', edit_version: 3 })
  }

  const result = await confirmProfileCandidate(77, 2, 'local')

  assert.equal(result.ok, true)
  assert.equal(captured[0], '/api/profile-candidates/77/confirm-with-state-baseline')
  assert.equal(captured[1].method, 'POST')
  assert.deepEqual(JSON.parse(captured[1].body), { edit_version: 2, confirmed_by: 'local' })
})

test('baseline status is a read-only product query', async () => {
  resetStorage()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ status: 'missing', has_confirmed_baseline: false })
  }
  const result = await getProjectStateBaselineStatus(9)
  assert.equal(result.ok, true)
  assert.equal(captured[0], '/api/projects/9/project-state-baseline/status')
  assert.equal(captured[1], undefined)
})

test('workbench never skips a missing project analysis ledger', async () => {
  const { projectBaselineNextAction } = await import('../../features/project-profile/projectBaselineNextAction.js')
  assert.equal(projectBaselineNextAction({ setupReady: true, moduleReady: true, baselineStatus: 'missing' }).section, 'modules')
  assert.equal(projectBaselineNextAction({ setupReady: true, moduleReady: true, baselineStatus: 'upgrade_required' }).section, 'modules')
  assert.equal(projectBaselineNextAction({ setupReady: true, moduleReady: true, baselineStatus: 'candidate_pending' }).section, 'modules')
  assert.match(projectBaselineNextAction({ setupReady: true, moduleReady: true, baselineStatus: 'missing' }).title, /分析台账/)
  assert.equal(projectBaselineNextAction({ setupReady: true, moduleReady: true, baselineStatus: 'established' }).section, 'git')
})

test('modules page keeps a visible legacy upgrade path and baseline scan action', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../../views/ProjectProfilePanel.vue', import.meta.url), 'utf8')
  assert.match(source, /分析代码前，请先升级功能档案/)
  assert.match(source, /创建 V2 功能档案候选/)
  assert.match(source, /检查当前代码并估算分析上限/)
  assert.match(source, /待首次全量分析/)
  assert.match(source, /v-if="awaitingFirstBaseline"[^>]*>尚无完整代码分析结果/)
  assert.match(source, /当前候选尚未确认，也可以重新执行全量分析/)
  assert.match(source, /baselinePlanProfile\.value\.id/)
  assert.doesNotMatch(source, /所有安全代码证据都会进入模型覆盖批次/)
  assert.doesNotMatch(source, /activeConfirmed\.value\?\.content\?\.schema_version/)
  assert.doesNotMatch(source, /无法完成首次分析准备。请检查 Git 连接和模型设置后重试。/)
  assert.match(source, /本地服务没有返回可识别原因/)
  assert.match(source, /formatProjectStateBaselineError\(result.body/)
  assert.match(source, /功能档案第 \$\{serverProfile.value.version_no\} 版 · 已确认/)
})

test('baseline preflight classifies a missing local session instead of throwing generic scan failure', async () => {
  storage.clear()
  let fetchCalls = 0
  globalThis.fetch = async () => { fetchCalls += 1; throw new Error('should not fetch without a session') }
  const result = await preflightProjectStateBaseline(9, 42)
  assert.equal(result.ok, false)
  assert.equal(result.body.detail.code, 'PROJECT_STATE_BASELINE_LOCAL_SESSION_UNAVAILABLE')
  assert.match(result.body.detail.message, /重启安心看板/)
  assert.equal(fetchCalls, 0)
})

test('workbench copy does not imply confirmed modules can skip the analysis ledger', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../../views/ProjectDetailView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /已确认，可继续研发分析/)
  assert.doesNotMatch(source, /只根据当前 PRD \/ 功能模块是否已经确认来决定/)
  assert.match(source, /尚无完整代码分析结果；完成分析后需要你检查并确认/)
  assert.match(source, /ready: summaryAvailability\.value\.baseline && baselineStatus\.value\?\.status === 'established'/)
  assert.match(source, /查看代码分析状态/)
  assert.match(source, /return nextAction\.value\.key/)
  assert.match(source, /homeAction\.section \? goSection\(homeAction\.section\) : reloadLatest\(\)/)
  assert.match(source, /projectBaselineNextAction\(\{/)
  assert.match(source, /baselineStatus: baselineStatus\.value\?\.status \|\| 'unknown'/)
})


test('direct preflight diagnostic codes use the same safe formatter gate',()=>{
 assert.equal(formatProjectStateBaselineError({detail:{code:'ATLAS_SCOPE_CHANGED'}},'fallback'),'fallback（诊断码：ATLAS_SCOPE_CHANGED）')
 assert.equal(formatProjectStateBaselineError({detail:{code:'secret code with spaces'}},'fallback'),'fallback')
})


test('confirmed all-unknown history recommends full analysis only for the bound complete profile', async () => {
  const { projectBaselineNextAction } = await import('../../features/project-profile/projectBaselineNextAction.js')
  const profile = { id: 44, status: 'confirmed', content: {
    schema_version: 'project_profile_v2',
    planned_modules: [{ client_id: 'a' }, { client_id: 'b' }],
    implementation_mappings: [{ planned_module_id: 'a', status: 'unknown' }, { planned_module_id: 'b', status: 'unknown' }]
  } }
  const input = { setupReady: true, moduleReady: true, baselineStatus: 'established', baselineProfileId: 44, confirmedProfile: profile }
  const action = projectBaselineNextAction(input)
  assert.equal(action.section, 'modules')
  assert.equal(action.key, 'full-analysis')
  assert.equal(action.title, '核对代码范围并全量分析')
  assert.match(action.description, /历史记录已确认.*所有模块.*待核实/)
  for (const change of [
    { baselineProfileId: 99 }, { baselineProfileId: null },
    { confirmedProfile: { ...profile, status: 'candidate' } },
    { confirmedProfile: { ...profile, content: { ...profile.content, implementation_mappings: [] } } },
    { confirmedProfile: { ...profile, content: { ...profile.content, implementation_mappings: [{ planned_module_id: 'a', status: 'implemented' }, { planned_module_id: 'b', status: 'unknown' }] } } },
    { confirmedProfile: { ...profile, content: { ...profile.content, implementation_mappings: [{ planned_module_id: 'a', status: 'not_started' }, { planned_module_id: 'b', status: 'unknown' }] } } }
  ]) assert.equal(projectBaselineNextAction({ ...input, ...change }).section, 'git')
  assert.equal(projectBaselineNextAction({ ...input, baselineStatus: 'candidate_pending' }).key, 'baseline')
})
