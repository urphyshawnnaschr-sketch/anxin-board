import assert from 'node:assert/strict'
import { ref, nextTick } from 'vue'
import { useProjectProfilePanel } from '../useProjectProfilePanel.js'

let passed = 0
let failed = 0

function report(name, fn) {
  try {
    fn()
    passed++
    console.log(`  PASS  ${name}`)
  } catch (err) {
    failed++
    console.error(`  FAIL  ${name}`)
    console.error(`        ${err.message}`)
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function createApi() {
  const calls = { list: [], get: [], create: [], update: [], confirm: [] }
  const api = {
    calls,
    listProjectProfiles: () => {
      const d = deferred()
      calls.list.push(d)
      return d.promise
    },
    getProjectProfile: () => {
      const d = deferred()
      calls.get.push(d)
      return d.promise
    },
    createProfileCandidate: () => {
      const d = deferred()
      calls.create.push(d)
      return d.promise
    },
    updateProfileCandidate: () => {
      const d = deferred()
      calls.update.push(d)
      return d.promise
    },
    confirmProfileCandidate: () => {
      const d = deferred()
      calls.confirm.push(d)
      return d.promise
    }
  }
  return api
}

function okResponse(body) {
  return { status: 200, ok: true, body }
}

async function flush(times = 4) {
  for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0))
}

const candidateProfile = (id, extra = {}) => ({
  id,
  status: 'candidate',
  edit_version: 1,
  content: { schema_version: '1.0', modules: [{ client_id: 'm1', name: 'M' }], domain_glossary: [] },
  ...extra
})
const confirmedProfile = (id) => ({
  id,
  status: 'confirmed',
  edit_version: 1,
  content: { schema_version: '1.0', modules: [], domain_glossary: [] }
})

async function loadCandidate(api, projectRef) {
  api.calls.list[0].resolve(okResponse([{ id: 'a', status: 'candidate', edit_version: 1 }]))
  await flush()
  api.calls.get[0].resolve(okResponse(candidateProfile('a')))
  await flush()
}

console.log('useProjectProfilePanel lifecycle tests')
console.log('------')

// 1. composable initializes without throwing; collapsedModules exists
// (also covers the Blocker fix)
report('composable initializes, collapsedModules is an array', () => {
  const projectRef = ref(null)
  const panel = useProjectProfilePanel({ projectId: projectRef, api: createApi() })
  assert.ok(Array.isArray(panel.collapsedModules.value))
})

async function testProjectSwitchLateResponseDiscarded() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  assert.equal(api.calls.list.length, 1, 'A list fired')
  projectRef.value = 'B'
  await nextTick()
  await flush()
  assert.equal(api.calls.list.length, 2, 'B list fired')
  // resolve B fully first
  api.calls.list[1].resolve(okResponse([{ id: 'b', status: 'confirmed', edit_version: 1 }]))
  await flush()
  assert.equal(api.calls.get.length, 1, 'B get fired')
  api.calls.get[0].resolve(okResponse(confirmedProfile('b')))
  await flush()
  // now A's late list response arrives
  api.calls.list[0].resolve(okResponse([{ id: 'a', status: 'confirmed', edit_version: 1 }]))
  await flush()
  assert.equal(panel.serverProfile.value && panel.serverProfile.value.id, 'b', 'A late response must not override B')
  assert.equal(panel.viewMode.value, 'readonly')
}

async function testSwitchToNullLateDiscarded() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  projectRef.value = null
  await nextTick()
  await flush()
  // A's list resolves late while current project is null
  api.calls.list[0].resolve(okResponse([{ id: 'a', status: 'confirmed', edit_version: 1 }]))
  await flush()
  assert.equal(api.calls.get.length, 0, 'no get may fire after switch to null (stale dropped before that stage)')
  assert.equal(panel.serverProfile.value, null, 'A late response must not restore state')
  assert.equal(panel.panelState.value, 'loading')
}

async function testSaveSwitchDoesNotTouchB() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  await loadCandidate(api, projectRef)
  assert.equal(panel.viewMode.value, 'editing')
  // start save on A
  panel.saveCandidate()
  await flush()
  assert.equal(api.calls.update.length, 1, 'A update fired')
  assert.equal(panel.saving.value, true)
  // switch to B
  projectRef.value = 'B'
  await nextTick()
  await flush()
  api.calls.list[1].resolve(okResponse([{ id: 'b', status: 'candidate', edit_version: 1 }]))
  await flush()
  api.calls.get[1].resolve(okResponse(candidateProfile('b')))
  await flush()
  assert.equal(panel.serverProfile.value.id, 'b')
  // A's save response arrives late with a success body
  api.calls.update[0].resolve(
    okResponse({ changed: true, profile: { id: 'a', status: 'candidate', content: candidateProfile('a').content } })
  )
  await flush()
  assert.equal(panel.serverProfile.value.id, 'b', 'A save response must not modify B')
  assert.equal(panel.saving.value, false)
}

async function testOldFinallyDoesNotClearNewSaving() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  await loadCandidate(api, projectRef)
  panel.saveCandidate()
  await flush()
  assert.equal(panel.saving.value, true, 'A saving starts')
  // switch to B, load, start B save
  projectRef.value = 'B'
  await nextTick()
  await flush()
  api.calls.list[1].resolve(okResponse([{ id: 'b', status: 'candidate', edit_version: 1 }]))
  await flush()
  api.calls.get[1].resolve(okResponse(candidateProfile('b')))
  await flush()
  panel.saveCandidate()
  await flush()
  assert.equal(api.calls.update.length, 2, 'B update fired')
  assert.equal(panel.saving.value, true, 'B saving is active')
  // A's old request resolves after B already saving
  api.calls.update[0].resolve({ status: 500, ok: false, body: {} })
  await flush()
  assert.equal(panel.saving.value, true, 'A old finally must not clear B saving')
  // B completes
  api.calls.update[1].resolve(
    okResponse({ changed: false, profile: { id: 'b', status: 'candidate', content: candidateProfile('b').content } })
  )
  await flush()
  assert.equal(panel.saving.value, false, 'B own finally clears saving')
}

