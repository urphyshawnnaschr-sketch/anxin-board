import test from 'node:test'
import assert from 'node:assert/strict'
import { getAtlasProfileResult } from '../projectProfiles.js'

const key = 'anxinboard:local-browser-session:v1'
const session = new Map()
globalThis.window = { location: { href: 'http://localhost/#/projects/4/modules' } }
globalThis.sessionStorage = { getItem: k => session.get(k), setItem: (k, v) => session.set(k, v) }

test('profile result is a protected exact-profile GET, never a task or provider operation', async () => {
  session.set(key, 'synthetic-session')
  let calls = 0
  globalThis.fetch = async (url, options) => {
    calls++
    assert.equal(url, '/api/projects/4/project-state-baseline/atlas/results/8')
    assert.equal(options.method, 'GET')
    assert.equal(options.headers['X-Anxin-Session'], 'synthetic-session')
    assert.equal(options.body, undefined)
    assert.equal(options.cache, 'no-store')
    return new Response(JSON.stringify({ task: null }))
  }
  assert.deepEqual((await getAtlasProfileResult(4, 8)).body, { task: null })
  assert.equal(calls, 1)
})

test('missing, expired and lost reads do not retry or expose exceptions', async () => {
  session.clear()
  let calls = 0
  globalThis.fetch = async () => { calls++; throw Error('private detail') }
  assert.equal((await getAtlasProfileResult(4, 8)).body.detail.code, 'LOCAL_SESSION_UNAVAILABLE')
  assert.equal(calls, 0)
  session.set(key, 'expired')
  globalThis.fetch = async () => { calls++; return new Response(JSON.stringify({ detail: { code: 'LOCAL_SESSION_SESSION_INVALID' } }), { status: 403 }) }
  assert.equal((await getAtlasProfileResult(4, 8)).status, 403)
  assert.equal(session.get(key), '')
  assert.equal(calls, 1)
  session.set(key, 'synthetic-session')
  globalThis.fetch = async () => { calls++; throw Error('private detail') }
  assert.equal((await getAtlasProfileResult(4, 8)).body.detail.code, 'BROWNFIELD_RESULT_READ_FAILED')
  assert.equal(calls, 2)
})
