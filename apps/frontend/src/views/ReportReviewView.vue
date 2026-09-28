<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  authorizeReportContradictionSend,
  authorizeReportReanalysisSend,
  cancelReportReanalysis,
  createReportApproval,
  createReportReanalysis,
  createReportSupplement,
  createReportValidation,
  executeReportContradiction,
  executeReportReanalysis,
  getCurrentReportReview,
  getReportApproval,
  getReportReanalysis,
  prepareReportContradiction,
  prepareReportReanalysis,
  previewReportContradictionSend,
  previewReportReanalysisSend
} from '../api/reportReview.js'

const props = defineProps({ projectId: { type: [Number, String], default: null } })

const viewState = ref('loading')
const bundle = ref(null)
const approvalSnapshot = ref(null)
const reanalysisBundle = ref(null)
const activeTab = ref('ai')
const notice = ref('')
const noticeType = ref('info')
const loadDetail = ref('')
const supplementText = ref('')
const supplementSourceType = ref('pm_external_fact')
const supplementProvidedBy = ref('')
const supplementSaving = ref(false)
const validationRunning = ref(false)
const approvalOpen = ref(false)
const approvalSaving = ref(false)
const approverName = ref('')
const correctionOpen = ref(false)
const reanalysisSaving = ref(false)
const cancellationOpen = ref(false)
const cancellationSaving = ref(false)
const cancellationBy = ref('')
const cancellationReason = ref('')
const cancellationKey = ref('')
const modelSendOpen = ref(false)
const modelSendPreparing = ref(false)
const modelSendExecuting = ref(false)
const modelSendAck = ref(false)
const modelSendFlow = ref('')
const modelSendPreview = ref(null)
const correctionLocation = ref('')
const correctionTruth = ref('')
const correctionBasis = ref('')
const correctionSource = ref('')
const correctionRequestedBy = ref('')

let authorityGeneration = 0

const report = computed(() => bundle.value?.report_version || null)
const aiRaw = computed(() => bundle.value?.ai_raw || null)
const aiContent = computed(() => {
  const content = aiRaw.value?.content
  if (content?.new_report && typeof content.new_report === 'object') return content.new_report
  return content && typeof content === 'object' ? content : {}
})
const gitFacts = computed(() => bundle.value?.git_facts || {})
const evidenceRefs = computed(() => Array.isArray(bundle.value?.evidence_refs) ? bundle.value.evidence_refs : [])
const PROGRESS_STAGES = ['暂时无法确认', '开发中', '等待联调', '等待测试', '测试中', '已完成']
const progressEdits = ref({})
const progressModules = computed(() => normalizeProgressPreview(bundle.value?.progress_preview))
const progressGitRefs = computed(() => selectProgressGitRefs(bundle.value?.progress_preview, evidenceRefs.value))
function selectProgressGitRefs(preview, legacyRefs) {
  const refs = Array.isArray(preview?.evidence_refs) ? preview.evidence_refs : legacyRefs
  return refs.filter(item => item?.type === 'git_file_fact' && typeof item.evidence_id === 'string' && item.evidence_id)
}
watch(() => [props.projectId, report.value?.report_version_id, report.value?.report_content_hash, report.value?.state_version], () => { progressEdits.value = {} }, { flush: 'sync' })

function normalizeProgressPreview(value) {
  if (value?.state !== 'ready' || !Array.isArray(value.modules) || !value.modules.length || value.modules.length > 100) return []
  const ids = new Set()
  for (const row of value.modules) {
    if (!row || typeof row.module_id !== 'string' || !row.module_id || ids.has(row.module_id) || typeof row.name !== 'string' || !row.name || !PROGRESS_STAGES.includes(row.previous_stage) || !PROGRESS_STAGES.includes(row.stage) || !Array.isArray(row.evidence_ids) || !row.evidence_ids.every(id => typeof id === 'string')) return []
    ids.add(row.module_id)
  }
  return value.modules
}
function buildProgressCorrections(modules, edits, refs) {
  const ids = new Set(modules.map(row => row.module_id))
  const allowed = new Set(refs.filter(row => row?.type === 'git_file_fact' && typeof row.evidence_id === 'string').map(row => row.evidence_id))
  const result = []
  for (const [module_id, edit] of Object.entries(edits)) {
    if (!edit?.enabled) continue
    if (!ids.has(module_id) || !PROGRESS_STAGES.includes(edit.stage) || typeof edit.reason !== 'string' || !edit.reason.trim() || edit.reason.trim().length > 2000 || !Array.isArray(edit.evidence_ids) || !edit.evidence_ids.length || edit.evidence_ids.length > 100 || new Set(edit.evidence_ids).size !== edit.evidence_ids.length || !edit.evidence_ids.every(id => allowed.has(id))) throw new Error('PROGRESS_CORRECTION_INVALID')
    result.push({ module_id, stage: edit.stage, reason: edit.reason.trim(), evidence_ids: [...edit.evidence_ids] })
  }
  return result
}
function toggleProgressCorrection(row, enabled) {
  if (!canApprove.value || approvalSaving.value) return
  const next = { ...progressEdits.value }
  if (enabled) Object.defineProperty(next, row.module_id, { value: { enabled: true, stage: row.stage, reason: '', evidence_ids: [] }, enumerable: true, configurable: true, writable: true })
  else delete next[row.module_id]
  progressEdits.value = next
}

const currentSupplement = computed(() => bundle.value?.current_supplement || null)
const latestValidation = computed(() => bundle.value?.latest_validation_result || null)
const featureProgress = computed(() => Array.isArray(aiContent.value?.feature_progress) ? aiContent.value.feature_progress : [])
const validationBlockers = computed(() => Array.isArray(latestValidation.value?.blockers) ? latestValidation.value.blockers : [])
const softValidationCodes = new Set(['PROJECT_GIT_AUTHORITY_DRIFT', 'ANALYSIS_LINEAGE_AUTHORITY_DRIFT', 'CURRENT_PRD_AUTHORITY_DRIFT', 'CURRENT_PROFILE_AUTHORITY_DRIFT', 'REPORT_CONTRADICTION_CHECK_REQUIRED', 'REPORT_CONTRADICTION_BLOCKED'])
const advisoryCheckCodes = new Set(['project_git_identity', 'analysis_lineage_identity', 'current_prd_identity', 'current_profile_identity', 'supplement_contradiction_optional'])
const hardValidationBlockers = computed(() => validationBlockers.value.filter(item => !softValidationCodes.has(item?.code)))
const validationAdvisories = computed(() => {
  const seen = new Map()
  for (const item of validationBlockers.value.filter(item => softValidationCodes.has(item?.code))) seen.set(item.code, item)
  for (const item of (Array.isArray(latestValidation.value?.checks) ? latestValidation.value.checks : [])) {
    if (item?.passed === false && advisoryCheckCodes.has(item?.code) && !seen.has(item.code)) seen.set(item.code, item)
  }
  return [...seen.values()]
})
const supplementWritable = computed(() => ['pending_review', 'blocked'].includes(report.value?.lifecycle))
const validationFresh = computed(() => Boolean(
  latestValidation.value?.state === 'passed' &&
  report.value?.state_version === latestValidation.value?.report_state_version &&
  report.value?.report_content_hash === latestValidation.value?.report_content_hash
))
const currentApprovalSnapshot = computed(() => (
  viewState.value === 'loaded' && approvalSnapshotMatchesReport(approvalSnapshot.value, report.value, props.projectId)
    ? approvalSnapshot.value
    : null
))
const alreadyApproved = computed(() => Boolean(currentApprovalSnapshot.value))
const approvalAuthorityBroken = computed(() => viewState.value === 'loaded' && report.value?.lifecycle === 'approved' && !currentApprovalSnapshot.value)
const canGoBoard = computed(() => viewState.value === 'loaded' && Boolean(currentApprovalSnapshot.value))
const canApprove = computed(() => !alreadyApproved.value && ['pending_review', 'blocked'].includes(report.value?.lifecycle))
const canReanalyze = computed(() => ['pending_review', 'blocked'].includes(report.value?.lifecycle) && !alreadyApproved.value && !reanalysisBundle.value?.cancelled)
const replacementTask = computed(() => reanalysisBundle.value?.replacement_task || null)
const canCancelReanalysis = computed(() => report.value?.lifecycle === 'superseded' &&
  !reanalysisBundle.value?.cancelled && replacementTask.value?.state === 'queued' &&
  !modelSendPreparing.value && !modelSendExecuting.value && !cancellationSaving.value)
