import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'

async function harness({ token = '', fragment = '?anxin_bootstrap=synthetic-handoff', exchangeOk = true } = {}) {
  const storage = new Map(token ? [['anxinboard:local-browser-session:v1', token]] : [])
  const location = { href: `http://127.0.0.1:8000/#/projects${fragment}` }
  let calls = 0
  const context = vm.createContext({ URL, URLSearchParams, window: { location },
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    history: { state: null, replaceState: (_state, _title, url) => { location.href = new URL(url, location.href).href } },
    fetch: async () => { calls++; await new Promise(resolve => setTimeout(resolve, 10)); return { ok: exchangeOk, json: async () => ({ session_state: 'active', session_token: 'synthetic-session' }) } }
  })
  const source = await readFile(new URL('../localSession.js', import.meta.url), 'utf8')
  vm.runInContext(source.replace(/export /g, ''), context)
  return { context, storage, location, calls: () => calls }
}

test('new launcher fragment replaces stale browser session before write headers', async () => {
  const h = await harness({ token: 'stale-session' })
  assert.equal((await h.context.localWriteHeaders())['X-Anxin-Session'], 'synthetic-session')
  assert.equal(h.calls(), 1)
  assert.ok(!h.location.href.includes('anxin_bootstrap'))
})

test('concurrent mount and reads consume one handoff exchange', async () => {
  const h = await harness()
  const results = await Promise.all([h.context.ensureLocalSession(), h.context.localReadHeaders(), h.context.localWriteHeaders()])
  assert.equal(h.calls(), 1)
  assert.equal(results[0], results[1]['X-Anxin-Session'])
  assert.equal(results[0], results[2]['X-Anxin-Session'])
})

test('failed new exchange does not silently authorize using old token and always strips fragment', async () => {
  const h = await harness({ token: 'stale-session', exchangeOk: false })
  await assert.rejects(h.context.localWriteHeaders(), /LOCAL_SESSION_UNAVAILABLE/)
  assert.ok(!h.location.href.includes('anxin_bootstrap'))
})

test('ordinary authenticated reads do not exchange or trigger writes', async () => {
  const h = await harness({ token: 'existing-session', fragment: '' })
  assert.equal((await h.context.localReadHeaders())['X-Anxin-Session'], 'existing-session')
  assert.equal(h.calls(), 0)
})

test('only known failed session responses invalidate the matching issued token', async () => {
  const h = await harness({ token: 'existing-session', fragment: '' })
  const headers = { 'X-Anxin-Session': 'existing-session' }
  for (const [status, code] of [[200, 'LOCAL_SESSION_SESSION_INVALID'], [403, 'OTHER_ERROR'], [500, 'LOCAL_SESSION_SESSION_INVALID']]) {
    assert.equal(h.context.handleLocalSessionFailure(status, { detail: { code } }, headers), false)
    assert.equal(h.context.getLocalSession(), 'existing-session')
  }
  assert.equal(h.context.handleLocalSessionFailure(403, { detail: { code: 'LOCAL_SESSION_SESSION_INVALID' } }, headers), true)
  assert.equal(h.context.getLocalSession(), '')
  assert.equal(h.calls(), 0)
})
