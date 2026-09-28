async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getRuntimeHealth() {
  const response = await fetch('/api/health')
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
