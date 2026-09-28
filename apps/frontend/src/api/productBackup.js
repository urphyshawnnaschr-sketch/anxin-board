import { localWriteHeaders } from './localSession.js'

function filenameFromDisposition(value) {
  if (typeof value !== 'string') return 'AnxinBoard-backup.zip'
  const match = /filename="?([^";]+)"?/i.exec(value)
  return match?.[1] || 'AnxinBoard-backup.zip'
}

export async function exportProductBackup() {
  const headers = await localWriteHeaders()
  const response = await fetch('/api/product-backup/export', {
    method: 'POST',
    headers
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    return { ok: false, status: response.status, body }
  }
  const blob = await response.blob()
  return {
    ok: true,
    status: response.status,
    blob,
    filename: filenameFromDisposition(response.headers.get('content-disposition'))
  }
}
