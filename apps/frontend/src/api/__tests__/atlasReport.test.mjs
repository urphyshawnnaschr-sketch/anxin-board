import assert from 'node:assert/strict'
import test from 'node:test'
const session = new Map()
globalThis.sessionStorage = { getItem: k => session.get(k) ?? null, setItem: (k,v) => session.set(k,v), removeItem: k => session.delete(k) }
globalThis.window = { location: { href: 'http://127.0.0.1:8000/#/projects/3/modules' } }
const api = await import('../projectProfiles.js')
test('HTML reports use protected GET without authorization or confirmation', async () => {
  session.set('anxinboard:local-browser-session:v1', 'synthetic-session')
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push([url, options]); return new Response('<!doctype html><p>synthetic</p>', { headers: { 'Content-Type': 'text/html; charset=utf-8' } }) }
  for (const download of [false, true]) {
    const result = await api.getAtlasReport(3, 'task-test', { download })
    assert.equal(result.ok, true)
    assert.equal(result.blob.type, 'text/html;charset=utf-8')
  }
  assert.deepEqual(calls.map(c => c[0]), ['/api/projects/3/project-state-baseline/atlas/tasks/task-test/review-html', '/api/projects/3/project-state-baseline/atlas/tasks/task-test/export-html'])
  for (const [, options] of calls) {
    assert.equal(options.method, 'GET'); assert.equal(options.headers['X-Anxin-Session'], 'synthetic-session')
    assert.equal(options.body, undefined); assert.equal(options.headers['Local-Idempotency-Key'], undefined)
  }
})
test('HTML report rejects wrong content type and safe backend denial', async () => {
  globalThis.fetch = async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } })
  assert.equal((await api.getAtlasReport(3, 'task')).ok, false)
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: 'BROWNFIELD_REPORT_CONFIRMATION_REQUIRED' } }), { status: 409, headers: { 'Content-Type': 'application/json' } })
  assert.equal((await api.getAtlasReport(3, 'task', { download: true })).body.detail.code, 'BROWNFIELD_REPORT_CONFIRMATION_REQUIRED')
})
test('missing session and network loss never initiate a second report request', async () => {
  let calls = 0
  session.clear(); globalThis.fetch = async () => { calls++; throw Error('private detail') }
  assert.equal((await api.getAtlasReport(3, 'task')).ok, false); assert.equal(calls, 0)
  session.set('anxinboard:local-browser-session:v1', 'synthetic-session')
  assert.equal((await api.getAtlasReport(3, 'task')).ok, false); assert.equal(calls, 1)
})

test('expired session response clears only its own token and provides launcher guidance', async () => {
  const key = 'anxinboard:local-browser-session:v1'
  for (const renewed of [false, true]) {
    session.set(key, 'old-session'); let calls = 0
    globalThis.fetch = async () => {
      calls++; if (renewed) session.set(key, 'new-session')
      return new Response(JSON.stringify({ detail: { code: 'LOCAL_SESSION_SESSION_INVALID' } }), { status: 403 })
    }
    const result = await api.getAtlasReport(3, 'task')
    assert.equal(calls, 1)
    assert.equal(session.get(key) || '', renewed ? 'new-session' : '')
    assert.match(api.atlasReportErrorMessage(result.body.detail.code), /桌面/)
  }
})

test('missing session task admission is a known pre-send denial with no HTTP or nonce writes', async () => {
  session.clear(); let calls = 0
  globalThis.fetch = async () => { calls++; throw Error('Must not send') }
  globalThis.localStorage = { getItem: () => { throw Error('Must not touch nonce') } }
  for (const invoke of [() => api.createAtlasTask(3, 6, 'hash'), () => api.resumeAtlasTask(3, 'task')]) {
    const result = await invoke()
    assert.equal(result.status, 403)
    assert.match(api.formatProjectStateBaselineError(result.body, 'fallback'), /桌面/)
  }
  assert.equal(calls, 0)
})

test('invalid session preflight clears matching token without retrying local scan', async () => {
  session.set('anxinboard:local-browser-session:v1', 'expired'); let calls = 0
  globalThis.fetch = async () => { calls++; return new Response(JSON.stringify({ detail: { code: 'LOCAL_SESSION_SESSION_INVALID' } }), { status: 403 }) }
  const result = await api.preflightAtlasBaseline(3, 6)
  assert.equal(calls, 1)
  assert.equal(session.get('anxinboard:local-browser-session:v1'), '')
  assert.match(api.formatProjectStateBaselineError(result.body, 'fallback'), /桌面/)
})

test('result lookup errors explain fixed identity outcomes without suggesting retries', () => {
  for (const [code, text] of [
    ['BROWNFIELD_RESULT_IDENTITY_INVALID', '不一致'],
    ['BROWNFIELD_RESULT_AMBIGUOUS', '多个'],
    ['BROWNFIELD_RESULT_NOT_FOUND', '未找到']
  ]) {
    assert.ok(api.atlasReportErrorMessage(code).includes(text))
    assert.ok(!api.atlasReportErrorMessage(code).includes('重试'))
  }
})
