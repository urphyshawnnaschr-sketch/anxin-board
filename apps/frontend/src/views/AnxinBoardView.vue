<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  getAnxinBoardReportHistory,
  getLatestAnxinBoardReport,
  getApprovedReportNarrative,
  getApprovedModuleNarrative
} from '../api/anxinBoardReports.js'
import { createNarrativeLoader, narrativeSourceLabel, narrativeScopeLabel, narrativeRiskLabel } from '../api/approvedReportNarrative.js'
import { createModuleNarrativeLoader } from '../api/approvedModuleNarrative.js'
import { createReportGitMetricsLoader, getApprovedReportGitMetrics } from '../api/approvedReportGitMetrics.js'
import { getProject } from '../api/projects.js'
import MailSendPanel from '../components/MailSendPanel.vue'
import WechatSendPanel from '../components/WechatSendPanel.vue'

const props = defineProps({
  projectId: {
    type: [Number, String],
    default: null
  }
})

const state = ref('loading')
const project = ref(null)
const report = ref(null)
const latestReport = ref(null)
const historyMode = ref(false)
const historyState = ref('idle')
const historyRecords = ref([])
const historyBeforeId = ref(null)
const historyCursorStack = ref([])
const selectedHistoryRecordId = ref(null)

const narrative = ref({ state: 'idle', content: null })
const narrativeLoader = createNarrativeLoader(getApprovedReportNarrative, value => { narrative.value = value })
const moduleNarrative = ref({ state: 'idle', value: null })
const moduleNarrativeLoader = createModuleNarrativeLoader(getApprovedModuleNarrative, value => { moduleNarrative.value = value })
const gitMetrics = ref({ state: 'idle', value: null })
const gitMetricsLoader = createReportGitMetricsLoader(getApprovedReportGitMetrics, value => { gitMetrics.value = value })
const moduleNotes = computed(() => new Map((moduleNarrative.value.value?.modules || []).map(row => [row.module_id, row])))
const baselineStatusLabel = status => ({ implemented: '已有实现证据', partial: '部分实现', unknown: '待核实' })[status]
const narrativeSections = [
  { key: 'code_change_summary', title: '本次具体工作' },
  { key: 'test_evidence', title: '检查与验证情况' },
  { key: 'risks', title: '分析判断与协助事项' },
  { key: 'unknown_items', title: '仍待核实' },
  { key: 'source_warnings', title: '分析依据的限制' }
]
function reloadNarrative() { return narrativeLoader.load(props.projectId, report.value) }
watch([() => props.projectId, report], reloadNarrative, { flush: 'sync' })
function reloadModuleNarrative() { return moduleNarrativeLoader.load(props.projectId, report.value) }
watch([() => props.projectId, report], reloadModuleNarrative, { flush: 'sync' })
function reloadGitMetrics() { return gitMetricsLoader.load(props.projectId, report.value) }
watch([() => props.projectId, report], reloadGitMetrics, { flush: 'sync' })
onUnmounted(() => { boardGeneration += 1; narrativeLoader.cancel(); moduleNarrativeLoader.cancel(); gitMetricsLoader.cancel() })

let boardGeneration = 0

const FORMAL_STAGES = new Set([
  '开发中',
  '等待联调',
  '等待测试',
  '测试中',
  '已完成',
  '暂时无法确认'
])
const FORMAL_TONES = new Set(['active', 'waiting', 'checking', 'done', 'unknown'])
const CHANGE_STATES = new Set(['ready', 'no_change', 'unavailable', 'partial'])
const V1_REPORT_KEYS = [
  'schema_version',
  'title',
  'motto',
  'project_name',
  'report_date',
  'module_count',
  'completed_module_count',
  'active_module_count',
  'unknown_module_count',
  'overall_message',
  'modules',
  'daily_change',
  'manager_supplement',
  'anxin_board_report_hash'
]
const V2_REPORT_KEYS = [
  'schema_version',
  'title',
  'motto',
  'project_name',
  'report_date',
  'profile_id',
  'profile_version_no',
  'profile_content_hash',
  'source_prd_id',
  'module_count',
  'completed_module_count',
  'active_module_count',
  'unknown_module_count',
  'overall_message',
  'modules',
  'daily_change',
  'manager_supplement',
  'anxin_board_report_hash'
]
const V3_PROVENANCE_KEYS = [
  'approval_snapshot_id', 'approval_snapshot_hash', 'report_version_id', 'report_version_no',
  'report_content_hash', 'validation_result_id', 'validation_result_hash', 'evidence_snapshot_id',
  'evidence_snapshot_hash', 'git_snapshot_id', 'git_facts_hash', 'git_branch', 'git_from_commit',
  'git_to_commit', 'prd_id', 'prd_source_hash', 'prd_parsed_hash', 'prd_structured_hash',
  'prd_document_fingerprint', 'model_execution_result_id', 'execution_result_hash', 'model_call_id',
  'call_identity_hash', 'provider', 'model_id', 'model_version', 'actual_model',
  'provider_runtime_fingerprint', 'rule_version', 'output_schema_version', 'benchmark_sample_pack_version',
  'qualification_hash', 'authorization_hash', 'supplement_version_id', 'supplement_content_hash',
  'supplement_provided_by', 'supplement_provided_at', 'supplement_provided_timezone',
  'supplement_source_type', 'confirmed_by', 'confirmed_at', 'confirmed_timezone',
  'confirmed_utc_offset_minutes', 'human_acknowledged'
]
const V3_REPORT_KEYS = [
  'schema_version',
  ...V2_REPORT_KEYS.filter((key) => key !== 'schema_version' && key !== 'anxin_board_report_hash'),
  ...V3_PROVENANCE_KEYS,
  'anxin_board_report_hash'
]
const MODULE_KEYS = [
  'module_id',
  'name',
  'stage',
  'display_stage',
  'tone',
  'summary',
  'next_step',
  'client_stage_summary_hash'
]
const CHANGE_KEYS = [
  'display_state',
  'headline',
  'summary',
  'highlights',
  'scope_note',
  'plain_language_change_summary_hash'
]
const HISTORY_RECORD_KEYS = [
  'id',
  'project_id',
  'schema_version',
  'report_date',
  'report_hash',
  'created_at',
  'report'
]
const V1_MODULES = [
  ['local_app', 'Windows 本地软件'],
  ['project_management', '项目管理'],
  ['git', '代码版本读取'],
  ['prd', '需求文档读取'],
  ['project_profile', '项目基础资料'],
  ['anxin_board', '安心看板自动生成'],
  ['ai_correction', 'AI 结果纠正'],
  ['formal_report', '正式 AI 报告'],
  ['email', '邮件发送'],
  ['report_evidence', '报告依据与留痕'],
  ['history', '历史报告查看']
]
const TONE_CLASS = {
  active: 'dev',
  waiting: 'waiting',
  checking: 'test',
  done: 'done',
  unknown: 'unknown'
}
const projectName = computed(() => report.value?.project_name || project.value?.name || '研发项目')
const reportBadge = computed(() => {
  if (state.value === 'loading') return '正在读取'
  if (historyMode.value && selectedHistoryRecordId.value != null) return '历史正式报告'
  if (historyMode.value) return '历史版本浏览'
  if (state.value === 'loaded') return '已审核报告'
  if (state.value === 'no-report') return '尚无正式报告'
  return '报告状态待确认'
})
const boardVisible = computed(() => state.value === 'loaded' && Boolean(report.value))
const boardTestId = computed(() => state.value === 'loaded' ? 'anxin-board' : 'anxin-board-template-preview')
const boardReport = computed(() => report.value)
const isV3Report = computed(() => report.value?.schema_version === 'anxin_board_report_v3')
const analysisModel = computed(() => {
  const value = report.value
  if (!value || !['provider', 'actual_model', 'model_version'].every(key => typeof value[key] === 'string' && value[key].trim())) return null
  return {
    name: `${value.provider === 'deepseek' ? 'DeepSeek' : value.provider} · ${value.actual_model}`,
    version: value.model_version
  }
})
const hasManagerSupplement = computed(() => isV3Report.value && Boolean(report.value?.supplement_version_id))
const clientActions = computed(() => narrative.value.state === 'loaded'
  ? narrative.value.content.risks.filter(item => item.risk_level === 'needs_client_action') : [])
