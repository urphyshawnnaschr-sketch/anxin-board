import test from 'node:test'
import assert from 'node:assert/strict'
import { ref, effectScope, nextTick } from 'vue'
import { useProjectProfilePanel } from '../useProjectProfilePanel.js'

const profile = status => ({ id: 9, project_id: 1, status, edit_version: status === 'confirmed' ? 2 : 1,
  content_hash: 'a'.repeat(64), content: { schema_version: '1.0', modules: [], domain_glossary: [] } })
function setup(get) {
  const calls = { posts: 0, reads: 0 }, project = ref(1), scope = effectScope()
  const api = { confirmProfileCandidate: async () => { calls.posts++; throw Error('response lost') },
    getProjectProfile: async id => { assert.equal(id, 9); calls.reads++; return get() },
    listProjectProfiles: async () => ({ ok: true, body: [] }) }
  const panel = scope.run(() => useProjectProfilePanel({ projectId: project, api }))
  panel.serverProfile.value = profile('candidate'); panel.viewMode.value = 'editing'
  return { panel, calls, scope, project, api }
}
test('lost confirmation response reconciles exact confirmed profile without another POST', async () => {
  const h = setup(() => ({ ok: true, body: profile('confirmed') }))
  await h.panel.confirmCandidate()
  assert.equal(h.panel.serverProfile.value.status, 'confirmed')
  assert.match(h.panel.message.value, /已确认/)
  await h.panel.confirmCandidate()
  assert.deepEqual(h.calls, { posts: 1, reads: 1 }); h.scope.stop()
})
test('failed read keeps confirmation uncertain and blocks repeated confirmation', async () => {
  const h = setup(() => { throw Error('offline') })
  await h.panel.confirmCandidate()
  assert.match(h.panel.message.value, /尚未核实/)
  assert.equal(h.panel.confirmDisabled.value, true)
  assert.equal(h.panel.conflict.value, true)
  await h.panel.confirmCandidate()
  assert.equal(h.calls.posts, 1); h.scope.stop()
})
test('candidate observation cannot prove a lost write will never commit', async () => {
  const h = setup(() => ({ ok: true, body: profile('candidate') }))
  await h.panel.confirmCandidate(); await h.panel.reloadServerVersion(); await h.panel.confirmCandidate()
  assert.equal(h.calls.posts, 1); assert.equal(h.panel.confirmDisabled.value, true); h.scope.stop()
})
test('manual reload later resolves uncertainty through GET only', async () => {
  let current = 'candidate'
  const h = setup(() => ({ ok: true, body: profile(current) }))
  await h.panel.confirmCandidate()
  current = 'confirmed'
  await h.panel.reloadServerVersion()
  assert.equal(h.panel.serverProfile.value.status, 'confirmed')
  assert.equal(h.panel.conflict.value, false)
  assert.equal(h.calls.posts, 1); assert.equal(h.calls.reads, 2); h.scope.stop()
})
for (const changed of [{ id: 10 }, { project_id: 2 }, { content_hash: 'b'.repeat(64) }, { edit_version: 1 }]) {
  test(`read recovery rejects changed identity ${Object.keys(changed)[0]}`, async () => {
    const h = setup(() => ({ ok: true, body: { ...profile('confirmed'), ...changed } }))
    await h.panel.confirmCandidate()
    assert.equal(h.panel.serverProfile.value.status, 'candidate')
    assert.equal(h.panel.confirmDisabled.value, true)
    assert.match(h.panel.message.value, /尚未核实/); h.scope.stop()
  })
}
test('HTTP 500 and malformed success are read-reconciled without resubmission', async () => {
  for (const response of [{ ok: false, status: 500, body: {} }, { ok: true, status: 200, body: {} }]) {
    const h = setup(() => ({ ok: true, body: profile('confirmed') }))
    h.api.confirmProfileCandidate = async () => { h.calls.posts++; return response }
    await h.panel.confirmCandidate()
    assert.equal(h.panel.serverProfile.value.status, 'confirmed')
    assert.deepEqual(h.calls, { posts: 1, reads: 1 }); h.scope.stop()
  }
})
for (const change of ['project', 'dispose']) test(`late confirmation recovery ignored after ${change}`, async () => {
  let release
  const h = setup(() => new Promise(resolve => { release = resolve }))
  const pending = h.panel.confirmCandidate()
  await new Promise(resolve => setImmediate(resolve))
  if (change === 'dispose') h.scope.stop()
  else { h.project.value = 2; await nextTick() }
  const previous = h.panel.serverProfile.value
  release({ ok: true, body: profile('confirmed') }); await pending
  assert.equal(h.panel.serverProfile.value, previous); h.scope.stop()
})
