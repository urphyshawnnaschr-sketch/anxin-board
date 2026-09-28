async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getHistoryTaskStatus(projectId, localTaskId) {
  const normalizedTaskId = String(localTaskId || '').trim()
  if (!normalizedTaskId) {
    return {
      status: 400,
      ok: false,
      body: {
        detail: {
          code: 'REPORT_GENERATION_TASK_INPUT_INVALID',
          message: '请输入任务 ID'
        }
      }
    }
  }

  const response = await fetch(
    `/api/projects/${projectId}/report-generation-tasks/${encodeURIComponent(normalizedTaskId)}`
  )
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