const suspectedCount = computed(() => narrative.value.state === 'loaded'
  ? narrative.value.content.risks.filter(item => item.risk_level === 'suspected').length : 0)
const reportPresentationState = computed(() => {
  if (isV3Report.value) return `正式确认版 · ${report.value.confirmed_by} 已确认`
  if (state.value === 'loaded') return '历史报告'
  return '尚无已审核报告'
})
const reportCutoff = computed(() => isV3Report.value
  ? `${report.value.confirmed_at} · ${report.value.confirmed_timezone}`
  : '未记录')
const approvalAt = computed(() => isV3Report.value
  ? `${report.value.confirmed_at} · ${report.value.confirmed_timezone}`
  : '暂不可用（无 ApprovalSnapshot）')
const reportMetricCards = computed(() => gitMetrics.value.state === 'loaded'
  ? [['added_lines', '本次代码新增', '行'], ['deleted_lines', '本次代码删除', '行'], ['changed_file_count', '本次改动文件', '个']]
    .map(([key, label, unit]) => ({ key, label, unit, value: gitMetrics.value.value.metrics[key] }))
  : [])

function clientModuleSummary(item) {
  const generic = [
    '这项工作正在推进，已经进入实际开发。',
    '这部分主体工作已经完成，正在等相关功能连起来一起验证。',
    '这部分开发内容已经准备好，正在等待进入完整检查。',
    '这项功能已经进入检查阶段，正在确认是否稳定、有没有遗漏。',
    '目前掌握的信息还不足以判断这项工作的真实状态，因此暂不下结论。',
    '这项功能已完成当前约定范围内的开发。',
    '当前约定范围已完成。',
    '这项工作已经完成当前约定范围内的开发和检查，可以作为已完成内容展示。'
  ]
  if (generic.includes(item.summary)) return null
  return item.summary
}

function hasExactKeys(value, expected) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const keys = Object.keys(value)
  return keys.length === expected.length && expected.every((key) => keys.includes(key))
}

function isSha256(value) {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
}

function isPositiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0
}

function hasCommonReportEnvelope(value) {
  return (
    value.title === '安心看板' &&
    value.motto === '非己所安，不加于物' &&
    typeof value.project_name === 'string' &&
    value.project_name.trim() !== '' &&
    /^\d{4}-\d{2}-\d{2}$/.test(value.report_date) &&
    Number.isInteger(value.completed_module_count) &&
    Number.isInteger(value.active_module_count) &&
    Number.isInteger(value.unknown_module_count) &&
    value.completed_module_count >= 0 &&
    value.active_module_count >= 0 &&
    value.unknown_module_count >= 0 &&
    typeof value.overall_message === 'string' &&
    value.overall_message.trim() !== '' &&
    typeof value.manager_supplement === 'string' &&
    value.manager_supplement.trim() !== '' &&
    isSha256(value.anxin_board_report_hash) &&
    Array.isArray(value.modules)
  )
}

function isFormalModule(item, expectedId = null, expectedName = null) {
  return (
    hasExactKeys(item, MODULE_KEYS) &&
    typeof item.module_id === 'string' &&
    item.module_id.trim() !== '' &&
    (expectedId == null || item.module_id === expectedId) &&
    typeof item.name === 'string' &&
    item.name.trim() !== '' &&
    (expectedName == null || item.name === expectedName) &&
    FORMAL_STAGES.has(item.stage) &&
    item.display_stage === item.stage &&
    FORMAL_TONES.has(item.tone) &&
    typeof item.summary === 'string' &&
    item.summary.trim() !== '' &&
    typeof item.next_step === 'string' &&
    item.next_step.trim() !== '' &&
    isSha256(item.client_stage_summary_hash)
  )
}

