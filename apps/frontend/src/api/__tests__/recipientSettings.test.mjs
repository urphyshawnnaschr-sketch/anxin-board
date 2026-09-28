globalThis.window = { location: { href: 'http://127.0.0.1:8000/#/projects' } }
import assert from 'node:assert/strict'
import test from 'node:test'

const SESSION_KEY = 'anxinboard:local-browser-session:v1'
const storage = new Map()
Object.defineProperty(globalThis, 'sessionStorage', {
  configurable: true,
  value: {
    getItem(key) { return storage.get(key) ?? null },
    setItem(key, value) { storage.set(key, String(value)) },
    removeItem(key) { storage.delete(key) }
  }
})

const { getRecipientConfig, saveRecipientConfig } = await import('../recipientSettings.js')

function response(body, { ok = true, status = 200 } = {}) {
  return { ok, status, async json() { return body } }
}

test('recipient GET is project-scoped plain read', async () => {
  storage.clear()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ configured: false, version_no: 0, to_recipients: [] })
  }

  const result = await getRecipientConfig(7)

  assert.equal(captured[0], '/api/projects/7/recipient-config')
  assert.equal(captured[1], undefined)
  assert.equal(result.body.configured, false)
})

test('recipient POST requires local session and idempotency header', async () => {
  storage.clear()
  storage.set(SESSION_KEY, 'recipient-session-test')
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ configured: true, version_no: 1, to_recipients: ['a@example.test'] })
  }
  const payload = { to_recipients: ['a@example.test'], expected_version_no: 0 }

  const result = await saveRecipientConfig(7, payload)

  assert.equal(captured[0], '/api/projects/7/recipient-config')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'recipient-session-test')
  assert.match(captured[1].headers['X-Request-ID'], /^req-/)
  assert.match(captured[1].headers['Local-Idempotency-Key'], /^idem-/)
  assert.deepEqual(JSON.parse(captured[1].body), payload)
  assert.deepEqual(result.body.to_recipients, ['a@example.test'])
})
