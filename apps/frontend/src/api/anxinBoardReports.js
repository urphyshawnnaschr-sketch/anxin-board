import { localReadHeaders, handleLocalSessionFailure } from './localSession.js'
async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getLatestAnxinBoardReport(projectId) {
  const response = await fetch(`/api/projects/${projectId}/anxin-board/latest`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function getAnxinBoardReportHistory(
  projectId,
  { beforeId = null, limit = 10 } = {}
) {
  const params = new URLSearchParams()
  if (beforeId != null) params.set('before_id', String(beforeId))
  params.set('limit', String(limit))

  const response = await fetch(
    `/api/projects/${projectId}/anxin-board/history?${params.toString()}`
  )
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function getApprovedReportNarrative(projectId, reportVersionId, reportHash) {
  try {
    const headers = await localReadHeaders()
    const params = new URLSearchParams({ report_hash: reportHash })
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/anxin-board/reports/${encodeURIComponent(reportVersionId)}/narrative?${params}`, {
      method: 'GET', headers, cache: 'no-store', redirect: 'error'
    })
    const body = await parseJSON(response)
    handleLocalSessionFailure(response.status, body, headers)
    return { status: response.status, ok: response.ok, body }
  } catch {
    return { status: 0, ok: false, body: {} }
  }
}

export async function getApprovedModuleNarrative(projectId, reportVersionId, reportHash) {
  try {
    const headers = await localReadHeaders()
    const params = new URLSearchParams({ report_hash: reportHash })
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/anxin-board/reports/${encodeURIComponent(reportVersionId)}/module-narrative?${params}`, {
      method: 'GET', headers, cache: 'no-store', redirect: 'error'
    })
    const body = await parseJSON(response)
    handleLocalSessionFailure(response.status, body, headers)
    return { status: response.status, ok: response.ok, body }
  } catch {
    return { status: 0, ok: false, body: {} }
  }
}
