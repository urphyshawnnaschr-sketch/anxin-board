globalThis.window = { location: { href: 'http://127.0.0.1:8000/#/projects' } }
import assert from 'node:assert/strict'
import test from 'node:test'

const SESSION_KEY = 'anxinboard:local-browser-session:v1'
const storage = new Map([[SESSION_KEY, 'session-test-token']])
Object.defineProperty(globalThis, 'sessionStorage', {
  configurable: true,
  value: {
    getItem(key) { return storage.get(key) || null },
    setItem(key, value) { storage.set(key, String(value)) }
  }
})

const { getPrdReviewChunk } = await import('../prd.js')

test('full PRD review uses protected local read headers and no-store', async () => {
  let observed = null
  globalThis.fetch = async (url, options = {}) => {
    observed = { url: String(url), options }
    return {
      ok: true,
      status: 200,
      async json() {
        return {
          schema_version: 'prd_parsed_review_v1',
          version_id: 7,
          parsed_hash: 'a'.repeat(64),
          offset: 100,
          content: '正文',
          next_offset: 102,
          complete: true,
          total_chars: 102
        }
      }
    }
  }

  const result = await getPrdReviewChunk(7, { offset: 100, limit: 50000 })

  assert.equal(result.ok, true)
  assert.equal(result.body.version_id, 7)
  assert.match(observed.url, /^\/api\/prd-versions\/7\/review\?/)
  const query = new URLSearchParams(observed.url.split('?')[1])
  assert.equal(query.get('offset'), '100')
  assert.equal(query.get('limit'), '50000')
  assert.equal(observed.options.method, 'GET')
  assert.equal(observed.options.cache, 'no-store')
  assert.equal(observed.options.headers['X-Anxin-Session'], 'session-test-token')
  assert.match(observed.options.headers['X-Request-ID'], /^read-/)
  assert.equal(Object.hasOwn(observed.options.headers, 'Local-Idempotency-Key'), false)
})
