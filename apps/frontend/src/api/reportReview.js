import { localWriteHeaders } from './localSession.js'

const MODEL_SEND_PREPARATION_TIMEOUT_MS = 45_000
let modelSendPreparation = null

function supersededPreparationResult() {
  return {
    status: 409,
    ok: false,
    body: {
      detail: {
        code: 'MODEL_SEND_PREPARATION_SUPERSEDED',
        message: '当前报告审阅身份已经变化，请基于最新页面重新准备发送范围。'
      }
    }
  }
}

function preparationTimeoutResult() {
  return {
    status: 408,
    ok: false,
    body: {
      detail: {
        code: 'MODEL_SEND_PREPARATION_TIMEOUT',
        message: '本地发送范围检查超时，可重试；本次未发起模型调用。'
      }
    }
  }
}

function finishModelSendPreparation(preparation) {
  if (!preparation || modelSendPreparation !== preparation) return
  clearTimeout(preparation.timer)
  modelSendPreparation = null
}

function invalidateModelSendPreparation() {
  if (!modelSendPreparation) return
  const preparation = modelSendPreparation
  modelSendPreparation = null
  clearTimeout(preparation.timer)
  preparation.controller.abort()
}

function beginModelSendPreparation(projectId, reportVersionId) {
  invalidateModelSendPreparation()
  const controller = new AbortController()
  const preparation = {
    projectId: String(projectId),
    reportVersionId: String(reportVersionId),
    controller,
    timedOut: false,
    timer: null
  }
  preparation.timer = setTimeout(() => {
    if (modelSendPreparation !== preparation || controller.signal.aborted) return
    preparation.timedOut = true
    controller.abort()
  }, MODEL_SEND_PREPARATION_TIMEOUT_MS)
  modelSendPreparation = preparation
  return preparation
}

function currentModelSendPreparation(projectId, reportVersionId) {
  if (
    !modelSendPreparation ||
    modelSendPreparation.projectId !== String(projectId) ||
    modelSendPreparation.reportVersionId !== String(reportVersionId) ||
    modelSendPreparation.controller.signal.aborted
  ) return null
  return modelSendPreparation
}

function storageKey(projectId, suffix) {
  return `rd-agent:project:${projectId}:${suffix}`
}

function persistReanalysisHandoff(projectId, reportVersionId, replacementTask) {
  const localTaskId = replacementTask?.local_task_id
  const evidenceSnapshotId = replacementTask?.evidence_snapshot_id
  if (!localTaskId || replacementTask?.task_type !== 'daily_report_regenerate' || !/^[1-9]\d*$/.test(String(reportVersionId)) || !/^[1-9]\d*$/.test(String(evidenceSnapshotId))) return false
  try {
    sessionStorage.setItem(storageKey(projectId, 'report-reanalysis-source-report-version-id'), String(reportVersionId))
    sessionStorage.setItem(storageKey(projectId, 'report-reanalysis-task-id'), String(localTaskId))
    sessionStorage.setItem(storageKey(projectId, 'report-generation-task-id'), String(localTaskId))
    sessionStorage.setItem(storageKey(projectId, 'evidence-snapshot-id'), String(evidenceSnapshotId))
  } catch {
    return false
  }
  return true
}

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, options)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

async function guardedPost(url, payload, { idempotent = false, signal } = {}) {
  return requestJSON(url, {
    method: 'POST',
    headers: await localWriteHeaders({ requireIdempotency: idempotent }),
    body: JSON.stringify(payload),
    ...(signal ? { signal } : {})
  })
}

async function guardedPreparationPost(url, payload, preparation, { finalize = false } = {}) {
  try {
    const result = await guardedPost(url, payload, { signal: preparation.controller.signal })
    if (!result.ok || finalize) finishModelSendPreparation(preparation)
    return result
  } catch (error) {
    const timedOut = preparation.timedOut
    finishModelSendPreparation(preparation)
    if (error?.name === 'AbortError') return timedOut ? preparationTimeoutResult() : supersededPreparationResult()
    throw error
  }
}

export function getCurrentReportReview(projectId) {
  invalidateModelSendPreparation()
  return requestJSON(`/api/projects/${projectId}/report-review/current`)
}