const canCheckContradiction = computed(() => Boolean(
  currentSupplement.value &&
  supplementWritable.value &&
  !alreadyApproved.value &&
  report.value?.lifecycle !== 'superseded'
))
const modelSendFlowLabel = computed(() => modelSendFlow.value === 'reanalysis' ? 'AI 重分析' : 'AI 冲突检查')
const modelSendScopeSummary = computed(() => {
  if (modelSendFlow.value === 'reanalysis') {
    return `报告 V${report.value?.version_no || '—'}、冻结 EvidenceSnapshot #${report.value?.evidence_snapshot_id || '—'}、本次退回更正事实与替换任务 ${replacementTask.value?.local_task_id || '—'}`
  }
  return `报告 V${report.value?.version_no || '—'}、冻结 EvidenceSnapshot #${report.value?.evidence_snapshot_id || '—'}、当前人工补充 V${currentSupplement.value?.version_no || '—'}`
})

const lifecycleLabel = computed(() => ({
  pending_review: '待审阅', blocked: '已阻断', approved: '已确认', superseded: '已退回重分析', voided: '已作废'
}[report.value?.lifecycle] || '状态未知'))
const validationLabel = computed(() => {
  if (report.value?.lifecycle === 'superseded') return '已失效'
  if (hardValidationBlockers.value.length) return '阻断'
  if (validationAdvisories.value.length) return '有提醒'
  if (latestValidation.value?.state === 'passed') return '通过'
  return '确认时自动检查'
})

function setNotice(text, type = 'info') { notice.value = text; noticeType.value = type }
function detailMessage(body, fallback) {
  const detail = body?.detail
  const text = typeof detail?.message === 'string' && detail.message ? detail.message : fallback
  // Same bounded diagnostic contract as the task page; never display raw response objects.
  const codes = [...new Set([detail?.code, detail?.cause_code, detail?.credential_error_code]
    .filter(value => typeof value === 'string' && /^[A-Z][A-Z0-9_]{2,95}$/.test(value)))]
  const stages = { credential: '读取模型凭据', model_availability: '核对可用模型',
    model_metadata: '核对模型规格', qualification: '核对模型资格',
    authorization: '核对发送授权', current_authority: '校验模型发送条件',
    context_manifest: '校验发送资料', request_budget: '校验请求预算', provider_compatibility: '校验模型兼容性',
    output_contract: '校验 AI 输出' }
  const stage = Object.hasOwn(stages, detail?.stage) ? stages[detail.stage] : null
  const validationIssues = Array.isArray(detail?.validation_issues)
    ? detail.validation_issues.slice(0, 6).filter(item =>
      item && typeof item.code === 'string' && /^AI_[A-Z0-9_]{2,92}$/.test(item.code) &&
      typeof item.path === 'string' && /^\$[A-Za-z0-9_.\[\]<>-]{0,191}$/.test(item.path))
    : []
  const validationLine = validationIssues.length
    ? `输出校验：${validationIssues.map(item => `${item.code} @ ${item.path}`).join('；')}`
    : ''
  return [text, stage ? `失败步骤：${stage}` : '', codes.length ? `错误码：${codes.join(' / ')}` : '', validationLine]
    .filter(Boolean).join('\n')
}
function localTimezone() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' } }
function localUtcOffsetMinutes() { return -new Date().getTimezoneOffset() }
function newIdempotencyKey(prefix) {
  const random = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)
  return `${prefix}-${Date.now()}-${random}`.slice(0, 120)
}
function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}
function compactHash(value) {
  if (typeof value !== 'string') return value == null ? '—' : String(value)
  return value.length > 22 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value
}
function blockerText(item) { return typeof item === 'string' ? item : item?.message || item?.code || '未知阻断项' }
function advisoryText(item) {
  const labels = { project_git_identity: '当前 Git 仓库或分支已变化；本报告仍按生成时冻结快照确认。', analysis_lineage_identity: '当前分析链已向前演进；不影响确认这份历史报告。', current_prd_identity: '当前 PRD 已更新；本报告仍绑定生成时的 PRD 快照。', current_profile_identity: '当前项目档案已更新；本报告仍绑定生成时的档案快照。', supplement_contradiction_optional: '存在人工补充；AI 冲突检查为可选辅助，不是确认前置条件。' }
  return item?.message || labels[item?.code] || item?.code || '存在一项非阻断提醒。'
}
function evidenceBinding(item) { return item?.bound_object || item?.feature || item?.path || item?.ref || item?.source_ref || '当前冻结证据' }
function sameProjectId(left, right) { return String(left ?? '') === String(right ?? '') }
function approvalSnapshotMatchesReport(snapshot, currentReport, projectId) {
  return Boolean(
    snapshot &&
    currentReport &&
    currentReport.lifecycle === 'approved' &&
    sameProjectId(currentReport.project_id, projectId) &&
    snapshot.project_id === currentReport.project_id &&
    snapshot.report_version_id === currentReport.report_version_id &&
    Number.isInteger(snapshot.report_state_version_before) &&
    Number.isInteger(snapshot.report_state_version_after) &&
    snapshot.report_state_version_after === currentReport.state_version &&
    snapshot.report_state_version_before + 1 === snapshot.report_state_version_after
  )
}
function authorityRequestIsCurrent(generation, projectId, reportVersionId = null, stateVersion = null) {
  if (generation !== authorityGeneration || !sameProjectId(props.projectId, projectId)) return false
  const currentReport = report.value
  if (reportVersionId !== null && currentReport?.report_version_id !== reportVersionId) return false
  if (stateVersion !== null && currentReport?.state_version !== stateVersion) return false
  return true
}
function invalidateLoadedAuthority() {
  progressEdits.value = {}
  bundle.value = null
  approvalSnapshot.value = null
  reanalysisBundle.value = null
  approvalOpen.value = false
  correctionOpen.value = false
  cancellationOpen.value = false
  approvalSaving.value = false
  modelSendOpen.value = false
  modelSendPreview.value = null
  modelSendFlow.value = ''
  modelSendAck.value = false
}

async function loadApproval(projectId, expectedReport, generation) {
  if (!expectedReport?.report_version_id) return
  try {
    const result = await getReportApproval(projectId, expectedReport.report_version_id)
    if (!authorityRequestIsCurrent(generation, projectId, expectedReport.report_version_id, expectedReport.state_version)) return
    const snapshot = result.ok ? result.body?.approval_snapshot || null : null
    approvalSnapshot.value = approvalSnapshotMatchesReport(snapshot, expectedReport, projectId) ? snapshot : null
  } catch {
    if (authorityRequestIsCurrent(generation, projectId, expectedReport.report_version_id, expectedReport.state_version)) approvalSnapshot.value = null
  }
}

async function loadReanalysis(projectId, expectedReport, generation) {
  if (!expectedReport?.report_version_id) return
  try {
    const result = await getReportReanalysis(projectId, expectedReport.report_version_id)
    if (!authorityRequestIsCurrent(generation, projectId, expectedReport.report_version_id, expectedReport.state_version)) return
    reanalysisBundle.value = result.ok ? result.body || null : null
  } catch {
    if (authorityRequestIsCurrent(generation, projectId, expectedReport.report_version_id, expectedReport.state_version)) reanalysisBundle.value = null
  }
}

