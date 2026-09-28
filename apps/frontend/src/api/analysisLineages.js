// Git 基线 / 分析链路接口客户端：只解析状态与结构化错误，不持有页面状态。

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getAnalysisLineage(projectId) {
  const response = await fetch(`/api/projects/${projectId}/analysis-lineage`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function createAnalysisLineage(projectId) {
  const response = await fetch(`/api/projects/${projectId}/analysis-lineages`, {
    method: 'POST'
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function refreshRangeCandidate(projectId) {
  const response = await fetch(`/api/projects/${projectId}/git/range-candidate`, {
    method: 'POST'
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}