function hasValidDailyChange(value) {
  const change = value.daily_change
  if (
    !hasExactKeys(change, CHANGE_KEYS) ||
    !CHANGE_STATES.has(change.display_state) ||
    typeof change.headline !== 'string' ||
    change.headline.trim() === '' ||
    typeof change.summary !== 'string' ||
    change.summary.trim() === '' ||
    typeof change.scope_note !== 'string' ||
    change.scope_note.trim() === '' ||
    !isSha256(change.plain_language_change_summary_hash) ||
    !Array.isArray(change.highlights) ||
    change.highlights.length > 6
  ) {
    return false
  }
  return change.highlights.every((highlight) => (
    hasExactKeys(highlight, ['label', 'value', 'unit']) &&
    typeof highlight.label === 'string' &&
    highlight.label.trim() !== '' &&
    Number.isInteger(highlight.value) &&
    highlight.value >= 0 &&
    typeof highlight.unit === 'string' &&
    highlight.unit.trim() !== ''
  ))
}

function isFormalReportV1(value) {
  if (!hasExactKeys(value, V1_REPORT_KEYS) || value.schema_version !== 'anxin_board_report_v1') return false
  if (!hasCommonReportEnvelope(value) || value.module_count !== V1_MODULES.length || value.modules.length !== V1_MODULES.length) return false
  if (value.completed_module_count + value.active_module_count + value.unknown_module_count !== V1_MODULES.length) return false

  for (let index = 0; index < V1_MODULES.length; index += 1) {
    const [expectedId, expectedName] = V1_MODULES[index]
    if (!isFormalModule(value.modules[index], expectedId, expectedName)) return false
  }
  return hasValidDailyChange(value)
}

function isFormalReportV2(value) {
  if (!hasExactKeys(value, V2_REPORT_KEYS) || value.schema_version !== 'anxin_board_report_v2') return false
  if (
    !hasCommonReportEnvelope(value) ||
    !isPositiveSafeInteger(value.profile_id) ||
    !isPositiveSafeInteger(value.profile_version_no) ||
    !isSha256(value.profile_content_hash) ||
    !isPositiveSafeInteger(value.source_prd_id) ||
    !isPositiveSafeInteger(value.module_count) ||
    value.modules.length !== value.module_count ||
    value.completed_module_count + value.active_module_count + value.unknown_module_count !== value.module_count
  ) {
    return false
  }

  const seenIds = new Set()
  for (const item of value.modules) {
    if (!isFormalModule(item)) return false
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(item.module_id) || item.name.length > 100 || seenIds.has(item.module_id)) return false
    seenIds.add(item.module_id)
  }
  return hasValidDailyChange(value)
}

function isFormalReportV3(value) {
  if (!hasExactKeys(value, V3_REPORT_KEYS) || value.schema_version !== 'anxin_board_report_v3') return false
  const base = Object.fromEntries(V2_REPORT_KEYS.map((key) => {
    if (key === 'schema_version') return [key, 'anxin_board_report_v2']
    return [key, value[key]]
  }))
  if (!isFormalReportV2(base)) return false

  const positiveIds = [
    'approval_snapshot_id', 'report_version_id', 'report_version_no', 'validation_result_id',
    'evidence_snapshot_id', 'git_snapshot_id', 'prd_id', 'model_execution_result_id', 'model_call_id'
  ]
  if (!positiveIds.every((key) => isPositiveSafeInteger(value[key]))) return false
  const hashes = [
    'approval_snapshot_hash', 'report_content_hash', 'validation_result_hash', 'evidence_snapshot_hash',
    'git_facts_hash', 'prd_source_hash', 'prd_parsed_hash', 'prd_structured_hash',
    'prd_document_fingerprint', 'execution_result_hash', 'call_identity_hash', 'qualification_hash',
    'authorization_hash'
  ]
  if (!hashes.every((key) => isSha256(value[key]))) return false
  const strings = [
    'git_branch', 'git_from_commit', 'git_to_commit', 'provider', 'model_id', 'model_version', 'actual_model',
    'provider_runtime_fingerprint', 'rule_version', 'output_schema_version', 'benchmark_sample_pack_version',
    'confirmed_by', 'confirmed_at', 'confirmed_timezone'
  ]
  if (!strings.every((key) => typeof value[key] === 'string' && value[key].trim() !== '')) return false
  if (!Number.isInteger(value.confirmed_utc_offset_minutes) || value.confirmed_utc_offset_minutes < -840 || value.confirmed_utc_offset_minutes > 840) return false
  if (value.human_acknowledged !== true) return false
  if (value.supplement_version_id == null) {
    if ([
      value.supplement_content_hash, value.supplement_provided_by, value.supplement_provided_at,
      value.supplement_provided_timezone, value.supplement_source_type
    ].some((item) => item != null)) return false
  } else {
    if (!isPositiveSafeInteger(value.supplement_version_id) || !isSha256(value.supplement_content_hash)) return false
    if (![value.supplement_provided_by, value.supplement_provided_at, value.supplement_provided_timezone, value.supplement_source_type]
      .every((item) => typeof item === 'string' && item.trim() !== '')) return false
  }
  return true
}

function isFormalReport(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (value.schema_version === 'anxin_board_report_v1') return isFormalReportV1(value)
  if (value.schema_version === 'anxin_board_report_v2') return isFormalReportV2(value)
  if (value.schema_version === 'anxin_board_report_v3') return isFormalReportV3(value)
  return false
}

function isFormalHistoryRecord(value, expectedProjectId) {
  const numericProjectId = Number(expectedProjectId)
  return (
    hasExactKeys(value, HISTORY_RECORD_KEYS) &&
    Number.isSafeInteger(value.id) &&
    value.id > 0 &&
    Number.isSafeInteger(value.project_id) &&
    value.project_id === numericProjectId &&
    ['anxin_board_report_v1', 'anxin_board_report_v2', 'anxin_board_report_v3'].includes(value.schema_version) &&
    /^\d{4}-\d{2}-\d{2}$/.test(value.report_date) &&
    isSha256(value.report_hash) &&
    typeof value.created_at === 'string' &&
    value.created_at.trim() !== '' &&
    isFormalReport(value.report) &&
    value.schema_version === value.report.schema_version &&
    value.report_date === value.report.report_date &&
    value.report_hash === value.report.anxin_board_report_hash
  )
}

function statusClass(tone) {
  return TONE_CLASS[tone] || 'unknown'
}

