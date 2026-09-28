import { localReadHeaders, localWriteHeaders, handleLocalSessionFailure, isLocalSessionError } from './localSession.js'

// 项目档案接口客户端：只负责项目档案请求与响应解析，不持有页面状态。

const PROFILE_GENERATION_ATTEMPT_PREFIX = 'anxinboard:profile-generation-attempt:v1:'
const PROFILE_GENERATION_UNKNOWN_CODE = 'PROFILE_GENERATION_RESULT_UNKNOWN'
const PROFILE_GENERATION_UNKNOWN_MESSAGE = '本次模型请求结果不确定，系统不会自动重发。再次点击只会核对同一次请求，不会重新调用模型。'
const PROJECT_STATE_BASELINE_ATTEMPT_PREFIX = 'anxinboard:project-state-baseline-attempt:v1:'
const PROJECT_STATE_BASELINE_UNKNOWN_CODE = 'PROJECT_STATE_BASELINE_BATCH_UNKNOWN'
const PROJECT_STATE_BASELINE_UNKNOWN_MESSAGE = '有一个全量分析批次的外部结果无法确认。系统不会自动重发；再次点击只会核对同一次授权。'
const PROJECT_STATE_BASELINE_DIAGNOSTIC_CODE = /^[A-Z][A-Z0-9_:-]{1,119}$/

async function parseJSON(response) {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

function generationAttemptStorageKey(projectId) {
  return `${PROFILE_GENERATION_ATTEMPT_PREFIX}${projectId}`
}

function baselineAttemptStorageKey(projectId) {
  return `${PROJECT_STATE_BASELINE_ATTEMPT_PREFIX}${projectId}`
}

function readStoredAttempt(storageKey) {
  try {
    return sessionStorage.getItem(storageKey) || ''
  } catch {
    throw new Error('PROFILE_GENERATION_IDEMPOTENCY_STORAGE_UNAVAILABLE')
  }
}

function writeStoredAttempt(storageKey, value) {
  try {
    sessionStorage.setItem(storageKey, value)
    if (sessionStorage.getItem(storageKey) !== value) {
      throw new Error('idempotency key did not persist')
    }
  } catch {
    throw new Error('PROFILE_GENERATION_IDEMPOTENCY_STORAGE_UNAVAILABLE')
  }
}

function clearStoredAttempt(storageKey) {
  try { sessionStorage.removeItem(storageKey) } catch {}
}

function readGenerationAttemptKey(projectId) {
  return readStoredAttempt(generationAttemptStorageKey(projectId))
}

function writeGenerationAttemptKey(projectId, value) {
  writeStoredAttempt(generationAttemptStorageKey(projectId), value)
}

function clearGenerationAttemptKey(projectId) {
  clearStoredAttempt(generationAttemptStorageKey(projectId))
}

export function discardProfileGenerationAttempt(projectId) {
  clearGenerationAttemptKey(projectId)
}

function unknownGenerationResult() {
  return {
    status: 0,
    ok: false,
    body: {
      detail: {
        code: PROFILE_GENERATION_UNKNOWN_CODE,
        message: PROFILE_GENERATION_UNKNOWN_MESSAGE
      }
    }
  }
}

function unknownBaselineResult() {
  return {
    status: 0,
    ok: false,
    body: {
      detail: {
        code: PROJECT_STATE_BASELINE_UNKNOWN_CODE,
        message: PROJECT_STATE_BASELINE_UNKNOWN_MESSAGE
      }
    }
  }
}

export function formatProjectStateBaselineError(body, fallback) {
  if (isLocalSessionError(body?.detail?.code)) return '当前窗口的本地授权已失效，请从桌面安心看板图标重新打开，再手动继续。未自动重发请求。'
  const detail = body?.detail
  const message = typeof detail?.message === 'string' && detail.message.trim() ? detail.message.trim() : fallback
  const rawCode = detail?.current?.error_code ?? detail?.code
  if (typeof rawCode !== 'string') return message
  const code = rawCode.trim()
  if (!PROJECT_STATE_BASELINE_DIAGNOSTIC_CODE.test(code)) return message
  return `${message}（诊断码：${code}）`
}

function isCompleteGenerationSuccess(body) {
  return Number.isInteger(body?.profile?.id) && body.profile.id > 0 && body.profile.status === 'candidate'
}

export async function createProfileCandidate(projectId, content) {
  const response = await fetch(`/api/projects/${projectId}/profile-candidates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(content)
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function generateProfileCandidate(projectId, options = null) {
  const headers = await localWriteHeaders({ requireIdempotency: true })
  const existingKey = readGenerationAttemptKey(projectId)
  const attemptKey = existingKey || headers['Local-Idempotency-Key']
  headers['Local-Idempotency-Key'] = attemptKey
  if (!existingKey) writeGenerationAttemptKey(projectId, attemptKey)

  try {
    const response = await fetch(`/api/projects/${projectId}/profile-candidates/${options ? "generate-v2" : "generate"}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(options ? { authorized: true, include_implementation: options.includeImplementation === true, ...(options.planProfileId != null ? { plan_profile_id: options.planProfileId } : {}) } : { authorized: true })
    })
    const body = await parseJSON(response)
    const code = body?.detail?.code

    if (response.ok) {
      // Do not clear the durable identity merely because an HTTP 2xx status arrived.
      // A truncated/malformed response is still ambiguous to the browser; replaying the
      // same key lets the server return the stored success without another provider call.
      if (!isCompleteGenerationSuccess(body)) return unknownGenerationResult()
      clearGenerationAttemptKey(projectId)
      return { status: response.status, ok: true, body }
    }

    if (!code || typeof code !== "string") return unknownGenerationResult()
    if (code !== PROFILE_GENERATION_UNKNOWN_CODE) {
      // A classified non-ambiguous server response closes this Human action. A later
      // click is therefore allowed to materialize a fresh explicit action/key.
      clearGenerationAttemptKey(projectId)
    }
    return { status: response.status, ok: false, body }
  } catch {
    // The browser cannot know whether the local server/provider already consumed this
    // request. Keep the exact same durable key so any Human retry can only replay/check
    // this attempt; it must not materialize a second provider call after an ambiguous loss.
    return unknownGenerationResult()
  }
}