async function testHistoryAndSaveDoNotCancelEachOther() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  await loadCandidate(api, projectRef)
  // start save and history view in the same project (same generation)
  panel.saveCandidate()
  await flush()
  assert.equal(api.calls.update.length, 1)
  panel.viewHistory({ id: 'hist1' })
  await flush()
  assert.equal(api.calls.get.length, 2, 'history get fired while save pending')
  // resolve history first — must NOT be cancelled
  api.calls.get[1].resolve(okResponse(confirmedProfile('hist1')))
  await flush()
  assert.equal(panel.historyViewing.value && panel.historyViewing.value.id, 'hist1', 'history survives save in flight')
  // resolve save
  api.calls.update[0].resolve(
    okResponse({ changed: false, profile: { id: 'a', status: 'candidate', content: candidateProfile('a').content } })
  )
  await flush()
  assert.equal(panel.saving.value, false, 'save completed independently')
  assert.equal(panel.historyViewing.value.id, 'hist1', 'save did not cancel history')
}

async function testOldFetchErrorNotPolluteNewProject() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  api.calls.list[0].resolve(okResponse([{ id: 'a', status: 'confirmed', edit_version: 1 }]))
  await flush()
  assert.equal(api.calls.get.length, 1, 'A get fired')
  // switch to B before A's get resolves
  projectRef.value = 'B'
  await nextTick()
  await flush()
  api.calls.list[1].resolve(okResponse([{ id: 'b', status: 'confirmed', edit_version: 1 }]))
  await flush()
  api.calls.get[1].resolve(okResponse(confirmedProfile('b')))
  await flush()
  // A's get resolves as an error
  api.calls.get[0].resolve({ status: 500, ok: false, body: { detail: { message: 'boom' } } })
  await flush()
  assert.equal(panel.message.value, '', 'old fetch error must not pollute new project message')
  assert.equal(panel.serverProfile.value && panel.serverProfile.value.id, 'b')
}

async function testConflictWithContentAppliesDirectly() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  await loadCandidate(api, projectRef)
  const getCountBefore = api.calls.get.length
  // save returns 409 with current carrying content
  const currentWithContent = candidateProfile('a4', { edit_version: 4 })
  panel.saveCandidate()
  await flush()
  api.calls.update[0].resolve({
    status: 409,
    ok: false,
    body: { detail: { code: 'PROJECT_PROFILE_VERSION_CONFLICT', current: currentWithContent } }
  })
  await flush()
  assert.equal(panel.conflict.value, true)
  // reload should apply directly, zero new GET
  panel.reloadServerVersion()
  await flush()
  assert.equal(api.calls.get.length, getCountBefore, 'no GET issued when conflictCurrent has content')
  assert.equal(panel.serverProfile.value.id, 'a4', 'direct apply used conflictCurrent')
  assert.equal(panel.conflict.value, false)
  // a second reload must now GET (conflictCurrent was cleared after direct apply)
  panel.reloadServerVersion()
  await flush()
  assert.equal(api.calls.get.length, getCountBefore + 1, 'conflictCurrent was cleared, reload now GETs')
}

async function testConflictNoContentAllowsGet() {
  const projectRef = ref(null)
  const api = createApi()
  const panel = useProjectProfilePanel({ projectId: projectRef, api })
  projectRef.value = 'A'
  await nextTick()
  await flush()
  await loadCandidate(api, projectRef)
  const getCountBefore = api.calls.get.length
  panel.saveCandidate()
  await flush()
  // current without content (only id)
  api.calls.update[0].resolve({
    status: 409,
    ok: false,
    body: { detail: { code: 'PROJECT_PROFILE_VERSION_CONFLICT', current: { id: 'a-refresh', status: 'candidate' } } }
  })
  await flush()
  panel.reloadServerVersion()
  await flush()
  assert.equal(api.calls.get.length, getCountBefore + 1, 'GET issued because conflictCurrent has no content')
  api.calls.get[getCountBefore].resolve(okResponse(candidateProfile('fresh')))
  await flush()
  assert.equal(panel.serverProfile.value.id, 'fresh', 'GET result applied')
}

async function runAll(array) {
  for (const [name, fn] of array) {
    try {
      await fn()
      passed++
      console.log(`  PASS  ${name}`)
    } catch (err) {
      failed++
      console.error(`  FAIL  ${name}`)
      console.error(`        ${err && err.stack ? err.stack.split('\n').slice(0, 3).join(' | ') : err}`)
    }
  }
}

const scenarios = [
  ['A -> B: A-s late response does not override B', testProjectSwitchLateResponseDiscarded],
  ['A -> null: A-s late response does not restore state', testSwitchToNullLateDiscarded],
  ['A save while switching to B: A response does not modify B', testSaveSwitchDoesNotTouchB],
  ['A old finally does not clear B saving', testOldFinallyDoesNotClearNewSaving],
  ['history read and save do not cancel each other', testHistoryAndSaveDoNotCancelEachOther],
  ['old fetchProfile error does not pollute new project message', testOldFetchErrorNotPolluteNewProject],
  ['conflictCurrent with content applies directly (GET count 0)', testConflictWithContentAppliesDirectly],
  ['conflictCurrent without content allows GET', testConflictNoContentAllowsGet]
]

console.log('------')
await runAll(scenarios)
console.log('------')
console.log(`RESULT: ${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)