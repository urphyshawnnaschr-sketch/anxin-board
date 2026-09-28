import assert from 'node:assert/strict'
import test from 'node:test'

const SESSION_KEY = 'anxinboard:local-browser-session:v1'
const ATTEMPT_KEY = 'anxinboard:profile-generation-attempt:v1:9'
const storage = new Map()
let storageWritable = true
Object.defineProperty(globalThis, 'sessionStorage', {
  configurable: true,
  value: {
    getItem(key) { return storage.get(key) ?? null },
    setItem(key, value) {
      if (!storageWritable && key === ATTEMPT_KEY) throw new Error('synthetic storage refusal')
      storage.set(key, String(value))
    },
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

const { discardProfileGenerationAttempt, generateProfileCandidate } = await import('../projectProfiles.js')

function resetStorage() {
  storage.clear()
  storageWritable = true
}

function response(body, { ok = true, status = 201 } = {}) {
  return { ok, status, async json() { return body } }
}

test('AI profile generation uses local-session plus durable idempotency headers', async () => {
  resetStorage()
  storage.set(SESSION_KEY, 'session-token-for-profile-generation')
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ profile: { id: 7, status: 'candidate' }, generation: { provider: 'synthetic' } })
  }

  const result = await generateProfileCandidate(9)

  assert.equal(captured[0], '/api/projects/9/profile-candidates/generate')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['Content-Type'], 'application/json')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'session-token-for-profile-generation')
  assert.match(captured[1].headers['X-Request-ID'], /^req-/)
  assert.match(captured[1].headers['Local-Idempotency-Key'], /^idem-/)
  assert.deepEqual(JSON.parse(captured[1].body), { authorized: true })
  assert.equal(result.ok, true)
  assert.equal(result.body.profile.status, 'candidate')
  assert.equal(storage.has(ATTEMPT_KEY), false)
})

test('AI profile generation cannot reach fetch without secure local session', async () => {
  resetStorage()
  let fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    throw new Error('fetch must not run without a local session')
  }

  await assert.rejects(() => generateProfileCandidate(9), /LOCAL_SESSION_UNAVAILABLE/)
  assert.equal(fetchCalls, 0)
})

test('generation fails closed before fetch when durable browser attempt identity cannot persist', async () => {
  resetStorage()
  storage.set(SESSION_KEY, 'session-token-for-profile-generation')
  storageWritable = false
  let fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    throw new Error('provider-bound fetch must not run without persisted attempt identity')
  }

  await assert.rejects(
    () => generateProfileCandidate(9),
    /PROFILE_GENERATION_IDEMPOTENCY_STORAGE_UNAVAILABLE/
  )
  assert.equal(fetchCalls, 0)
  assert.equal(storage.has(ATTEMPT_KEY), false)
})

test('ambiguous browser transport loss keeps and reuses the exact same provider attempt key', async () => {
  resetStorage()
  storage.set(SESSION_KEY, 'session-token-for-profile-generation')
  const seenKeys = []
  let calls = 0
  globalThis.fetch = async (_url, init) => {
    calls += 1
    seenKeys.push(init.headers['Local-Idempotency-Key'])
    if (calls === 1) throw new Error('synthetic browser connection loss after request dispatch')
    return response({ detail: { code: 'PROFILE_GENERATION_RESULT_UNKNOWN', message: 'still unknown' } }, { ok: false, status: 409 })
  }

  const first = await generateProfileCandidate(9)
  const persisted = storage.get(ATTEMPT_KEY)
  const second = await generateProfileCandidate(9)

  assert.equal(first.ok, false)
  assert.equal(first.status, 0)
  assert.equal(first.body.detail.code, 'PROFILE_GENERATION_RESULT_UNKNOWN')
  assert.match(first.body.detail.message, /不会自动重发/)
  assert.match(persisted, /^idem-/)
  assert.deepEqual(seenKeys, [persisted, persisted])
  assert.equal(second.status, 409)
  assert.equal(second.body.detail.code, 'PROFILE_GENERATION_RESULT_UNKNOWN')
  assert.equal(storage.get(ATTEMPT_KEY), persisted)
})

test('HTTP success with incomplete body remains UNKNOWN and reuses the exact same key', async () => {
  resetStorage()
  storage.set(SESSION_KEY, 'session-token-for-profile-generation')
  const seenKeys = []
  globalThis.fetch = async (_url, init) => {
    seenKeys.push(init.headers['Local-Idempotency-Key'])
    return response({ generation: { provider: 'synthetic' } })
  }

  const first = await generateProfileCandidate(9)
  const persisted = storage.get(ATTEMPT_KEY)
  const second = await generateProfileCandidate(9)

  assert.equal(first.ok, false)
  assert.equal(first.body.detail.code, 'PROFILE_GENERATION_RESULT_UNKNOWN')
  assert.match(persisted, /^idem-/)
  assert.deepEqual(seenKeys, [persisted, persisted])
  assert.equal(second.body.detail.code, 'PROFILE_GENERATION_RESULT_UNKNOWN')
  assert.equal(storage.get(ATTEMPT_KEY), persisted)
})

test('explicit Human discard closes UNKNOWN browser replay and next generation gets a fresh key', async () => {
  resetStorage()
  storage.set(SESSION_KEY, 'session-token-for-profile-generation')
  const seenKeys = []
  globalThis.fetch = async (_url, init) => {
    seenKeys.push(init.headers['Local-Idempotency-Key'])
    return response(
      { detail: { code: 'PROFILE_GENERATION_RESULT_UNKNOWN', message: 'network result unknown' } },
      { ok: false, status: 409 }
    )
  }

  const first = await generateProfileCandidate(9)
  const oldKey = storage.get(ATTEMPT_KEY)
  assert.equal(first.body.detail.code, 'PROFILE_GENERATION_RESULT_UNKNOWN')
  assert.match(oldKey, /^idem-/)

  discardProfileGenerationAttempt(9)
  assert.equal(storage.has(ATTEMPT_KEY), false)

  const second = await generateProfileCandidate(9)
  assert.equal(second.body.detail.code, 'PROFILE_GENERATION_RESULT_UNKNOWN')
  assert.notEqual(seenKeys[0], seenKeys[1])
  assert.equal(storage.get(ATTEMPT_KEY), seenKeys[1])
})

test('classified server failure clears the old attempt key so a later explicit click is a new action', async () => {
  resetStorage()
  storage.set(SESSION_KEY, 'session-token-for-profile-generation')
  const seenKeys = []
  globalThis.fetch = async (_url, init) => {
    seenKeys.push(init.headers['Local-Idempotency-Key'])
    return response(
      { detail: { code: 'PROFILE_GENERATION_CREDENTIAL_REQUIRED', message: 'credential required' } },
      { ok: false, status: 409 }
    )
  }

  const first = await generateProfileCandidate(9)
  const second = await generateProfileCandidate(9)

  assert.equal(first.body.detail.code, 'PROFILE_GENERATION_CREDENTIAL_REQUIRED')
  assert.equal(second.body.detail.code, 'PROFILE_GENERATION_CREDENTIAL_REQUIRED')
  assert.equal(storage.has(ATTEMPT_KEY), false)
  assert.notEqual(seenKeys[0], seenKeys[1])
})
