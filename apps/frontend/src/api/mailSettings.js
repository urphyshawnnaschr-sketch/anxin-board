import { localWriteHeaders } from './localSession.js'

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function getMailTransportSettings() {
  const response = await fetch('/api/settings/mail-transport')
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function configureMailTransport(payload) {
  const headers = await localWriteHeaders()
  const response = await fetch('/api/settings/mail-transport', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function testMailTransportConnection(expectedVersionNo) {
  const version = Number(expectedVersionNo)
  if (!Number.isInteger(version) || version <= 0) throw new Error('MAIL_TRANSPORT_TEST_VERSION_INVALID')
  const headers = await localWriteHeaders()
  const response = await fetch(`/api/settings/mail-transport/test?expected_version_no=${encodeURIComponent(version)}`, {
    method: 'POST',
    headers
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}