async function loadReview({ keepNotice = false } = {}) {
  const generation = ++authorityGeneration
  const projectId = props.projectId
  invalidateLoadedAuthority()
  viewState.value = 'loading'
  loadDetail.value = ''
  if (!keepNotice) notice.value = ''
  if (!projectId) {
    viewState.value = 'empty'; loadDetail.value = '尚未选择项目。'; return false
  }
  try {
    const { status, ok, body } = await getCurrentReportReview(projectId)
    if (!authorityRequestIsCurrent(generation, projectId)) return false
    if (status === 404) {
      viewState.value = 'empty'; loadDetail.value = detailMessage(body, '当前项目还没有可审阅的报告。'); return false
    }
    if (!ok || !body?.report_version || !body?.ai_raw) {
      viewState.value = status === 409 ? 'blocked' : 'error'; loadDetail.value = detailMessage(body, '当前报告审阅数据无法可靠读取。'); return false
    }
    if (!sameProjectId(body.project_id, projectId) || !sameProjectId(body.report_version.project_id, projectId)) {
      viewState.value = 'error'; loadDetail.value = '当前报告审阅数据与所选项目身份不一致。'; return false
    }
    const expectedReport = body.report_version
    bundle.value = body
    supplementText.value = body.current_supplement?.content || ''
    supplementSourceType.value = body.current_supplement?.source_type || 'pm_external_fact'
    supplementProvidedBy.value = body.current_supplement?.provided_by || ''
    await Promise.all([
      loadApproval(projectId, expectedReport, generation),
      loadReanalysis(projectId, expectedReport, generation)
    ])
    if (!authorityRequestIsCurrent(generation, projectId, expectedReport.report_version_id, expectedReport.state_version)) return false
    viewState.value = 'loaded'
    return true
  } catch {
    if (!authorityRequestIsCurrent(generation, projectId)) return false
    invalidateLoadedAuthority()
    viewState.value = 'error'; loadDetail.value = '无法连接本地报告审阅服务。'; return false
  }
}

async function runValidation({ quiet = false } = {}) {
  if (!report.value || validationRunning.value || alreadyApproved.value || !supplementWritable.value) return
  validationRunning.value = true
  if (!quiet) notice.value = ''
  try {
    const { status, ok, body } = await createReportValidation(props.projectId, report.value.report_version_id, {
      expected_report_state_version: report.value.state_version
    })
    if (!ok) {
      setNotice(detailMessage(body, status === 409 ? '报告依据已经变化，请刷新后重新校验。' : `确认前校验失败（HTTP ${status}）。`), status === 409 ? 'warning' : 'error')
      return
    }
    const result = body?.validation_result
    if (!result || !['passed', 'blocked'].includes(result.state)) {
      setNotice('校验结果格式无法确认，系统不会把它当成通过。', 'warning'); return
    }
    setNotice(result.state === 'passed' ? '确认前校验已通过，可以填写最终确认人。' : `确认前校验已阻断，共 ${result.blockers?.length || 0} 项。`, result.state === 'passed' ? 'success' : 'error')
    await loadReview({ keepNotice: true })
  } catch {
    setNotice('校验结果暂时无法确认，系统不会自动重试。', 'warning')
  } finally {
    validationRunning.value = false
  }
}

async function saveSupplement() {
  if (!report.value || supplementSaving.value || !supplementWritable.value) return
  if (!supplementText.value.trim()) return setNotice('请先填写项目经理补充内容。', 'error')
  if (!supplementProvidedBy.value.trim()) return setNotice('请填写人工补充的提供人。', 'error')
  supplementSaving.value = true
  try {
    const { status, ok, body } = await createReportSupplement(props.projectId, report.value.report_version_id, {
      content: supplementText.value,
      source_type: supplementSourceType.value,
      provided_by: supplementProvidedBy.value,
      provided_timezone: localTimezone(),
      idempotency_key: newIdempotencyKey(`page07-supp-${report.value.report_version_id}`),
      expected_latest_supplement_version: currentSupplement.value?.version_no || 0
    })
    if (!ok) return setNotice(detailMessage(body, `保存人工补充失败（HTTP ${status}）。`), status === 409 ? 'warning' : 'error')
    setNotice('人工补充已保存。你可以直接确认本版报告；AI 冲突检查仅作为可选辅助。', 'success')
    await loadReview({ keepNotice: true })
  } catch {
    setNotice('保存结果暂时无法确认。请先刷新，不会自动重复写入。', 'warning')
  } finally {
    supplementSaving.value = false
  }
}

function openApproval() {
  if (!canApprove.value) {
    setNotice(alreadyApproved.value ? '本版报告已经正式确认。' : '当前报告状态不能确认。', alreadyApproved.value ? 'success' : 'warning')
    return
  }
  approvalOpen.value = true
}

async function confirmApproval() {
  if (!canApprove.value || approvalSaving.value) return
  if (!approverName.value.trim()) return setNotice('请填写最终确认人姓名；该姓名会显示在正式安心看板中。', 'error')
  const generation = authorityGeneration
  const projectId = props.projectId
  const currentReport = report.value
  const reportVersionId = currentReport?.report_version_id
  const expectedStateVersion = currentReport?.state_version
  if (!reportVersionId || !Number.isInteger(expectedStateVersion) || !sameProjectId(currentReport?.project_id, projectId)) return
  let corrections
  try { corrections = buildProgressCorrections(progressModules.value, progressEdits.value, progressGitRefs.value) }
  catch { return setNotice('请为每项更正选择状态、填写原因，并选择本次已有的代码依据。', 'error') }
  approvalSaving.value = true
  try {
    const { status, ok, body } = await createReportApproval(projectId, reportVersionId, {
      expected_report_state_version: expectedStateVersion,
      confirmed_by: approverName.value,
      confirmed_timezone: localTimezone(),
      confirmed_utc_offset_minutes: localUtcOffsetMinutes(),
      human_confirmed: true,
      ...(corrections.length ? { progress_corrections: corrections } : {})
    })
    if (!authorityRequestIsCurrent(generation, projectId, reportVersionId, expectedStateVersion)) return
    const snapshot = body?.approval_snapshot
    if (!ok || !snapshot) {
      setNotice(detailMessage(body, status === 409 ? '最终确认依据已经变化，请刷新并重新校验。' : `最终确认失败（HTTP ${status}）。`), status === 409 ? 'warning' : 'error')
      return
    }
    if (
      snapshot.project_id !== currentReport.project_id ||
      !sameProjectId(snapshot.project_id, projectId) ||
      snapshot.report_version_id !== reportVersionId ||
      !Number.isInteger(snapshot.report_state_version_before) ||
      !Number.isInteger(snapshot.report_state_version_after) ||
      snapshot.report_state_version_before !== expectedStateVersion ||
      snapshot.report_state_version_after !== expectedStateVersion + 1
    ) {
      setNotice('最终确认已经返回，但确认快照无法与当前页面报告身份闭合。系统不会自动重复提交；请刷新当前状态。', 'warning')
      return
    }
    approvalSnapshot.value = snapshot
    bundle.value = {
      ...bundle.value,
      report_version: {
        ...bundle.value.report_version,
        lifecycle: 'approved',
        state_version: snapshot.report_state_version_after
      }
    }
    approvalOpen.value = false
    setNotice('本版报告已正式确认。ApprovalSnapshot 已保存；这不代表邮件已发送。', 'success')
  } catch {
    if (authorityRequestIsCurrent(generation, projectId, reportVersionId, expectedStateVersion)) {
      setNotice('最终确认结果暂时无法确认。系统不会自动重复提交；请先刷新当前状态。', 'warning')
    }
  } finally {
    if (generation === authorityGeneration && sameProjectId(props.projectId, projectId)) approvalSaving.value = false
  }
}

function openCancellation() {
  if (!canCancelReanalysis.value) return
  cancellationKey.value = newIdempotencyKey('cancel-reanalysis')
  cancellationBy.value = supplementProvidedBy.value || ''
  cancellationReason.value = ''
  cancellationOpen.value = true
}

