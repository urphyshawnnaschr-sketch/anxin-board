import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { location: { href: 'http://127.0.0.1:8000/#/settings' } }
})
Object.defineProperty(globalThis, 'history', {
  configurable: true,
  value: { state: null, replaceState() {} }
})

const {
  configureDeepSeekCredential,
  getDeepSeekStatus,
  selectDeepSeekModel,
  testDeepSeekConnection
} = await import('../modelSettings.js')

function response(body, { ok = true, status = 200 } = {}) {
  return { ok, status, async json() { return body } }
}

function installSession() {
  storage.clear()
  storage.set(SESSION_KEY, 'session-token-for-model-test')
}

test('model credential POST uses secure local-session write headers and exact payload', async () => {
  installSession()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ provider: 'deepseek', credential_ref: 'deepseek-api-key', configured: true, connected: false })
  }

  const result = await configureDeepSeekCredential('test-only-api-key')

  assert.equal(captured[0], '/api/settings/deepseek-credential')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['Content-Type'], 'application/json')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'session-token-for-model-test')
  assert.match(captured[1].headers['X-Request-ID'], /^req-/)
  assert.equal(captured[1].headers['Local-Idempotency-Key'], undefined)
  assert.deepEqual(JSON.parse(captured[1].body), { api_key: 'test-only-api-key' })
  assert.equal(result.ok, true)
  assert.equal(JSON.stringify(result.body).includes('test-only-api-key'), false)
})

test('status GET is non-secret and does not use local write authority', async () => {
  storage.clear()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ provider: 'deepseek', configured: true, connected: false, selected_model: 'deepseek-flash' })
  }

  const result = await getDeepSeekStatus()

  assert.equal(captured[0], '/api/settings/deepseek-status')
  assert.equal(captured[1], undefined)
  assert.equal(result.body.connected, false)
  assert.equal(result.body.selected_model, 'deepseek-flash')
})

test('connection test is an explicit local Human action and sends no key or project content', async () => {
  installSession()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ provider: 'deepseek', configured: true, connected: true, available_models: ['deepseek-flash', 'deepseek-v4-pro'], selected_model: null })
  }

  const result = await testDeepSeekConnection()

  assert.equal(captured[0], '/api/settings/deepseek-connection-test')
  assert.equal(captured[1].method, 'POST')
  assert.deepEqual(JSON.parse(captured[1].body), {})
  assert.equal(captured[1].headers['X-Anxin-Session'], 'session-token-for-model-test')
  assert.equal(result.body.connected, true)
  assert.deepEqual(result.body.available_models, ['deepseek-flash', 'deepseek-v4-pro'])
})

test('selected model POST binds one exact live model id', async () => {
  installSession()
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({ provider: 'deepseek', configured: true, connected: true, available_models: ['deepseek-v4-pro'], selected_model: 'deepseek-v4-pro' })
  }

  const result = await selectDeepSeekModel('deepseek-v4-pro')

  assert.equal(captured[0], '/api/settings/deepseek-model-selection')
  assert.equal(captured[1].method, 'POST')
  assert.deepEqual(JSON.parse(captured[1].body), { model_id: 'deepseek-v4-pro' })
  assert.equal(result.body.selected_model, 'deepseek-v4-pro')
})

test('model credential write fails closed before fetch when no local session exists', async () => {
  storage.clear()
  let fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    throw new Error('fetch must not run without a local session')
  }

  await assert.rejects(() => configureDeepSeekCredential('test-only-api-key'), /LOCAL_SESSION_UNAVAILABLE/)
  assert.equal(fetchCalls, 0)
})

test('model setup UI distinguishes saved key, tested connection, and selected live model', async () => {
  const source = await readFile(new URL('../../components/DeepSeekConnectionSettingsCard.vue', import.meta.url), 'utf8')
  const quick = await readFile(new URL('../../components/ModelQuickSetup.vue', import.meta.url), 'utf8')

  assert.equal(source.includes('Key 已保存·待测试'), true)
  assert.equal(source.includes('测试连接'), true)
  assert.equal(source.includes('当前可用模型'), true)
  assert.equal(source.includes('使用此模型'), true)
  assert.equal(source.includes('不会发送 PRD、代码或报告内容'), true)
  assert.equal(quick.includes('<DeepSeekConnectionSettingsCard compact />'), true)
  assert.equal(source.includes('真实返回的模型列表为准'), true)
  assert.equal(source.includes('DeepSeek · 当前可用'), false)
})