export function getReportReview(projectId, reportVersionId) {
  return requestJSON(`/api/projects/${projectId}/reports/${reportVersionId}/review`)
}

export async function createReportSupplement(projectId, reportVersionId, payload) {
  return guardedPost(`/api/projects/${projectId}/reports/${reportVersionId}/supplements`, payload)
}

export async function createReportValidation(projectId, reportVersionId, payload) {
  return guardedPost(`/api/projects/${projectId}/reports/${reportVersionId}/validations`, payload)
}

export function getReportApproval(projectId, reportVersionId) {
  return requestJSON(`/api/projects/${projectId}/reports/${reportVersionId}/approval`)
}

export async function createReportApproval(projectId, reportVersionId, payload) {
  return guardedPost(`/api/projects/${projectId}/reports/${reportVersionId}/approval`, payload, { idempotent: true })
}

export function getReportReanalysis(projectId, reportVersionId) {
  return requestJSON(`/api/projects/${projectId}/reports/${reportVersionId}/reanalysis`)
}

export async function cancelReportReanalysis(projectId, reportVersionId, payload) {
  invalidateModelSendPreparation()
  return guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis/cancel`,
    payload,
    { idempotent: true }
  )
}

export async function createReportReanalysis(projectId, reportVersionId, payload) {
  const originHash = globalThis.window?.location?.hash
  const result = await guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis`,
    payload,
    { idempotent: true }
  )
  if (result.ok && result.body?.replacement_task?.local_task_id) {
    if (!persistReanalysisHandoff(projectId, reportVersionId, result.body.replacement_task)) {
      return { ...result, ok: false, body: { detail: { code: 'REANALYSIS_HANDOFF_UNAVAILABLE', message: '替换任务已创建，但无法保存交接身份。已停止跳转，请保留当前报告并核对原任务，不要重复创建。' } } }
    }
    if (typeof window !== 'undefined' && window.location.hash === originHash) window.location.hash = `#/projects/${projectId}/task?reanalysis=${reportVersionId}`
  }
  return result
}

export async function prepareReportReanalysis(projectId, reportVersionId) {
  const preparation = beginModelSendPreparation(projectId, reportVersionId)
  return guardedPreparationPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis/prepare`,
    { preparation_authorized: true },
    preparation
  )
}

export async function previewReportReanalysisSend(projectId, reportVersionId, modelCallId) {
  const preparation = currentModelSendPreparation(projectId, reportVersionId)
  if (!preparation) return supersededPreparationResult()
  return guardedPreparationPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis/send-authorization-preview`,
    { model_call_id: modelCallId },
    preparation,
    { finalize: true }
  )
}

export async function authorizeReportReanalysisSend(projectId, reportVersionId, payload) {
  return guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis/authorize-send`,
    payload,
    { idempotent: true }
  )
}

export async function executeReportReanalysis(projectId, reportVersionId, payload) {
  invalidateModelSendPreparation()
  return guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis/execute`,
    payload,
    { idempotent: true }
  )
}

export async function finalizeExistingReportReanalysis(projectId, reportVersionId, modelCallId) {
  invalidateModelSendPreparation()
  return guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/reanalysis/finalize-existing`,
    { model_call_id: modelCallId },
    { idempotent: true }
  )
}

export async function prepareReportContradiction(projectId, reportVersionId) {
  const preparation = beginModelSendPreparation(projectId, reportVersionId)
  return guardedPreparationPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/contradiction/prepare`,
    { preparation_authorized: true },
    preparation
  )
}

export async function previewReportContradictionSend(projectId, reportVersionId, modelCallId) {
  const preparation = currentModelSendPreparation(projectId, reportVersionId)
  if (!preparation) return supersededPreparationResult()
  return guardedPreparationPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/contradiction/send-authorization-preview`,
    { model_call_id: modelCallId },
    preparation,
    { finalize: true }
  )
}

export async function authorizeReportContradictionSend(projectId, reportVersionId, payload) {
  return guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/contradiction/authorize-send`,
    payload,
    { idempotent: true }
  )
}

export async function executeReportContradiction(projectId, reportVersionId, payload) {
  invalidateModelSendPreparation()
  return guardedPost(
    `/api/projects/${projectId}/reports/${reportVersionId}/contradiction/execute`,
    payload,
    { idempotent: true }
  )
}
