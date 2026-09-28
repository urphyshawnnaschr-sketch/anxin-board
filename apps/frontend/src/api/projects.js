// 项目接口客户端：只负责四个现有接口的请求与响应解析，不持有页面状态。

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

function buildRequestError(response, body, fallbackMessage) {
  const detail = body && body.detail && typeof body.detail === 'object' ? body.detail : {}
  const error = new Error(detail.message || fallbackMessage)
  error.status = response.status
  error.code = detail.code
  error.current = detail.current
  return error
}

export async function listProjects() {
  const response = await fetch('/api/projects')
  if (!response.ok) {
    const body = await parseJSON(response)
    throw buildRequestError(response, body, '加载失败')
  }
  return response.json()
}

export async function getProject(id) {
  const response = await fetch(`/api/projects/${id}`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function createProject(payload) {
  const response = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  if (!response.ok) {
    const body = await parseJSON(response)
    throw buildRequestError(response, body, '创建失败')
  }
  return response.json()
}

export async function updateProject(id, payload) {
  const response = await fetch(`/api/projects/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