function resetHistoryState() {
  historyMode.value = false
  historyState.value = 'idle'
  historyRecords.value = []
  historyBeforeId.value = null
  historyCursorStack.value = []
  selectedHistoryRecordId.value = null
}

function boardRequestIsCurrent(generation, projectId) {
  return generation === boardGeneration && String(props.projectId ?? '') === String(projectId ?? '')
}

async function loadBoard(id) {
  const generation = ++boardGeneration
  state.value = 'loading'
  project.value = null
  report.value = null
  latestReport.value = null
  resetHistoryState()

  if (id == null) {
    state.value = 'failed'
    return
  }

  try {
    const projectResponse = await getProject(id)
    if (!boardRequestIsCurrent(generation, id)) return
    if (projectResponse.status === 404) {
      state.value = 'not-found'
      return
    }
    if (!projectResponse.ok || String(projectResponse.body?.id ?? '') !== String(id)) {
      throw new Error('project load failed')
    }
    project.value = projectResponse.body

    const reportResponse = await getLatestAnxinBoardReport(id)
    if (!boardRequestIsCurrent(generation, id)) return
    if (
      reportResponse.status === 404 &&
      reportResponse.body?.detail?.code === 'ANXIN_BOARD_REPORT_NOT_AVAILABLE'
    ) {
      state.value = 'no-report'
      return
    }
    if (!reportResponse.ok || !isFormalReport(reportResponse.body)) {
      throw new Error('report load failed')
    }
    latestReport.value = reportResponse.body
    report.value = reportResponse.body
    state.value = 'loaded'
  } catch {
    if (boardRequestIsCurrent(generation, id)) state.value = 'failed'
  }
}

async function loadHistoryPage(beforeId, { rememberCurrent = false, consumePrevious = false } = {}) {
  const generation = boardGeneration
  const projectId = props.projectId
  if (!boardRequestIsCurrent(generation, projectId)) return false
  historyState.value = 'loading'
  try {
    const response = await getAnxinBoardReportHistory(projectId, {
      beforeId,
      limit: 10
    })
    if (!boardRequestIsCurrent(generation, projectId)) return false
    if (response.status === 404 && response.body?.detail?.code === 'PROJECT_NOT_FOUND') {
      state.value = 'not-found'
      report.value = null
      latestReport.value = null
      resetHistoryState()
      return false
    }
    if (!response.ok || !Array.isArray(response.body)) {
      throw new Error('history load failed')
    }
    if (!response.body.every((record) => isFormalHistoryRecord(record, projectId))) {
      throw new Error('history payload invalid')
    }

    if (rememberCurrent) historyCursorStack.value.push(historyBeforeId.value)
    if (consumePrevious) historyCursorStack.value.pop()
    historyBeforeId.value = beforeId
    historyRecords.value = response.body
    selectedHistoryRecordId.value = null
    report.value = latestReport.value
    historyState.value = response.body.length ? 'loaded' : 'empty'
    return true
  } catch {
    if (boardRequestIsCurrent(generation, projectId)) historyState.value = 'failed'
    return false
  }
}

async function enterHistory() {
  historyMode.value = true
  historyRecords.value = []
  historyBeforeId.value = null
  historyCursorStack.value = []
  selectedHistoryRecordId.value = null
  report.value = latestReport.value
  await loadHistoryPage(null)
}

function selectHistoryRecord(record) {
  selectedHistoryRecordId.value = record.id
  report.value = record.report
}

async function loadOlderHistory() {
  if (!historyRecords.value.length || historyState.value === 'loading') return
  const lastRecord = historyRecords.value[historyRecords.value.length - 1]
  await loadHistoryPage(lastRecord.id, { rememberCurrent: true })
}

async function loadPreviousHistory() {
  if (!historyCursorStack.value.length || historyState.value === 'loading') return
  const previousCursor = historyCursorStack.value[historyCursorStack.value.length - 1]
  await loadHistoryPage(previousCursor, { consumePrevious: true })
}

function returnLatest() {
  resetHistoryState()
  report.value = latestReport.value
}

function formatHistoryRecord(record) {
  return `${record.report_date} · 修订 #${record.id} · ${record.created_at}`
}

function goWorkspace() {
  if (props.projectId != null) window.location.hash = `#/projects/${props.projectId}`
}

onMounted(() => loadBoard(props.projectId))
watch(() => props.projectId, (id) => loadBoard(id))
</script>

