const SESSION_KEY = 'anxinboard:local-browser-session:v1'
let sessionExchange = null

function randomId(prefix) {
  const value = typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID().replaceAll('-', '')
    : `${Date.now()}${Math.random().toString(16).slice(2)}`
  return `${prefix}-${value}`.slice(0, 120)
}

function readSession() {
  try { return sessionStorage.getItem(SESSION_KEY) || '' } catch { return '' }
}

function writeSession(value) {
  try { sessionStorage.setItem(SESSION_KEY, value) } catch {}
}

function readFragmentBootstrap(url) {
  const raw = url.hash.startsWith('#') ? url.hash.slice(1) : url.hash
  const separator = raw.indexOf('?')
  if (separator < 0) return ''
  const params = new URLSearchParams(raw.slice(separator + 1))
  return params.get('anxin_bootstrap') || ''
}

function stripBootstrap(url) {
  let changed = false
  // Remove any stale query-form bootstrap without accepting it for exchange.
  if (url.searchParams.has('anxin_bootstrap')) {
    url.searchParams.delete('anxin_bootstrap')
    changed = true
  }

  const raw = url.hash.startsWith('#') ? url.hash.slice(1) : url.hash
  const separator = raw.indexOf('?')
  if (separator >= 0) {
    const path = raw.slice(0, separator)
    const params = new URLSearchParams(raw.slice(separator + 1))
    if (params.has('anxin_bootstrap')) {
      params.delete('anxin_bootstrap')
      const query = params.toString()
      url.hash = `#${path}${query ? `?${query}` : ''}`
      changed = true
    }
  }

  if (changed) {
    history.replaceState(history.state, '', `${url.pathname}${url.search}${url.hash}`)
  }
}

export async function ensureLocalSession() {
  if (sessionExchange) return sessionExchange
  const url = new URL(window.location.href)
  const bootstrap = readFragmentBootstrap(url)
  if (!bootstrap) {
    stripBootstrap(url)
    return readSession()
  }
  // A launcher handoff takes priority over a stale token from a previous runtime.
  // Mount/read/write callers share one exchange because the handoff is single-use.
  writeSession('')
  sessionExchange = exchangeBootstrap(url, bootstrap)
  try { return await sessionExchange } finally { sessionExchange = null }
}

async function exchangeBootstrap(url, bootstrap) {
  try {
    const response = await fetch('/api/local-session/exchange', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Request-ID': randomId('bootstrap')
      },
      body: JSON.stringify({ bootstrap_secret: bootstrap })
    })
    if (!response.ok) return ''
    const body = await response.json().catch(() => ({}))
    if (body?.session_state !== 'active' || typeof body?.session_token !== 'string' || !body.session_token) return ''
    writeSession(body.session_token)
    return body.session_token
  } catch {
    return ''
  } finally {
    stripBootstrap(url)
  }
}

export function getLocalSession() {
  return readSession()
}

export function isLocalSessionError(code) {
  return ['LOCAL_SESSION_UNAVAILABLE', 'LOCAL_SESSION_SESSION_INVALID',
    'PROJECT_STATE_BASELINE_LOCAL_SESSION_UNAVAILABLE'].includes(code)
}

export function handleLocalSessionFailure(status, body, requestHeaders) {
  if (![401, 403, 503].includes(status) || !isLocalSessionError(body?.detail?.code)) return false
  const sent = requestHeaders?.['X-Anxin-Session']
  // An old request must never clear a session obtained by a newer launcher handoff.
  if (typeof sent === 'string' && sent && readSession() === sent) writeSession('')
  return true
}

export async function localReadHeaders() {
  const session = await ensureLocalSession()
  if (!session) throw new Error('LOCAL_SESSION_UNAVAILABLE')
  return {
    'X-Anxin-Session': session,
    'X-Request-ID': randomId('read')
  }
}

export async function localWriteHeaders({ requireIdempotency = false } = {}) {
  const session = await ensureLocalSession()
  if (!session) throw new Error('LOCAL_SESSION_UNAVAILABLE')
  const headers = {
    'Content-Type': 'application/json',
    'X-Anxin-Session': session,
    'X-Request-ID': randomId('req')
  }
  if (requireIdempotency) headers['Local-Idempotency-Key'] = randomId('idem')
  return headers
}
