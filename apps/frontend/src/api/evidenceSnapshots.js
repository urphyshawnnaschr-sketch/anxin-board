async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function confirmEvidenceSnapshot(projectId, payload) {
  const response = await fetch(`/api/projects/${projectId}/evidence-snapshots`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