<template>
  <WechatSendPanel
    v-if="projectId != null"
    :project-id="projectId"
    :displayed-report-version-id="boardVisible ? boardReport.report_version_id : null"
    :displayed-report-hash="boardVisible ? boardReport.anxin_board_report_hash : null"
    :module-narrative-state="moduleNarrative.state"
    :displayed-module-narrative-hash="moduleNarrative.value?.module_narrative_hash ?? null"
    :displayed-git-metrics-state="gitMetrics.state"
  />
  <MailSendPanel
    v-if="projectId != null"
    :project-id="projectId"
    :displayed-report-version-id="boardVisible ? boardReport.report_version_id : null"
    :displayed-report-hash="boardVisible ? boardReport.anxin_board_report_hash : null"
    :module-narrative-state="moduleNarrative.state"
    :displayed-module-narrative-hash="moduleNarrative.value?.module_narrative_hash ?? null"
    :displayed-git-metrics-state="gitMetrics.state"
  />
  <section class="anxin-board-page" aria-label="安心看板">
    <div class="preview-toolbar">
      <button class="preview-back" type="button" @click="goWorkspace">← 返回项目工作台</button>
      <span class="preview-state" :class="{ ready: state === 'loaded' }">{{ reportBadge }}</span>
    </div>

    <section v-if="state === 'loading'" class="preview-state-card">正在读取安心看板...</section>
    <section v-else-if="state === 'not-found'" class="preview-state-card">项目不存在或已被删除。</section>
    <section v-else-if="state === 'failed'" class="preview-state-card">安心看板数据无法确认，请稍后重试。</section>
    <section v-else-if="state === 'no-report'" class="preview-state-card no-report" data-testid="anxin-board-empty">
      <strong>{{ projectName }}</strong>
      <span>当前还没有可展示的正式安心看板。</span>
      <p>请到项目工作台查看分析和报告审核进度。报告审核通过后，这里会显示可分享的项目进展。</p>
      <button class="history-action primary" type="button" @click="goWorkspace">前往项目工作台</button>
    </section>

    <section v-if="state === 'loaded' && report" class="history-panel" data-testid="anxin-board-history-controls">
      <div class="history-toolbar">
        <div>
          <strong>报告版本</strong>
          <span>{{ historyMode ? '正在浏览历史报告' : '当前显示最新报告' }}</span>
        </div>
        <button v-if="!historyMode" type="button" class="history-action primary" @click="enterHistory">查看历史版本</button>
        <button v-else type="button" class="history-action" @click="returnLatest">返回最新版本</button>
      </div>

      <div v-if="historyMode" class="history-browser">
        <p v-if="historyState === 'loading'" class="history-message" data-testid="history-loading">正在加载历史版本...</p>
        <p v-else-if="historyState === 'failed'" class="history-message error" data-testid="history-error">历史报告数据无法确认</p>
        <p v-else-if="historyState === 'empty'" class="history-message" data-testid="history-empty">暂无更早历史版本</p>

        <div v-if="historyRecords.length" class="history-list" data-testid="history-list">
          <button
            v-for="record in historyRecords"
            :key="record.id"
            type="button"
            class="history-record"
            :class="{ selected: selectedHistoryRecordId === record.id }"
            @click="selectHistoryRecord(record)"
          >
            {{ formatHistoryRecord(record) }}
          </button>
        </div>

        <div class="history-pagination">
          <button
            type="button"
            class="history-action"
            :disabled="!historyCursorStack.length || historyState === 'loading'"
            @click="loadPreviousHistory"
          >返回上一页</button>
          <button
            type="button"
            class="history-action"
            :disabled="!historyRecords.length || historyState === 'loading'"
            @click="loadOlderHistory"
          >更早一页</button>
        </div>
      </div>
    </section>

    <main v-if="boardVisible" class="board-shell" :data-testid="boardTestId" data-schema="anxin_board_report_v1-v2-v3_compat">
      <header class="board-hero">
        <div class="hero-main">
          <div>
            <div class="kicker">研发进展</div>
            <div class="hero-title-lock">
              <h1>{{ boardReport.title }}</h1>
              <img class="calligraphy-motto" src="../assets/anxin-board-calligraphy.png" :alt="boardReport.motto" />
            </div>
          </div>
          <div class="hero-report">
            <strong>{{ boardReport.project_name }}</strong>
            报告日期：{{ boardReport.report_date }}<br />
            报告状态：{{ reportPresentationState }}<br />
            截至时间：{{ reportCutoff }}
          </div>
        </div>

        <div class="today-title">本次代码变化</div>
        <div v-if="reportMetricCards.length" class="today-grid" data-testid="anxin-board-git-metrics">
          <div v-for="metric in reportMetricCards" :key="metric.key" class="today-item" :data-metric="metric.key">
            <div class="today-label">{{ metric.label }}</div>
            <div class="today-value">
              <strong>{{ metric.value }}</strong><span>{{ metric.unit }}</span>
            </div>
          </div>
        </div>
        <template v-if="gitMetrics.state === 'loaded'">
          <div class="today-note">统计基于本报告绑定的代码版本范围，仅反映变化量。</div>
          <details class="today-note" data-testid="git-metrics-details">
            <summary>查看统计范围</summary>
            <div data-testid="git-metrics-range" style="overflow-wrap:anywhere">
              分支：{{ gitMetrics.value.source_git_facts.branch }}<br />
              起点：{{ gitMetrics.value.source_git_facts.from_commit }}<br />
              终点：{{ gitMetrics.value.source_git_facts.to_commit }}
            </div>
          </details>
        </template>
        <div v-else-if="gitMetrics.state === 'loading'" class="today-note" data-testid="git-metrics-loading">正在读取本报告的代码变化统计…</div>
        <div v-else-if="gitMetrics.state === 'failed'" class="today-note" data-testid="git-metrics-error">
          代码变化统计未能读取或验证，暂不展示数字。
          <button class="history-action" type="button" @click="reloadGitMetrics">重新读取代码统计</button>
        </div>
        <div v-else class="today-note" data-testid="git-metrics-absent">本报告未保存可核对的代码变化统计。</div>

      </header>

      <section class="board-card" aria-label="本次工作与进展">
        <div class="section-head"><h2 class="section-title"><span class="num">01</span>本次工作与进展</h2></div>
        <template v-if="isV3Report">
          <div v-if="hasManagerSupplement">
            <h3 class="narrative-title">项目经理说明</h3>
            <div class="manager-box">
              <p class="narrative-text">{{ boardReport.manager_supplement }}</p>
              <div class="manager-provenance">
                补充来源：{{ narrativeSourceLabel(boardReport.supplement_source_type) }}<br />
                提供人：{{ boardReport.supplement_provided_by }}<br />
                提供时间：{{ boardReport.supplement_provided_at }} · {{ boardReport.supplement_provided_timezone }}
              </div>
            </div>
          </div>
          <p v-if="narrative.state === 'loading'" role="status">正在读取这份已审核报告的工作内容…</p>
          <div v-else-if="narrative.state === 'failed'" role="alert" class="narrative-failure">
            <p>本次工作内容未能载入，不能据此判断没有工作或问题。下方功能状态仍来自当前选中的报告。</p>
            <button type="button" class="history-action" @click="reloadNarrative">重新读取工作内容</button>
          </div>
          <div v-else-if="narrative.state === 'loaded'">
            <div v-if="!hasManagerSupplement" class="summary-box">
              <h3 class="narrative-title">AI 摘要（原文）</h3>
              <p class="narrative-text">{{ narrative.content.plain_summary }}</p>
            </div>
            <article v-for="(item, index) in clientActions" :key="index" class="client-action">
              <h3 class="narrative-title">需甲方协助</h3>
              <p class="narrative-text">{{ item.content }}</p>
              <div class="narrative-tags">{{ narrativeSourceLabel(item.source_type) }} · AI 原文条目</div>
            </article>
            <p v-if="suspectedCount" class="helper">AI 另列出 {{ suspectedCount }} 项尚待验证的分析判断，详见下方原文与依据。</p>
          </div>
        </template>
        <div v-else class="summary-box">
          <p><strong>{{ boardReport.daily_change.headline }}</strong></p>
          <p>{{ boardReport.daily_change.summary }}</p>
          <p class="helper">历史报告保留原有内容，不补写未记录的工作细节。</p>
        </div>
      </section>

      <section class="board-card">
        <div class="section-head">
          <h2 class="section-title"><span class="num">02</span>当前功能状态</h2>
          <span class="narrative-context">本报告记录的全项目状态，不代表本次新增成果</span>
        </div>
        <div v-if="moduleNarrative.state === 'loaded'" class="helper" data-testid="module-narrative-provenance">
          <p>模块说明依据已保存的历史代码盘点；代码证据不等于实际运行或验收。本次变化见项目经理说明。</p>
          <p>模块说明已由 {{ moduleNarrative.value.confirmed_by }} 确认 · {{ moduleNarrative.value.confirmed_at }}；此说明独立确认，与本报告原审批分开记录。</p>
        </div>
        <div v-else-if="moduleNarrative.state === 'failed'" class="helper" data-testid="module-narrative-error">
          <p>具体模块说明未能安全载入，未使用其他版本内容替代。</p>
          <button type="button" @click="reloadModuleNarrative">重新读取模块说明</button>
        </div>
        <p v-else-if="moduleNarrative.state === 'loading'" class="helper">正在读取已确认模块说明...</p>
        <p v-else class="helper">本版未另行确认具体模块说明；仅有阶段模板的条目不补写能力或缺项。</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>功能</th><th>现在到哪了</th><th>具体进展与待完善事项</th></tr></thead>
            <tbody>
              <tr v-for="item in boardReport.modules" :key="item.module_id">
                <td class="module-name">{{ item.name }}</td>
                <td><span class="status" :class="statusClass(item.tone)">{{ item.display_stage }}</span></td>
                <td>
                  <template v-if="moduleNotes.has(item.module_id)">
                    <div class="helper">已有依据</div>
                    <div>{{ moduleNotes.get(item.module_id).summary }}</div>
                    <div class="helper" style="margin-top:9px">待完善/待核实</div>
                    <div>{{ moduleNotes.get(item.module_id).remaining }}</div>
                    <details :data-testid="`module-original-${item.module_id}`" class="module-original">
                      <summary>查看原始需求结论与依据</summary>
                      <article v-for="original in moduleNotes.get(item.module_id).source_requirements" :key="original.requirement_index">
                        <h4>{{ original.requirement_text }}</h4>
                        <p class="helper">{{ baselineStatusLabel(original.status) }} · 历史代码盘点原文</p>
                        <p class="narrative-text">{{ original.rationale }}</p>
                        <p v-for="ref in original.evidence_ids" :key="ref" class="narrative-text">{{ ref }}</p>
                      </article>
                    </details>
                  </template>
                  <template v-else-if="moduleNarrative.state === 'absent' && clientModuleSummary(item)">
                    <div>{{ clientModuleSummary(item) }}</div>
                    <div class="next-step">建议下一步：{{ item.next_step }}</div>
                  </template>
                  <span v-else class="helper">—</span>
                </td>
              </tr>
              <tr v-if="!boardReport.modules.length" class="unavailable-row">
                <td class="module-name">功能数据</td>
                <td><span class="status unknown">暂时无法确认</span></td>
                <td>
                  <div>当前尚无可核实的正式报告功能进展数据。</div>
                  <div class="next-step">建议下一步：数据接通后仍在这一张冻结模板中原位展示，不另做替代页面。</div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="narrative.state === 'loaded' && narrative.unassociatedFeatures?.length" class="narrative-section" data-testid="unassociated-features">
          <h3 class="narrative-title">尚未关联到已确认功能模块</h3>
          <p class="helper">以下保留本报告的原始功能判断。名称尚不能唯一对应到上表模块，因此不改变上表状态；仍需核对关联关系。</p>
          <article v-for="(item, index) in narrative.unassociatedFeatures" :key="index" class="narrative-item">
            <h4 class="narrative-title">{{ item.feature }}</h4>
            <p>原报告判断：{{ item.stage }}</p>
            <div class="narrative-tags"><span>{{ narrativeSourceLabel(item.source_type) }}</span><span>{{ narrativeScopeLabel(item.implementation_scope) }}</span></div>
            <details class="narrative-evidence"><summary>查看依据引用（{{ item.evidence_ids.length }}）</summary>
              <p>引用编号不代表实际运行或验收通过。</p>
              <ul><li v-for="(id, refIndex) in item.evidence_ids" :key="refIndex">{{ id }}</li></ul>
            </details>
          </article>
        </div>
      </section>

      <section v-if="isV3Report" class="board-card">
        <div class="section-head"><h2 class="section-title"><span class="num">03</span>AI 分析原文与依据</h2></div>
        <details v-if="narrative.state === 'loaded'" :key="boardReport.anxin_board_report_hash" :open="!hasManagerSupplement" class="narrative-original">
          <summary>查看完整 AI 原文与依据</summary>
          <div data-testid="approved-report-narrative">
            <p class="helper">以下保留生成时的完整分析原文；项目经理说明与 AI 原文分别展示。</p>
            <h3 class="narrative-title">AI 摘要（原文）</h3>
            <p class="narrative-text">{{ narrative.content.plain_summary }}</p>
            <section v-for="section in narrativeSections.filter(item => narrative.content[item.key].length)" :key="section.key" class="narrative-section">
              <h3 class="narrative-title">{{ section.title }}</h3>
              <article v-for="(item, index) in narrative.content[section.key]" :key="index" class="narrative-item">
                <div class="narrative-tags">
                  <span>{{ narrativeSourceLabel(item.source_type) }}</span>
                  <span v-if="item.implementation_scope">{{ narrativeScopeLabel(item.implementation_scope) }}</span>
                  <span v-if="item.risk_level">{{ narrativeRiskLabel(item.risk_level) }}</span>
                </div>
                <p class="narrative-text">{{ item.content }}</p>
                <details class="narrative-evidence"><summary>查看依据引用（{{ item.evidence_ids.length }}）</summary>
                  <ul><li v-for="id in item.evidence_ids" :key="id">{{ id }}</li></ul>
                </details>
              </article>
            </section>
          </div>
        </details>
        <p v-else class="helper">原文尚未载入，请查看上方的读取状态。</p>
      </section>
      <section v-else class="board-card">
        <div class="section-head"><h2 class="section-title"><span class="num">03</span>项目经理补充与下一步</h2></div>
        <div class="manager-box">
          <p>{{ boardReport.manager_supplement }}</p>
          <div v-if="isV3Report && boardReport.supplement_version_id" class="manager-provenance">

            提供人：{{ boardReport.supplement_provided_by }}<br />
            提供时间：{{ boardReport.supplement_provided_at }}
          </div>
        </div>
      </section>

      <section class="board-card">
        <div class="section-head"><h2 class="section-title"><span class="num">04</span>报告信息</h2></div>

        <section class="basis-group" aria-label="基本报告信息">
          <h3 class="basis-group-title">基本报告信息</h3>
          <div class="basis">
            <div class="basis-item"><div class="basis-label">项目名称</div><div class="basis-value">{{ boardReport.project_name }}</div></div>
            <div class="basis-item"><div class="basis-label">报告类型</div><div class="basis-value">安心看板</div></div>
            <div class="basis-item"><div class="basis-label">报告日期</div><div class="basis-value">{{ boardReport.report_date }}</div></div>
            <div class="basis-item"><div class="basis-label">报告状态</div><div class="basis-value">{{ reportPresentationState }}</div></div>
            <div class="basis-item"><div class="basis-label">截至时间</div><div class="basis-value">{{ reportCutoff }}</div></div>
            <div v-if="isV3Report" class="basis-item"><div class="basis-label">报告版本</div><div class="basis-value">V{{ boardReport.report_version_no }}</div></div>
            <div class="basis-item" data-testid="analysis-model">
              <div class="basis-label">AI 分析模型</div>
              <div class="basis-value" data-testid="analysis-model-name">{{ analysisModel?.name || '本版未记录' }}</div>
              <small v-if="analysisModel" class="helper" data-testid="analysis-model-version">版本记录：{{ analysisModel.version }}</small>
            </div>
          </div>
        </section>

        <section v-if="isV3Report" class="basis-group" aria-label="确认信息">
          <h3 class="basis-group-title">审核信息</h3>
          <div class="basis">
            <div class="basis-item"><div class="basis-label">审核人</div><div class="basis-value">{{ boardReport.confirmed_by }}</div></div>
            <div class="basis-item"><div class="basis-label">审核时间</div><div class="basis-value">{{ approvalAt }}</div></div>
          </div>
        </section>

        <details v-if="boardReport.daily_change.scope_note" class="narrative-evidence">
          <summary>查看原报告的分析范围说明</summary>
          <p class="narrative-text">{{ boardReport.daily_change.scope_note }}</p>
        </details>
      </section>

      <footer class="board-footer">安心看板 · {{ boardReport.report_date }}</footer>
    </main>
  </section>
