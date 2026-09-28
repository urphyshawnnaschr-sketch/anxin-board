import { localWriteHeaders } from './localSession.js'

async function parseJSON(response) {
  try { return await response.json() } catch { return {} }
}

export async function sendApprovedReportMail(projectId, payload) {
  const headers = await localWriteHeaders({ requireIdempotency: true })
  const response = await fetch(`/api/projects/${projectId}/mail-send`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
