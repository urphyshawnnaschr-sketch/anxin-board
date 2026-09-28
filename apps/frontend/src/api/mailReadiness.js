async function parseJSON(response) {
  try { return await response.json() } catch { return {} }
}

export async function getMailReadiness(projectId) {
  const response = await fetch(`/api/projects/${projectId}/mail-readiness`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
