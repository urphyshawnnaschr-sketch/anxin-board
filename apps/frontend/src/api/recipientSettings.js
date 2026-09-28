import { localWriteHeaders } from './localSession.js'

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getRecipientConfig(projectId) {
  const response = await fetch(`/api/projects/${projectId}/recipient-config`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function saveRecipientConfig(projectId, payload) {
  const headers = await localWriteHeaders({ requireIdempotency: true })
  const response = await fetch(`/api/projects/${projectId}/recipient-config`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