async function submitCancellation() {
  if (!canCancelReanalysis.value || !report.value) return
  if (!cancellationBy.value.trim() || !cancellationReason.value.trim()) {
    return setNotice('请填写撤销执行者和撤销原因。', 'warning')
  }
  const generation = authorityGeneration
  const projectId = props.projectId
  const reportId = report.value.report_version_id
  const stateVersion = report.value.state_version
  const payload = {
    expected_report_state_version: stateVersion,
    expected_reanalysis_request_hash: reanalysisBundle.value.reanalysis_request.request_hash,
    expected_replacement_task_identity_hash: replacementTask.value.identity_hash,
    cancelled_by: cancellationBy.value.trim(),
    cancellation_reason: cancellationReason.value.trim(),
    idempotency_key: cancellationKey.value
  }
  cancellationSaving.value = true
  try {
    const result = await cancelReportReanalysis(projectId, reportId, payload)
    if (!authorityRequestIsCurrent(generation, projectId, reportId, stateVersion)) return
    if (!result.ok || result.body?.cancelled !== true) {
      cancellationOpen.value = false
      return setNotice(detailMessage(result.body, '无法撤销，请刷新状态核对；不要重复发送模型请求。'), 'warning')
    }
    cancellationOpen.value = false
    modelSendOpen.value = false
    modelSendPreview.value = null
    const loaded = await loadReview({ keepNotice: true })
    if (loaded) setNotice('未发送的重分析已撤销，原报告已恢复审阅。', 'success')
  } catch {
    if (authorityRequestIsCurrent(generation, projectId, reportId, stateVersion)) {
      cancellationOpen.value = false
      setNotice('未收到撤销结果，请刷新状态核对，暂不重复操作。', 'warning')
    }
  } finally {
    cancellationSaving.value = false
  }
}

function openCorrection() {
  if (!canReanalyze.value) return
  correctionOpen.value = true
}

async function submitReanalysis() {
  if (!report.value || !canReanalyze.value || reanalysisSaving.value) return
  if (!correctionLocation.value.trim()) return setNotice('请填写报告中需要更正的位置。', 'error')
  if (!correctionTruth.value.trim()) return setNotice('请填写经过核对的正确事实。', 'error')
  if (!correctionBasis.value.trim()) return setNotice('请填写更正依据。', 'error')
  if (!correctionSource.value.trim()) return setNotice('请填写更正来源。', 'error')
  if (!correctionRequestedBy.value.trim()) return setNotice('请填写退回重分析的申请人。', 'error')
  reanalysisSaving.value = true
  try {
    const { status, ok, body } = await createReportReanalysis(props.projectId, report.value.report_version_id, {
      expected_report_state_version: report.value.state_version,
      error_location: correctionLocation.value,
      corrected_truth: correctionTruth.value,
      correction_basis: correctionBasis.value,
      correction_source: correctionSource.value,
      requested_by: correctionRequestedBy.value,
      requested_timezone: localTimezone()
    })
    if (!ok || !body?.replacement_task || !body?.reanalysis_request) {
      setNotice(detailMessage(body, status === 409 ? '本版报告状态已经变化，请刷新后再决定是否退回。' : `退回重分析失败（HTTP ${status}）。`), status === 409 ? 'warning' : 'error')
      return
    }
    reanalysisBundle.value = body
    correctionOpen.value = false
    setNotice('已退回重分析：旧报告保留为历史，新任务已经排队；这里没有调用 AI。', 'success')
    await loadReview({ keepNotice: true })
  } catch {
    setNotice('退回请求结果暂时无法确认。系统不会自动重复提交；请先刷新当前状态。', 'warning')
  } finally {
    reanalysisSaving.value = false
  }
}

async function prepareExactModelSend(flow) {
  if (!report.value || modelSendPreparing.value || modelSendExecuting.value) return
  if (flow === 'reanalysis' && report.value.lifecycle !== 'superseded') return setNotice('只有已经退回并创建 replacement task 的报告才能准备 AI 重分析。', 'warning')
  if (flow === 'contradiction' && !canCheckContradiction.value) return setNotice('请先保存当前人工补充，再请求 AI 冲突检查。', 'warning')
  const projectId = props.projectId
  const reportVersionId = report.value.report_version_id
  modelSendPreparing.value = true
  modelSendOpen.value = false
  modelSendPreview.value = null
  modelSendAck.value = false
  try {
    const prepared = flow === 'reanalysis'
      ? await prepareReportReanalysis(projectId, reportVersionId)
      : await prepareReportContradiction(projectId, reportVersionId)
    if (!prepared.ok || !Number.isInteger(prepared.body?.model_call_id)) {
      return setNotice(detailMessage(prepared.body, `AI ${flow === 'reanalysis' ? '重分析' : '冲突检查'}准备失败（HTTP ${prepared.status}）。当前步骤没有发送模型数据。`), prepared.status === 409 ? 'warning' : 'error')
    }
    const modelCallId = prepared.body.model_call_id
    const previewed = flow === 'reanalysis'
      ? await previewReportReanalysisSend(projectId, reportVersionId, modelCallId)
      : await previewReportContradictionSend(projectId, reportVersionId, modelCallId)
    const preview = previewed.body
    if (
      !previewed.ok ||
      preview?.model_call_id !== modelCallId ||
      typeof preview?.data_scope_hash !== 'string' ||
      preview.data_scope_hash.length !== 64 ||
      preview?.authorization_state !== 'awaiting_human_confirmation' ||
      preview?.provider_send_state !== 'not_attempted'
    ) {
      return setNotice(detailMessage(preview, `发送范围无法可靠闭合（HTTP ${previewed.status}）。不会请求真实发送授权。`), 'warning')
    }
    modelSendFlow.value = flow
    modelSendPreview.value = preview
    modelSendAck.value = false
    modelSendOpen.value = true
  } catch {
    setNotice('AI 发送范围准备结果暂时无法确认。系统不会自动发送或自动重试。', 'warning')
  } finally {
    modelSendPreparing.value = false
  }
}

async function confirmExactModelSend() {
  if (!modelSendOpen.value || !modelSendAck.value || modelSendExecuting.value || !modelSendPreview.value) return
  const preview = modelSendPreview.value
  const flow = modelSendFlow.value
  const projectId = props.projectId
  const reportVersionId = report.value?.report_version_id
  if (!reportVersionId || !Number.isInteger(preview.model_call_id) || typeof preview.data_scope_hash !== 'string') return
  modelSendExecuting.value = true
  try {
    const authorize = flow === 'reanalysis'
      ? await authorizeReportReanalysisSend(projectId, reportVersionId, {
          model_call_id: preview.model_call_id,
          data_scope_hash: preview.data_scope_hash,
          human_confirmed: true
        })
      : await authorizeReportContradictionSend(projectId, reportVersionId, {
          model_call_id: preview.model_call_id,
          data_scope_hash: preview.data_scope_hash,
          human_confirmed: true
        })
    if (!authorize.ok || authorize.body?.authorization_state !== 'authorized_once' || authorize.body?.data_scope_hash !== preview.data_scope_hash) {
      setNotice(detailMessage(authorize.body, `真实发送授权失败（HTTP ${authorize.status}）。不会执行模型调用。`), 'warning')
      return
    }
    const executed = flow === 'reanalysis'
      ? await executeReportReanalysis(projectId, reportVersionId, {
          model_call_id: preview.model_call_id,
          data_scope_hash: preview.data_scope_hash
        })
      : await executeReportContradiction(projectId, reportVersionId, {
          model_call_id: preview.model_call_id,
          data_scope_hash: preview.data_scope_hash
        })
    modelSendOpen.value = false
    modelSendAck.value = false
    modelSendPreview.value = null
    if (!executed.ok) {
      setNotice(detailMessage(executed.body, `模型执行结果无法可靠确认（HTTP ${executed.status}）。系统不会盲目重发；请先刷新状态。`), 'warning')
      return
    }
    setNotice(
      flow === 'reanalysis'
        ? 'AI 重分析已完成并形成新的报告版本。请继续审阅新报告；系统没有执行第二次发送。'
        : 'AI 冲突检查已完成并持久化验证结果；系统没有执行第二次发送。',
      'success'
    )
    await loadReview({ keepNotice: true })
  } catch {
    modelSendOpen.value = false
    modelSendAck.value = false
    setNotice('真实模型执行结果暂时无法确认。系统不会自动重试或切换 provider；请先刷新当前状态。', 'warning')
  } finally {
    modelSendExecuting.value = false
  }
}

