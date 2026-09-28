import { localWriteHeaders } from './localSession.js'

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getDeepSeekStatus() {
  const response = await fetch('/api/settings/deepseek-status')
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function configureDeepSeekCredential(apiKey) {
  const headers = await localWriteHeaders()
  const response = await fetch('/api/settings/deepseek-credential', {
    method: 'POST',
    headers,
    body: JSON.stringify({ api_key: apiKey })
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function testDeepSeekConnection() {
  const headers = await localWriteHeaders()
  const response = await fetch('/api/settings/deepseek-connection-test', {
    method: 'POST',
    headers,
    body: JSON.stringify({})
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function selectDeepSeekModel(modelId) {
  const headers = await localWriteHeaders()
  const response = await fetch('/api/settings/deepseek-model-selection', {
    method: 'POST',
    headers,
    body: JSON.stringify({ model_id: modelId })
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
