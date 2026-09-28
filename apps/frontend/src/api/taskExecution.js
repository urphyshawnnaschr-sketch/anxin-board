import { localWriteHeaders } from './localSession.js'

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

async function postJSON(url, payload, { guarded = false, idempotent = false } = {}) {
  const headers = guarded
    ? await localWriteHeaders({ requireIdempotency: idempotent })
    : { 'Content-Type': 'application/json' }
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

function storageKey(projectId, suffix) {
  return `rd-agent:project:${projectId}:${suffix}`
}

function readStorage(key) {
  try {
    return sessionStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

export function activeReanalysis(projectId, localTaskId) {
  const sourceReportVersionId = readStorage(storageKey(projectId, 'report-reanalysis-source-report-version-id'))
  const replacementLocalTaskId = readStorage(storageKey(projectId, 'report-reanalysis-task-id'))
  const evidenceSnapshotId = readStorage(storageKey(projectId, 'evidence-snapshot-id'))
  const query = new URLSearchParams((globalThis.window?.location?.hash || '').split('?')[1] || '')
  const marked = query.has('reanalysis')
  if (!marked && !sourceReportVersionId && !replacementLocalTaskId) return null
  if (!/^[1-9]\d*$/.test(sourceReportVersionId) || !replacementLocalTaskId ||
      !/^[1-9]\d*$/.test(evidenceSnapshotId) || String(localTaskId || '') !== replacementLocalTaskId ||
      (marked && query.get('reanalysis') !== sourceReportVersionId)) {
    return { invalid: true }
  }
  return { sourceReportVersionId, replacementLocalTaskId, evidenceSnapshotId }
}

function reanalysisRouteMarked() {
  const query = new URLSearchParams((globalThis.window?.location?.hash || '').split('?')[1] || '')
  return query.has('reanalysis')
}

function clearStaleReanalysisHandoff(projectId) {
  try {
    sessionStorage.setItem(storageKey(projectId, 'report-reanalysis-source-report-version-id'), '')
    sessionStorage.setItem(storageKey(projectId, 'report-reanalysis-task-id'), '')
  } catch {}
}


function invalidBinding() {
  return { status: 409, ok: false, body: { detail: {
    code: 'REANALYSIS_TASK_BINDING_INVALID',
    message: '退回重分析身份缺失或不一致，已停止。请返回报告审阅核对原任务。'
  } } }
}

function reanalysisUrl(projectId, sourceReportVersionId, suffix = '') {
  return `/api/projects/${projectId}/reports/${encodeURIComponent(sourceReportVersionId)}/reanalysis${suffix}`
}

export async function createReportGenerationTask(projectId, payload) {
  const handoff = activeReanalysis(projectId, payload?.local_task_id)
  if (handoff) {
    if (!handoff.invalid || reanalysisRouteMarked()) return invalidBinding()
    // On the normal task route, a mismatched historical replacement-task marker is stale.
    // Backend checkpoint occupancy remains the authority and will still block a live chain.
    clearStaleReanalysisHandoff(projectId)
  }
  return postJSON(`/api/projects/${projectId}/report-generation-tasks`, payload)
}

export async function voidUnknownReportGenerationTask(projectId, localTaskId) {
  const encodedTaskId = encodeURIComponent(localTaskId)
  return postJSON(
    `/api/projects/${projectId}/report-generation-recovery/void-unknown/${encodedTaskId}`,
    { human_confirmed: true },
    { guarded: true }
  )
}

export async function finalizeExistingReportGeneration(projectId, localTaskId) {
  const encodedTaskId = encodeURIComponent(localTaskId)
  return postJSON(
    `/api/projects/${projectId}/report-generation-recovery/finalize-existing/${encodedTaskId}`,
    {},
    { guarded: true }
  )
}


export async function getReportGenerationTask(projectId, localTaskId) {
  const reanalysis = activeReanalysis(projectId, localTaskId)
  if (reanalysis?.invalid) return invalidBinding()
  if (reanalysis) {
    const response = await fetch(reanalysisUrl(projectId, reanalysis.sourceReportVersionId))
    const body = await parseJSON(response)
    const replacementTask = body?.replacement_task
    if (
      response.ok &&
      replacementTask?.local_task_id === reanalysis.replacementLocalTaskId &&
      replacementTask?.task_type === 'daily_report_regenerate' &&
      String(replacementTask.evidence_snapshot_id) === reanalysis.evidenceSnapshotId &&
      String(body?.source_report_version?.report_version_id) === reanalysis.sourceReportVersionId &&
      String(body?.source_report_version?.evidence_snapshot_id) === reanalysis.evidenceSnapshotId &&
      body?.reanalysis_request?.replacement_local_task_id === reanalysis.replacementLocalTaskId
    ) {
      return { status: response.status, ok: true, body: {
        ...replacementTask,
        reanalysis_cancelled: body.cancelled === true &&
          body.cancellation?.state === 'cancelled_before_send' && replacementTask.state === 'voided'
      } }
    }
    return {
      status: response.status,
      ok: false,
      body: response.ok
        ? { detail: { code: 'REANALYSIS_TASK_BINDING_INVALID', message: '退回重分析任务与当前页面保存的 replacement task 身份不一致。' } }
        : body
    }
  }
  const encodedTaskId = encodeURIComponent(localTaskId)
  const url = `/api/projects/${projectId}/report-generation-tasks/${encodedTaskId}`
  const response = await fetch(url)
  const body = await parseJSON(response)
  if (body?.task_type === 'daily_report_regenerate') return invalidBinding()
  if (response.ok && body?.task_type === 'daily_report_generate' && body?.state === 'running' && !body?.batch_plan) {
    const recovered = await finalizeExistingReportGeneration(projectId, localTaskId)
    if (recovered.ok && recovered.body?.task_state === 'succeeded') {
      const reread = await fetch(url)
      return { status: reread.status, ok: reread.ok, body: await parseJSON(reread) }
    }
    if (recovered.body?.detail?.code !== 'REPORT_GENERATION_EXECUTION_RESULT_UNAVAILABLE') return recovered
  }
  return { status: response.status, ok: response.ok, body }
}

export async function prepareReportGenerationModelCall(projectId, localTaskId, payload) {
  const reanalysis = activeReanalysis(projectId, localTaskId)
  if (reanalysis?.invalid) return invalidBinding()
  if (reanalysis) {
    return postJSON(
      reanalysisUrl(projectId, reanalysis.sourceReportVersionId, '/prepare'),
      payload,
      { guarded: true }
    )
  }
  const encodedTaskId = encodeURIComponent(localTaskId)
  return postJSON(
    `/api/projects/${projectId}/report-generation-tasks/${encodedTaskId}/prepare-model-call`,
    payload
  )
}

export async function previewReportSendAuthorization(projectId, localTaskId, modelCallId) {
  const reanalysis = activeReanalysis(projectId, localTaskId)
  if (reanalysis?.invalid) return invalidBinding()
  if (reanalysis) {
    return postJSON(
      reanalysisUrl(projectId, reanalysis.sourceReportVersionId, '/send-authorization-preview'),
      { model_call_id: modelCallId },
      { guarded: true }
    )
  }
  const encodedTaskId = encodeURIComponent(localTaskId)
  return postJSON(
    `/api/projects/${projectId}/report-generation-tasks/${encodedTaskId}/send-authorization-preview`,
    { model_call_id: modelCallId },
    { guarded: true }
  )
}

export async function authorizeReportSend(projectId, localTaskId, payload) {
  const reanalysis = activeReanalysis(projectId, localTaskId)
  if (reanalysis?.invalid) return invalidBinding()
  if (reanalysis) {
    return postJSON(
      reanalysisUrl(projectId, reanalysis.sourceReportVersionId, '/authorize-send'),
      payload,
      { guarded: true, idempotent: true }
    )
  }
  const encodedTaskId = encodeURIComponent(localTaskId)
  return postJSON(
    `/api/projects/${projectId}/report-generation-tasks/${encodedTaskId}/authorize-send`,
    payload,
    { guarded: true, idempotent: true }
  )
}

export async function executeAuthorizedReport(projectId, localTaskId, payload) {
  const reanalysis = activeReanalysis(projectId, localTaskId)
  if (reanalysis?.invalid) return invalidBinding()
  if (reanalysis) {
    const result = await postJSON(
      reanalysisUrl(projectId, reanalysis.sourceReportVersionId, '/execute'),
      payload,
      { guarded: true, idempotent: true }
    )
    return result
  }
  const encodedTaskId = encodeURIComponent(localTaskId)
  return postJSON(
    `/api/projects/${projectId}/report-generation-tasks/${encodedTaskId}/execute-authorized`,
    payload,
    { guarded: true, idempotent: true }
  )
}

export async function getActiveReportGenerationTask(projectId, evidenceSnapshotId = null) {
  const query = evidenceSnapshotId ? `?evidence_snapshot_id=${encodeURIComponent(evidenceSnapshotId)}` : ''
  const response = await fetch(`/api/projects/${projectId}/report-generation-tasks/active${query}`)
  return { status: response.status, ok: response.ok, body: await parseJSON(response) }
}
