<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { formatTime } from '../utils/projectFormat.js'
import {
  createProfileCandidate,
  createAtlasTask, getLatestAtlasTask, getAtlasTask, resumeAtlasTask, isAtlasTask, getAtlasReport,
  getAtlasProfileResult, atlasReportErrorMessage,
  formatProjectStateBaselineError,
  generateProfileCandidate,
  getProjectProfile,
  getProjectStateBaselineStatus,
  listProjectProfiles,
  preflightAtlasBaseline,
  updateProfileCandidate,
  confirmProfileCandidate
} from '../api/projectProfiles.js'
import { plannedModules, implementationLabel, profileFormToContent } from '../features/project-profile/projectProfileForm.js'
import { useProjectProfilePanel } from '../features/project-profile/useProjectProfilePanel.js'
import { summarizeRequirementRows, summarizeAtlasRequirements, summarizeModuleMappings, atlasTaskPresentation } from '../features/project-profile/projectProfilePresentation.js'

const props = defineProps({ projectId: { type: [Number, String], default: null } })
const api = { createProfileCandidate, getProjectProfile, listProjectProfiles, updateProfileCandidate, confirmProfileCandidate }
const {
  panelState, viewMode, serverProfile, history, activeConfirmed, historyViewing,
  message, messageType, saving, confirming, creating, conflict, missingList, collapsedModules,
  form, hasUnsaved, formLocked, confirmDisabled, loadPanel, createFromConfirmed, saveCandidate,
  reloadServerVersion, confirmCandidate, viewHistory, returnToCurrent, addModule, removeModule,
  toggleModule, addPath, removePath
} = useProjectProfilePanel({ projectId: computed(() => props.projectId), api })

const generationAuthorized = ref(false)
const includeImplementation = ref(false)
const displayedContent = computed(() => historyViewing.value?.content || (viewMode.value === "editing" && !serverProfile.value?.generated_baseline_result ? profileFormToContent(form.value) : serverProfile.value?.content))
const generating = ref(false)
const generationUnknown = ref(false)
const generationMessage = ref('')
const generationMessageType = ref('')

const reconciliationAuthorized = ref(false)
const baselinePreflighting = ref(false)
// Presentation identity only: a new task must never inherit the previous-record label.
const preflightPreviousTaskId = ref(null)
const baselineExecuting = ref(false)
const baselineUnknown = ref(false)
const baselinePreflight = ref(null)
const baselineMessage = ref('')
const baselineMessageType = ref('')
const baselineLedgerStatus = ref(null)
let baselineStatusRequest = 0
let baselinePrepareRequest = 0
const legacyUpgrading = ref(false)
const legacyConfirmed = computed(() => !historyViewing.value && serverProfile.value?.status === 'confirmed' && serverProfile.value?.content?.schema_version !== 'project_profile_v2')
const baselineEstablished = computed(() => baselineLedgerStatus.value?.status === 'established')
const moduleOverview = computed(() => summarizeModuleMappings(displayedContent.value))
const confirmedAllUnknown = computed(() => !historyViewing.value && !hasUnsaved.value &&
  baselineEstablished.value && serverProfile.value?.status === 'confirmed' &&
  baselineLedgerStatus.value?.profile_id != null &&
  String(baselineLedgerStatus.value.profile_id) === String(serverProfile.value.id) &&
  moduleOverview.value?.total > 0 && moduleOverview.value.unknown === moduleOverview.value.total)
const collapseReanalysis = computed(() => (baselineCandidatePending.value || baselineEstablished.value) && !confirmedAllUnknown.value)
const awaitingFirstBaseline = computed(() =>
  !historyViewing.value &&
  serverProfile.value?.status === 'confirmed' &&
  serverProfile.value?.content?.schema_version === 'project_profile_v2' &&
  baselineLedgerStatus.value?.status !== 'established'
)
const baselineCandidatePending = computed(() => baselineLedgerStatus.value?.status === 'candidate_pending')
const baselinePlanProfile = computed(() => {
  if (historyViewing.value) return null
  if (serverProfile.value?.status === 'confirmed' && serverProfile.value?.content?.schema_version === 'project_profile_v2') return serverProfile.value
  if (baselineCandidatePending.value && activeConfirmed.value?.status === 'confirmed' && activeConfirmed.value?.id) return activeConfirmed.value
  return null
})
const canReconcile = computed(() => Boolean(baselinePlanProfile.value))
const baselineBusy = computed(() => baselinePreflighting.value || baselineExecuting.value)
const reconciliationDisabled = computed(() =>
  baselineBusy.value ||
  generating.value ||
  generationUnknown.value ||
  baselineUnknown.value ||
  hasUnsaved.value ||
  saving.value ||
  confirming.value ||
  creating.value ||
  !baselinePreflight.value ||
  !reconciliationAuthorized.value
)

function statusLabel(status) { return ({ candidate:'候选', confirmed:'当前有效', superseded:'历史' })[status] || status || '' }
const headingBadge = computed(() => {
  if (baselineExecuting.value) return { text:'正在核对代码证据', cls:'status-info' }
  if (baselinePreflighting.value) return { text:'正在扫描代码', cls:'status-info' }
  if (baselineUnknown.value) return { text:'分析结果暂无法确认', cls:'status-error' }
  if (generating.value) return { text:'AI 正在整理', cls:'status-info' }
  if (generationUnknown.value) return { text:'结果不确定', cls:'status-error' }
  if (historyViewing.value) return { text:`${statusLabel(historyViewing.value.status)} · 历史`, cls:'status-info' }
  if (serverProfile.value?.status === 'candidate' && resultMatchesProfile.value) return { text:'分析已完成 · 待你审核', cls:'status-info' }
  if (viewMode.value === 'editing' && serverProfile.value) return { text:`${form.value.modules.length} 个待确认模块`, cls:'status-info' }
  if (confirmedAllUnknown.value) return { text:'记录已确认 · 实现待核实', cls:'status-info' }
  if (serverProfile.value?.status === 'confirmed') return { text:`功能档案第 ${serverProfile.value.version_no} 版 · 已确认`, cls:'status-active' }
  return { text:'等待生成', cls:'status-info' }
})

function collapseAllModules() {
  collapsedModules.value = form.value.modules.map(() => true)
}

function goSettings() {
  if (props.projectId) window.location.hash = `#/projects/${props.projectId}/settings`
}

function goSetup() {
  if (props.projectId) window.location.hash = `#/projects/${props.projectId}/setup`
}

function goGit() {
  if (props.projectId) window.location.hash = `#/projects/${props.projectId}/git`
}

