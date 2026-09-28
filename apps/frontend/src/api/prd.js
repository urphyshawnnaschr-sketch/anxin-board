// PRD 接口客户端：负责上传、版本、完整审阅、确认与历史请求，不持有页面状态。

import { localReadHeaders } from './localSession.js'

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function uploadPrd(projectId, file) {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(`/api/projects/${projectId}/prd-versions`, {
    method: 'POST',
    body: form
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function getPrdVersion(id) {
  const response = await fetch(`/api/prd-versions/${id}`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function getPrdReviewChunk(id, { offset = 0, limit = 50000 } = {}) {
  const headers = await localReadHeaders()
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  const response = await fetch(`/api/prd-versions/${id}/review?${params}`, {
    method: 'GET',
    headers,
    cache: 'no-store'
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function confirmPrd(id, confirmedBy) {
  const response = await fetch(`/api/prd-versions/${id}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmed_by: confirmedBy || 'local' })
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function listPrdVersions(projectId) {
  const response = await fetch(`/api/projects/${projectId}/prd-versions`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