export async function preflightProjectStateBaseline(projectId, planProfileId) {
  try {
    const headers = await localWriteHeaders()
    const response = await fetch(`/api/projects/${projectId}/project-state-baseline/preflight`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ plan_profile_id: planProfileId })
    })
    const body = await parseJSON(response)
    return { status: response.status, ok: response.ok, body }
  } catch (error) {
    const localSessionUnavailable = error instanceof Error && error.message === 'LOCAL_SESSION_UNAVAILABLE'
    return {
      status: 0,
      ok: false,
      body: { detail: {
        code: localSessionUnavailable ? 'PROJECT_STATE_BASELINE_LOCAL_SESSION_UNAVAILABLE' : 'PROJECT_STATE_BASELINE_PREFLIGHT_NETWORK_FAILED',
        message: localSessionUnavailable
          ? '本地会话已失效，请重启安心看板后再建立分析台账。'
          : '无法连接安心看板本地分析服务，请确认应用仍在运行后重试。'
      } }
    }
  }
}

export async function executeProjectStateBaseline(projectId, planProfileId, preflightIdentityHash) {
  const storageKey = baselineAttemptStorageKey(projectId)
  const headers = await localWriteHeaders({ requireIdempotency: true })
  const existingKey = readStoredAttempt(storageKey)
  const attemptKey = existingKey || headers['Local-Idempotency-Key']
  headers['Local-Idempotency-Key'] = attemptKey
  if (!existingKey) writeStoredAttempt(storageKey, attemptKey)

  try {
    const response = await fetch(`/api/projects/${projectId}/project-state-baseline/execute`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        plan_profile_id: planProfileId,
        authorized: true,
        authorization_nonce: attemptKey,
        preflight_identity_hash: preflightIdentityHash
      })
    })
    const body = await parseJSON(response)
    const code = body?.detail?.code
    if (response.ok) {
      if (!isCompleteGenerationSuccess(body)) return unknownBaselineResult()
      clearStoredAttempt(storageKey)
      return { status: response.status, ok: true, body }
    }
    if (!code || typeof code !== 'string') return unknownBaselineResult()
    if (code !== PROJECT_STATE_BASELINE_UNKNOWN_CODE) clearStoredAttempt(storageKey)
    return { status: response.status, ok: false, body }
  } catch {
    return unknownBaselineResult()
  }
}

