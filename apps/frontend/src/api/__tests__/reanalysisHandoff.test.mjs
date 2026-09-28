import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as task from '../taskExecution.js'
import { createReportReanalysis, prepareReportReanalysis } from '../reportReview.js'

const key = suffix => `rd-agent:project:1:${suffix}`
function setup(entries = {}, hash = '#/projects/1/task?reanalysis=3') {
  const storage = new Map(Object.entries(entries))
  globalThis.sessionStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) }
  globalThis.window = { location: { hash, href: `http://127.0.0.1:8000/${hash}` } }
  return storage
}
const valid = {
  [key('report-reanalysis-source-report-version-id')]: '3',
  [key('report-reanalysis-task-id')]: 'replacement-3',
  [key('evidence-snapshot-id')]: '11',
  'anxinboard:local-browser-session:v1': 'mock-session'
}

test('missing, partial, malformed and mismatched handoffs deny all endpoints without fallback', async () => {
  for (const entries of [{}, { ...valid, [key('report-reanalysis-task-id')]: '' }, { ...valid, [key('report-reanalysis-source-report-version-id')]: 'NaN' }, valid]) {
    setup(entries)
    globalThis.fetch = () => { throw Error('network must remain unused') }
    const id = entries === valid ? 'wrong-task' : 'replacement-3'
    for (const result of [await task.getReportGenerationTask(1, id), await task.prepareReportGenerationModelCall(1, id, {}), await task.previewReportSendAuthorization(1, id, 9), await task.authorizeReportSend(1, id, {}), await task.executeAuthorizedReport(1, id, {}), await task.createReportGenerationTask(1, { local_task_id: id })]) {
      assert.equal(result.ok, false)
      assert.equal(result.body.detail.code, 'REANALYSIS_TASK_BINDING_INVALID')
    }
  }
})

test('handoff storage failure leaves review visible and returns an actionable failure', async () => {
  setup({}, '#/projects/1/review')
  sessionStorage.getItem = () => 'mock-session'
  sessionStorage.setItem = () => { throw Error('unavailable') }
  globalThis.fetch = async () => new Response(JSON.stringify({ replacement_task: { local_task_id: 'replacement-3', evidence_snapshot_id: 11, task_type: 'daily_report_regenerate' } }), { status: 201 })
  const result = await createReportReanalysis(1, 3, {})
  assert.equal(result.ok, false)
  assert.equal(window.location.hash, '#/projects/1/review')
})

test('local send-scope preparation times out visibly without retrying or calling execute', { concurrency: false }, async () => {
  setup(valid, '#/projects/1/review')
  const originalSetTimeout = globalThis.setTimeout
  const originalClearTimeout = globalThis.clearTimeout
  const originalFetch = globalThis.fetch
  let requestCount = 0
  globalThis.setTimeout = callback => { queueMicrotask(callback); return 1 }
  globalThis.clearTimeout = () => {}
  globalThis.fetch = async (_url, options = {}) => {
    requestCount += 1
    const error = Object.assign(new Error('aborted'), { name: 'AbortError' })
    if (options.signal?.aborted) throw error
    return await new Promise((_, reject) => options.signal?.addEventListener('abort', () => reject(error), { once: true }))
  }
  try {
    const result = await prepareReportReanalysis(1, 3)
    assert.equal(result.ok, false)
    assert.equal(result.status, 408)
    assert.equal(result.body.detail.code, 'MODEL_SEND_PREPARATION_TIMEOUT')
    assert.match(result.body.detail.message, /未发起模型调用/)
    assert.equal(requestCount, 1)
  } finally {
    globalThis.setTimeout = originalSetTimeout
    globalThis.clearTimeout = originalClearTimeout
    globalThis.fetch = originalFetch
  }
})

test('successful HTTP reanalysis execution never fabricates task success', async () => {
  setup(valid)
  globalThis.fetch = async () => new Response(JSON.stringify({ provider_send_state: 'unknown' }))
  const result = await task.executeAuthorizedReport(1, 'replacement-3', {})
  assert.equal(result.body.task_state, undefined)
})

test('normal initial generation retains its original endpoint', async () => {
  setup({}, '#/projects/1/task')
  let endpoint
  globalThis.fetch = async url => { endpoint = url; return new Response('{}') }
  await task.createReportGenerationTask(1, { local_task_id: 'normal' })
  assert.equal(endpoint, '/api/projects/1/report-generation-tasks')
})

test('stale completed reanalysis marker does not block a new normal generation task', async () => {
  const storage = setup(valid, '#/projects/1/task')
  let endpoint
  globalThis.fetch = async url => { endpoint = url; return new Response('{}', { status: 201 }) }
  const result = await task.createReportGenerationTask(1, { local_task_id: 'normal-next', evidence_snapshot_id: 12 })
  assert.equal(result.status, 201)
  assert.equal(endpoint, '/api/projects/1/report-generation-tasks')
  assert.equal(storage.get(key('report-reanalysis-source-report-version-id')), '')
  assert.equal(storage.get(key('report-reanalysis-task-id')), '')
})

test('running single task finalizes from an existing durable result without execute endpoint', async () => {
  setup({ 'anxinboard:local-browser-session:v1': 'mock-session' }, '#/projects/1/task')
  const urls = []
  globalThis.fetch = async (url, options = {}) => {
    urls.push([url, options.method || 'GET'])
    if (urls.length === 1) return new Response(JSON.stringify({ local_task_id: 'normal', task_type: 'daily_report_generate', state: 'running' }))
    if (String(url).includes('/report-generation-recovery/finalize-existing/')) return new Response(JSON.stringify({ task_state: 'succeeded' }))
    return new Response(JSON.stringify({ local_task_id: 'normal', task_type: 'daily_report_generate', state: 'succeeded' }))
  }
  const result = await task.getReportGenerationTask(1, 'normal')
  assert.equal(result.ok, true)
  assert.equal(result.body.state, 'succeeded')
  assert.ok(urls.some(([url, method]) => method === 'POST' && String(url).includes('/report-generation-recovery/finalize-existing/')))
  assert.ok(!urls.some(([url]) => String(url).includes('/execute-authorized')))
})

test('queued durable reread releases the single-task execution attempt latch', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../../views/TaskExecutionView.vue', import.meta.url), 'utf8')
  assert.match(source, /result\.body\.batch_plan \|\| result\.body\.state === 'queued'/)
})
