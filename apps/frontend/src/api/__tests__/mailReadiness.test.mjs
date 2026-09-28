import assert from 'node:assert/strict'
import test from 'node:test'

const { getMailReadiness } = await import('../mailReadiness.js')

function response(body, { ok = true, status = 200 } = {}) {
  return { ok, status, async json() { return body } }
}

test('mail readiness is project-scoped GET with no session or credential headers', async () => {
  let captured
  globalThis.fetch = async (...args) => {
    captured = args
    return response({
      schema_version: 'mail_readiness_v1',
      project_id: 7,
      state: 'blocked',
      candidate_ready: false,
      send_action_available: false,
      checks: [],
      candidate: null
    })
  }

  const result = await getMailReadiness(7)

  assert.equal(captured[0], '/api/projects/7/mail-readiness')
  assert.equal(captured[1], undefined)
  assert.equal(result.body.send_action_available, false)
})