export async function getProjectStateBaselineStatus(projectId) {
  const response = await fetch(`/api/projects/${projectId}/project-state-baseline/status`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function getProjectProfile(profileId) {
  const response = await fetch(`/api/profile-candidates/${profileId}`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function getAtlasReport(projectId, taskId, { download = false } = {}) {
  try {
    const headers = await localReadHeaders()
    const suffix = download ? 'export-html' : 'review-html'
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/project-state-baseline/atlas/tasks/${encodeURIComponent(taskId)}/${suffix}`, { method: 'GET', headers, cache: 'no-store', redirect: 'error' })
    if (!response.ok) {
      const body = await parseJSON(response)
      handleLocalSessionFailure(response.status, body, headers)
      return { ok: false, status: response.status, body }
    }
    if (!/^text\/html(?:;|$)/i.test(response.headers.get('Content-Type') || '')) {
      return { ok: false, status: response.status, body: { detail: { code: 'BROWNFIELD_REPORT_CONTENT_INVALID' } } }
    }
    return { ok: true, status: response.status, blob: new Blob([await response.text()], { type: 'text/html;charset=utf-8' }) }
  } catch (error) {
    return { ok: false, status: 0, body: { detail: { code: error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? 'LOCAL_SESSION_UNAVAILABLE' : 'BROWNFIELD_REPORT_READ_FAILED' } } }
  }
}

export function atlasReportErrorMessage(code) {
  if (isLocalSessionError(code)) return '当前窗口的本地授权已失效，请从桌面安心看板图标重新打开。没有重新调用模型。'
  const messages = {
    BROWNFIELD_RESULT_IDENTITY_INVALID: '当前版本与保存的分析结果不一致，请核对档案版本。',
    BROWNFIELD_RESULT_AMBIGUOUS: '存在多个分析结果，暂时无法唯一确认这版结果。',
    BROWNFIELD_RESULT_NOT_FOUND: '未找到这版档案对应的已保存分析结果。',
    BROWNFIELD_REPORT_CONFIRMATION_REQUIRED: '下载前请先人工确认本次结果。',
    BROWNFIELD_REPORT_IDENTITY_INVALID: '结果版本已变化，请重新读取当前分析结果。',
    BROWNFIELD_REPORT_SCOPE_CHANGED: '当前代码或需求范围已变化，请核对后再下载。'
  }
  return Object.hasOwn(messages, code) ? messages[code] : '暂时无法读取安心看板，请稍后手动重试。没有重新调用模型。'
}

export async function listProjectProfiles(projectId) {
  const response = await fetch(`/api/projects/${projectId}/profiles`)
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function updateProfileCandidate(profileId, editVersion, content) {
  const response = await fetch(`/api/profile-candidates/${profileId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ edit_version: editVersion, content })
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}

export async function confirmProfileCandidate(profileId, editVersion, confirmedBy) {
  // One product confirmation endpoint handles both ordinary candidates and candidates
  // created by the full Project State Baseline lane. The backend falls back to ordinary
  // confirmation when no baseline provenance exists; baseline-origin candidates atomically
  // bind their analyzed exact HEAD as the next incremental Git lineage origin.
  const response = await fetch(`/api/profile-candidates/${profileId}/confirm-with-state-baseline`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ edit_version: editVersion, confirmed_by: confirmedBy || 'local' })
  })
  const body = await parseJSON(response)
  return { status: response.status, ok: response.ok, body }
}


const atlasURL = projectId => `/api/projects/${encodeURIComponent(projectId)}/project-state-baseline/atlas`
export function isAtlasTask(task) {
  return task && typeof task.task_id === 'string' && task.task_id.length > 0 &&
    ['queued', 'running', 'succeeded', 'failed_pre_send', 'failed_after_send', 'unknown'].includes(task.status)
}
async function atlasRead(url) {
  try {
    const response = await fetch(url)
    return { ok: response.ok, status: response.status, body: await parseJSON(response) }
  } catch { return unknownBaselineResult() }
}
export const getLatestAtlasTask = projectId => atlasRead(`${atlasURL(projectId)}/tasks/latest`)
export const getAtlasTask = (projectId, taskId) => atlasRead(`${atlasURL(projectId)}/tasks/${encodeURIComponent(taskId)}`)
export async function preflightAtlasBaseline(projectId, planProfileId) {
  try {
    const headers = await localWriteHeaders()
    const response = await fetch(`${atlasURL(projectId)}/preflight`, {
      method: 'POST', headers, body: JSON.stringify({ plan_profile_id: planProfileId })
    })
    const body = await parseJSON(response)
    if (!response.ok) handleLocalSessionFailure(response.status, body, headers)
    return { ok: response.ok, status: response.status, body }
  } catch (error) {
    const missingSession = error instanceof Error && error.message === 'LOCAL_SESSION_UNAVAILABLE'
    return { ok: false, status: 0, body: { detail: {
      code: missingSession ? 'PROJECT_STATE_BASELINE_LOCAL_SESSION_UNAVAILABLE' : 'PROJECT_STATE_BASELINE_PREFLIGHT_NETWORK_FAILED',
      message: missingSession ? '本地会话已失效，请重新打开应用后再扫描。' : '无法完成本地代码扫描。此准备步骤不会调用模型。'
    } } }
  }
}
export async function createAtlasTask(projectId, planProfileId, preflightIdentityHash, { retryTask = null } = {}) {
  // Identity survives browser sessions and is bound to the exact reviewed preparation.
  const key = `anxinboard:atlas-attempt:v1:${projectId}:${planProfileId}:${preflightIdentityHash}`
  let headers
  try { headers = await localWriteHeaders({ requireIdempotency: true }) }
  catch (error) {
    if (error?.message === 'LOCAL_SESSION_UNAVAILABLE') return missingAtlasSession()
    throw error
  }
  let nonce = localStorage.getItem(key)
  const explicitKnownFailure = isAtlasTask(retryTask) &&
    ['failed_pre_send', 'failed_after_send'].includes(retryTask.status) &&
    String(retryTask.plan_profile_id) === String(planProfileId) &&
    retryTask.preflight_identity_hash === preflightIdentityHash &&
    retryTask.authorization_nonce === nonce
  if (!nonce || explicitKnownFailure) {
    nonce = headers['Local-Idempotency-Key']
    localStorage.setItem(key, nonce)
    if (localStorage.getItem(key) !== nonce) throw new Error('ATLAS_IDEMPOTENCY_STORAGE_UNAVAILABLE')
  }
  headers['Local-Idempotency-Key'] = nonce
  try {
    const response = await fetch(`${atlasURL(projectId)}/tasks`, {
      method: 'POST', headers, body: JSON.stringify({ plan_profile_id: planProfileId,
        authorized: true, authorization_nonce: nonce, preflight_identity_hash: preflightIdentityHash })
    })
    const body = await parseJSON(response)
    if (!response.ok) handleLocalSessionFailure(response.status, body, headers)
    if (response.ok && isAtlasTask(body?.task)) return { ok: true, status: response.status, body }
    if (!response.ok && body?.detail?.code) return { ok: false, status: response.status, body }
  } catch { /* Resolve uncertain admission by observation, never a second POST. */ }
  const observed = await getLatestAtlasTask(projectId)
  if (observed.ok && isAtlasTask(observed.body?.task) &&
      observed.body.task.authorization_nonce === nonce &&
      observed.body.task.preflight_identity_hash === preflightIdentityHash) return observed
  return { ...unknownBaselineResult(), atlasIdentity: { authorization_nonce: nonce, preflight_identity_hash: preflightIdentityHash } }
}
export async function resumeAtlasTask(projectId, taskId) {
  let headers
  try { headers = await localWriteHeaders({ requireIdempotency: true }) }
  catch (error) {
    if (error?.message === 'LOCAL_SESSION_UNAVAILABLE') return missingAtlasSession()
    throw error
  }
  try {
    const response = await fetch(`${atlasURL(projectId)}/tasks/${encodeURIComponent(taskId)}/resume`, { method: 'POST', headers })
    const body = await parseJSON(response)
    if (!response.ok) handleLocalSessionFailure(response.status, body, headers)
    if (response.ok && !isAtlasTask(body?.task)) return unknownBaselineResult()
    return { ok: response.ok, status: response.status, body }
  } catch { return getAtlasTask(projectId, taskId) }
}

export async function getAtlasProfileResult(projectId, profileId) {
  try {
    const headers = await localReadHeaders()
    const response = await fetch(`${atlasURL(projectId)}/results/${encodeURIComponent(profileId)}`, {
      method: 'GET', headers, cache: 'no-store', redirect: 'error'
    })
    const body = await parseJSON(response)
    handleLocalSessionFailure(response.status, body, headers)
    return { ok: response.ok, status: response.status, body }
  } catch (error) {
    return { ok: false, status: 0, body: { detail: {
      code: isLocalSessionError(error?.message) ? 'LOCAL_SESSION_UNAVAILABLE' : 'BROWNFIELD_RESULT_READ_FAILED'
    } } }
  }
}

function missingAtlasSession() {
  return { ok: false, status: 403, body: { detail: {
    code: 'PROJECT_STATE_BASELINE_LOCAL_SESSION_UNAVAILABLE',
    message: '当前窗口的本地授权已失效，请从桌面安心看板图标重新打开。请求尚未发送。'
  } } }
}