function goBoard() {
  if (!canGoBoard.value) { setNotice('当前项目的正式确认凭据尚未闭合，不能进入正式安心看板。', 'warning'); return }
  window.location.hash = `#/projects/${props.projectId}/board`
}
function goTask() { window.location.hash = `#/projects/${props.projectId}/task` }

onMounted(() => loadReview())
watch(() => props.projectId, () => loadReview())
</script>

<template>
  <section class="page" aria-label="报告审阅">
    <header class="page-head">
      <div>
        <span class="eyebrow">审阅报告</span>
        <h1>报告审阅</h1>
        <p>先看 AI 结论，再补充项目经理掌握的事实；有错误就退回重分析，确认依据闭合后再正式确认。</p>
      </div>
      <div class="head-actions">
        <button class="btn" type="button" :disabled="viewState === 'loading'" @click="loadReview()">刷新状态</button>
        <button v-if="approvalAuthorityBroken" class="btn primary" type="button" disabled>正式确认凭据不可用</button>
        <button v-else-if="alreadyApproved" class="btn primary" type="button" @click="goBoard">下一步：查看安心看板 →</button>
      </div>
    </header>

    <div v-if="notice" :class="['notice', noticeType]" role="status"><b>{{ notice }}</b></div>

    <section v-if="viewState === 'loading'" class="state-card"><span class="spinner"></span><div><b>正在读取当前报告</b><small>只读加载，不会修改报告或触发 AI。</small></div></section>
    <section v-else-if="viewState !== 'loaded'" class="state-card"><span class="state-icon">{{ viewState === 'empty' ? '○' : '!' }}</span><div><b>{{ viewState === 'empty' ? '暂无可审阅报告' : '当前报告无法可靠读取' }}</b><small>{{ loadDetail }}</small></div><button class="btn" type="button" @click="loadReview()">重新读取</button></section>

    <template v-else-if="bundle && report">
      <section v-if="reanalysisBundle?.cancelled" class="notice">
        <b>已撤销未发送的重分析</b>
        <p>原报告和退回记录均已保留，可以继续补充和确认。该报告的历史重分析链已经关闭。</p>
      </section>
      <section v-else-if="reanalysisBundle" class="reanalysis-banner">
        <div class="seal rework">↺</div>
        <div>
          <span>本版已经退回重分析</span>
          <h2>{{ replacementTask?.state === 'queued' ? '重分析任务等待处理' : '重分析任务状态待核对' }}</h2>
          <p>旧报告继续保留为历史；替换任务 {{ replacementTask?.local_task_id || '已创建' }} 将沿用同一 EvidenceSnapshot 重新分析。</p>
        </div>
        <div class="head-actions">
          <button class="btn primary" type="button" :disabled="modelSendPreparing || modelSendExecuting || cancellationSaving" @click="prepareExactModelSend('reanalysis')">{{ modelSendPreparing ? '正在闭合发送范围…' : '准备 AI 重分析发送范围' }}</button>
          <button v-if="canCancelReanalysis" class="btn" type="button" @click="openCancellation">撤销重分析并恢复审阅</button>
          <button class="btn warning" type="button" :disabled="modelSendExecuting" @click="goTask">回到研发分析 →</button>
        </div>
      </section>

      <section v-if="alreadyApproved" class="approval-banner">
        <div class="seal">✓</div>
        <div>
          <span>本版已经正式确认</span>
          <h2>{{ approvalSnapshot?.confirmed_by || '已确认' }} · {{ formatTime(approvalSnapshot?.confirmed_at) }}</h2>
          <p>这版已经进入只读状态。ApprovalSnapshot 绑定当前报告与确认依据；邮件发送仍是独立后续动作。</p>
        </div>
      </section>

      <div class="version-strip">
        <div><span>报告版本</span><b>V{{ report.version_no }}</b></div>
        <div><span>审阅状态</span><b>{{ lifecycleLabel }}</b></div>
        <div><span>确认前校验</span><b :class="report.lifecycle === 'superseded' ? '' : latestValidation?.state === 'passed' ? 'good' : latestValidation?.state === 'blocked' ? 'bad' : ''">{{ validationLabel }}</b></div>
      </div>

      <section v-if="bundle.progress_preview && !progressModules.length" class="card" role="status">
        <h2>累计进度暂时无法核对</h2>
        <p>{{ typeof bundle.progress_preview.message === 'string' ? bundle.progress_preview.message : '累计进度资料不完整，请重新读取报告。下方仍可查看本次分析原文。' }}</p>
      </section>
      <section v-if="progressModules.length" class="card progress-review" aria-label="累计功能进度核对" data-testid="progress-review">
        <h2>确认后的完整功能进度</h2>
        <p>未涉及的功能沿用上次进度。本次活动明细单独列在下方，不能把补测试误当成功能退回开发。</p>
        <div class="progress-table-wrap"><table class="progress-table">
          <thead><tr><th>功能</th><th>上次进度</th><th>本次确认后</th><th>核对与更正</th></tr></thead>
          <tbody><tr v-for="row in progressModules" :key="row.module_id">
            <th scope="row">{{ row.name }}</th><td>{{ row.previous_stage }}</td>
            <td>{{ progressEdits[row.module_id]?.enabled ? progressEdits[row.module_id].stage : row.stage }}</td>
            <td>
              <label v-if="canApprove" class="progress-toggle"><input type="checkbox" :checked="Boolean(progressEdits[row.module_id]?.enabled)" :disabled="!canApprove || approvalSaving" @change="toggleProgressCorrection(row, $event.target.checked)" />更正状态</label>
              <span v-else>只读</span>
              <fieldset v-if="progressEdits[row.module_id]?.enabled && canApprove" :disabled="!canApprove || approvalSaving" class="progress-edit">
                <legend>{{ row.name }}的状态更正</legend>
                <label>确认后的状态<select v-model="progressEdits[row.module_id].stage"><option v-for="stage in PROGRESS_STAGES" :key="stage" :value="stage">{{ stage }}</option></select></label>
                <label>更正原因（必填）<textarea v-model="progressEdits[row.module_id].reason" maxlength="2000" /></label>
                <span>选择本次代码依据（至少一项）</span>
                <label v-for="ref in progressGitRefs" :key="ref.evidence_id" class="progress-toggle"><input v-model="progressEdits[row.module_id].evidence_ids" type="checkbox" :value="ref.evidence_id" />{{ evidenceBinding(ref) || ref.evidence_id }}</label>
                <p v-if="!progressGitRefs.length">本次没有可选代码依据，不能提交状态更正。</p>
              </fieldset>
            </td>
          </tr></tbody>
        </table></div>
      </section>

      <section class="card review-step">
        <div class="step-head"><span class="step-number">1</span><div><span class="eyebrow">先看结论</span><h2>本次活动与分析</h2><p>以下保留 AI 原文，与上方累计功能进度分开核对。</p></div><span class="pill">不可直接修改</span></div>
        <p class="summary">{{ aiContent?.plain_summary || '当前结构化结果没有白话摘要。' }}</p>
        <div v-if="featureProgress.length" class="feature-list">
          <article v-for="(item, index) in featureProgress" :key="index">
            <div><b>{{ item.feature || item.module || `功能项 ${index + 1}` }}</b><span>{{ item.stage || item.status || '暂时无法确认' }}</span></div>
            <p>{{ item.summary || item.description || '暂无补充说明。' }}</p>
            <small v-if="item.evidence_ids?.length">证据：{{ item.evidence_ids.join('、') }}</small>
          </article>
        </div>
      </section>

      <section class="card review-step">
        <div class="step-head"><span class="step-number">2</span><div><span class="eyebrow">再补人工事实</span><h2>项目经理人工补充</h2><p>人工来源单独保存，不改写 AI 原文；保存后可以直接确认，AI 冲突检查为可选辅助。</p></div><span class="pill">独立版本</span></div>
        <template v-if="alreadyApproved">
          <div class="readonly-supplement">
            <b>{{ currentSupplement ? `人工补充 V${currentSupplement.version_no}` : '本版没有人工补充' }}</b>
            <span v-if="currentSupplement">{{ currentSupplement.provided_by }} · {{ formatTime(currentSupplement.provided_at) }}</span>
            <p v-if="currentSupplement">{{ currentSupplement.content }}</p>
            <small>报告已正式确认，人工补充保持只读。</small>
          </div>
        </template>
        <template v-else>
          <div v-if="currentSupplement" class="supp-meta">当前补充 V{{ currentSupplement.version_no }} · {{ currentSupplement.provided_by }} · {{ formatTime(currentSupplement.provided_at) }}</div>
          <div class="supplement-grid">
            <label>补充类型<select v-model="supplementSourceType" :disabled="!supplementWritable || supplementSaving"><option value="pm_external_fact">项目经理外部事实</option><option value="pm_correction">项目经理更正</option></select></label>
            <label>提供人<input v-model="supplementProvidedBy" maxlength="128" placeholder="填写提供人" :disabled="!supplementWritable || supplementSaving" /></label>
            <label class="full">补充内容<textarea v-model="supplementText" maxlength="20000" placeholder="填写需要与 AI 原文分开保留的事实。" :disabled="!supplementWritable || supplementSaving"></textarea></label>
          </div>
          <div class="step-actions">
            <button class="btn" type="button" :disabled="!supplementWritable || supplementSaving" @click="saveSupplement">{{ supplementSaving ? '保存中…' : '保存人工补充' }}</button>
            <button v-if="currentSupplement" class="btn primary" type="button" :disabled="!canCheckContradiction || modelSendPreparing || modelSendExecuting" @click="prepareExactModelSend('contradiction')">{{ modelSendPreparing ? '正在闭合发送范围…' : '可选：让 AI 检查补充冲突' }}</button>
          </div>
        </template>
      </section>

      <div class="decision-grid">
        <section class="card review-step correction-card">
          <div class="step-head"><span class="step-number">3</span><div><span class="eyebrow">发现错误</span><h2>退回重分析</h2><p>如果 AI 结论有事实错误，保留旧报告并创建新的重分析任务；这里不会调用 AI。</p></div></div>
          <template v-if="report.lifecycle === 'superseded'">
            <div class="pending-box"><b>本版已退回</b><span>新任务已经排队，旧报告保留为历史，本版不再进入最终确认。</span></div>
            <button class="btn warning" type="button" @click="goTask">回到研发分析 →</button>
          </template>
          <template v-else>
            <button class="btn warning" type="button" :disabled="!canReanalyze" @click="openCorrection">退回重分析</button>
          </template>
        </section>

        <section class="card review-step gate-card">
          <div class="step-head"><span class="step-number">4</span><div><span class="eyebrow">最后确认</span><h2>{{ approvalAuthorityBroken ? '正式确认凭据异常' : alreadyApproved ? '已正式确认' : '确认前校验' }}</h2><p>确认时自动做本地完整性检查；正常的 Git / PRD / Profile 演进只提醒，不阻断。</p></div><span :class="['status', alreadyApproved ? 'passed' : approvalAuthorityBroken ? 'blocked' : latestValidation?.state || 'pending']">{{ alreadyApproved ? '已确认' : approvalAuthorityBroken ? '阻断' : validationLabel }}</span></div>
          <div v-if="approvalAuthorityBroken" class="blockers"><div><b>!</b><span>ReportVersion 显示已确认，但当前无法读取并验证对应 ApprovalSnapshot。系统保持关闭，不允许进入正式安心看板。</span></div></div>
          <div v-else-if="alreadyApproved" class="pass"><b>✓ 本版确认完成</b><span>ApprovalSnapshot 已冻结当前报告与确认依据；正式发送仍是后续独立步骤。</span></div>
          <div v-else-if="report.lifecycle === 'superseded'" class="pending-box"><b>本版已失效</b><span>已经退回重分析，等待新任务生成新的报告版本。</span></div>
          <div v-else-if="hardValidationBlockers.length" class="blockers"><div v-for="(item, index) in hardValidationBlockers" :key="item.code || index"><b>!</b><span>{{ blockerText(item) }}</span></div></div>
          <div v-else-if="validationAdvisories.length" class="pending-box"><b>有提醒，但不阻断确认</b><span>{{ validationAdvisories.map(advisoryText).join('；') }}</span></div>
          <div v-else-if="validationFresh" class="pass"><b>✓ 本地完整性检查已通过</b><span>Git / PRD / Profile 后续演进只作提醒，不会阻断这份冻结报告。</span></div>
          <div v-else class="pending-box"><b>可以直接确认</b><span>点击“确认本版报告”时会自动完成本地 durable integrity 检查，无需单独跑一遍校验。</span></div>
          <div class="step-actions">
            <button v-if="canApprove" class="btn primary" type="button" @click="openApproval">确认本版报告</button>
          </div>
          <div v-if="currentApprovalSnapshot" class="approval-mini"><span>ApprovalSnapshot</span><b>{{ currentApprovalSnapshot.confirmed_by }}</b><small>{{ formatTime(currentApprovalSnapshot.confirmed_at) }}</small><code>{{ compactHash(currentApprovalSnapshot.approval_snapshot_hash) }}</code></div>
        </section>
      </div>

      <details class="evidence-details">
        <summary>查看确认依据与技术详情</summary>
        <div class="evidence-detail-grid">
          <section>
            <span class="eyebrow">Git 客观事实</span><h3>本次冻结代码范围</h3>
            <div class="fact-grid"><div><span>分支</span><b>{{ gitFacts.branch || '—' }}</b></div><div><span>起点</span><code>{{ compactHash(gitFacts.from_commit) }}</code></div><div><span>终点</span><code>{{ compactHash(gitFacts.to_commit) }}</code></div><div><span>Git Snapshot</span><b>#{{ gitFacts.git_snapshot_id || '—' }}</b></div></div>
            <p class="helper">这里是冻结代码事实，不把提交数量或代码行数解释成项目完成度。</p>
          </section>
          <section>
            <span class="eyebrow">证据引用</span><h3>EvidenceSnapshot #{{ report.evidence_snapshot_id }}</h3>
            <div v-if="evidenceRefs.length" class="evidence-list"><div v-for="(item, index) in evidenceRefs" :key="item.evidence_id || index"><code>{{ item.evidence_id || `E${index + 1}` }}</code><span>{{ item.type || item.source_type || '冻结证据' }}</span><b>{{ evidenceBinding(item) }}</b></div></div>
            <div v-else class="empty-inline">当前 AI 结果没有逐项 evidence_id，不会虚构证据行。</div>
          </section>
        </div>
        <div class="technical-meta"><span>实际模型</span><code>{{ aiRaw?.actual_model || aiRaw?.model_id || '—' }}</code><span>Provider</span><code>{{ aiRaw?.provider || '—' }}</code><span>Model version</span><code>{{ aiRaw?.model_version || '—' }}</code><span>Result hash</span><code>{{ compactHash(aiRaw?.validated_result_hash) }}</code></div>
      </details>
    </template>

    <div v-if="modelSendOpen && modelSendPreview" class="modal-backdrop" @click.self="!modelSendExecuting && (modelSendOpen = false)">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="model-send-title">
        <div class="modal-head"><div><span class="eyebrow">Exact Human Send Authorization</span><h2 id="model-send-title">确认 {{ modelSendFlowLabel }} 的发送范围</h2></div><button class="icon" aria-label="关闭" :disabled="modelSendExecuting" @click="modelSendOpen = false">×</button></div>
        <div class="confirm-summary"><div><span>Provider</span><b>{{ modelSendPreview.provider }}</b></div><div><span>模型</span><b>{{ modelSendPreview.model_id }}</b></div><div><span>授权</span><b>一次 / 最长 5 分钟</b></div></div>
        <div class="warning-box"><b>即将进入真实模型发送门</b><span>{{ modelSendScopeSummary }}。准备和预览阶段没有发送数据；只有你在这里确认后，系统才会为下面这个 exact scope 开一次性发送权。</span></div>
        <div class="technical-meta"><span>ModelCall</span><code>#{{ modelSendPreview.model_call_id }}</code><span>Data scope</span><code>{{ compactHash(modelSendPreview.data_scope_hash) }}</code><span>Request envelope</span><code>{{ compactHash(modelSendPreview.request_envelope_hash) }}</code></div>
        <label class="ack"><input v-model="modelSendAck" type="checkbox" :disabled="modelSendExecuting" /><span>我已核对上面的数据范围，并明确同意仅把这个 exact scope 发送给 {{ modelSendPreview.provider }} 一次。范围变化、授权过期或结果未知时都不得自动重发。</span></label>
        <div class="modal-actions"><button class="btn" :disabled="modelSendExecuting" @click="modelSendOpen = false">取消</button><button class="btn primary" :disabled="modelSendExecuting || !modelSendAck" @click="confirmExactModelSend">{{ modelSendExecuting ? '执行中…' : '确认范围并只发送一次' }}</button></div>
      </section>
    </div>

    <div v-if="approvalOpen" class="modal-backdrop" @click.self="approvalOpen = false">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <div class="modal-head"><div><span class="eyebrow">不可变确认快照</span><h2 id="approval-title">确认本版报告</h2></div><button class="icon" aria-label="关闭" @click="approvalOpen = false">×</button></div>
        <div class="confirm-summary"><div><span>报告</span><b>V{{ report?.version_no }} / #{{ report?.report_version_id }}</b></div><div><span>本地检查</span><b class="good">{{ validationLabel }}</b></div><div><span>下一步</span><b>安心看板</b></div></div>
        <label>最终确认人<input v-model="approverName" maxlength="120" placeholder="填写姓名；会显示在正式报告中" /></label>
                <div class="warning-box"><b>这是重大确认动作</b><span>确认后会生成不可修改的 ApprovalSnapshot，但不会自动发送邮件，也不会把“已确认”冒充成“已发送”。安心看板模板由下一步单独负责。</span></div>
        <div class="modal-actions"><button class="btn" @click="approvalOpen = false">取消</button><button class="btn primary" :disabled="approvalSaving" @click="confirmApproval">{{ approvalSaving ? '确认中…' : '正式确认本版报告' }}</button></div>
      </section>
    </div>

    <div v-if="cancellationOpen" class="modal-backdrop" @click.self="!cancellationSaving && (cancellationOpen = false)">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="cancellation-title">
        <div class="modal-head"><h2 id="cancellation-title">恢复原报告审阅</h2></div>
        <p>服务端将核对重分析确实没有发送，关闭替代任务并撤销相关发送许可。已开始执行或结果不确定时不能恢复。</p>
        <label>撤销执行者<input v-model="cancellationBy" maxlength="128" /></label>
        <label>撤销原因<textarea v-model="cancellationReason" maxlength="2000"></textarea></label>
        <div class="modal-actions">
          <button class="btn" :disabled="cancellationSaving" @click="cancellationOpen = false">返回</button>
          <button class="btn primary" :disabled="cancellationSaving" @click="submitCancellation">{{ cancellationSaving ? '正在核对并撤销…' : '确认撤销并恢复审阅' }}</button>
        </div>
      </section>
    </div>
    <div v-if="correctionOpen" class="modal-backdrop" @click.self="correctionOpen = false">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="reanalysis-title">
        <div class="modal-head"><div><span class="eyebrow">Correction / Reanalysis</span><h2 id="reanalysis-title">退回重分析</h2></div><button class="icon" aria-label="关闭" @click="correctionOpen = false">×</button></div>
        <div class="warning-box"><b>退回只创建新的重分析任务</b><span>旧报告会保留并标记为已被替代；提交这里不会调用 AI，也不会假装已经生成新报告。</span></div>
        <label>错误位置<input v-model="correctionLocation" maxlength="20000" placeholder="例如：支付模块 / 测试状态" /></label>
        <label>正确事实<textarea v-model="correctionTruth" maxlength="20000" placeholder="填写项目经理已经核对的正确事实。"></textarea></label>
        <label>更正依据<textarea v-model="correctionBasis" maxlength="20000" placeholder="说明依据哪一项冻结证据或人工确认。"></textarea></label>
        <label>更正来源<input v-model="correctionSource" maxlength="20000" placeholder="例如：项目经理核对冻结证据" /></label>
        <label>申请人<input v-model="correctionRequestedBy" maxlength="200" placeholder="填写项目经理姓名或内部标识" /></label>
                <div class="modal-actions"><button class="btn" :disabled="reanalysisSaving" @click="correctionOpen = false">取消</button><button class="btn warning" :disabled="reanalysisSaving" @click="submitReanalysis">{{ reanalysisSaving ? '提交中…' : '退回并创建重分析任务' }}</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.progress-review h2{font-size:var(--ui-text-section);margin:0 0 12px}.progress-review>p{color:var(--ui-text-secondary,#6e6e73);line-height:1.6}.progress-table-wrap{overflow-x:auto}.progress-table{width:100%;border-collapse:collapse;min-width:650px}.progress-table th,.progress-table td{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid #e2e6eb;font-size:14px}.progress-table thead{background:#f5f5f7}.progress-edit{display:grid;gap:10px;border:1px solid #d2d2d7;border-radius:12px;padding:12px;margin-top:10px;min-width:240px}.progress-edit label{display:grid;gap:6px}.progress-edit select,.progress-edit textarea{font:inherit;width:100%;padding:8px;border:1px solid #d2d2d7;border-radius:8px}.progress-toggle{display:flex!important;align-items:flex-start;gap:8px!important;font-size:13px}.progress-toggle input{width:auto;flex-shrink:0}.progress-edit :focus-visible,.progress-toggle input:focus-visible{outline:2px solid #007aff;outline-offset:2px}

*{box-sizing:border-box}.page{display:grid;gap:14px;color:#1f2937}.page-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.page-head h1{margin:4px 0 6px;font-size:var(--ui-text-page);font-weight:var(--ui-weight-strong);line-height:1.4}.page-head p{max-width:780px;margin:0;color:#748096;font-size:var(--ui-text-helper);line-height:1.6}.eyebrow{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.08em;color:#526be7}.head-actions,.step-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.btn{min-height:38px;padding:8px 13px;border:1px solid #dbe1ea;border-radius:9px;background:#fff;color:#435066;font:inherit;font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong)}.btn.primary{border-color:#4f6ef7;background:#4f6ef7;color:#fff}.btn.warning{background:#fff9ed;border-color:#eed6a7;color:#93631c}.btn:disabled{opacity:.48;cursor:not-allowed}.notice{padding:11px 14px;border-radius:10px;border:1px solid #dce3ed;background:#f5f7fb;font-size:var(--ui-text-helper)}.notice.success{background:#eef9f5;border-color:#c9e9dd;color:#176b59}.notice.error{background:#fff1f2;border-color:#f0c4c8;color:#9c3039}.notice.warning{background:#fff9ed;border-color:#efd8a8;color:#805a19}.state-card{min-height:190px;display:flex;align-items:center;justify-content:center;gap:14px;padding:28px;border:1px solid #e1e6ee;border-radius:16px;background:#fff}.state-card div{display:grid;gap:4px}.state-card small{color:#7a8597}.spinner{width:30px;height:30px;border:3px solid #e5e9f1;border-top-color:#4f6ef7;border-radius:50%}.state-icon{font-size:26px}.approval-banner,.reanalysis-banner{display:grid;grid-template-columns:52px 1fr auto;align-items:center;gap:16px;padding:18px 20px;border-radius:16px;background:linear-gradient(135deg,#f4fcf8,#fff)}.approval-banner{border:1px solid #bfe4d7}.reanalysis-banner{border:1px solid #ead29c;background:linear-gradient(135deg,#fffaf0,#fff)}.seal{width:48px;height:48px;display:grid;place-items:center;border-radius:15px;background:#dff4eb;color:#12705a;font-size:22px;font-weight:var(--ui-weight-strong)}.seal.rework{background:#fff0cc;color:#966315}.approval-banner span,.reanalysis-banner span{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);color:#16866f}.reanalysis-banner span{color:#966315}.approval-banner h2,.reanalysis-banner h2{margin:3px 0;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.approval-banner p,.reanalysis-banner p{margin:0;color:#66766f;font-size:var(--ui-text-caption)}.version-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.version-strip>div{display:grid;gap:3px;padding:11px 13px;border:1px solid #e2e7ee;border-radius:11px;background:#fff}.version-strip span,.fact-grid span{color:#8791a2;font-size:var(--ui-text-caption)}.version-strip b{font-size:var(--ui-text-body)}.good{color:#16866f}.bad{color:#c7484f}.card{border:1px solid #e1e6ee;border-radius:15px;background:#fff;box-shadow:0 7px 20px rgba(31,45,81,.045)}.review-step{padding:18px}.step-head{display:grid;grid-template-columns:34px minmax(0,1fr) auto;gap:11px;align-items:start;margin-bottom:12px}.step-number{width:32px;height:32px;display:grid;place-items:center;border-radius:10px;background:#eef2ff;color:#4058cf;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.step-head h2{margin:2px 0 3px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.step-head p{margin:0;color:#7b8698;font-size:var(--ui-text-caption);line-height:1.55}.pill,.status{display:inline-flex;padding:4px 8px;border-radius:999px;background:#f1f4f8;color:#69768a;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.status.passed{background:#e8f7f1;color:#14705b}.status.blocked{background:#fff0f1;color:#b13d47}.summary{padding:13px 15px;border-left:3px solid #4f6ef7;background:#f8f9ff;color:#44516a;font-size:var(--ui-text-helper);line-height:1.7}.feature-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.feature-list article{padding:12px 13px;border:1px solid #e5e9f0;border-radius:10px}.feature-list article>div{display:flex;justify-content:space-between;gap:10px}.feature-list article span{color:#5067d8;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.feature-list p{margin:6px 0;color:#586579;font-size:var(--ui-text-caption)}.feature-list small{color:#8993a3}.supplement-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.supplement-grid label,.modal label{display:grid;gap:5px;color:#5e6a7d;font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong)}.supplement-grid .full{grid-column:1/-1}.supplement-grid input,.supplement-grid select,.supplement-grid textarea,.modal input,.modal textarea{width:100%;border:1px solid #dce2eb;border-radius:8px;padding:9px 10px;background:#fff;font:inherit;font-size:var(--ui-text-body)}.supplement-grid textarea,.modal textarea{min-height:92px;resize:vertical}.supp-meta{margin-bottom:10px;padding:8px 10px;border-radius:8px;background:#f8fafc;color:#748096;font-size:var(--ui-text-caption)}.readonly-supplement{display:grid;gap:5px;padding:13px;border-radius:10px;background:#f8fafc}.readonly-supplement b{font-size:var(--ui-text-body)}.readonly-supplement span,.readonly-supplement small{color:#7a8597;font-size:var(--ui-text-caption)}.readonly-supplement p{margin:5px 0;color:#4f5d73;font-size:var(--ui-text-caption);line-height:1.6}.decision-grid{display:grid;grid-template-columns:minmax(0,.85fr) minmax(0,1.15fr);gap:12px}.correction-card{background:#fffdf8}.gate-card{border-color:#d7def7}.blockers{display:grid;gap:7px;margin:10px 0}.blockers>div{display:flex;gap:8px;padding:9px;border-radius:8px;background:#fff2f3;color:#9e3942;font-size:var(--ui-text-caption)}.pass,.pending-box{display:grid;gap:3px;margin:10px 0;padding:11px;border-radius:9px;font-size:var(--ui-text-caption)}.pass{background:#eef9f5;color:#176b59}.pending-box{background:#f7f8fb;color:#68758a}.approval-mini{display:grid;gap:4px;margin-top:12px;padding:12px;border:1px solid #c7e7dc;border-radius:10px;background:#f2fbf8}.approval-mini span{color:#16866f;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.approval-mini code{font-size:var(--ui-text-caption)}.evidence-details{border:1px solid #e1e6ee;border-radius:14px;background:#fff}.evidence-details summary{padding:14px 16px;cursor:pointer;color:#626f83;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.evidence-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:0 16px 14px}.evidence-detail-grid section{padding:13px;border-radius:10px;background:#f8fafc}.evidence-detail-grid h3{margin:4px 0 10px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.fact-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.fact-grid>div{display:grid;gap:4px;padding:9px;background:#fff;border-radius:8px}.fact-grid b,.fact-grid code{font-size:var(--ui-text-body);word-break:break-all}.helper{display:block;margin-top:9px;color:#828d9f;font-size:var(--ui-text-caption);line-height:1.6}.evidence-list{display:grid;gap:6px}.evidence-list>div{display:grid;grid-template-columns:80px 105px 1fr;gap:8px;padding:8px;background:#fff;border-radius:8px;font-size:var(--ui-text-caption)}.empty-inline{padding:16px;border:1px dashed #dfe4ec;border-radius:9px;color:#8993a3;font-size:var(--ui-text-caption)}.technical-meta{display:grid;grid-template-columns:100px 1fr 100px 1fr;gap:7px;padding:0 16px 16px;font-size:var(--ui-text-caption)}.technical-meta span{color:#8791a2}.technical-meta code{word-break:break-all}.modal-backdrop{position:fixed;inset:0;z-index:60;display:grid;place-items:center;padding:20px;background:rgba(20,28,43,.48)}.modal{width:min(620px,100%);max-height:calc(100vh - 40px);overflow:auto;padding:20px;border-radius:16px;background:#fff;box-shadow:0 24px 70px rgba(15,23,42,.25)}.modal-head{display:flex;justify-content:space-between;gap:12px}.modal-head h2{margin:4px 0;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.icon{width:34px;height:34px;border:0;border-radius:8px;background:#f2f4f7;font-size:20px}.confirm-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}.confirm-summary>div{display:grid;gap:3px;padding:10px;background:#f8fafc;border-radius:9px}.confirm-summary span{font-size:var(--ui-text-caption);color:#8791a2}.confirm-summary b{font-size:var(--ui-text-body)}.ack{display:flex!important;align-items:flex-start;grid-template-columns:none!important;gap:9px!important;padding:11px;border:1px solid #dfe4ec;border-radius:9px}.ack input{width:auto!important;margin-top:2px}.ack span{font-size:var(--ui-text-caption);font-weight:600;line-height:1.6}.warning-box{display:grid;gap:4px;margin-top:12px;padding:11px;border-radius:9px;background:#fff8e8;color:#805a19;font-size:var(--ui-text-caption)}.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}@media(max-width:900px){.decision-grid,.evidence-detail-grid,.feature-list{grid-template-columns:1fr}.technical-meta{grid-template-columns:100px 1fr}}@media(max-width:680px){.page-head{flex-direction:column}.head-actions{justify-content:flex-start}.version-strip,.confirm-summary,.fact-grid,.supplement-grid{grid-template-columns:1fr}.step-head{grid-template-columns:34px 1fr}.step-head>.pill,.step-head>.status{grid-column:2}.approval-banner,.reanalysis-banner{grid-template-columns:48px 1fr}.reanalysis-banner .btn{grid-column:1/-1}.evidence-list>div{grid-template-columns:1fr}.supplement-grid .full{grid-column:auto}}
</style>