function shortHead(value) {
  return typeof value === 'string' && value ? `${value.slice(0, 10)}…` : '—'
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / 1024 / 1024).toFixed(1)} MiB`
}

async function loadBaselineStatus() {
  if (!props.projectId) return
  const project = props.projectId, epoch = atlasEpoch, request = ++baselineStatusRequest
  const current = () => !atlasDisposed && epoch === atlasEpoch && project === props.projectId && request === baselineStatusRequest
  try {
    const { ok, body } = await getProjectStateBaselineStatus(project)
    if (!current()) return
    baselineLedgerStatus.value = ok && body ? body : { status: 'unknown', has_confirmed_baseline: false }
  } catch {
    if (!current()) return
    baselineLedgerStatus.value = { status: 'unknown', has_confirmed_baseline: false }
  }
}

function legacyContentToV2(content) {
  const source = content || {}
  return {
    schema_version: 'project_profile_v2',
    project_summary: source.project_summary || '',
    planned_modules: plannedModules(source).map((module) => ({
      client_id: module.client_id,
      name: module.name || '',
      description: module.description || '',
      prd_refs: Array.isArray(module.prd_refs) ? [...module.prd_refs] : [],
      requirements: Array.isArray(module.requirements) ? [...module.requirements] : [],
      exclusions: Array.isArray(module.exclusions) ? [...module.exclusions] : []
    })),
    implementation_mappings: [],
    unplanned_code_features: [],
    domain_glossary: Array.isArray(source.domain_glossary) ? JSON.parse(JSON.stringify(source.domain_glossary)) : [],
    exclude_patterns: Array.isArray(source.exclude_patterns) ? [...source.exclude_patterns] : [],
    notes: source.notes || ''
  }
}

async function upgradeLegacyProfile() {
  if (!props.projectId || !legacyConfirmed.value || legacyUpgrading.value) return
  legacyUpgrading.value = true
  baselineMessage.value = ''
  baselineMessageType.value = ''
  try {
    const { ok, body } = await createProfileCandidate(props.projectId, legacyContentToV2(serverProfile.value.content))
    if (!ok || !body?.id || body.status !== 'candidate') {
      baselineMessage.value = body?.detail?.message || '无法创建新版功能档案候选，原档案未改变。'
      baselineMessageType.value = 'error'
      return
    }
    baselineMessage.value = '新版 V2 功能档案候选已建立。请检查 PRD 引用和模块内容，保存并确认后即可扫描当前 HEAD 全仓代码。'
    baselineMessageType.value = 'success'
    await loadPanel({ keepMessage: true })
    await loadBaselineStatus()
  } catch {
    baselineMessage.value = '无法创建新版功能档案候选，原档案未改变。'
    baselineMessageType.value = 'error'
  } finally {
    legacyUpgrading.value = false
  }
}

function resetBaselinePreparation() {
  baselinePreflight.value = null
  reconciliationAuthorized.value = false
  baselineMessage.value = ''
  baselineMessageType.value = ''
  baselineUnknown.value = false
}

async function generateCandidate() {
  if (generating.value || generationUnknown.value || !props.projectId || !generationAuthorized.value) return
  generating.value = true
  generationMessage.value = ''
  generationMessageType.value = ''
  try {
    const options = { includeImplementation: includeImplementation.value }
    const { ok, body } = await generateProfileCandidate(props.projectId, options)
    if (!ok) {
      generationUnknown.value = body?.detail?.code === 'PROFILE_GENERATION_RESULT_UNKNOWN'
      generationMessage.value = body?.detail?.message || 'AI 候选生成失败，未创建候选。'
      generationMessageType.value = 'error'
      return
    }
    generationUnknown.value = false
    if (!body?.profile?.id || body.profile.status !== 'candidate') {
      generationMessage.value = 'AI 候选生成响应不完整，未进入编辑状态。'
      generationMessageType.value = 'error'
      return
    }
    generationAuthorized.value = false
    generationMessage.value = '第一版功能模块已经整理好。请直接修改不准确的地方，确认无误后再建立当前代码状态基线。'
    generationMessageType.value = 'success'
    await loadPanel()
  } catch {
    generationMessage.value = '无法完成 AI 候选生成请求，未创建候选。'
    generationMessageType.value = 'error'
  } finally {
    generating.value = false
  }
}

function baselineMapping(module) {
  return displayedContent.value?.implementation_mappings?.find(item => item.planned_module_id === module.client_id) || null
}

function baselineMappingLabel(module) {
  if (awaitingFirstBaseline.value) return '待首次全量分析'
  return implementationLabel(baselineMapping(module)?.status)
}

function baselineMappingTone(module) {
  const status = baselineMapping(module)?.status
  if (status === 'implemented') return 'mapping-implemented'
  if (status === 'partial') return 'mapping-partial'
  if (status === 'missing') return 'mapping-missing'
  return 'mapping-unknown'
}

async function prepareStateBaseline() {
  if (!props.projectId || !canReconcile.value || baselineBusy.value || baselineUnknown.value) return
  const project = props.projectId, profile = baselinePlanProfile.value.id, epoch = atlasEpoch, request = ++baselinePrepareRequest
  const current = () => !atlasDisposed && epoch === atlasEpoch && project === props.projectId && profile === baselinePlanProfile.value?.id && request === baselinePrepareRequest
  const previousTaskId = ['succeeded', 'unknown', 'failed_pre_send', 'failed_after_send'].includes(atlasTask.value?.status) ? atlasTask.value.task_id : null
  baselinePreflighting.value = true
  baselineMessage.value = ''
  baselineMessageType.value = ''
  reconciliationAuthorized.value = false
  baselinePreflight.value = null
  try {
    const { status, ok, body } = await preflightAtlasBaseline(project, profile)
    if (!current()) return
    const preflightIdentity = body?.preflight?.preflight_identity_hash
    if (!ok || body?.status !== 'ready' || !body?.preflight || typeof preflightIdentity !== 'string' || !/^[0-9a-f]{64}$/.test(preflightIdentity)) {
      baselineMessage.value = formatProjectStateBaselineError(body, (status ? `首次分析准备失败（HTTP ${status}），本地服务没有返回可识别原因；系统未调用模型。` : '无法连接安心看板本地分析服务，请重新打开应用后再试。'))
      baselineMessageType.value = 'error'
      return
    }
    baselinePreflight.value = body.preflight
    preflightPreviousTaskId.value = previousTaskId
    baselineMessage.value = '代码检查完成。请核对文件范围和分析上限；确认后才会将本次资料发送给 AI。'
    baselineMessageType.value = 'success'
  } catch {
    if (!current()) return
    baselineMessage.value = '无法完成首次分析准备。请检查安心看板本地服务是否仍在运行。'
    baselineMessageType.value = 'error'
  } finally {
    if (current()) baselinePreflighting.value = false
  }
}

const atlasTask = ref(null)
const atlasStalled = ref(false)
const displayedProfile = computed(() => historyViewing.value || serverProfile.value)
const resultTask = ref(null)
const resultError = ref('')
const resultLoading = ref(false)
let resultRequest = 0
let resultDisposed = false
const resultScope = ref('')
function displayedResultScope() {
  return JSON.stringify([props.projectId, displayedProfile.value?.id, displayedProfile.value?.content_hash])
}
async function loadProfileResult() {
  const scope = displayedResultScope(), serial = ++resultRequest
  const project = props.projectId, profile = displayedProfile.value
  resultTask.value = null; resultError.value = ''; resultScope.value = ''; resultLoading.value = false
  if (!project || !profile?.id || !/^[a-f0-9]{64}$/.test(profile.content_hash || '')) return
  const current = () => !resultDisposed && serial === resultRequest && scope === displayedResultScope()
  resultLoading.value = true
  try {
    const response = await getAtlasProfileResult(project, profile.id)
    if (!current()) return
    if (!response.ok) {
      resultError.value = atlasReportErrorMessage(response.body?.detail?.code)
      return
    }
    const task = response.body?.task
    if (task === null) return
    if (!atlasMatchesDisplayedProfile(task)) {
      resultError.value = '分析结果与当前版本不一致，请重新读取。'
      return
    }
    resultScope.value = scope
    resultTask.value = task
  } catch {
    if (current()) resultError.value = '暂时无法读取这版分析结果，请重新读取。不会重新调用模型。'
  } finally { if (current()) resultLoading.value = false }
}
const resultMatchesProfile = computed(() => resultScope.value === displayedResultScope() && atlasMatchesDisplayedProfile(resultTask.value))
const generatedResultReadOnly = computed(() => serverProfile.value?.generated_baseline_result === true ||
  (!historyViewing.value && resultMatchesProfile.value))
const candidateEditingLocked = computed(() => formLocked.value || generatedResultReadOnly.value || resultLoading.value)
function atlasMatchesDisplayedProfile(task) {
  return task?.status === 'succeeded' && task.profile_id != null && displayedProfile.value?.id != null &&
    String(task.profile_id) === String(displayedProfile.value.id) &&
    typeof task.generated_content_hash === 'string' && /^[0-9a-f]{64}$/.test(task.generated_content_hash) &&
    task.generated_content_hash === displayedProfile.value.content_hash
}
const reportBound = computed(() => (historyViewing.value || !hasUnsaved.value) && resultMatchesProfile.value)
const reportBusy = ref(false)
const reportError = ref('')
const reportUrl = ref('')
const reportDialog = ref(null)
const reportDownloadUrls = new Set()
let reportEpoch = 0
function closeReport() {
  reportEpoch++
  reportDialog.value?.close()
  if (reportUrl.value) URL.revokeObjectURL(reportUrl.value)
  reportUrl.value = ''
  reportBusy.value = false
}
function reportFailure(code) {
  return atlasReportErrorMessage(code)
}
async function openAtlasReport(download = false) {
  if (reportBusy.value || !reportBound.value || (download && displayedProfile.value?.status !== 'confirmed')) return
  closeReport()
  const epoch = reportEpoch
  const project = props.projectId, taskId = resultTask.value.task_id, profileId = displayedProfile.value.id
  reportBusy.value = true
  reportError.value = ''
  const current = () => epoch === reportEpoch && reportBound.value && String(props.projectId) === String(project) && resultTask.value?.task_id === taskId
  try {
    const result = await getAtlasReport(project, taskId, { download })
    if (!current()) return
    if (!result.ok) { reportError.value = reportFailure(result.body?.detail?.code); return }
    const url = URL.createObjectURL(result.blob)
    if (download) {
      reportDownloadUrls.add(url)
      const link = document.createElement('a')
      link.href = url
      link.download = `anxin-board-baseline-${project}-${profileId}.html`
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => { URL.revokeObjectURL(url); reportDownloadUrls.delete(url) }, 1000)
    } else {
      reportUrl.value = url
      await nextTick()
      if (current()) reportDialog.value?.showModal()
    }
  } catch {
    if (current()) { closeReport(); reportError.value = reportFailure() }
  } finally { if (epoch === reportEpoch) reportBusy.value = false }
}
onUnmounted(() => {
  resultDisposed = true; resultRequest++
  closeReport()
  for (const url of reportDownloadUrls) URL.revokeObjectURL(url)
  reportDownloadUrls.clear()
})
function atlasRequirementRows(module) {
  if (!resultMatchesProfile.value || (!historyViewing.value && hasUnsaved.value)) return []
  const rows = resultTask.value.module_requirements?.[module.client_id]
  if (!Array.isArray(rows) || !Array.isArray(module.requirements)) return []
  return rows.filter(row => Number.isInteger(row?.requirement_index) && row.requirement_index >= 0 && row.requirement_index < module.requirements.length)
    .map(row => ({ ...row, text: module.requirements[row.requirement_index] }))
}
const requirementOverview = computed(() => summarizeAtlasRequirements(
  resultMatchesProfile.value ? resultTask.value : null, displayedProfile.value, !historyViewing.value && hasUnsaved.value
))
const taskPresentation = computed(() => atlasTaskPresentation(atlasTask.value?.status, Boolean(baselinePreflight.value && preflightPreviousTaskId.value && atlasTask.value?.task_id === preflightPreviousTaskId.value)))
const confirmedTaskRecord = computed(() => !historyViewing.value && !hasUnsaved.value && !baselineUnknown.value &&
  serverProfile.value?.status === 'confirmed' && atlasMatchesDisplayedProfile(atlasTask.value))
const confirmedSuccessMessage = computed(() => confirmedTaskRecord.value && baselineMessageType.value === 'success' &&
  baselineMessage.value === '本次分析结果已人工确认，可继续查看需求和代码依据。')
function moduleRequirementSummary(module) {
  return summarizeRequirementRows(atlasRequirementRows(module), module.requirements?.length)
}
const atlasCoverageSummary = computed(() => {
  const coverage = atlasTask.value?.coverage
  if (!coverage) return ''
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : '—'
  const gap = coverage.gap_check_completed === true ? '遗漏检查已完成' : coverage.gap_check_completed === false ? '遗漏检查未完成' : '遗漏检查状态未知'
  return `已检查 ${count(coverage.inspected_paths)} 条路径 · 尚未解释 ${count(coverage.unexplained_safe_paths)} 条安全路径 · ${gap}。本次证据检查并非穷尽，不代表整仓语义已证明或全部需求已实现。`
})
const atlasFailureCounts = computed(() => {
  if (!atlasTask.value?.status?.startsWith('failed_')) return ''
  const diagnostic = atlasTask.value.failure_diagnostic
  const count = value => Number.isInteger(value) && value >= 0 && value <= 2147483647
  const parts = []
  if (count(diagnostic?.expected_row_count)) parts.push(`每份完整结果要求 ${diagnostic.expected_row_count} 条记录`)
  if (count(diagnostic?.tool_call_count) && diagnostic.tool_call_count >= 1 && diagnostic.tool_call_count <= 16) parts.push(`AI 返回 ${diagnostic.tool_call_count} 份结果`)
  if (atlasExactRowMismatch(diagnostic)) parts.push(`第 ${diagnostic.tool_call_ordinal + 1} 份结果含 ${diagnostic.extra_row_count} 个批外记录键，缺少 ${diagnostic.missing_row_count} 个本批记录键`)
  return parts.length ? parts.join('；') + '。' : ''
})
function atlasExactRowMismatch(diagnostic) {
  const count = value => Number.isInteger(value) && value >= 0 && value <= 2147483647
  return diagnostic?.wire_reason === 'ROWS_INVALID' && diagnostic.wire_rule === 'EXACT_KEYS'
    && count(diagnostic.expected_row_count) && diagnostic.expected_row_count > 0
    && count(diagnostic.tool_call_count) && diagnostic.tool_call_count >= 1 && diagnostic.tool_call_count <= 16
    && count(diagnostic.tool_call_ordinal) && diagnostic.tool_call_ordinal < diagnostic.tool_call_count
    && count(diagnostic.missing_row_count) && diagnostic.missing_row_count <= diagnostic.expected_row_count
    && count(diagnostic.extra_row_count) && (diagnostic.missing_row_count > 0 || diagnostic.extra_row_count > 0)
}
const atlasFailureReason = computed(() => {
  if (!atlasTask.value?.status?.startsWith('failed_')) return ''
  const diagnostic = atlasTask.value.failure_diagnostic
  const httpStatus = diagnostic?.provider_http_status
  if (Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599) {
    const reasons = { 400: '模型服务拒绝本次请求参数', 401: '模型服务身份验证失败，请检查模型配置',
      402: '模型账户余额不足，请检查账户余额', 403: '模型服务拒绝当前调用权限',
      404: '当前选择的模型不可用', 422: '模型请求参数无效', 429: '模型服务当前限流' }
    const reason = reasons[httpStatus] || (httpStatus >= 500 ? '模型服务暂时不可用' : '模型服务拒绝了本次请求')
    return `${reason}（HTTP ${httpStatus}）。分析已停止，不会自动重发。`
  }
  if (atlasTask.value.error_code !== 'PROFILE_GENERATION_STRICT_WIRE_INVALID') return ''
  const reasons = {
    ROWS_INVALID: '返回的记录不完整，或包含本批之外的记录',
    FIELDS_INVALID: '返回记录缺少必填字段，或包含额外字段',
    RATIONALE_INVALID: '分析说明为空、类型不对或超过长度限制',
    INDEX_INVALID: '代码引用编号类型不对或超出本批范围',
    STATUS_INVALID: '返回了无法识别的实现状态',
    STATUS_CITATION_INVALID: '实现状态与代码证据不一致',
    MESSAGES_INVALID: '本地分析请求格式无法校验',
    CONTEXT_INVALID: '本地分析范围无法校验'
  }
  if (typeof diagnostic?.wire_reason !== 'string' || !Object.hasOwn(reasons, diagnostic.wire_reason)) {
    return '这条记录未保存具体校验原因，暂时无法进一步定位；不会自动重发。'
  }
  const precise = {
    RATIONALE_INVALID: {
      STRING_TYPE: '分析说明的类型不是文本', NONBLANK_STRING: '分析说明为空或只有空白字符', STRING_LENGTH: '分析说明超过长度限制'
    },
    INDEX_INVALID: { INTEGER_TYPE: '代码引用编号的类型不是整数', INTEGER_RANGE: '代码引用编号超出本批范围' },
    STATUS_CITATION_INVALID: {
      UNKNOWN_WITH_CITATIONS: 'AI 判断为无法确认，但同时填写了实现证据引用',
      POSITIVE_WITHOUT_CITATIONS: 'AI 判断为已实现或部分实现，但没有提供代码证据',
      POSITIVE_FIRST_SLOT_EMPTY: 'AI 提供了代码引用，但引用排列不符合当前返回格式'
    }
  }
  const rules = Object.hasOwn(precise, diagnostic.wire_reason) ? precise[diagnostic.wire_reason] : null
  const explanation = atlasExactRowMismatch(diagnostic) ? 'AI 返回的内容未能对应本批分析范围；具体数量可展开分析详情查看' : rules && typeof diagnostic.wire_rule === 'string' && Object.hasOwn(rules, diagnostic.wire_rule)
    ? rules[diagnostic.wire_rule] : reasons[diagnostic.wire_reason]
  const positions = []
  const count = value => Number.isInteger(value) && value >= 0 && value <= 2147483647
  if (count(diagnostic.tool_call_ordinal) && diagnostic.tool_call_ordinal < 16
      && (diagnostic.tool_call_count === undefined || (count(diagnostic.tool_call_count) && diagnostic.tool_call_count >= 1
        && diagnostic.tool_call_count <= 16 && diagnostic.tool_call_ordinal < diagnostic.tool_call_count))) positions.push(`第 ${diagnostic.tool_call_ordinal + 1} 份结果`)
  if (count(diagnostic.row_ordinal)) positions.push(`第 ${diagnostic.row_ordinal + 1} 条记录`)
  if (count(diagnostic.slot_ordinal)) positions.push(`第 ${diagnostic.slot_ordinal + 1} 个引用位置`)
  return `具体原因：${explanation}${positions.length ? `（${positions.join('，')}）` : ''}。`
})
let atlasAdmissionUnknown = false
let atlasPendingIdentity = null
let atlasTimer = null
let atlasDisposed = false
let atlasEpoch = 0
let atlasObservation = 0
let atlasCreating = false
const atlasActive = task => ['queued', 'running'].includes(task?.status)
function atlasScopeIsStale(task) {
  // Active server work stays visible even after scope changes; preflight gates new work.
  if (atlasActive(task) || atlasMatchesDisplayedProfile(task)) return false
  const profile = baselinePlanProfile.value?.id
  return task.stale_scope === true || (profile != null && task.plan_profile_id != null && String(profile) !== String(task.plan_profile_id))
}
function beginAtlasObservation() {
  const epoch = atlasEpoch, project = props.projectId, serial = ++atlasObservation
  return () => !atlasDisposed && epoch === atlasEpoch && project === props.projectId && serial === atlasObservation
}
function stopAtlasPolling() { clearTimeout(atlasTimer); atlasTimer = null }
async function acceptAtlasTask(task) {
  if (!isAtlasTask(task)) throw new Error('ATLAS_TASK_INVALID')
  const previous = atlasTask.value
  if (previous?.task_id === task.task_id && !atlasActive(previous) && task.status !== previous.status) return
  atlasObservation++
  if (atlasScopeIsStale(task) && !atlasAdmissionUnknown) {
    stopAtlasPolling(); atlasTask.value = null; baselineExecuting.value = false; baselineUnknown.value = false
    baselineMessage.value = '历史分析任务属于旧范围，请扫描并核对当前代码。'
    return
  }
  atlasStalled.value = previous?.task_id === task.task_id && previous?.status === task.status && previous?.completed_stages === task.completed_stages && previous?.current_stage === task.current_stage
  atlasTask.value = task
  baselineExecuting.value = ['queued', 'running'].includes(task.status)
  baselineUnknown.value = atlasAdmissionUnknown || task.status === 'unknown'
  stopAtlasPolling()
  if (task.status === 'succeeded') {
    baselineMessage.value = atlasMatchesDisplayedProfile(task) && displayedProfile.value?.status === 'confirmed' ? '本次分析结果已人工确认，可继续查看需求和代码依据。' : '当前代码状态候选已建立。请核对需求和证据后人工确认。'
    baselineMessageType.value = 'success'
    baselinePreflight.value = null
    reconciliationAuthorized.value = false
    if (previous?.task_id !== task.task_id || previous?.status !== 'succeeded') {
      await loadPanel()
      await loadBaselineStatus()
    }
  } else if (task.status === 'unknown' || task.status.startsWith('failed_')) {
    reconciliationAuthorized.value = false
    const code = /^[A-Z][A-Z0-9_:-]{1,119}$/.test(task.error_code || '') ? `（${task.error_code}）` : ''
    const knownFailureMessages = {
      PROFILE_GENERATION_RESPONSES_CONTENT_JSON_DUPLICATE_KEY_INVALID: 'AI 返回了重复字段，旧版本未能解析；已保存成功步骤，不会自动重发。',
      PROFILE_GENERATION_STRICT_ARGUMENT_CONFLICT: 'AI 返回了相互冲突的字段，本次已停止；不会自动重发。',
      PROFILE_GENERATION_STRICT_TOOL_COUNT_INVALID: 'AI 未按本批要求返回结果，本次分析已停止；已完成步骤仍保留，不会自动重发。',
      PROFILE_GENERATION_STRICT_WIRE_INVALID: 'AI 未按本批要求返回结果，本次分析已停止；已完成步骤仍保留，不会自动重发。',
      PROFILE_GENERATION_STRICT_BATCH_CONFLICT: 'AI 对同一批返回了相互矛盾的结果，本次已停止；不会自动选择其中一份或重发。',
    }
    const knownFailure = task.status === 'failed_after_send' && Object.hasOwn(knownFailureMessages, task.error_code)
      ? knownFailureMessages[task.error_code] : '分析停止，未生成不完整候选。'
    baselineMessage.value = (task.status === 'unknown' ? '模型执行结果无法确认，系统不会自动重发。' : knownFailure) + code
    baselineMessageType.value = 'error'
  } else {
    baselineMessage.value = '分析任务已保存，可以离开页面后回来查看。'
    baselineMessageType.value = 'success'
    if (!atlasDisposed) atlasTimer = setTimeout(pollAtlasTask, 2500)
  }
}
async function pollAtlasTask() {
  const project = props.projectId, taskId = atlasTask.value?.task_id
  if (!taskId || atlasDisposed || atlasCreating) return
  const current = beginAtlasObservation()
  const result = await getAtlasTask(project, taskId)
  if (!current() || atlasTask.value?.task_id !== taskId) return
  if (result.ok && isAtlasTask(result.body?.task) && result.body.task.task_id === taskId) await acceptAtlasTask(result.body.task)
  else {
    atlasStalled.value = true
    baselineMessage.value = '暂时无法读取任务进度。未重发模型请求，可继续查询原任务。'
    if (!atlasDisposed) atlasTimer = setTimeout(pollAtlasTask, 5000)
  }
}
async function recoverAtlasTask() {
  if (atlasCreating) return
  const current = beginAtlasObservation()
  const result = await getLatestAtlasTask(props.projectId)
  if (!current()) return
  if (result.ok && isAtlasTask(result.body?.task)) {
    const task = result.body.task
    if (atlasAdmissionUnknown && atlasPendingIdentity && (task.authorization_nonce !== atlasPendingIdentity.authorization_nonce || task.preflight_identity_hash !== atlasPendingIdentity.preflight_identity_hash)) {
      baselineMessage.value = '本次任务尚未找回，已有历史任务不能证明本次创建结果。系统不会自动重发。'
      return
    }
    atlasAdmissionUnknown = false; atlasPendingIdentity = null
    await acceptAtlasTask(task)
  } else if (result.ok && result.body?.task === null) {
    baselineUnknown.value = atlasAdmissionUnknown
    if (!atlasAdmissionUnknown) baselineMessage.value = ''
  } else if (!result.ok) {
    baselineUnknown.value = true
    baselineMessage.value = '暂时无法核对已有分析任务，请先恢复任务查询。'
  }
}
async function continueAtlasTask() {
  if (atlasCreating || !atlasTask.value?.resume_available || !['queued', 'running'].includes(atlasTask.value.status)) return
  atlasStalled.value = false
  const taskId = atlasTask.value.task_id, current = beginAtlasObservation()
  const result = await resumeAtlasTask(props.projectId, taskId)
  if (!current() || atlasTask.value?.task_id !== taskId) return
  if (result.ok && isAtlasTask(result.body?.task)) await acceptAtlasTask(result.body.task)
  else {
    baselineMessage.value = formatProjectStateBaselineError(result.body, '无法继续原任务，请查询任务状态。')
    baselineMessageType.value = 'error'
    await pollAtlasTask()
  }
}
async function executeStateBaseline() {
  if (!props.projectId || !canReconcile.value || reconciliationDisabled.value) return
  baselineExecuting.value = true
  atlasCreating = true
  const creationEpoch = atlasEpoch
  const current = beginAtlasObservation()
  stopAtlasPolling()
  try {
    const result = await createAtlasTask(props.projectId, baselinePlanProfile.value.id, baselinePreflight.value.preflight_identity_hash, { retryTask: atlasTask.value })
    if (!current()) return
    if (result.ok && isAtlasTask(result.body?.task)) await acceptAtlasTask(result.body.task)
    else {
      baselineExecuting.value = false
      atlasAdmissionUnknown = result.status === 0
      atlasPendingIdentity = result.atlasIdentity || null
      baselineUnknown.value = atlasAdmissionUnknown
      baselineMessage.value = formatProjectStateBaselineError(result.body, '无法确认任务是否创建，请查询已有任务；不会自动重发。')
      baselineMessageType.value = 'error'
    }
  } catch {
    if (!current()) return
    baselineExecuting.value = false
    atlasAdmissionUnknown = true
    baselineUnknown.value = true
    baselineMessage.value = '无法安全创建或核对任务，请查询已有任务。'
  } finally {
    if (creationEpoch === atlasEpoch) atlasCreating = false
  }
}
onUnmounted(() => { atlasDisposed = true; atlasEpoch++; stopAtlasPolling() })
watch(() => props.projectId, async () => {
  preflightPreviousTaskId.value = null
  atlasEpoch++; stopAtlasPolling(); atlasTask.value = null; atlasAdmissionUnknown = false; atlasPendingIdentity = null; atlasCreating = false
  baselineExecuting.value = false; baselineUnknown.value = false
  baselinePreflighting.value = false; baselinePreflight.value = null; baselineLedgerStatus.value = null; reconciliationAuthorized.value = false
  if (props.projectId) await recoverAtlasTask()
})

onMounted(async () => {
  if (props.projectId != null) {
    await loadPanel()
    await loadBaselineStatus()
    await recoverAtlasTask()
    collapseAllModules()
  }
})

watch(
  () => serverProfile.value?.id,
  async () => {
    collapseAllModules()
    baselinePreflight.value = null
    baselinePrepareRequest++; baselinePreflighting.value = false
    reconciliationAuthorized.value = false
    baselineUnknown.value = atlasAdmissionUnknown || atlasTask.value?.status === 'unknown'
    await loadBaselineStatus()
    await recoverAtlasTask()
  },
  { flush: 'post' }
)
// Confirmation preserves the profile id; refresh its server-owned baseline status separately.
watch(() => serverProfile.value?.status, (status, previous) => {
  if (status === 'confirmed' && previous === 'candidate') loadBaselineStatus()
})
watch(() => [props.projectId, resultTask.value?.task_id, reportBound.value, displayedProfile.value?.content_hash, displayedProfile.value?.status], () => {
  closeReport(); reportError.value = ''
})
watch(() => [props.projectId, displayedProfile.value?.id, displayedProfile.value?.content_hash], loadProfileResult, { immediate: true })
</script>

<template>
  <section class="panel-card lane-profile" aria-labelledby="profile-panel-title">
    <div class="module-section-head">
      <div>
        <h3 id="profile-panel-title">代码盘点</h3>
        <p>需求、实现结果与代码依据。</p>
      </div>
      <span v-if="panelState !== 'loading' && panelState !== 'failed'" :class="['status-badge', headingBadge.cls]">{{ headingBadge.text }}</span>
    </div>

    <section v-if="confirmedAllUnknown" class="profile-evidence-gap" aria-label="实现状态待核实">
      <div><strong>记录已确认，开发进度仍待核实</strong><p>已保存的 {{ moduleOverview.total }} 个模块都缺少足够实现证据，暂时无法判断开发程度。这不代表功能没有开发。</p>
        <p>这份结果对应分支 <b>{{ baselineLedgerStatus.git_branch }}</b> · 版本 <code>{{ shortHead(baselineLedgerStatus.exact_head) }}</code>。请先确认读取的是完整业务代码，再进行全量分析。</p></div>
      <button class="secondary-button" type="button" @click="goSetup">核对仓库与分支</button>
    </section>
    <section v-else-if="!historyViewing && baselineEstablished && serverProfile?.status === 'confirmed'" class="profile-next-action" aria-label="下一步">
      <div><strong>分析记录已确认</strong><p>可以选择本次研发范围，进行后续增量分析。各功能状态以证据结果为准。</p></div>
      <button class="primary-button" type="button" @click="goGit">下一步：选择研发范围 →</button>
    </section>
    <p class="profile-safety-note">只有人工确认的结果会用于后续分析；证据不足的需求保持待核实。</p>

    <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>
    <p v-if="generationMessage" :class="['message', generationMessageType]" role="status">{{ generationMessage }}</p>
    <p v-if="baselineMessage && !confirmedSuccessMessage && !(baselineExecuting && baselineMessage === '代码检查完成。请核对文件范围和分析上限；确认后才会将本次资料发送给 AI。')" :class="['message', baselineMessageType]" role="status">{{ baselineMessage }}</p>
    <component :is="confirmedTaskRecord || taskPresentation.foldPreviousRecord ? 'details' : 'section'" v-if="atlasTask" :key="`${atlasTask.task_id}:${Boolean(taskPresentation.foldPreviousRecord)}`" :class="['atlas-task-card', taskPresentation.tone, { 'confirmed-task-record': confirmedTaskRecord, 'previous-task-record': taskPresentation.foldPreviousRecord }]" aria-label="代码分析任务">
      <summary v-if="confirmedTaskRecord || taskPresentation.foldPreviousRecord">{{ taskPresentation.foldPreviousRecord ? `${taskPresentation.label} · 查看记录` : '本次分析记录' }}</summary>
      <p v-if="confirmedSuccessMessage" :class="['message', baselineMessageType]" role="status">{{ baselineMessage }}</p>
      <div class="atlas-task-heading">
        <div><b>{{ taskPresentation.label }}</b><p v-if="['queued', 'running'].includes(atlasTask.status)">已完成 {{ atlasTask.completed_stages ?? 0 }} 个检查步骤，请等待结果。</p></div>
        <div class="atlas-observation-actions">
          <button v-if="atlasStalled && atlasTask.resume_available && ['queued', 'running'].includes(atlasTask.status)" class="secondary-button" type="button" @click="continueAtlasTask">继续原分析任务</button>
          <button class="secondary-button" type="button" @click="recoverAtlasTask">查询已有任务</button>
        </div>
      </div>
      <p v-if="taskPresentation.previousRecord" class="atlas-safety-note">本次代码检查已完成；以下状态属于此前的分析记录。</p>
      <p class="atlas-safety-note">证据选择和关系扩展有范围上限；无法确认的需求保持未知，最终结果需要人工确认。</p>
      <p v-if="atlasFailureReason" class="atlas-safety-note" role="status">{{ taskPresentation.previousRecord ? '上次分析记录：' : '' }}{{ atlasFailureReason }}</p>
      <details class="atlas-technical-details">
        <summary>查看任务详情</summary>
        <dl><div><dt>已完成阶段</dt><dd>{{ atlasTask.completed_stages ?? 0 }}</dd></div><div><dt>已建立阶段</dt><dd>{{ atlasTask.stage_count ?? 0 }}</dd></div><div><dt>模型调用上限</dt><dd>{{ atlasTask.max_calls }}</dd></div></dl>
        <p v-if="atlasTask.current_stage">当前阶段标识：<code>{{ atlasTask.current_stage }}</code></p>
        <p v-if="atlasTask.error_code">诊断编号：<code>{{ atlasTask.error_code }}</code></p>
        <p v-if="atlasFailureCounts">{{ atlasFailureCounts }}</p>
        <p v-if="atlasCoverageSummary">{{ atlasCoverageSummary }}</p>
      </details>
    </component>
    <button v-else-if="baselineUnknown" type="button" @click="recoverAtlasTask">查询已有任务</button>
    <div v-if="missingList.length" class="lane-missing" role="alert"><b>还有信息需要补齐</b><ul><li v-for="(item,index) in missingList" :key="index">{{ item.module_name || item.module_client_id ? `【${item.module_name || item.module_client_id}】` : '' }}{{ item.message }}</li></ul></div>

    <section v-if="legacyConfirmed" class="generation-state baseline-state" aria-label="升级旧版功能档案">
      <div class="generation-copy">
        <b>分析代码前，请先升级功能档案</b>
        <span>当前是旧版功能档案，只保存了计划模块和手工代码范围，不能冒充全量代码分析结果。升级只会创建一个 V2 候选，不会覆盖当前确认版，也不会调用模型。</span>
      </div>
      <button class="primary-button generation-primary" type="button" :disabled="legacyUpgrading" @click="upgradeLegacyProfile">{{ legacyUpgrading ? '正在创建新版候选...' : '创建 V2 功能档案候选' }}</button>
      <p class="baseline-note">创建后请先检查并确认模块；确认完成后，同一页会出现“检查当前代码并估算分析上限”。</p>
    </section>

    <component :is="collapseReanalysis ? 'details' : 'div'" v-if="canReconcile" :class="{ 'reanalysis-disclosure': collapseReanalysis }">
      <summary v-if="collapseReanalysis">重新检查当前代码<span>仅在需要更新全量结果时使用</span></summary>
    <section class="baseline-command-card" aria-label="建立当前项目状态基线">
      <div class="baseline-command-head">
        <div>
          <h4>{{ confirmedAllUnknown ? '全量分析现有代码' : (baselineEstablished ? '重新检查当前代码' : '检查当前实现') }}</h4>
          <p>{{ confirmedAllUnknown ? '适用于已有代码的项目：先本地检查完整仓库范围，再经你授权逐项分析。检查范围不会调用模型。' : (baselineCandidatePending ? '已有一版待确认结果。你可以重新分析；只有新结果完整成功后才会替换旧候选。' : (baselineEstablished ? '只有仓库现状需要重新盘点时才需要再次执行。' : '系统将检查当前代码版本，逐项核对已确认的需求。')) }}</p>
        </div>
        <button
          type="button"
          :class="baselinePreflight || baselineCandidatePending ? 'secondary-button' : 'primary-button'"
          :disabled="baselineBusy || baselineUnknown"
          @click="prepareStateBaseline"
        >{{ baselinePreflighting ? '正在扫描代码…' : (baselinePreflight ? '重新扫描' : '检查当前代码并估算分析上限') }}</button>
      </div>

      <div v-if="baselinePreflight" class="baseline-ready-strip" aria-label="分析范围摘要">
        <div><b>{{ baselinePreflight.tracked_files ?? '—' }}</b><span>文件</span></div>
        <div><b>{{ formatBytes(baselinePreflight.safe_text_bytes) }}</b><span>可分析代码</span></div>
        <div><b>{{ baselinePreflight.max_calls }}</b><span>模型调用上限</span></div>
        <div><code>{{ shortHead(baselinePreflight.exact_head) }}</code><span>代码版本</span></div>
      </div>

      <p v-if="baselinePreflight" class="baseline-note">本次检查 {{ baselinePreflight.module_count }} 个模块。模型调用次数为上限，实际数量取决于检查结果。</p>
      <div v-if="baselinePreflight" class="baseline-go-row">
        <label class="baseline-consent-compact">
          <input v-model="reconciliationAuthorized" type="checkbox" :disabled="baselineBusy || baselineUnknown" />
          <span>允许将本次检查范围内的安全代码发送给当前模型</span>
        </label>
        <button
          type="button"
          class="primary-button baseline-go-button"
          :disabled="reconciliationDisabled"
          @click="executeStateBaseline"
        >{{ baselineExecuting ? `正在分阶段分析…` : '开始 AI 对账' }}</button>
      </div>

      <p class="baseline-safety-note">模型可能产生费用；执行结果不确定时不会自动重发。分析结果需人工确认。</p>
      <details class="baseline-tech-details">
        <summary>分析范围与安全规则</summary>
        <p v-if="baselinePreflight">本次检查的完整代码版本：<code>{{ baselinePreflight.exact_head }}</code></p>
        <p>系统先建立代码地图，再沿关系选择相关证据并检查遗漏；未读取或证据不足的内容保持未知，不代表整仓语义已经证明。授权绑定当前 HEAD、已确认 PRD、项目档案版本和当前模型，任一项变化都会停止。</p>
        <p v-if="baselineCandidatePending">当前候选尚未确认，也可以重新执行全量分析；完整成功前不会替换现有候选。</p>
      </details>

      <div v-if="baselineUnknown" class="unknown-retry-panel compact-alert" role="alert">
        <b>上一笔分析结果不确定</b>
        <span>系统没有自动重发。请先核对候选与历史执行记录。</span>
      </div>
    </section>

    </component>

    <p v-if="resultLoading" class="result-read-state" role="status">正在读取这版需求与代码依据…</p>
    <div v-else-if="resultError" class="result-read-error" role="alert">
      <p>{{ resultError }}</p><button class="secondary-button" type="button" @click="loadProfileResult">重新读取分析结果</button>
    </div>
      <div v-if="resultMatchesProfile" class="atlas-report-actions">
        <button class="secondary-button" type="button" :disabled="!reportBound || reportBusy" @click="openAtlasReport(false)">{{ reportBusy ? '正在读取…' : '预览安心看板' }}</button>
        <button class="secondary-button" type="button" :disabled="!reportBound || reportBusy || displayedProfile?.status !== 'confirmed'" @click="openAtlasReport(true)">下载 HTML</button>
        <span v-if="!reportBound">该任务与当前显示版本不匹配，请重新读取结果。</span>
        <span v-else-if="displayedProfile?.status !== 'confirmed'">下载前请先人工确认本次结果。</span>
      </div>
      <p v-if="reportError" class="message error" role="alert">{{ reportError }}</p>
      <dialog v-if="reportUrl" ref="reportDialog" class="atlas-report-dialog" aria-label="安心看板预览" @cancel.prevent="closeReport" @close="closeReport">
        <div><strong>安心看板预览</strong><button class="secondary-button" type="button" @click="closeReport">关闭预览</button></div>
        <p>只读预览，不会确认结果或发起分析。</p>
        <iframe :src="reportUrl" title="安心看板只读预览" sandbox="" referrerpolicy="no-referrer"></iframe>
      </dialog>

    <section v-if="displayedContent?.schema_version === 'project_profile_v2'" class="reconciliation-panel" aria-label="计划与实现对账">
      <div class="reconciliation-head">
        <div>
          <h4>需求实现情况</h4>
          <p v-if="awaitingFirstBaseline">尚无完整代码分析结果，以下为已整理的需求。</p>
          <p v-else>{{ displayedProfile?.status === 'candidate' ? (resultMatchesProfile ? '代码分析已完成，请核对下方结果和依据后采用。' : '这是待确认版本。请核对需求与依据，再确认使用。') : '当前版本的结果；展开模块可查看需求与代码依据。' }}</p>
        </div>
        <span class="module-count-chip">{{ displayedContent.planned_modules.length }} 个模块</span>
      </div>

      <section class="requirement-overview" aria-label="需求实现概览">
      <dl v-if="requirementOverview" class="requirement-totals">
        <div class="total"><dt>需求总数</dt><dd>{{ requirementOverview.total }}</dd></div>
        <div class="implemented"><dt>已实现</dt><dd>{{ requirementOverview.implemented }}</dd></div>
        <div class="partial"><dt>部分实现</dt><dd>{{ requirementOverview.partial }}</dd></div>
        <div class="unknown"><dt>暂无法确认</dt><dd>{{ requirementOverview.unknown }}</dd></div>
      </dl>
      <p v-else-if="confirmedAllUnknown" class="overview-unavailable">已读取 {{ moduleOverview.total }} 个模块结论，均为暂无法确认；这里显示的是模块状态，不是逐条需求统计。</p>
      <p v-else class="overview-unavailable">逐条需求统计暂不可用，请查看模块结果。</p>
      </section>


      <div class="reconciliation-list">
        <article v-for="module in displayedContent.planned_modules" :key="module.client_id" class="reconciliation-item">
          <div class="reconciliation-item-main">
            <b>{{ module.name }}</b>
            <span :class="['mapping-pill', baselineMappingTone(module)]">{{ baselineMappingLabel(module) }}</span>
          </div>
          <p v-if="awaitingFirstBaseline" class="reconciliation-muted">尚无完整代码分析结果</p>
          <template v-else>
            <p v-if="moduleRequirementSummary(module)" class="mapping-rationale">{{ moduleRequirementSummary(module).total }} 条需求 · 已实现 {{ moduleRequirementSummary(module).implemented }} · 部分实现 {{ moduleRequirementSummary(module).partial }} · 暂无法确认 {{ moduleRequirementSummary(module).unknown }}</p>
            <details v-if="baselineMapping(module)" class="evidence-details">
              <summary>查看需求与代码依据</summary>
              <div class="evidence-body">
                <details class="module-evidence-source">
                  <summary>代码来源与关联文件（{{ baselineMapping(module).paths?.length ?? 0 }}）</summary>
                  <p>代码版本</p><code>{{ baselineMapping(module).exact_head }}</code>
                  <p v-if="baselineMapping(module).paths?.length">关联路径</p>
                  <ul><li v-for="path in baselineMapping(module).paths" :key="path.pattern"><code>{{ path.pattern }}</code></li></ul>
                </details>
                <details v-if="baselineMapping(module).rationale" class="module-analysis"><summary>查看模块原始分析</summary><p>{{ baselineMapping(module).rationale }}</p></details>
                <article v-for="row in atlasRequirementRows(module)" :key="row.requirement_index" class="requirement-evidence">
                  <div class="requirement-evidence-heading"><b>{{ row.text }}</b><span :class="['mapping-pill', 'mapping-' + row.status]">{{ implementationLabel(row.status) }}</span></div>
                  <details class="requirement-analysis"><summary>查看原始分析说明</summary><p>{{ row.rationale }}</p></details>
                  <details v-if="row.evidence_ids?.length" class="evidence-references"><summary>{{ row.status === 'unknown' ? '已检查的相关代码（仍待核实）' : '证据引用' }}（{{ row.evidence_ids.length }}）</summary><p v-if="row.status === 'unknown'">这些引用仅供核查，不代表已经实现。</p><p>以下为引用标识，不是文件路径。</p><code v-for="evidenceId in row.evidence_ids" :key="evidenceId">{{ evidenceId }}</code></details>
                  <p v-else class="requirement-no-evidence">本条需求暂无可引用的代码证据。</p>
                </article>
              </div>
            </details>
          </template>
        </article>
      </div>

      <details v-if="!awaitingFirstBaseline" class="unplanned-details">
        <summary>计划外代码功能 · {{ displayedContent.unplanned_code_features.length }}</summary>
        <p v-if="!displayedContent.unplanned_code_features.length">当前没有已识别的计划外代码候选；这不代表代码中不存在额外功能。</p>
        <article v-for="feature in displayedContent.unplanned_code_features" :key="feature.client_id" class="unplanned-item">
          <b>{{ feature.name }}</b>
          <p>{{ feature.description }}</p>
          <div class="evidence-body"><code>{{ feature.exact_head }}</code><span v-for="path in feature.paths" :key="path.pattern">{{ path.pattern }}</span></div>
        </article>
      </details>
    </section>

    <div v-if="panelState === 'loading'" class="lane-state"><div class="lane-spinner"></div><b>正在读取功能模块...</b></div>
    <div v-else-if="panelState === 'failed'" class="lane-state error-state"><b>功能模块加载失败</b><span>当前结果暂不可用，请重新加载。</span><button class="secondary-button" type="button" @click="loadPanel">重新加载</button></div>

    <div v-else-if="panelState === 'empty'" class="lane-state generation-state">
      <div class="generation-copy">
        <b>先让 AI 按 PRD 整理计划功能</b>
        <span>先按已确认的需求整理功能模块。你确认计划后，再检查当前代码的实现情况。</span>
      </div>

      <div class="preparation-actions" aria-label="生成前准备">
        <button class="secondary-button" type="button" :disabled="generating" @click="goSetup">检查项目资料</button>
        <button class="secondary-button" type="button" :disabled="generating" @click="goSettings">配置 AI 模型</button>
      </div>

      <label class="generation-consent"><input v-model="includeImplementation" type="checkbox" disabled /><span>当前只整理需求，不发送代码；确认计划后再检查当前代码。</span></label>
      <label class="generation-consent">
        <input v-model="generationAuthorized" type="checkbox" :disabled="generating" />
        <span>我同意本次把已确认 PRD 发送给当前模型，生成计划功能候选。本次可能产生模型费用；候选不会自动确认。</span>
      </label>

      <button class="primary-button generation-primary" type="button" :disabled="generating || !generationAuthorized || generationUnknown" @click="generateCandidate">{{ generating ? 'AI 正在整理模块...' : '生成第一版计划功能' }}</button>

      <div v-if="generationUnknown" class="unknown-retry-panel" role="alert">
        <b>上一笔模型调用结果仍不确定</b>
        <span>系统不会自动重发。请先核对历史尝试与候选记录；当前页面不提供清除记录后重新付费的入口。</span>
      </div>

      <details class="generation-details">
        <summary>发送范围与失败规则</summary>
        <p>当前步骤只发送已确认 PRD。整仓代码不会在这里单次发送；计划确认后才进入 exact-HEAD 本地建图、敏感路径隔离和自动分批对账。</p>
      </details>
    </div>

    <template v-else-if="historyViewing">
      <div class="lane-readonly-head"><div><b>项目档案 V{{ historyViewing.version_no }}</b><span>{{ statusLabel(historyViewing.status) }} · {{ formatTime(historyViewing.created_at) }}</span></div><button class="secondary-button" type="button" @click="returnToCurrent">返回当前版本</button></div>
      <div class="lane-readonly-modules">
        <article v-for="(module,index) in plannedModules(historyViewing.content)" :key="module.client_id || index"><b>{{ module.name || '未命名模块' }}</b><p>{{ module.description || '暂无模块说明' }}</p><div class="lane-paths"><code v-for="(path,i) in module.paths || []" :key="i">{{ path.type }} · {{ path.pattern }}</code></div></article>
      </div>
    </template>

    <template v-else-if="viewMode === 'editing' && serverProfile">
      <p v-if="generatedResultReadOnly" class="result-readonly-note" role="status">分析结果保留原文。修改计划请返回准备项目；核对完成后仍可人工确认本版结果。</p>
      <component :is="generatedResultReadOnly ? 'details' : 'div'" class="candidate-plan-editor">
      <summary v-if="generatedResultReadOnly">查看功能计划原文</summary>
      <div class="lane-editor-head">
        <div><b>第 V{{ serverProfile.version_no }} 版建议</b><span>{{ generatedResultReadOnly ? '这是本次代码分析的候选结果，请核对需求和代码依据。' : '核对名称、说明和范围，保存修改后再确认。' }}</span></div>
        <button class="secondary-button" type="button" :disabled="candidateEditingLocked" @click="addModule">＋ 新增模块</button>
      </div>

      <div v-if="form.modules.length" class="lane-module-list">
        <article v-for="(module,index) in form.modules" :key="module.client_id" class="lane-module-card">
          <div class="lane-module-row">
            <button class="lane-drag" type="button" :disabled="formLocked" @click="toggleModule(index)" :aria-expanded="!collapsedModules[index]" aria-label="展开或收起模块边界">☷</button>
            <input class="module-name-input" v-model="module.name" maxlength="100" aria-label="模块名称" :disabled="candidateEditingLocked" placeholder="模块名称" />
            <input class="module-description-input" v-model="module.description" maxlength="2000" aria-label="模块说明" :disabled="candidateEditingLocked" placeholder="模块说明" />
            <div class="lane-module-chips" aria-label="代码路径边界摘要">
              <code v-for="(path,pathIndex) in module.paths.slice(0,3)" :key="pathIndex">{{ path.pattern || path.type }}</code>
            </div>
            <button class="secondary-button boundary-button" type="button" :disabled="formLocked" @click="toggleModule(index)">{{ collapsedModules[index] ? '查看范围' : '收起范围' }}</button>
            <span class="candidate-dot" title="待确认模块">待确认</span>
          </div>

          <div v-if="!collapsedModules[index]" class="lane-module-detail">
            <div class="detail-head"><b>这个模块包含什么</b><button class="lane-remove" type="button" :disabled="candidateEditingLocked" @click="removeModule(index)">删除模块</button></div>
            <div class="field"><label>PRD 引用（每行一项）</label><textarea v-model="module.prd_refs_text" rows="2" :disabled="candidateEditingLocked"></textarea></div>
            <div class="field"><label>必要组成项（每行一项）</label><textarea v-model="module.requirements_text" rows="2" :disabled="candidateEditingLocked"></textarea></div>
            <div v-if="form.schema_version !== 'project_profile_v2'" class="lane-path-heading"><b>代码范围</b><button class="secondary-button" type="button" :disabled="candidateEditingLocked" @click="addPath(module)">新增路径</button></div>
            <div v-if="form.schema_version !== 'project_profile_v2' && module.paths.length" class="lane-path-editor">
              <div v-for="(path,pathIndex) in module.paths" :key="pathIndex" class="lane-path-row">
                <select v-model="path.type" :disabled="candidateEditingLocked"><option value="frontend">frontend</option><option value="backend">backend</option><option value="data">data</option><option value="test">test</option><option value="other">other</option></select>
                <input v-model="path.pattern" maxlength="500" placeholder="路径模式" :disabled="candidateEditingLocked" />
                <label><input v-model="path.required" type="checkbox" :disabled="candidateEditingLocked" /> 必选</label>
                <button class="lane-remove" type="button" :disabled="candidateEditingLocked" @click="removePath(module,pathIndex)">删除</button>
              </div>
            </div>
            <div class="field"><label>不属于这个模块的范围（每行一项）</label><textarea v-model="module.exclusions_text" rows="2" :disabled="candidateEditingLocked"></textarea></div>
          </div>
        </article>
      </div>
      <p v-else class="empty state-placeholder">这一版暂时没有功能模块。你可以手动新增，但系统不会把它标成 AI 自动生成。</p>

      <details class="profile-advanced">
        <summary>项目概述与高级信息</summary>
        <div class="field lane-summary-field"><label for="profile-summary">项目概述</label><textarea id="profile-summary" v-model="form.project_summary" rows="3" maxlength="2000" :disabled="candidateEditingLocked"></textarea></div>
      </details>
      </component>

      <div class="lane-confirm-bar">
        <div><b>确认这版功能模块</b><span>{{ generatedResultReadOnly ? '人工确认后，这版结果才会用于后续分析。' : '有修改先保存，确认后这版才会用于后面的研发分析。' }}</span></div>
        <div class="lane-actions"><button v-if="!generatedResultReadOnly" class="secondary-button" type="button" :disabled="candidateEditingLocked" @click="saveCandidate">{{ saving ? '保存中...' : '保存修改' }}</button><button class="primary-button" type="button" :disabled="confirmDisabled" @click="confirmCandidate">{{ confirming ? '确认中...' : `确认并使用 V${serverProfile.version_no}` }}</button><button v-if="conflict" class="secondary-button" type="button" :disabled="saving || confirming || creating || panelState === 'loading'" @click="reloadServerVersion">重新加载最新版本</button></div>
      </div>
      <p v-if="hasUnsaved && !saving && !confirming" class="message info">还有未保存的修改，请先保存再确认。</p>
    </template>

    <template v-else-if="serverProfile">
      <div class="lane-readonly-head"><div><b>已确认的功能模块 · V{{ serverProfile.version_no }}</b><span>{{ formatTime(serverProfile.confirmed_at || serverProfile.updated_at) }}</span></div><button v-if="activeConfirmed" class="secondary-button" type="button" :disabled="creating" @click="createFromConfirmed">{{ creating ? '创建中...' : '需要修改这版' }}</button></div>
      <details class="profile-plan-details">
      <summary>功能计划 · {{ plannedModules(serverProfile.content).length }} 个模块</summary>
      <div class="lane-readonly-modules">
        <article v-for="(module,index) in plannedModules(serverProfile.content)" :key="module.client_id || index"><b>{{ module.name || '未命名模块' }}</b><p>{{ module.description || '暂无模块说明' }}</p><div class="lane-paths"><code v-for="(path,i) in module.paths || []" :key="i">{{ path.type }} · {{ path.pattern }}</code></div></article>
      </div>
      </details>
    </template>

    <details v-if="panelState !== 'loading'" class="history-details">
      <summary>历史版本 · {{ history.length }} 个</summary>
      <ul v-if="history.length" class="lane-history-list"><li v-for="version in history" :key="version.id"><span><b>V{{ version.version_no }} · {{ statusLabel(version.status) }}</b><small>{{ formatTime(version.created_at) }}</small></span><button class="lane-link-button" type="button" @click="viewHistory(version)">查看</button></li></ul>
      <p v-else class="empty state-placeholder">暂无历史版本</p>
    </details>
  </section>
</template>

<style scoped>
.atlas-report-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 12px 0;
}

.atlas-report-actions span {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.atlas-report-dialog {
  width: min(1100px, 94vw);
  max-width: 94vw;
  height: 88vh;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  background: white;
}

.atlas-report-dialog::backdrop {
  background: #17233499;
}

.atlas-report-dialog > div {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.atlas-report-dialog p {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.atlas-report-dialog iframe {
  display: block;
  width: 100%;
  height: calc(100% - 88px);
  border: 1px solid var(--border);
}

.profile-next-action button {
  flex-shrink: 0;
}

@media (max-width:720px) {
  .profile-next-action {
    align-items: stretch;
    flex-direction: column;
    padding: 16px;
    margin: 16px 0;
    border: 1px solid var(--border);
    border-left: 1px solid var(--border);
    border-radius: 12px;
    background: #f5f5f7;
  }
}

.module-section-head,.lane-editor-head,.lane-readonly-head,.lane-confirm-bar,.lane-actions,.lane-path-heading,.detail-head,.lane-next-step {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.module-section-head p {
  margin: 0;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  line-height: 1.55;
}

.lane-info-alert {
  margin: 12px 0;
  border: 1px solid #e5e9f0;
  border-radius: 12px;
  background: #f5f5f7;
  color: #5f6f82;
}

.lane-info-alert summary {
  padding: 10px 12px;
  font-size: var(--ui-text-caption);
  font-weight: var(--ui-weight-strong);
  cursor: pointer;
}

.lane-info-alert span {
  display: block;
  padding: 0 12px 11px;
  font-size: var(--ui-text-caption);
  line-height: 1.6;
}

.lane-missing {
  padding: 11px;
  border: 1px solid #efc2c2;
  border-radius: 12px;
  background: #fff4f4;
  color: var(--danger);
  font-size: var(--ui-text-caption);
}

.lane-state {
  min-height: 170px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: 12px;
  text-align: center;
  color: var(--muted);
}

.lane-state b {
  color: var(--title);
}

.lane-spinner {
  width: 24px;
  height: 24px;
  border: 3px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin .8s linear infinite;
}

.error-state {
  color: var(--danger);
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.generation-state {
  max-width: 700px;
  margin: 8px auto 0;
  padding: 20px 0 10px;
}

.generation-copy {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.generation-copy b {
  font-size: var(--ui-text-body);
}

.generation-copy span {
  max-width: 620px;
  font-size: var(--ui-text-caption);
  line-height: 1.65;
}

.preparation-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}

.generation-consent {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  max-width: 660px;
  padding: 12px 14px;
  border: 1px solid #d8e3f8;
  border-radius: 12px;
  background: #f5f5f7;
  text-align: left;
  color: #49617c;
}

.generation-consent input {
  margin-top: 3px;
  flex: 0 0 auto;
}

.generation-consent span {
  font-size: var(--ui-text-caption);
  line-height: 1.6;
}

.generation-primary {
  min-width: 220px;
}

.generation-details {
  width: min(100%,660px);
  border-top: 1px solid var(--border);
  padding-top: 3px;
  text-align: left;
}

.generation-details summary {
  width: max-content;
  margin: 0 auto;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  font-weight: var(--ui-weight-strong);
  cursor: pointer;
}

.generation-details p {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  line-height: 1.55;
}

.generation-actions {
  justify-content: center;
  flex-wrap: wrap;
}

.baseline-state {
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  margin: 14px auto;
  padding: 18px 0;
}

.baseline-summary {
  width: min(100%,660px);
  display: grid;
  grid-template-columns: repeat(4,minmax(0,1fr));
  gap: 8px;
}

.baseline-summary>div {
  padding: 10px 11px;
  border: 1px solid #dfe6ef;
  border-radius: 12px;
  background: #f5f5f7;
  text-align: left;
}

.baseline-summary span,.baseline-summary b,.baseline-summary code {
  display: block;
}

.baseline-summary span {
  color: var(--muted);
  font-size: var(--ui-text-caption);
  font-weight: var(--ui-weight-strong);
}

.baseline-summary b,.baseline-summary code {
  margin-top: 4px;
  color: var(--title);
  font-size: var(--ui-text-body);
}

.baseline-note {
  max-width: 660px;
  margin: 0;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  line-height: 1.6;
}

.unknown-retry-panel {
  max-width: 660px;
  padding: 12px 14px;
  border: 1px solid #ecc0c0;
  border-radius: 12px;
  background: #fff5f5;
  text-align: left;
  color: #8b2d2d;
}

.unknown-retry-panel b,.unknown-retry-panel span {
  display: block;
}

.unknown-retry-panel b {
  font-size: var(--ui-text-body);
}

.unknown-retry-panel span {
  margin-top: 4px;
  font-size: var(--ui-text-caption);
  line-height: 1.6;
}

.lane-editor-head,.lane-readonly-head {
  margin: 14px 0 10px;
  align-items: center;
}

.lane-editor-head b,.lane-editor-head span,.lane-readonly-head b,.lane-readonly-head span,.lane-next-step b,.lane-next-step span {
  display: block;
}

.lane-editor-head b,.lane-readonly-head b,.lane-next-step b {
  color: var(--title);
  font-size: var(--ui-text-body);
}

.lane-editor-head span,.lane-readonly-head span,.lane-next-step span {
  margin-top: 3px;
  color: var(--muted);
  font-size: var(--ui-text-caption);
}

.lane-module-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.lane-module-row input {
  min-width: 0;
  height: 38px;
  padding: 7px 8px;
  border: 1px solid #e1e6ee;
  border-radius: 7px;
  color: var(--title);
  background: #fff;
  font: inherit;
  font-size: var(--ui-text-body);
}

.module-name-input {
  font-weight: var(--ui-weight-strong);
}

.lane-drag,.lane-remove {
  border: 0;
  background: transparent;
  cursor: pointer;
}

.lane-drag {
  color: var(--muted);
  font-size: var(--ui-text-section);
}

.lane-remove {
  color: var(--danger);
  font-size: var(--ui-text-caption);
  font-weight: var(--ui-weight-strong);
}

.lane-module-chips {
  min-width: 0;
  display: flex;
  gap: 4px;
  overflow: hidden;
}

.lane-module-chips code {
  max-width: 110px;
  overflow: hidden;
  padding: 3px 6px;
  border-radius: 5px;
  background: #f5f5f7;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.boundary-button {
  min-height: 38px;
  padding: 6px 9px;
  font-size: var(--ui-text-body);
}

.candidate-dot {
  display: inline-flex;
  justify-content: center;
  padding: 4px 6px;
  border-radius: 999px;
  background: #f5f5f7;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  font-weight: var(--ui-weight-strong);
}

.lane-module-detail {
  padding: 12px;
  border-top: 1px solid var(--border);
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  background: #f5f5f7;
}

.detail-head,.lane-module-detail>.field:last-child,.lane-path-heading,.lane-path-editor {
  grid-column: 1/-1;
}

.detail-head {
  align-items: center;
}

.detail-head>b,.lane-path-heading b {
  font-size: var(--ui-text-body);
  color: var(--title);
}

.lane-path-editor {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.lane-path-row {
  display: grid;
  grid-template-columns: 110px 1fr auto auto;
  gap: 7px;
  align-items: center;
}

.lane-path-row label {
  font-size: var(--ui-text-body);
  color: var(--muted);
  white-space: nowrap;
}

.profile-advanced,.history-details {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #f5f5f7;
}

.profile-advanced summary,.history-details summary {
  padding: 9px 11px;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  font-weight: var(--ui-weight-strong);
  cursor: pointer;
}

.lane-summary-field {
  padding: 0 11px 11px;
}

.lane-confirm-bar b,.lane-confirm-bar span {
  display: block;
}

.lane-confirm-bar b {
  font-size: var(--ui-text-body);
  color: var(--title);
}

.lane-confirm-bar span {
  margin-top: 3px;
  font-size: var(--ui-text-caption);
  color: var(--muted);
}

.lane-readonly-modules article>b {
  font-size: var(--ui-text-body);
  color: var(--title);
}

.lane-paths {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.lane-paths code {
  padding: 3px 6px;
  border-radius: 5px;
  background: #f5f5f7;
  font-size: var(--ui-text-caption);
}

.lane-next-step {
  margin-top: 14px;
  padding: 14px 15px;
  border: 1px solid #dbe5f5;
  border-radius: 12px;
  background: #f5f5f7;
  align-items: center;
}

.lane-history-list {
  list-style: none;
  margin: 0;
  padding: 0 10px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.lane-history-list li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 9px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #fff;
}

.lane-history-list b,.lane-history-list small {
  display: block;
}

.lane-history-list b {
  font-size: var(--ui-text-body);
  color: var(--title);
}

.lane-history-list small {
  margin-top: 3px;
  color: var(--muted);
  font-size: var(--ui-text-caption);
}

.lane-link-button {
  border: 0;
  background: transparent;
  color: var(--primary);
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  cursor: pointer;
}

@media (max-width:1100px) {
  .lane-module-row {
    grid-template-columns: 28px 140px 1fr auto 48px;
    gap: 8px;
  }
  .lane-module-chips {
    grid-column: 2/4;
  }
  .lane-readonly-modules {
    grid-template-columns: 1fr;
  }
}

@media (max-width:720px) {
  .module-section-head,.lane-editor-head,.lane-readonly-head,.lane-confirm-bar,.lane-next-step {
    flex-direction: column;
  }
  .baseline-summary {
    grid-template-columns: repeat(2,minmax(0,1fr));
  }
  .lane-module-row,.lane-module-detail,.lane-path-row {
    grid-template-columns: 1fr;
  }
  .lane-module-row>* {
    grid-column: 1 !important;
  }
  .lane-module-detail>* {
    grid-column: 1 !important;
  }
  .lane-actions {
    flex-wrap: wrap;
  }
}

/* Results and task status share one restrained reading hierarchy. */

.lane-profile {
  padding: 20px;
  font-size: var(--ui-text-body);
  line-height: 1.6;
}

.module-section-head {
  align-items: center;
}

.module-section-head h3 {
  margin: 0 0 5px;
  color: var(--title);
  line-height: 1.4;
  font-size: var(--ui-text-section);
  font-weight: var(--ui-weight-strong);
}

.module-section-head p, .profile-safety-note {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.profile-safety-note {
  margin: 12px 0 20px;
}

.lane-profile > .message {
  padding: 12px 14px;
  margin: 12px 0;
  font-size: var(--ui-text-body);
  line-height: 1.6;
}

.profile-next-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin: 16px 0;
  border: 1px solid var(--border);
  border-left: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  background: #f5f5f7;
}

.profile-next-action strong {
  font-size: var(--ui-text-section);
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.profile-next-action p {
  margin: 8px 0 0;
  line-height: 1.6;
  font-size: var(--ui-text-body);
  color: var(--muted);
}

.atlas-task-card {
  margin: 16px 0;
  padding: 16px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.confirmed-task-record {
  margin: 12px 0;
  padding: 10px 0;
  border-top: 0;
}

.confirmed-task-record > summary,
.previous-task-record > summary {
  color: var(--muted);
  font-size: var(--ui-text-helper);
}

.confirmed-task-record[open] .atlas-task-heading {
  margin-top: 12px;
}

.atlas-task-heading, .baseline-command-head, .baseline-go-row, .reconciliation-head, .reconciliation-item-main, .requirement-evidence-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.atlas-task-heading b {
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.atlas-task-heading p, .atlas-safety-note {
  margin: 6px 0 0;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.atlas-task-card.warning, .atlas-task-card.error {
  border-left: 3px solid #ba7632;
  padding-left: 16px;
}

.atlas-task-card.error {
  border-left-color: #b44444;
}

.atlas-observation-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.atlas-technical-details {
  margin-top: 12px;
}

.atlas-technical-details dl {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin: 12px 0;
}

.atlas-technical-details dt {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.atlas-technical-details dd {
  margin: 4px 0 0;
  font-size: var(--ui-text-body);
}

.atlas-technical-details p, .baseline-tech-details p {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.atlas-technical-details code {
  overflow-wrap: anywhere;
}

.baseline-command-card {
  padding: 20px 0;
  margin: 16px 0;
  border-block: 1px solid var(--border);
}

.baseline-command-head h4, .reconciliation-head h4 {
  margin: 0;
  font-size: var(--ui-text-section);
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.baseline-command-head p, .reconciliation-head p {
  margin: 8px 0 0;
  max-width: 760px;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.baseline-ready-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0,1fr));
  gap: 20px;
  margin: 20px 0 12px;
}

.baseline-ready-strip b, .baseline-ready-strip code, .baseline-ready-strip span {
  display: block;
}

.baseline-ready-strip b, .baseline-ready-strip code {
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.baseline-ready-strip span {
  margin-top: 4px;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.baseline-note, .baseline-safety-note {
  max-width: none;
  font-size: var(--ui-text-helper);
  line-height: 1.6;
  color: var(--muted);
}

.baseline-go-row {
  align-items: center;
  margin-top: 16px;
}

.baseline-consent-compact {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: var(--ui-text-body);
}

.baseline-consent-compact input {
  margin-top: 5px;
  flex-shrink: 0;
}

.baseline-go-button {
  flex-shrink: 0;
}

.baseline-safety-note {
  margin: 12px 0;
}

.baseline-tech-details {
  margin-top: 12px;
}

.reanalysis-disclosure {
  margin: 16px 0;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
}

.reanalysis-disclosure > summary {
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.reanalysis-disclosure > summary span {
  display: inline-block;
  margin-left: 12px;
  font-size: var(--ui-text-helper);
  font-weight: 400;
  color: var(--muted);
}

.reanalysis-disclosure .baseline-command-card {
  margin: 0;
  padding: 16px 0 0;
  border: 0;
}

.compact-alert {
  margin-top: 14px;
}

.reconciliation-panel {
  margin: 24px 0;
}

.reconciliation-head {
  align-items: center;
  margin-bottom: 16px;
}

.module-count-chip {
  font-size: var(--ui-text-helper);
  color: var(--muted);
  white-space: nowrap;
}

.requirement-overview {
  margin: 0 0 20px;
  padding: 16px;
  background: #f5f5f7;
  border-radius: 12px;
}

.requirement-totals {
  display: grid;
  grid-template-columns: repeat(4, minmax(0,1fr));
  gap: 24px;
  margin: 0;
}

.requirement-totals dt {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.requirement-totals dd {
  margin: 6px 0 0;
  font-size: var(--ui-text-page);
  font-weight: var(--ui-weight-strong);
  line-height: 1.3;
  color: var(--title);
  font-variant-numeric: tabular-nums;
}

.requirement-totals .implemented dd {
  color: #237a45;
}

.requirement-totals .partial dd {
  color: #946200;
}

.requirement-totals .unknown dd {
  color: var(--muted);
}

.overview-unavailable {
  margin: 0;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.profile-evidence-gap {
  display: flex;
  align-items: center;
  gap: 20px;
  margin: 20px 0;
  padding: 16px 20px;
  border-left: 3px solid #b6802f;
  background: #fff9ef;
}

.profile-evidence-gap strong {
  font-size: 15px;
  color: #76511d;
}

.profile-evidence-gap p {
  margin: 7px 0 0;
  font-size: var(--ui-text-helper);
  line-height: 1.7;
  color: #6d5e47;
  overflow-wrap: anywhere;
}

.profile-evidence-gap button {
  flex-shrink: 0;
}

@media (max-width:720px) {
  .profile-evidence-gap {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }
}

.reconciliation-list {
  border-top: 1px solid var(--border);
}

.reconciliation-item {
  padding: 16px 0;
  border-bottom: 1px solid var(--border);
}

.reconciliation-item-main {
  align-items: center;
}

.reconciliation-item-main > b {
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  color: var(--title);
  overflow-wrap: anywhere;
}

.mapping-pill {
  display: inline-block;
  flex-shrink: 0;
  padding: 3px 8px;
  border-radius: 5px;
  font-size: var(--ui-text-helper);
  line-height: 1.5;
  font-weight: var(--ui-weight-strong);
}

.mapping-implemented {
  background: #eaf8ef;
  color: #237a45;
}

.mapping-partial {
  background: #fff6df;
  color: #946200;
}

.mapping-missing {
  background: #fff0f0;
  color: #b42318;
}

.mapping-unknown {
  background: #f5f5f7;
  color: var(--muted);
}

.mapping-rationale, .reconciliation-muted {
  margin: 6px 0 0;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.evidence-details {
  margin-top: 10px;
}

.lane-profile summary {
  max-width: 100%;
  cursor: pointer;
  font-size: var(--ui-text-helper);
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.evidence-details > summary {
  color: var(--primary);
}

.evidence-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  margin-top: 16px;
}

.evidence-body code, .evidence-body span {
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
  font-size: var(--ui-text-helper);
  line-height: 1.6;
}

.module-evidence-source, .module-analysis {
  padding: 0;
  min-width: 0;
}

.module-evidence-source p {
  margin: 10px 0 4px;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.module-evidence-source code {
  display: block;
  color: var(--muted);
}

.module-evidence-source ul {
  margin: 0;
  padding-left: 18px;
}

.module-analysis p, .requirement-analysis p {
  margin: 10px 0 0;
  font-size: var(--ui-text-body);
  line-height: 1.7;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.requirement-evidence {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 16px;
  border-top: 1px solid var(--border);
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #fff;
}

.requirement-evidence-heading > b {
  min-width: 0;
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  line-height: 1.6;
  overflow-wrap: anywhere;
  color: var(--title);
}

.requirement-analysis, .evidence-references {
  margin-top: 10px;
}

.evidence-references p, .requirement-no-evidence {
  margin: 6px 0;
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.evidence-references code {
  display: block;
  margin: 6px 0;
  color: var(--muted);
}

.unplanned-details {
  margin-top: 16px;
}

.unplanned-item {
  padding: 16px 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--ui-text-body);
}

.unplanned-item p {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

/* Candidate editing stays explicit, with one confirmation bar. */

.lane-editor-head, .lane-readonly-head {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
}

.lane-editor-head span, .lane-readonly-head span, .lane-confirm-bar span, .lane-next-step span {
  font-size: var(--ui-text-helper);
  line-height: 1.6;
}

.lane-module-card {
  background: #fff;
  overflow: hidden;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
}

.lane-module-row {
  display: grid;
  grid-template-columns: 28px 150px minmax(190px,1.35fr) minmax(180px,1fr) auto 48px;
  align-items: center;
  background: #fff;
  padding: 12px 0;
  gap: 8px;
}

.lane-confirm-bar {
  align-items: center;
  padding: 16px;
  margin-top: 16px;
  border: 0;
  border-radius: 12px;
  background: #f5f5f7;
}

.generation-copy span, .generation-consent span, .generation-details p, .unknown-retry-panel span {
  font-size: var(--ui-text-helper);
}

.lane-readonly-modules {
  display: grid;
  grid-template-columns: repeat(2,minmax(0,1fr));
  gap: 0 24px;
}

.lane-readonly-modules article {
  padding: 16px 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
}

.lane-readonly-modules p {
  margin: 5px 0;
  color: var(--muted);
  font-size: var(--ui-text-helper);
  line-height: 1.6;
}

@media (max-width: 720px) {
  .lane-profile {
    padding: 20px;
  }
  .atlas-task-heading, .baseline-command-head, .baseline-go-row, .reconciliation-head, .requirement-evidence-heading {
    flex-direction: column;
    align-items: stretch;
  }
  .requirement-totals, .baseline-ready-strip {
    grid-template-columns: repeat(2,minmax(0,1fr));
    gap: 18px;
  }
  .requirement-overview {
    padding: 16px;
    background: #f5f5f7;
    border-radius: 12px;
  }
  .reanalysis-disclosure > summary span {
    display: block;
    margin: 4px 0 0;
  }
  .requirement-evidence {
    padding-left: 0;
    padding: 16px;
    border: 1px solid var(--border);
    border-top: 1px solid var(--border);
    border-radius: 12px;
    background: #fff;
  }
  .requirement-evidence-heading .mapping-pill {
    align-self: flex-start;
  }
  .lane-confirm-bar .lane-actions {
    width: 100%;
  }
  .lane-confirm-bar button {
    flex: 1 1 auto;
  }
}

.result-read-state, .result-readonly-note { font-size: var(--ui-text-helper); color: var(--muted); line-height: 1.6; }
.result-read-error { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding: 12px 16px; border: 1px solid var(--border); border-radius: 12px; }
.result-read-error p { margin: 0; font-size: var(--ui-text-helper); color: var(--danger); }
.profile-plan-details {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding: 16px 0;
}

.profile-plan-details > summary {
  color: var(--title);
  font-weight: var(--ui-weight-strong);
}

.lane-profile :is(button, input, textarea, select):focus-visible, .lane-profile summary:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 3px;
}
</style>
