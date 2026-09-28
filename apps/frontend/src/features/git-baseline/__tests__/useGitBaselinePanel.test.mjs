import assert from 'node:assert/strict'
import { ref, nextTick } from 'vue'
import { useGitBaselinePanel } from '../useGitBaselinePanel.js'
import { lineageStatusText, lineageStatusClass } from '../baselineStatusDisplay.js'

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

function okResponse(body) {
  return { status: 200, ok: true, body }
}

async function flush(times = 4) {
  for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0))
}

const FROM_COMMIT = 'a'.repeat(40)
const TO_COMMIT = 'b'.repeat(40)
const LINEAGE_ID = 17
const mockLineage = {
  lineage: {
    id: LINEAGE_ID,
    baseline_commit: FROM_COMMIT,
    branch: 'main',
    created_at: '2026-01-01T00:00:00Z'
  }
}
const mockCandidate = {
  baseline_commit: FROM_COMMIT,
  remote_head: TO_COMMIT,
  continuity: 'continuous',
  capacity: 'within_capacity'
}

function createApi() {
  const calls = { gitStatus: [], lineage: [], create: [], refresh: [], confirm: [] }
  const api = {
    calls,
    getGitStatus: (pid) => {
      const d = deferred()
      calls.gitStatus.push({ pid, deferred: d })
      return d.promise
    },
    getAnalysisLineage: (pid) => {
      const d = deferred()
      calls.lineage.push({ pid, deferred: d })
      return d.promise
    },
    createAnalysisLineage: (pid) => {
      const d = deferred()
      calls.create.push({ pid, deferred: d })
      return d.promise
    },
    refreshRangeCandidate: (pid) => {
      const d = deferred()
      calls.refresh.push({ pid, deferred: d })
      return d.promise
    },
    confirmGitSnapshot: (pid, payload) => {
      const d = deferred()
      calls.confirm.push({ pid, payload, deferred: d })
      return d.promise
    }
  }
  return api
}

function createPanel(api, savedUrl, savedBranch) {
  const projectRef = ref(null)
  const savedGitUrlRef = ref(savedUrl)
  const savedBranchRef = ref(savedBranch)
  const panel = useGitBaselinePanel({
    projectId: projectRef,
    savedGitUrl: savedGitUrlRef,
    savedBranch: savedBranchRef,
    api
  })
  return { panel, projectRef, savedGitUrlRef, savedBranchRef, api }
}

async function loadActiveLineage(api, gitIndex = 0, lineageIndex = 0) {
  api.calls.gitStatus[gitIndex].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[lineageIndex].deferred.resolve(okResponse({ status: 'active', lineage: mockLineage.lineage }))
  await flush()
}

async function startActive(panel) {
  panel.active()
  await flush()
}

report('composable initializes without throwing', () => {
  const projectRef = ref(null)
  const panel = useGitBaselinePanel({ projectId: projectRef, savedGitUrl: ref(''), savedBranch: ref(''), api: createApi() })
  assert.equal(panel.loading.value, false)
  assert.equal(panel.lineage.value, null)
  assert.equal(panel.gitConnected.value, false)
})

report('display: loading text is 正在读取', () => {
  assert.equal(lineageStatusText('loading'), '正在读取')
})

report('display: active text is 基线已建立', () => {
  assert.equal(lineageStatusText('active'), '基线已建立')
})

report('display: no_lineage text is 未建立基线', () => {
  assert.equal(lineageStatusText('no_lineage'), '未建立基线')
})

report('display: error text is 读取失败 and never 未建立基线', () => {
  assert.equal(lineageStatusText('error'), '读取失败')
  assert.notEqual(lineageStatusText('error'), '未建立基线')
})

report('display: status classes map to badge styles', () => {
  assert.equal(lineageStatusClass('loading'), 'lineage-loading')
  assert.equal(lineageStatusClass('active'), 'lineage-active')
  assert.equal(lineageStatusClass('no_lineage'), 'lineage-none')
  assert.equal(lineageStatusClass('error'), 'status-error')
})

