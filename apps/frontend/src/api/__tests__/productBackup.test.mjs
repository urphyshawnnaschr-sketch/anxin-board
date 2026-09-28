import assert from 'node:assert/strict'
import test from 'node:test'

const SESSION_KEY = 'anxinboard:local-browser-session:v1'
const storage = new Map([[SESSION_KEY, 'backup-session-token']])
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

const { exportProductBackup } = await import('../productBackup.js')

test('backup export uses authenticated local write boundary and returns the ZIP', async () => {
  let captured
  const blob = new Blob(['safe-backup'])
  globalThis.fetch = async (...args) => {
    captured = args
    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-disposition': 'attachment; filename="AnxinBoard-backup.zip"' }),
      async blob() { return blob }
    }
  }

  const result = await exportProductBackup()

  assert.equal(captured[0], '/api/product-backup/export')
  assert.equal(captured[1].method, 'POST')
  assert.equal(captured[1].headers['X-Anxin-Session'], 'backup-session-token')
  assert.match(captured[1].headers['X-Request-ID'], /^req-/)
  assert.equal(result.ok, true)
  assert.equal(result.filename, 'AnxinBoard-backup.zip')
  assert.equal(result.blob, blob)
})