</template>

<style scoped>
.narrative-context{font-size:13px;color:#68758a}
.client-action{margin-top:16px;padding:16px 18px;border:1px solid #edbe73;border-radius:10px;background:#fff6df;color:#62420f}
.narrative-original>summary{cursor:pointer;color:#3852d0;font-weight:600}.narrative-original[open]>summary{margin-bottom:18px}
.narrative-title{font-size:16px;font-weight:600;margin:0 0 10px;color:#182b48}
.narrative-section{margin-top:24px}
.narrative-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;margin:8px 0}
.narrative-item{padding:16px 0;border-bottom:1px solid #e8edf3}
.narrative-tags{display:flex;flex-wrap:wrap;gap:8px;color:#607087;font-size:13px}
.narrative-tags span{background:#f3f6fa;border-radius:6px;padding:3px 8px}
.narrative-evidence{font-size:13px;color:#607087;overflow-wrap:anywhere;margin-top:10px}
.narrative-evidence summary{cursor:pointer}.narrative-failure{color:#8c4d17}

.anxin-board-page{display:flex;flex-direction:column;gap:16px;width:min(1120px,calc(100% - 28px));margin:26px auto 54px}.preview-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px}.preview-back{border:0;background:transparent;color:#4f6ef7;font:inherit;font-weight:750;cursor:pointer;padding:6px 0}.preview-state{display:inline-flex;padding:5px 10px;border-radius:999px;background:#fff7e8;color:#8a5b12;font-size:11px;font-weight:800}.preview-state.ready{background:#eaf9f4;color:#16866f}.preview-state-card{width:100%;max-width:1060px;margin:0 auto;padding:24px;border:1px solid #e2e8f0;border-radius:16px;background:#fff;color:#68758a}.no-report{display:flex;flex-direction:column;gap:6px}.no-report strong{color:#1f2a3d;font-size:18px}.no-report small{color:#8792a3}.history-panel{width:100%;max-width:1060px;margin:0 auto;padding:14px 16px;border:1px solid #dfe5f1;border-radius:14px;background:#fff;box-shadow:0 8px 22px rgba(15,23,42,.05)}.history-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px}.history-toolbar>div{display:flex;flex-direction:column;gap:2px}.history-toolbar strong{color:#1f2a3d;font-size:13px}.history-toolbar span{color:#7a8799;font-size:11px}.history-browser{display:flex;flex-direction:column;gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid #edf1f6}.history-message{margin:0;color:#68758a;font-size:12px}.history-message.error{color:#a33a3a}.history-list{display:grid;gap:7px}.history-record,.history-action{border:1px solid #dfe5f1;border-radius:9px;background:#fff;color:#3b485e;font:inherit;font-size:12px;cursor:pointer}.history-record{padding:9px 11px;text-align:left}.history-record:hover,.history-record.selected{border-color:#9baafc;background:#f5f7ff;color:#3852d0}.history-action{padding:7px 10px;font-weight:750}.history-action.primary{border-color:#4f6ef7;background:#4f6ef7;color:#fff}.history-action:disabled{cursor:not-allowed;opacity:.45}.history-pagination{display:flex;justify-content:flex-end;gap:8px}
.board-shell{--primary:#4f6ef7;--primary-dark:#3852d0;--primary-soft:#eef2ff;--success:#16866f;--success-soft:#eaf9f4;--warning:#bd7414;--muted:#68758a;--ink:#1f2a3d;--text:#3b485e;--line:#e2e8f0;--card:#fff;--soft:#f8fafc;--shadow:0 10px 30px rgba(15,23,42,.075);width:100%;max-width:1060px;margin:0 auto;color:var(--text);line-height:1.68}.board-hero{padding:34px 38px 26px;border-radius:22px;color:#fff;overflow:hidden;background:radial-gradient(circle at 84% 16%,rgba(0,212,170,.18),transparent 28%),radial-gradient(circle at 11% 94%,rgba(79,110,247,.16),transparent 31%),linear-gradient(135deg,#1c2738,#2a3857 52%,#4f6ef7);box-shadow:0 18px 44px rgba(30,42,58,.20)}.hero-main{display:flex;justify-content:space-between;gap:28px;align-items:flex-start}.kicker{display:inline-block;padding:5px 12px;border:1px solid rgba(255,255,255,.18);border-radius:999px;background:rgba(255,255,255,.1);font-size:12px}.hero-title-lock{display:flex;flex-direction:column;align-items:flex-start;width:192px;max-width:192px;min-width:192px}.hero-title-lock h1{display:block;width:192px;margin:14px 0 0;color:#fff;font-size:44px;line-height:1.12;letter-spacing:.09em;white-space:nowrap}.calligraphy-motto{display:block;width:192px;max-width:192px;height:auto;margin-top:8px;object-fit:contain;filter:invert(1);user-select:none;-webkit-user-drag:none}.hero-report{min-width:270px;text-align:right;color:rgba(255,255,255,.76);font-size:13px}.hero-report strong{display:block;margin-bottom:6px;color:#fff;font-size:20px}.today-title{margin-top:25px;color:rgba(255,255,255,.7);font-size:12px;font-weight:700;letter-spacing:.08em}.today-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:9px}.today-item{padding:13px 14px;border:1px solid rgba(255,255,255,.13);border-radius:12px;background:rgba(255,255,255,.08)}.today-label{color:rgba(255,255,255,.62);font-size:11px}.today-value{margin-top:4px;color:#fff;font-size:15px;font-weight:750}.today-value strong{font-size:23px;margin-right:4px}.today-unavailable{margin-top:2px;color:rgba(255,255,255,.56);font-size:10px}.today-note{margin-top:9px;color:rgba(255,255,255,.66);font-size:10px}.today-note-secondary{color:rgba(255,255,255,.52)}.board-card{margin-top:18px;padding:23px 24px;border:1px solid var(--line);border-radius:18px;background:var(--card);box-shadow:var(--shadow)}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:15px}.section-title{display:flex;align-items:center;gap:10px;margin:0;color:var(--ink);font-size:20px;font-weight:850}.num{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--primary),#6b85fa);color:#fff;font-size:12px}.helper{color:var(--muted);font-size:11px}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:13px}table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}thead th{padding:12px 14px;background:var(--primary);color:#fff;text-align:left;font-size:12px}tbody td{padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:middle}tbody tr:nth-child(even){background:#fafbfd}tbody tr:last-child td{border-bottom:none}.module-name{color:var(--ink);font-weight:750}.next-step{margin-top:4px;color:#7a8799;font-size:11px}.status{display:inline-flex;padding:4px 9px;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap}.status.done{background:var(--success-soft);color:#166f56}.status.dev{background:var(--primary-soft);color:var(--primary-dark)}.status.waiting{background:#f1efff;color:#6653bd}.status.test{background:#fff5e6;color:#bd7414}.status.unknown{background:#f1f5f9;color:#64748b}.summary-box{padding:18px 20px;border:1px solid #d9e0f2;border-left:4px solid var(--primary);border-radius:0 13px 13px 0;background:linear-gradient(180deg,#f8faff,#f4f7ff)}.summary-box p{margin:0 0 9px}.summary-box p:last-child{margin:0}.manager-box{padding:17px 19px;border:1px solid #f1d8a9;border-left:4px solid var(--warning);border-radius:0 13px 13px 0;background:#fffdf7;color:#4a3a20}.manager-box p{margin:0}.manager-provenance{margin-top:12px;padding-top:10px;border-top:1px solid #f0dfbf;color:#806b48;font-size:11px}.basis-group{margin-top:18px}.basis-group:first-of-type{margin-top:0}.basis-group-title{margin:0 0 10px;color:var(--ink);font-size:13px;font-weight:800}.basis{display:grid;grid-template-columns:repeat(3,1fr);gap:11px}.basis-item{padding:12px 13px;border:1px solid var(--line);border-radius:10px;background:var(--soft)}.basis-wide{grid-column:1/-1}.basis-label{margin-bottom:4px;color:var(--muted);font-size:11px}.basis-value{color:var(--ink);font-size:13px;font-weight:700;word-break:break-word}.responsibility-box{padding:14px 15px;border:1px dashed #cfd7e6;border-radius:10px;background:var(--soft);color:#58677c;font-size:12px}.scope-note{margin-top:13px;color:#7a8698;text-align:center;font-size:10px}.board-footer{padding-top:16px;color:var(--muted);text-align:center;font-size:11px}@media(max-width:760px){.anxin-board-page{width:min(100% - 16px,1120px);margin-top:8px}.preview-toolbar,.history-toolbar{align-items:flex-start;flex-direction:column}.history-pagination{justify-content:flex-start}.board-hero{padding:28px 20px 22px;border-radius:16px}.hero-main{flex-direction:column}.hero-report{text-align:left}.today-grid{grid-template-columns:1fr}.basis{grid-template-columns:1fr 1fr}.basis-wide{grid-column:1/-1}.board-card{padding:18px;border-radius:14px}}@media(max-width:520px){.today-grid,.basis{grid-template-columns:1fr}.basis-wide{grid-column:auto}.hero-title-lock{width:148px;max-width:148px;min-width:148px}.hero-title-lock h1{width:148px;font-size:34px}.calligraphy-motto{width:148px;max-width:148px;height:auto}}
</style>
