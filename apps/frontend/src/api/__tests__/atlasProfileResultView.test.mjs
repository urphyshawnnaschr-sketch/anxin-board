import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { runInNewContext } from 'node:vm'
import { ref, computed, reactive } from 'vue'
import * as presentation from '../../features/project-profile/projectProfilePresentation.js'

const hash = 'a'.repeat(64)
const profile = { id: 8, project_id: 3, content_hash: hash, status: 'confirmed', content: {
  planned_modules: [{ client_id: 'm', requirements: ['A'] }]
} }
const result = { task_id: 'old-success', status: 'succeeded', profile_id: 8, generated_content_hash: hash,
  module_requirements: { m: [{ requirement_index: 0, status: 'unknown', evidence_ids: [], rationale: 'Uncertain' }] } }

async function harness(read = async () => ({ ok: true, body: { task: result } })) {
  const text = await readFile(new URL('../../views/ProjectProfilePanel.vue', import.meta.url), 'utf8')
  const source = text.split('<script setup>')[1].split('</script>')[0].replace(/^import[\s\S]*?from ['"][^'"]+['"]\r?\n/gm, '')
  const hooks = [], watchers = [], props = reactive({ projectId: 3 })
  const context = { ...presentation, ref, computed, defineProps: () => props,
    onMounted() {}, onUnmounted: fn => hooks.push(fn), watch: (get, run) => watchers.push({ get, run }),
    clearTimeout() {}, setTimeout() {}, URL: { revokeObjectURL() {} },
    createProfileCandidate() {}, getProjectProfile() {}, listProjectProfiles() {}, updateProfileCandidate() {}, confirmProfileCandidate() {},
    getAtlasProfileResult: read, atlasReportErrorMessage: () => '请重新打开本地窗口',
    useProjectProfilePanel: () => ({ serverProfile: ref(structuredClone(profile)), historyViewing: ref(null),
      viewMode: ref('readonly'), form: ref({ modules: [] }), hasUnsaved: ref(false), formLocked: ref(false), confirmDisabled: ref(false),
      activeConfirmed: ref(null), collapsedModules: ref([]), saving: ref(false), confirming: ref(false), creating: ref(false) }) }
  runInNewContext(source + '\nglobalThis.resultHarness={loadProfileResult,resultTask,resultError,resultLoading,reportBound,requirementOverview,atlasRequirementRows,atlasTask,serverProfile,historyViewing,hasUnsaved,candidateEditingLocked,generatedResultReadOnly,confirmDisabled,headingBadge,viewMode,baselineExecuting,baselineUnknown}', context)
  return { ...context.resultHarness, props, hooks, watchers }
}

test('saved evidence remains reactive and available with a newer failed or running task', async () => {
  for (const status of ['failed_after_send', 'running']) {
    const h = await harness()
    h.atlasTask.value = { task_id: 'new-task', status }
    // Evaluate before the request completes: real Vue computed must invalidate later.
    assert.equal(h.reportBound.value, false)
    assert.equal(h.requirementOverview.value, null)
    await h.loadProfileResult()
    assert.equal(h.resultTask.value.task_id, 'old-success')
    assert.equal(h.reportBound.value, true)
    assert.equal(h.requirementOverview.value.total, 1)
    assert.equal(h.atlasRequirementRows(profile.content.planned_modules[0]).length, 1)
    assert.equal(h.atlasTask.value.task_id, 'new-task')
  }
})

test('late result cannot bind after history, project, or same-profile hash changes', async () => {
  for (const mutate of [h => { h.historyViewing.value = { ...profile, id: 9 } },
    h => { h.props.projectId = 4 }, h => { h.serverProfile.value.content_hash = 'b'.repeat(64) }]) {
    let resolve
    const h = await harness(() => new Promise(r => { resolve = r }))
    const pending = h.loadProfileResult()
    mutate(h)
    resolve({ ok: true, body: { task: result } })
    await pending
    assert.equal(h.resultTask.value, null)
    assert.equal(h.reportBound.value, false)
  }
})

test('hash mismatch and lookup failure never borrow evidence or report binding', async () => {
  for (const read of [async () => ({ ok: true, body: { task: { ...result, generated_content_hash: 'b'.repeat(64) } } }),
    async () => { throw Error('synthetic') }]) {
    const h = await harness(read)
    await h.loadProfileResult()
    assert.equal(h.reportBound.value, false)
    assert.equal(h.requirementOverview.value, null)
    assert.ok(h.resultError.value)
  }
})

test('unmount invalidates an outstanding result lookup', async () => {
  let resolve
  const h = await harness(() => new Promise(r => { resolve = r }))
  const pending = h.loadProfileResult()
  for (const hook of h.hooks) hook()
  resolve({ ok: true, body: { task: result } })
  await pending
  assert.equal(h.resultTask.value, null)
})

test('older concurrent lookup cannot overwrite the newest result or loading state', async () => {
  const resolvers = []
  const h = await harness(() => new Promise(resolve => resolvers.push(resolve)))
  const old = h.loadProfileResult()
  h.historyViewing.value = { ...profile, id: 9, status: 'superseded' }
  const current = h.loadProfileResult()
  resolvers[0]({ ok: false, body: { detail: { code: 'SYNTHETIC_FAILURE' } } })
  await old
  assert.equal(h.resultLoading.value, true)
  assert.equal(h.resultError.value, '')
  resolvers[1]({ ok: true, body: { task: { ...result, profile_id: 9, task_id: 'history-success' } } })
  await current
  assert.equal(h.resultLoading.value, false)
  assert.equal(h.resultTask.value.task_id, 'history-success')
  assert.equal(h.requirementOverview.value.total, 1)
})

test('generated marker locks edits even if lookup fails without locking manual confirmation', async () => {
  const h = await harness(async () => ({ ok: false, body: { detail: { code: 'BROWNFIELD_RESULT_IDENTITY_INVALID' } } }))
  h.serverProfile.value = { ...profile, status: 'candidate', generated_baseline_result: true }
  await h.loadProfileResult()
  assert.equal(h.generatedResultReadOnly.value, true)
  assert.equal(h.candidateEditingLocked.value, true)
  assert.equal(h.confirmDisabled.value, false)
  assert.equal(h.reportBound.value, false)
  assert.ok(h.resultError.value)
})


test('only a hash-bound successful candidate says analysis completed awaiting review', async () => {
  const h = await harness()
  h.serverProfile.value.status = 'candidate'
  h.viewMode.value = 'editing'
  assert.equal(h.headingBadge.value.text, '0 个待确认模块')
  await h.loadProfileResult()
  assert.equal(h.headingBadge.value.text, '分析已完成 · 待你审核')
  h.serverProfile.value.content_hash = 'b'.repeat(64)
  assert.equal(h.headingBadge.value.text, '0 个待确认模块')
})

test('active analysis and unknown outcome retain priority over a saved candidate result', async () => {
  const h = await harness()
  h.serverProfile.value.status = 'candidate'
  await h.loadProfileResult()
  h.baselineExecuting.value = true
  assert.equal(h.headingBadge.value.text, '正在核对代码证据')
  h.baselineExecuting.value = false
  h.baselineUnknown.value = true
  assert.equal(h.headingBadge.value.text, '分析结果暂无法确认')
})
