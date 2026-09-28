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

const { configureMailTransport, getMailTransportSettings, testMailTransportConnection } = await import('../mailSettings.js')

function response(body, { ok = true, status = 200 } = {}) {
  return { ok, status, async json() { return body } }
}

test('mail settings GET is a plain non-secret status read', async () => {
  storage.clear()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ configured: false, profile: null })
  }

  const result = await getMailTransportSettings()

  assert.equal(captured[0], '/api/settings/mail-transport')
  assert.equal(captured[1], undefined)
  assert.equal(result.ok, true)
  assert.equal(result.body.configured, false)
})

test('mail settings POST uses local-session write headers and exact payload', async () => {
  storage.clear()
  storage.set(SESSION_KEY, 'session-token-for-test')
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({
      schema_version: 'mail_transport_settings_v1',
      configured: true,
      credential_reference_bound: true,
      profile: { version_no: 1 }
    })
  }
  const payload = {
    host: 'smtp.example.test',
    port: 587,
    security: 'starttls',
    username: 'mailer@example.test',
    from_identity: 'reports@example.test',
    password: 'test-only-password',
    timeout_seconds: 30,
    expected_version_no: 0
  }

  const result = await configureMailTransport(payload)

  assert.equal(captured[0], '/api/settings/mail-transport')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['Content-Type'], 'application/json')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'session-token-for-test')
  assert.match(captured[1].headers['X-Request-ID'], /^req-/)
  assert.equal(captured[1].headers['Local-Idempotency-Key'], undefined)
  assert.deepEqual(JSON.parse(captured[1].body), payload)
  assert.equal(result.body.configured, true)
  assert.equal(JSON.stringify(result.body).includes('test-only-password'), false)
})

test('SMTP connection test is a protected POST bound to the exact viewed profile and has no message payload', async () => {
  storage.clear()
  storage.set(SESSION_KEY, 'session-token-for-test')
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({
      schema_version: 'mail_transport_connection_test_v1',
      status: 'passed',
      profile_version: 3,
      checks: {
        credential_read: true,
        smtp_connect: true,
        tls_ready: true,
        smtp_auth: true,
        post_auth_noop: true,
        message_submission: false
      }
    })
  }

  const result = await testMailTransportConnection(3)

  assert.equal(captured[0], '/api/settings/mail-transport/test?expected_version_no=3')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'session-token-for-test')
  assert.match(captured[1].headers['X-Request-ID'], /^req-/)
  assert.equal(captured[1].body, undefined)
  assert.equal(result.body.status, 'passed')
  assert.equal(result.body.profile_version, 3)
  assert.equal(result.body.checks.message_submission, false)
})

test('SMTP connection test rejects an invalid profile version before fetch', async () => {
  storage.clear()
  storage.set(SESSION_KEY, 'session-token-for-test')
  let fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    throw new Error('fetch must not run')
  }

  await assert.rejects(() => testMailTransportConnection(0), /MAIL_TRANSPORT_TEST_VERSION_INVALID/)
  assert.equal(fetchCalls, 0)
})
