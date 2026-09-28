// 本次分析范围确认接口客户端：只解析状态与结构化错误，不持有页面状态。

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function confirmGitSnapshot(projectId, payload) {
  const response = await fetch(`/api/projects/${projectId}/git-snapshots`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
