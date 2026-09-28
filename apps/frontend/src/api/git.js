// Git 连接接口客户端：只解析状态与结构化错误，不持有页面状态。

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getGitStatus(projectId) {
  const response = await fetch(`/api/projects/${projectId}/git/status`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function checkGitConnection(projectId) {
  const response = await fetch(`/api/projects/${projectId}/git/check`, { method: 'POST' })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
