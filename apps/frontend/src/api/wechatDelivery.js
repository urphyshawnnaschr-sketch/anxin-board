import { localReadHeaders, localWriteHeaders, handleLocalSessionFailure } from './localSession.js'

function projectPath(projectId) {
  if (!/^[1-9]\d*$/.test(String(projectId))) throw new Error('WECHAT_PROJECT_INVALID')
  return `/api/projects/${projectId}`
}

async function readJSON(response, headers) {
  const body = await response.json().catch(() => ({}))
  handleLocalSessionFailure(response.status, body, headers)
  return { ok: response.ok, status: response.status, body }
}

async function read(projectId, suffix) {
  const headers = await localReadHeaders()
  const response = await fetch(projectPath(projectId) + suffix, { headers, cache: 'no-store' })
  return readJSON(response, headers)
}

async function write(projectId, suffix, payload, idempotent = false, options = {}) {
  const headers = await localWriteHeaders({ requireIdempotency: idempotent })
  const response = await fetch(projectPath(projectId) + suffix, { method: 'POST', headers, body: JSON.stringify(payload), ...options })
  return readJSON(response, headers)
}

export const getWechatSettings = projectId => read(projectId, '/wechat-settings')
export const getWechatHistory = projectId => read(projectId, '/wechat-history')
export const saveWechatSettings = (projectId, payload) => write(projectId, '/wechat-settings', payload, true)
export const createWechatPreview = (projectId, payload) => write(projectId, '/wechat-preview', payload)
export const sendWechatPreview = (projectId, previewId) => write(projectId, '/wechat-send', { preview_id: previewId, human_confirmed: true }, true)

export const getWechatBinding = projectId => read(projectId, '/wechat-binding')
export const startWechatLogin = (projectId, version) => write(projectId, '/wechat-login/start', { expected_version_no: version }, true)
export const pollWechatLogin = (projectId, flowId, verificationCode, signal) => write(projectId, '/wechat-login/poll', {
  flow_id: flowId, ...(verificationCode ? { verification_code: verificationCode } : {})
}, true, { signal })
export const cancelWechatLogin = (projectId, flowId) => write(projectId, '/wechat-login/cancel', { flow_id: flowId }, true, { keepalive: true })
export const refreshWechatBinding = (projectId, version) => write(projectId, '/wechat-binding/refresh', { expected_version_no: version }, true)
export const disconnectWechatBinding = (projectId, version) => write(projectId, '/wechat-binding/disconnect', { expected_version_no: version, human_confirmed: true }, true)

export async function getWechatLoginQR(projectId, flowId, signal) {
  if (typeof flowId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(flowId)) throw new Error('WECHAT_FLOW_INVALID')
  const headers = await localReadHeaders()
  const response = await fetch(`${projectPath(projectId)}/wechat-login/${encodeURIComponent(flowId)}/qr.png`, { headers, cache: 'no-store', signal })
  if (!response.ok) { await readJSON(response, headers); throw new Error('WECHAT_QR_UNAVAILABLE') }
  if (response.headers.get('content-type')?.split(';')[0] !== 'image/png') throw new Error('WECHAT_QR_INVALID')
  const blob = await response.blob()
  if (blob.size < 8 || blob.size > 1300000) throw new Error('WECHAT_QR_INVALID')
  return blob
}

export async function getWechatImage(projectId, previewId, index) {
  if (typeof previewId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(previewId) || !Number.isInteger(index) || index < 1 || index > 24) throw new Error('WECHAT_IMAGE_INVALID')
  const headers = await localReadHeaders()
  const response = await fetch(`${projectPath(projectId)}/wechat-preview/${encodeURIComponent(previewId)}/images/${index}`, { headers, cache: 'no-store' })
  if (!response.ok) {
    await readJSON(response, headers)
    throw new Error('WECHAT_IMAGE_UNAVAILABLE')
  }
  if (response.headers.get('content-type')?.split(';')[0] !== 'image/png') throw new Error('WECHAT_IMAGE_INVALID')
  const blob = await response.blob()
  if (blob.size < 8 || blob.size > 1300000) throw new Error('WECHAT_IMAGE_INVALID')
  return blob
}