async function testUrlChangeInvalidatesState() {
  const api = createApi()
  const { panel, projectRef, savedGitUrlRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  assert.equal(api.calls.gitStatus.length, 1, 'initial gitStatus fired')
  await loadActiveLineage(api)
  assert.ok(panel.lineage.value, 'lineage loaded')
  assert.equal(panel.gitConnected.value, true)
  // change savedGitUrl
  savedGitUrlRef.value = 'https://new.url/repo.git'
  await nextTick()
  await flush()
  // after watch fires, resetState + loadPanel
  assert.equal(panel.lineage.value, null, 'lineage cleared on url change')
  assert.equal(panel.gitConnected.value, false, 'gitConnected reset on url change')
  assert.equal(api.calls.gitStatus.length, 2, 'new gitStatus request after url change')
  // resolve the new git status to trigger lineage call
  api.calls.gitStatus[1].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  assert.equal(api.calls.lineage.length, 2, 'new lineage request after url change')
}

async function testBranchChangeInvalidatesState() {
  const api = createApi()
  const { panel, projectRef, savedBranchRef } = createPanel(api, '', 'main')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  assert.ok(panel.lineage.value, 'lineage loaded')
  // change branch
  savedBranchRef.value = 'develop'
  await nextTick()
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage cleared on branch change')
  assert.equal(panel.gitConnected.value, false, 'gitConnected reset on branch change')
  assert.equal(api.calls.gitStatus.length, 2, 'new gitStatus request after branch change')
}

async function testUrlChangeWithoutProjectDoesNotCallLoad() {
  const api = createApi()
  const { savedGitUrlRef, savedBranchRef } = createPanel(api, '', '')
  // no projectId set
  savedGitUrlRef.value = 'https://new.url/repo.git'
  await nextTick()
  await flush()
  assert.equal(api.calls.gitStatus.length, 0, 'no requests fired without projectId')
  assert.equal(api.calls.lineage.length, 0, 'no lineage requests fired without projectId')
}

async function testSameUrlNoInvalidation() {
  const api = createApi()
  const savedUrl = ref('https://same.url/repo.git')
  const savedBranch = ref('main')
  const projectRef = ref(1)
  const panel = useGitBaselinePanel({ projectId: projectRef, savedGitUrl: savedUrl, savedBranch, api })
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  assert.ok(panel.lineage.value)
  // set same values — watch should not fire (no change)
  savedUrl.value = 'https://same.url/repo.git'
  await nextTick()
  await flush()
  assert.equal(api.calls.gitStatus.length, 1, 'no additional request for unchanged url')
}

async function testUrlChangeResetsLoadingBeforeNewRequest() {
  const api = createApi()
  const { panel, projectRef, savedGitUrlRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  assert.equal(panel.loading.value, false, 'initial load complete')
  savedGitUrlRef.value = 'https://changed.url/repo.git'
  await nextTick()
  await flush()
  assert.equal(panel.loading.value, true, 'loading true after url change triggers new load')
  await loadActiveLineage(api, 1, 1)
  assert.equal(panel.loading.value, false, 'loading cleared after new load completes')
}

const scenarios = [
  ['URL change invalidates state and reloads', testUrlChangeInvalidatesState],
  ['Branch change invalidates state and reloads', testBranchChangeInvalidatesState],
  ['URL change without projectId does not fetch', testUrlChangeWithoutProjectDoesNotCallLoad],
  ['Same URL does not trigger reload', testSameUrlNoInvalidation],
  ['URL change sets loading true during reload', testUrlChangeResetsLoadingBeforeNewRequest],
  ['lineageStatus is no_lineage when backend reports it explicitly', testLineageStatusNoLineage],
  ['lineageStatus is active when lineage is loaded', testLineageStatusActive],
  ['lineageStatus is loading during initial load', testLineageStatusLoading],
  ['lineageStatus is error after 404 response', testLineageStatusErrorAfter404],
  ['lineageStatus is error after 409 response', testLineageStatusErrorAfter409],
  ['lineageStatus is error after 500 response', testLineageStatusErrorAfter500],
  ['lineageStatus is error after network exception', testLineageStatusErrorAfterNetwork],
  ['lineageStatus is error after illegal 200 empty object', testLineageStatusErrorAfterEmpty200],
  ['lineageStatus is error after illegal 200 null lineage', testLineageStatusErrorAfterNullLineage200],
  ['lineageStatus is error after illegal 200 unknown status', testLineageStatusErrorAfterIllegal200],
  ['establishBaseline sets lineage and shows success', testEstablishBaselineSuccess],
  ['establishBaseline shows info when already exists', testEstablishBaselineAlreadyExists],
  ['establishBaseline shows error on 409', testEstablishBaseline409],
  ['refreshRange with no_new_commit shows info', testRefreshRangeNoNewCommit],
  ['refreshRange with continuous shows success', testRefreshRangeContinuous],
  ['refreshRange with capacity_exceeded shows warning', testRefreshRangeCapacityExceeded],
  ['refreshRange with branch_changed shows error', testRefreshRangeBranchChanged],
  ['refreshRange with checkpoint_unreachable shows error', testRefreshRangeUnreachable],
  ['refreshRange with no_lineage shows info', testRefreshRangeNoLineageStatus],
  ['error state disables establish button', testErrorStateDisablesEstablishButton],
  ['error state disables refresh button', testErrorStateDisablesRefreshButton],
  ['loading state disables both buttons', testLoadingDisablesBothButtons],
  ['git not connected disables both buttons', testGitNotConnectedDisablesButtons],
  ['config inconsistent disables both buttons', testConfigInconsistentDisablesButtons],
  ['no_lineage state enables establish button', testNoLineageEnablesEstablishButton],
  ['active state enables refresh button', testActiveEnablesRefreshButton],
  ['retry after error recovers via reload', testRetryClearsLineageError],
  ['git-connected reload clears error and recovers', testGitConnectedReloadRecovers],
  ['savedGitUrl change clears old error and re-reads', testUrlChangeClearsLineageError],
  ['savedBranch change clears old error and re-reads', testBranchChangeClearsLineageError],
  ['late stale response does not override new project state', testLateOldResponseDoesNotOverride],
  ['confirm sends the displayed lineage/from/to and shows success', testConfirmRangeSuccess],
  ['stale confirmation asks for refresh without retrying', testConfirmRangeStale],
  ['idempotent confirmation is still successful', testConfirmRangeAlreadyFrozen]
]

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

console.log('------')
await runAll(scenarios)
console.log('------')
console.log(`RESULT: ${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)

async function testLineageStatusNoLineage() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  assert.equal(panel.lineageStatus.value, 'loading', 'initial status is loading')
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage is null')
  assert.equal(panel.noLineage.value, true, 'noLineage flag set')
  assert.equal(panel.lineageStatus.value, 'no_lineage', 'lineageStatus is no_lineage')
}

async function testLineageStatusActive() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'active', lineage: mockLineage.lineage }))
  await flush()
  assert.ok(panel.lineage.value, 'lineage loaded')
  assert.equal(panel.lineageStatus.value, 'active', 'lineageStatus is active')
}

async function testLineageStatusLoading() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  assert.equal(panel.loading.value, true, 'loading is true')
  assert.equal(panel.lineageStatus.value, 'loading', 'lineageStatus is loading')
}

async function testLineageStatusErrorAfter404() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 404, ok: false })
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after 404')
  assert.equal(panel.lineageError.value, true, 'lineageError true after 404')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error after 404')
}

async function testLineageStatusErrorAfter409() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 409, ok: false, body: { detail: { message: 'conflict' } } })
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after 409')
  assert.equal(panel.lineageError.value, true, 'lineageError true after 409')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error after 409')
}

async function testLineageStatusErrorAfter500() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 500, ok: false })
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after 500')
  assert.equal(panel.lineageError.value, true, 'lineageError true after 500')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error after 500')
}

async function testLineageStatusErrorAfterNetwork() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.reject(new Error('network down'))
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after network error')
  assert.equal(panel.lineageError.value, true, 'lineageError true after network error')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error after network error')
}

async function testLineageStatusErrorAfterEmpty200() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({}))
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after empty 200')
  assert.equal(panel.lineageError.value, true, 'lineageError true after empty 200')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error after empty 200')
}

async function testLineageStatusErrorAfterNullLineage200() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ lineage: null }))
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after null-lineage 200')
  assert.equal(panel.lineageError.value, true, 'lineageError true after null-lineage 200')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error after null-lineage 200 (not no_lineage)')
}

async function testLineageStatusErrorAfterIllegal200() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'unknown' }))
  await flush()
  assert.equal(panel.lineage.value, null, 'lineage null after illegal 200')
  assert.equal(panel.lineageError.value, true, 'lineageError true after illegal 200')
  assert.equal(panel.lineageStatus.value, 'error', 'lineageStatus error for illegal 200 body')
}

async function testEstablishBaselineSuccess() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  assert.equal(panel.creating.value, false, 'not creating initially')
  panel.establishBaseline()
  await flush()
  assert.equal(panel.creating.value, true, 'creating is true')
  api.calls.create[0].deferred.resolve(okResponse({ lineage: mockLineage.lineage }))
  await flush()
  assert.equal(panel.creating.value, false, 'creating is false after resolve')
  assert.ok(panel.lineage.value, 'lineage set')
  assert.equal(panel.messageType.value, 'success', 'message type success')
}

async function testEstablishBaselineAlreadyExists() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.establishBaseline()
  await flush()
  api.calls.create[0].deferred.resolve(okResponse({ lineage: mockLineage.lineage, created: false }))
  await flush()
  assert.equal(panel.messageType.value, 'info', 'info message when already exists')
}

async function testEstablishBaseline409() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.establishBaseline()
  await flush()
  api.calls.create[0].deferred.resolve({ status: 409, ok: false, body: { detail: { message: 'not allowed' } } })
  await flush()
  assert.equal(panel.messageType.value, 'error', 'error on 409')
}

async function testRefreshRangeNoNewCommit() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  assert.equal(panel.refreshing.value, true, 'refreshing is true')
  api.calls.refresh[0].deferred.resolve(okResponse({ candidate: { continuity: 'no_new_commit' }, status: 'no_new_commit' }))
  await flush()
  assert.equal(panel.refreshing.value, false, 'refreshing resolved')
  assert.equal(panel.messageType.value, 'info', 'info for no_new_commit')
}

async function testRefreshRangeContinuous() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  api.calls.refresh[0].deferred.resolve(okResponse({ candidate: { continuity: 'continuous' }, status: 'continuous' }))
  await flush()
  assert.equal(panel.messageType.value, 'success', 'success for continuous')
}

async function testRefreshRangeCapacityExceeded() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  api.calls.refresh[0].deferred.resolve(okResponse({ candidate: { continuity: 'continuous', capacity: 'capacity_exceeded' }, status: 'capacity_exceeded' }))
  await flush()
  assert.equal(panel.messageType.value, 'warning', 'warning for capacity_exceeded')
}

async function testRefreshRangeBranchChanged() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  api.calls.refresh[0].deferred.resolve(okResponse({ candidate: { continuity: 'continuous' }, status: 'branch_changed' }))
  await flush()
  assert.equal(panel.messageType.value, 'error', 'error for branch_changed')
}

async function testRefreshRangeUnreachable() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  api.calls.refresh[0].deferred.resolve(okResponse({ candidate: { continuity: 'checkpoint_unreachable' }, status: 'checkpoint_unreachable' }))
  await flush()
  assert.equal(panel.messageType.value, 'error', 'error for checkpoint_unreachable')
}

async function testRefreshRangeNoLineageStatus() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  api.calls.refresh[0].deferred.resolve(okResponse({ candidate: { continuity: 'continuous' }, status: 'no_lineage' }))
  await flush()
  assert.equal(panel.messageType.value, 'info', 'info for no_lineage status')
}

async function testErrorStateDisablesEstablishButton() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 404, ok: false })
  await flush()
  assert.equal(panel.lineageStatus.value, 'error', 'error state for 404')
  assert.equal(panel.lineage.value, null, 'lineage null')
  assert.equal(panel.baselineDisabled.value, true, 'establish button disabled in error state')
}

async function testErrorStateDisablesRefreshButton() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 500, ok: false })
  await flush()
  assert.equal(panel.lineageStatus.value, 'error', 'error state for 500')
  assert.equal(panel.refreshDisabled.value, true, 'refresh button disabled in error state')
}

async function testLoadingDisablesBothButtons() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  assert.equal(panel.lineageStatus.value, 'loading', 'loading state')
  assert.equal(panel.baselineDisabled.value, true, 'establish disabled during loading')
  assert.equal(panel.refreshDisabled.value, true, 'refresh disabled during loading')
}

async function testGitNotConnectedDisablesButtons() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve({ status: 500, ok: false })
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.gitConnected.value, false, 'git not connected')
  assert.equal(panel.lineageStatus.value, 'no_lineage', 'lineage read still completes')
  assert.equal(panel.baselineDisabled.value, true, 'establish disabled when git not connected')
  assert.equal(panel.refreshDisabled.value, true, 'refresh disabled when git not connected')
}

async function testConfigInconsistentDisablesButtons() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'not_tested' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.configConsistent.value, false, 'config inconsistent')
  assert.equal(panel.baselineDisabled.value, true, 'establish disabled when config inconsistent')
  assert.equal(panel.refreshDisabled.value, true, 'refresh disabled when config inconsistent')
}

async function testNoLineageEnablesEstablishButton() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.lineageStatus.value, 'no_lineage', 'no_lineage state')
  assert.equal(panel.baselineDisabled.value, false, 'establish button enabled in no_lineage')
  assert.equal(panel.refreshDisabled.value, true, 'refresh disabled in no_lineage (not active)')
}

async function testActiveEnablesRefreshButton() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'active', lineage: mockLineage.lineage }))
  await flush()
  assert.equal(panel.lineageStatus.value, 'active', 'active state')
  assert.equal(panel.refreshDisabled.value, false, 'refresh button enabled in active')
  assert.equal(panel.baselineDisabled.value, false, 'establish button enabled in active')
}

async function testRetryClearsLineageError() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 404, ok: false })
  await flush()
  assert.equal(panel.lineageStatus.value, 'error', 'error after first fail')
  assert.equal(panel.lineageError.value, true, 'lineageError true after 404')

  // retry through reload() (the page reload entry)
  panel.reload()
  await flush()
  assert.equal(panel.lineageStatus.value, 'loading', 'reload re-enters loading')
  assert.equal(panel.lineageError.value, false, 'old error cleared at reload start')
  api.calls.gitStatus[1].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[1].deferred.resolve(okResponse({ status: 'active', lineage: mockLineage.lineage }))
  await flush()
  assert.equal(panel.lineageError.value, false, 'lineageError cleared after successful retry')
  assert.equal(panel.lineageStatus.value, 'active', 'lineageStatus active after retry')
}

async function testGitConnectedReloadRecovers() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 500, ok: false })
  await flush()
  assert.equal(panel.lineageStatus.value, 'error', 'error after 500')
  // simulate @git-connected -> reloadBaseline -> panel.reload()
  panel.reload()
  await flush()
  assert.equal(panel.lineageError.value, false, 'old error cleared on git-connected reload')
  assert.equal(panel.lineageStatus.value, 'loading', 're-reading after git-connected reload')
  api.calls.gitStatus[1].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[1].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.lineageError.value, false, 'error cleared after recovery')
  assert.equal(panel.lineageStatus.value, 'no_lineage', 'recovered to no_lineage after git-connected reload')
  assert.equal(panel.baselineDisabled.value, false, 'establish enabled after recovery')
}

async function testUrlChangeClearsLineageError() {
  const api = createApi()
  const { panel, projectRef, savedGitUrlRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 404, ok: false })
  await flush()
  assert.equal(panel.lineageError.value, true, 'lineageError true after 404')
  assert.equal(panel.lineageStatus.value, 'error', 'error after 404')
  // change URL - resetState should clear lineageError and re-read
  savedGitUrlRef.value = 'https://other.url/repo.git'
  await nextTick()
  await flush()
  assert.equal(panel.lineageError.value, false, 'lineageError false after url change')
  assert.equal(panel.lineage.value, null, 'lineage null')
  assert.equal(panel.lineageStatus.value, 'loading', 're-reading after url change')
  api.calls.gitStatus[1].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[1].deferred.resolve(okResponse({ status: 'active', lineage: mockLineage.lineage }))
  await flush()
  assert.equal(panel.lineageStatus.value, 'active', 'recovered after url change')
}

async function testBranchChangeClearsLineageError() {
  const api = createApi()
  const { panel, projectRef, savedBranchRef } = createPanel(api, '', 'main')
  projectRef.value = 1
  await nextTick()
  panel.active()
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[0].deferred.resolve({ status: 500, ok: false })
  await flush()
  assert.equal(panel.lineageError.value, true, 'lineageError true after 500')
  assert.equal(panel.lineageStatus.value, 'error', 'error after 500')
  savedBranchRef.value = 'develop'
  await nextTick()
  await flush()
  assert.equal(panel.lineageError.value, false, 'lineageError false after branch change')
  assert.equal(panel.lineageStatus.value, 'loading', 're-reading after branch change')
  api.calls.gitStatus[1].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  api.calls.lineage[1].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.lineageStatus.value, 'no_lineage', 'recovered after branch change')
}

async function testLateOldResponseDoesNotOverride() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  projectRef.value = 1
  await nextTick()
  panel.active() // generation A
  await flush()
  api.calls.gitStatus[0].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  assert.equal(api.calls.lineage.length, 1, 'project 1 lineage read started (in flight)')
  // switch project and start a fresh load (component projectId watch -> active)
  projectRef.value = 2
  await nextTick()
  panel.active() // generation B
  await flush()
  api.calls.gitStatus[1].deferred.resolve(okResponse({ status: 'connected' }))
  await flush()
  assert.equal(api.calls.lineage.length, 2, 'project 2 lineage read started')
  // late stale response for project 1 arrives first; must be dropped
  api.calls.lineage[0].deferred.resolve(okResponse({ status: 'active', lineage: mockLineage.lineage }))
  await flush()
  assert.equal(panel.lineage.value, null, 'stale response must not apply lineage')
  assert.equal(panel.lineageStatus.value, 'loading', 'stale response must not overwrite loading')
  // project 2 read completes
  api.calls.lineage[1].deferred.resolve(okResponse({ status: 'no_lineage' }))
  await flush()
  assert.equal(panel.lineage.value, null, 'no stale lineage applied')
  assert.equal(panel.noLineage.value, true, 'no_lineage flag reflects new project')
  assert.equal(panel.lineageStatus.value, 'no_lineage', 'new project state applied')
}

async function loadConfirmableRange(api, panel, projectRef) {
  projectRef.value = 1
  await nextTick()
  await startActive(panel)
  await loadActiveLineage(api)
  panel.refreshRange()
  await flush()
  api.calls.refresh[0].deferred.resolve(okResponse({
    candidate: { ...mockCandidate },
    status: 'continuous'
  }))
  await flush()
  assert.equal(panel.confirmDisabled.value, false, 'confirm enabled for continuous range')
}

async function testConfirmRangeSuccess() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  await loadConfirmableRange(api, panel, projectRef)

  const confirmation = panel.confirmRange()
  await flush()

  assert.equal(api.calls.confirm.length, 1, 'one confirmation request sent')
  assert.deepEqual(api.calls.confirm[0].payload, {
    expected_lineage_id: LINEAGE_ID,
    expected_from_commit: FROM_COMMIT,
    expected_to_commit: TO_COMMIT
  })
  api.calls.confirm[0].deferred.resolve(okResponse({ created: true, snapshot: {} }))
  await confirmation
  await flush()

  assert.equal(panel.message.value, '本次分析范围已确认。')
  assert.equal(panel.messageType.value, 'success')
  assert.deepEqual(panel.confirmedRange.value, {
    fromCommit: FROM_COMMIT,
    toCommit: TO_COMMIT
  })
}

async function testConfirmRangeStale() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  await loadConfirmableRange(api, panel, projectRef)

  const confirmation = panel.confirmRange()
  await flush()
  api.calls.confirm[0].deferred.resolve({
    status: 409,
    ok: false,
    body: { detail: { code: 'GIT_SNAPSHOT_TO_COMMIT_STALE', message: 'stale' } }
  })
  await confirmation
  await flush()

  assert.equal(panel.confirmedRange.value, null, 'stale range is not shown as confirmed')
  assert.equal(
    panel.message.value,
    '当前提交范围已经发生变化，请重新刷新提交范围后再确认。'
  )
  assert.equal(api.calls.confirm.length, 1, 'no automatic confirmation retry')
  assert.equal(api.calls.refresh.length, 1, 'no automatic range refresh')
}

async function testConfirmRangeAlreadyFrozen() {
  const api = createApi()
  const { panel, projectRef } = createPanel(api, '', '')
  await loadConfirmableRange(api, panel, projectRef)

  const confirmation = panel.confirmRange()
  await flush()
  api.calls.confirm[0].deferred.resolve(okResponse({ created: false, snapshot: {} }))
  await confirmation
  await flush()

  assert.equal(panel.message.value, '该分析范围已经确认。')
  assert.equal(panel.messageType.value, 'success')
  assert.deepEqual(panel.confirmedRange.value, {
    fromCommit: FROM_COMMIT,
    toCommit: TO_COMMIT
  })
}
