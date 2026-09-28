<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  activeReanalysis,
  authorizeReportSend,
  createReportGenerationTask,
  executeAuthorizedReport,
  getReportGenerationTask,
  getActiveReportGenerationTask,
  prepareReportGenerationModelCall,
  previewReportSendAuthorization,
  voidUnknownReportGenerationTask
} from '../api/taskExecution.js'
import { formatTime } from '../utils/projectFormat.js'

const props = defineProps({ projectId: { type: [Number, String], default: null } })

const loading = ref(false)
const creating = ref(false)
const preparing = ref(false)
const task = ref(null)
const preparation = ref(null)
const localTaskId = ref('')
const evidenceSnapshotId = ref('')
const message = ref('')
const messageType = ref('info')
const scopeVisible = ref(false)
const scopeLoading = ref(false)
const scopePreview = ref(null)
const sendAuthorized = ref(false)
const authorizing = ref(false)
const executing = ref(false)
const voidingUnknown = ref(false)
const blocked = ref(false)
const executionAttempted = ref(false)
const operationInFlight = computed(() => preparing.value || scopeLoading.value || authorizing.value || executing.value || creating.value || voidingUnknown.value)
let epoch = 0
function currentRequest() { const version = epoch; const project = props.projectId; return () => version === epoch && project === props.projectId }

const taskState = computed(() => task.value?.state || 'not_started')
const hasEvidence = computed(() => Number(evidenceSnapshotId.value) > 0)
const hasTask = computed(() => Boolean(task.value?.local_task_id))
const hasPreparation = computed(() => preparation.value?.preparation_state === 'prepared')
const batchPlan = computed(() => preparation.value?.batch_plan || task.value?.batch_plan || null)
const batchResumeReady = computed(() => taskState.value === 'running' && task.value?.batch_plan?.can_resume === true &&
  !task.value.batch_plan.batches?.some(batch => ['claimed', 'unknown'].includes(batch.state)))
const taskActionable = computed(() => taskState.value === 'queued' || batchResumeReady.value)
const remainingCalls = computed(() => (scopePreview.value?.batch_plan || batchPlan.value)?.pending_batch_count || 0)
const batchStateLabel = state => ({ pending: '尚未发送', claimed: '正在执行或等待核对', succeeded: '已完成', unknown: '结果不确定', failed: '明确失败' }[state] || '等待核对')
const localReadiness = computed(() => preparation.value?.local_request_readiness_state || '')
const localReady = computed(() => localReadiness.value === 'ready_for_gateway_evaluation')
const localContextBlocked = computed(() => localReadiness.value === 'blocked_context_denied')
const localBudgetBlocked = computed(() => localReadiness.value === 'blocked_context_budget')
const contextFindings = computed(() => preparation.value?.context_findings || [])
const canCreate = computed(() => hasEvidence.value && !hasTask.value && !localTaskId.value && !blocked.value && !loading.value && !creating.value)
const canPrepare = computed(() => !blocked.value && !loading.value && hasTask.value && taskActionable.value && !hasPreparation.value && !preparing.value && !executing.value)
const canAuthorizeSend = computed(() => !blocked.value && taskActionable.value && !executionAttempted.value && localReady.value && scopePreview.value?.data_scope_hash && !sendAuthorized.value && !authorizing.value)
const canExecute = computed(() => !blocked.value && taskActionable.value && !executionAttempted.value && localReady.value && sendAuthorized.value && scopePreview.value?.data_scope_hash && !executing.value)

const stateMeta = computed(() => {
  if (task.value?.reanalysis_cancelled) return ['未发送的重分析已撤销', 'neutral', '替代任务已关闭，原报告恢复审阅。撤销不会重新调用 AI。']
  if (executing.value) return ['已提交执行请求，正在等待分析结果', 'info', '请勿重复操作。实际发送情况和分析结果以服务端返回为准；软件不会自动重发。']
  if (loading.value && !hasTask.value) return ['正在恢复分析任务', 'info', '正在读取本机持久化任务，完成前不会创建新任务或调用 AI。']
  if (blocked.value && !hasTask.value) return ['任务状态无法确认', 'warning', '恢复尚未通过检查，已停止创建新任务。请查看下方提示。']
  if (batchResumeReady.value && !hasPreparation.value) return ['已恢复分批分析进度', 'info', '已完成的批次会保留。先在本机核对剩余批次，再重新确认发送范围；不会自动重新调用 AI。']
  if (taskActionable.value && localContextBlocked.value) {
    return ['发送范围被本地检查拦住', 'warning', `发现 ${preparation.value?.denied_target_count || 0} 项内容不能进入模型上下文。当前没有发送任何数据。`]
  }
  if (taskActionable.value && localBudgetBlocked.value) {
    return ['本轮上下文超过预算', 'warning', '冻结证据已经完成本地整理，但按当前保守预算不能安全进入模型请求。当前没有发送任何数据。']
  }
  if (taskActionable.value && localReady.value && sendAuthorized.value) {
    return ['本次真实发送已授权', 'success', '授权只绑定本次待发送范围、5 分钟内有效且只能消费一次。只有你再次点击“开始 AI 分析”才会发送剩余资料。']
  }
  if (taskActionable.value && localReady.value) {
    return ['本地发送范围检查通过', 'info', '模型调用、预算、脱敏和敏感路径检查都已闭合。这次本地准备不会发送数据，下一步需要你授权本次待发送的精确范围。']
  }
  if (taskActionable.value && hasPreparation.value) {
    return ['调用准备状态无法确认', 'warning', '准备记录已经建立，但本地发送范围状态无法可靠判断。软件不会继续发送。']
  }
  return ({
    not_started: ['还没开始', 'neutral', '先完成上一步“选择研发范围”。'],
    queued: ['已经准备好任务', 'info', '分析任务已创建。下一步可以在本机准备正式 AI 调用并检查发送范围；这一步不会发送数据。'],
    running: ['正在分析', 'info', 'AI 正在分析本轮研发证据。'],
    succeeded: ['分析完成', 'success', '可以继续到“审阅报告”。'],
    failed: ['分析失败', 'danger', '这次执行结果已经明确判定失败，不是未知状态。可以重新准备新的分析任务；再次发送仍需要你重新确认。'],
    unknown: ['结果暂时无法确认', 'warning', '原调用不会自动重试。若你要优先继续业务流程，可明确放弃这条不确定任务后重新开始；原调用仍可能已被服务商处理或计费。'],
    voided: ['已作废', 'neutral', '这次任务已作废。']
  }[taskState.value] || ['状态无法确认', 'warning', '当前状态无法可靠判断。'])
})

function storageKey(suffix) { return `rd-agent:project:${props.projectId}:${suffix}` }
function readStorage(key) { try { return sessionStorage.getItem(key) || '' } catch { return '' } }
function writeStorage(key, value) { try { sessionStorage.setItem(key, value) } catch {} }
function makeOpaque(prefix) {
  const random = typeof globalThis.crypto?.randomUUID === 'function' ? globalThis.crypto.randomUUID().replaceAll('-', '') : `${Date.now()}${Math.random().toString(16).slice(2)}`
  return `${prefix}_${random}`.slice(0, 120)
}
function showMessage(text, type = 'info') { message.value = text; messageType.value = type }
function apiFailure(result, fallback) {
  const detail = result.body?.detail
  const text = typeof detail?.message === 'string' ? detail.message : fallback
  // Display only bounded error identifiers, never arbitrary diagnostic objects.
  const codes = [...new Set([detail?.code, detail?.cause_code, detail?.credential_error_code]
    .filter(value => typeof value === 'string' && /^[A-Z][A-Z0-9_]{2,95}$/.test(value)))]
  const stages = { credential: '读取模型凭据', model_availability: '核对可用模型',
    model_metadata: '核对模型规格', qualification: '核对模型资格',
    authorization: '核对发送授权', current_authority: '校验模型发送条件',
    context_manifest: '校验发送资料', request_budget: '校验请求预算', provider_compatibility: '校验模型兼容性',
    output_contract: '校验 AI 输出' }
  const stage = stages[detail?.stage]
  const validationIssues = Array.isArray(detail?.validation_issues)
    ? detail.validation_issues.slice(0, 6).filter(item =>
      item && typeof item.code === 'string' && /^AI_[A-Z0-9_]{2,92}$/.test(item.code) &&
      typeof item.path === 'string' && /^\$[A-Za-z0-9_.\[\]<>-]{0,191}$/.test(item.path))
    : []
  const validationLine = validationIssues.length
    ? `输出校验：${validationIssues.map(item => `${item.code} @ ${item.path}`).join('；')}`
    : ''
  return [text, stage ? `失败步骤：${stage}` : '', codes.length ? `错误码：${codes.join(' / ')}` : '', validationLine,
    !codes.length && Number.isInteger(result.status) ? `HTTP ${result.status}` : ''].filter(Boolean).join('\n')
}

function resetSendGate() {
  scopeVisible.value = false
  scopeLoading.value = false
  scopePreview.value = null
  sendAuthorized.value = false
  authorizing.value = false
  executing.value = false
}
function localSessionMessage() {
  return '真实发送授权需要由 Windows 一键启动器建立本地浏览器会话。请从 Start-AnxinBoard.cmd 打开软件；普通浏览器直开页面时这道门保持关闭。'
}

async function restoreDurableTask() {
  const isCurrent = currentRequest()
  const snapshot = evidenceSnapshotId.value
  loading.value = true
  blocked.value = true
  resetSendGate()
  try {
    const result = await getActiveReportGenerationTask(props.projectId, snapshot || null)
    if (!isCurrent()) return
    if (!result.ok || !Object.hasOwn(result.body || {}, 'task')) {
      showMessage(apiFailure(result, '无法核对持久化分析任务，已停止创建。'), 'error')
      return
    }
    const durable = result.body.task
    if (durable === null) {
      localTaskId.value = ''
      writeStorage(storageKey('report-generation-task-id'), '')
      blocked.value = false
      return
    }
    if (String(durable.project_id) !== String(props.projectId) || !durable.local_task_id ||
        !['queued', 'running', 'unknown'].includes(durable.state) ||
        (snapshot && String(durable.evidence_snapshot_id) !== snapshot)) {
      showMessage('持久化分析任务与当前项目或证据快照不一致，已停止恢复。', 'error')
      return
    }
    if (durable.task_type !== 'daily_report_generate') {
      showMessage('当前快照由退回重分析任务占用，请从原报告审阅页面继续。不会创建另一条分析任务。', 'warning')
      return
    }
    task.value = durable
    localTaskId.value = durable.local_task_id
    evidenceSnapshotId.value = String(durable.evidence_snapshot_id)
    writeStorage(storageKey('report-generation-task-id'), durable.local_task_id)
    writeStorage(storageKey('evidence-snapshot-id'), evidenceSnapshotId.value)
    blocked.value = false
    showMessage('已恢复原有分析任务。没有创建新任务，也没有调用 AI。', 'success')
  } catch {
    if (isCurrent()) showMessage('无法读取持久化分析任务，当前状态无法确认。没有创建新任务。', 'error')
  } finally {
    if (isCurrent()) loading.value = false
  }
}

async function loadTask({ preserveMessage = false, internal = false } = {}) {
  if (!internal && (loading.value || operationInFlight.value)) return
  const isCurrent = currentRequest()
  const id = localTaskId.value.trim()
  if (!props.projectId || !id) return
  loading.value = true
  blocked.value = true
  resetSendGate()
  if (!preserveMessage) message.value = ''
  try {
    const result = await getReportGenerationTask(props.projectId, id)
    if (!isCurrent()) return
    if (result.ok && result.body?.local_task_id === id) {
      if (evidenceSnapshotId.value && String(result.body.evidence_snapshot_id) !== evidenceSnapshotId.value &&
          !activeReanalysis(props.projectId, id)) {
        localTaskId.value = ''
        await restoreDurableTask()
        return
      }
      blocked.value = false
      task.value = result.body
      if (result.body.batch_plan || result.body.state === 'queued') {
        preparation.value = null
        executionAttempted.value = false
      }
      evidenceSnapshotId.value = String(result.body.evidence_snapshot_id)
      writeStorage(storageKey('report-generation-task-id'), result.body.local_task_id)
    } else if (result.status === 404 && !activeReanalysis(props.projectId, id)) {
      localTaskId.value = ''
      await restoreDurableTask()
    } else {
      if (!preserveMessage) showMessage(apiFailure(result, '没有读取到这个分析任务。'), 'warning')
    }
  } catch {
    if (!isCurrent()) return
    if (!preserveMessage) showMessage('无法连接本地服务，当前任务状态无法确认。', 'error')
  } finally {
    if (!isCurrent()) return
    loading.value = false
  }
}

async function createTask() {
  const isCurrent = currentRequest()
  if (!canCreate.value) return
  creating.value = true
  preparation.value = null
  resetSendGate()
  message.value = ''
  const id = makeOpaque('tsk')
  localTaskId.value = id
  try {
    const result = await createReportGenerationTask(props.projectId, {
      local_task_id: id,
      evidence_snapshot_id: Number(evidenceSnapshotId.value),
      create_key: makeOpaque('ui')
    })
    if (!isCurrent()) return
    if (!result.ok || !result.body?.local_task_id) {
      localTaskId.value = ''
      writeStorage(storageKey('report-generation-task-id'), '')
      if (result.body?.detail?.code === 'REPORT_GENERATION_TASK_CHECKPOINT_ACTIVE_CONFLICT') {
        await restoreDurableTask()
        return
      }
      showMessage(apiFailure(result, '创建分析任务失败。没有调用 AI。'), 'error')
      return
    }
    task.value = result.body
    writeStorage(storageKey('report-generation-task-id'), result.body.local_task_id)
    showMessage('分析任务已经建立。下一步可以准备 AI 调用并做本地发送范围检查，但现在仍不会发送数据。', 'success')
  } catch {
    if (!isCurrent()) return
    showMessage('创建结果暂时无法确认。软件不会自动重复创建或重复调用 AI。', 'error')
  } finally {
    if (!isCurrent()) return
    creating.value = false
  }
}

async function prepareModelCall() {
  const isCurrent = currentRequest()
  if (!canPrepare.value) return
  preparing.value = true
  resetSendGate()
  message.value = ''
  try {
    const result = await prepareReportGenerationModelCall(props.projectId, task.value.local_task_id, {
      preparation_authorized: true
    })
    if (!isCurrent()) return
    if (!result.ok || result.body?.preparation_state !== 'prepared' || result.body?.provider_send_state !== 'not_attempted') {
      showMessage(apiFailure(result, 'AI 调用准备失败。没有发送任何数据。'), 'error')
      return
    }
    preparation.value = result.body
    if (result.body.next_gate === 'exact_human_send_authorization_required') {
      showMessage(result.body.batch_plan
        ? `已按模型预算分为 ${result.body.batch_plan.batch_count} 批，保留全部可用证据。本次本地准备没有调用 AI；请确认剩余批次的发送范围。`
        : '本地发送范围检查通过。还没有向 DeepSeek 发送数据，下一步需要你查看并确认这次精确发送范围。', 'success')
    } else if (result.body.next_gate === 'local_context_denied') {
      showMessage('本地检查发现不能发送的内容，已停止在本机。没有向 DeepSeek 发送任何数据。', 'warning')
    } else if (result.body.next_gate === 'local_context_budget_exceeded') {
      showMessage('本轮上下文超过当前安全预算，已停止在本机。没有向 DeepSeek 发送任何数据。', 'warning')
    } else {
      showMessage('本地准备状态无法可靠确认。软件不会继续发送。', 'warning')
    }
  } catch {
    if (!isCurrent()) return
    showMessage('无法确认 AI 调用准备结果。没有自动发送，也不会自动重试。', 'error')
  } finally {
    if (!isCurrent()) return
    preparing.value = false
  }
}

async function openSendAuthorization() {
  const isCurrent = currentRequest()
  if (blocked.value || !taskActionable.value || executionAttempted.value || !localReady.value || !preparation.value?.model_call_id || scopeLoading.value) return
  scopeVisible.value = true
  scopeLoading.value = true
  scopePreview.value = null
  sendAuthorized.value = false
  try {
    const result = await previewReportSendAuthorization(
      props.projectId,
      task.value.local_task_id,
      preparation.value.model_call_id
    )
    if (!isCurrent()) return
    if (!result.ok || result.body?.authorization_state !== 'awaiting_human_confirmation') {
      showMessage(apiFailure(result, '无法建立本次真实发送范围预览。没有调用 AI。'), 'error')
      return
    }
    scopePreview.value = result.body
    showMessage('请核对下方本次发送范围。查看范围本身不会调用 AI。', 'info')
  } catch (error) {
    if (!isCurrent()) return
    showMessage(error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? localSessionMessage() : '本地发送授权会话不可用。没有调用 AI。', 'error')
  } finally {
    if (!isCurrent()) return
    scopeLoading.value = false
  }
}

async function authorizeSend() {
  const isCurrent = currentRequest()
  if (!canAuthorizeSend.value) return
  authorizing.value = true
  try {
    const result = await authorizeReportSend(props.projectId, task.value.local_task_id, {
      model_call_id: preparation.value.model_call_id,
      data_scope_hash: scopePreview.value.data_scope_hash,
      human_confirmed: true
    })
    if (!isCurrent()) return
    if (!result.ok || result.body?.authorization_state !== 'authorized_once' || result.body?.data_scope_hash !== scopePreview.value?.data_scope_hash || result.body?.provider_send_state !== 'not_attempted') {
          showMessage(apiFailure(result, '本次发送授权失败。没有调用 AI。'), 'error')
      return
    }
    scopePreview.value = result.body
    sendAuthorized.value = true
    showMessage('本次精确发送范围已授权。AI 仍未被调用；授权 5 分钟内有效并且只能用于一次执行。', 'success')
  } catch (error) {
    if (!isCurrent()) return
      showMessage(error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? localSessionMessage() : '无法确认本次发送授权。没有调用 AI。', 'error')
  } finally {
    if (!isCurrent()) return
    authorizing.value = false
  }
}

async function executeReport() {
  const isCurrent = currentRequest()
  if (!canExecute.value) return
  executing.value = true
  executionAttempted.value = true
  message.value = ''
  try {
    const result = await executeAuthorizedReport(props.projectId, task.value.local_task_id, {
      model_call_id: preparation.value.model_call_id,
      data_scope_hash: scopePreview.value.data_scope_hash
    })
    if (!isCurrent()) return
    sendAuthorized.value = false
      if (result.ok && result.body?.task_state === 'succeeded') {
      showMessage('AI 分析已经完成并生成待审阅报告。', 'success')
    } else {
      showMessage(apiFailure(result, '本次 AI 执行没有成功闭合。软件不会自动重试。'), 'error')
    }
    await loadTask({ preserveMessage: true, internal: true })
  } catch (error) {
    if (!isCurrent()) return
    sendAuthorized.value = false
      showMessage(error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? localSessionMessage() : '无法确认这次 AI 执行结果。软件不会自动重试；请刷新状态确认。', 'error')
    await loadTask({ preserveMessage: true, internal: true })
  } finally {
    if (!isCurrent()) return
    executing.value = false
  }
}

async function voidUnknownAnalysis() {
  const isCurrent = currentRequest()
  if (taskState.value !== 'unknown' || voidingUnknown.value || operationInFlight.value) return
  voidingUnknown.value = true
  message.value = ''
  try {
    const result = await voidUnknownReportGenerationTask(props.projectId, task.value.local_task_id)
    if (!isCurrent()) return
    if (!result.ok || result.body?.state !== 'voided' || result.body?.provider_retry_state !== 'not_retried') {
      showMessage(apiFailure(result, '无法放弃这条不确定任务。原任务保持不变，也没有重新调用 AI。'), 'error')
      return
    }
    task.value = null
    preparation.value = null
      localTaskId.value = ''
    writeStorage(storageKey('report-generation-task-id'), '')
    blocked.value = false
    executionAttempted.value = false
    resetSendGate()
    showMessage('原不确定任务已作废，没有重新调用 AI。现在可以直接重新准备分析；真正发送仍需要新的范围确认和发送授权。', 'success')
  } catch (error) {
    if (!isCurrent()) return
    showMessage(error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? localSessionMessage() : '无法确认作废操作。原任务保持不变，没有重新调用 AI。', 'error')
  } finally {
    if (isCurrent()) voidingUnknown.value = false
  }
}

function restartFailedAnalysis() {
  if (taskState.value !== 'failed' || operationInFlight.value) return
  task.value = null
  preparation.value = null
  localTaskId.value = ''
  writeStorage(storageKey('report-generation-task-id'), '')
  blocked.value = false
  executionAttempted.value = false
  resetSendGate()
  showMessage('上一条明确失败的执行链已保留为历史记录。现在可以重新准备新的分析任务；重新发送仍需要再次确认。', 'info')
}

function goGit() { window.location.hash = `#/projects/${props.projectId}/git` }
function goReview() { window.location.hash = `#/projects/${props.projectId}/review` }

function restore() {
  epoch += 1
  loading.value = false
  creating.value = false
  preparing.value = false
  blocked.value = false
  executionAttempted.value = false
  if (!props.projectId) return
  localTaskId.value = readStorage(storageKey('report-generation-task-id'))
  evidenceSnapshotId.value = readStorage(storageKey('evidence-snapshot-id'))
  task.value = null
  preparation.value = null
  resetSendGate()
  message.value = ''
  const handoff = activeReanalysis(props.projectId, localTaskId.value)
  if (handoff?.invalid) {
    blocked.value = true
    showMessage('退回重分析身份缺失或不一致，已停止。请返回报告审阅核对原任务。', 'error')
  } else if (localTaskId.value) loadTask()
  else restoreDurableTask()
}

onBeforeUnmount(() => { epoch += 1 })
onMounted(restore)
watch(() => props.projectId, restore)
</script>

<template>
  <section class="task-page" aria-labelledby="task-title">
    <header class="page-head">
      <div>
        <span class="eyebrow">第 3 步 · 研发分析</span>
        <h1 id="task-title">生成研发报告</h1>
        <p>这页只做两件事：先确认这次要交给 AI 的资料，再由你单独点击开始 AI 分析。前一个动作不会调用模型。</p>
      </div>
      <button class="secondary" type="button" :disabled="loading || operationInFlight || !localTaskId" @click="loadTask">{{ loading ? '刷新中…' : '刷新状态' }}</button>
    </header>

    <section :class="['hero-card', stateMeta[1]]">
      <div class="hero-icon">{{ taskState === 'succeeded' ? '✓' : taskState === 'failed' ? '!' : 'AI' }}</div>
      <div class="hero-copy">
        <span>当前状态</span>
        <h2>{{ stateMeta[0] }}</h2>
        <p>{{ stateMeta[2] }}</p>
      </div>
      <div class="hero-action">
        <button v-if="!hasEvidence" class="primary" type="button" @click="goGit">先选择研发范围 →</button>
        <button v-else-if="!hasTask" class="primary" type="button" :disabled="!canCreate" @click="createTask">{{ creating ? '正在准备…' : '准备本次分析' }}</button>
        <button v-else-if="taskActionable && !hasPreparation" class="primary" type="button" :disabled="!canPrepare" @click="prepareModelCall">{{ preparing ? '正在本地检查…' : '准备 AI 调用' }}</button>
        <button v-else-if="taskActionable && localReady && !scopeVisible" class="primary" type="button" @click="openSendAuthorization">确认本次发送范围 →</button>
        <button v-else-if="taskActionable && localReady && !sendAuthorized" class="primary" type="button" disabled>请在下方完成发送授权</button>
        <button v-else-if="taskActionable && localReady && sendAuthorized" class="primary execute-primary" type="button" :disabled="executing" @click="executeReport">{{ executing ? '正在调用 AI…' : '开始 AI 分析' }}</button>
        <button v-else-if="taskActionable && hasPreparation" class="primary" type="button" disabled>本地检查未通过</button>
        <button v-else-if="taskState === 'failed'" class="primary" type="button" :disabled="operationInFlight" @click="restartFailedAnalysis">重新准备分析</button>
        <button v-else-if="taskState === 'unknown'" class="primary" type="button" :disabled="operationInFlight" @click="voidUnknownAnalysis">{{ voidingUnknown ? '正在释放旧任务…' : '放弃不确定任务并重新开始' }}</button>
        <button v-else-if="task?.reanalysis_cancelled" class="primary" type="button" @click="goReview">返回原报告审阅 →</button>
        <button v-else-if="taskState === 'succeeded'" class="primary" type="button" @click="goReview">下一步：审阅报告 →</button>
        <button v-else class="primary" type="button" disabled>真实 AI 执行 · 当前不可操作</button>
      </div>
    </section>

    <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>

    <section v-if="batchPlan" class="batch-card" aria-label="分批分析进度">
      <h2>按模型预算分批分析</h2>
      <p>共 {{ batchPlan.batch_count }} 批 · 已完成 {{ batchPlan.completed_batch_count }} 批 · 尚未发送 {{ batchPlan.pending_batch_count }} 批</p>
      <p>资料会按预算分批读取，不会偷偷截断。各批结果分别保存，全部完成后合成一份待审阅报告。</p>
      <ol>
        <li v-for="batch in batchPlan.batches" :key="batch.ordinal">
          第 {{ batch.ordinal }} 批：{{ batchStateLabel(batch.state) }}
          <span v-if="Number.isFinite(batch.estimated_input_tokens)"> · 输入预算估计 {{ batch.estimated_input_tokens.toLocaleString() }} tokens</span>
        </li>
      </ol>
      <p v-if="batchPlan.batches?.some(batch => ['unknown', 'claimed'].includes(batch.state))">存在尚未确认结果的批次，停止续发，避免重复调用。</p>
      <p v-else-if="batchResumeReady">已完成的批次不会重发。继续剩余批次需要重新核对并授权。</p>
    </section>

    <section class="send-phase-card" aria-label="AI 分析两阶段">
      <div :class="['send-phase', { done: sendAuthorized || ['running', 'succeeded'].includes(taskState), current: taskActionable && !sendAuthorized }]">
        <span>1</span>
        <div><b>确认这次要交给 AI 的资料</b><small>{{ executing || ['running', 'succeeded'].includes(taskState) ? '已完成发送授权' : sendAuthorized ? '范围已授权，AI 尚未被调用' : localReady ? '等待你确认精确发送范围' : hasPreparation ? '本地检查未通过，停止在本机' : hasTask ? '先完成本地准备和范围检查' : '等待建立本次分析任务' }}</small></div>
      </div>
      <div :class="['send-phase', { done: taskState === 'succeeded', current: sendAuthorized || taskState === 'running' }]">
        <span>2</span>
        <div><b>开始 AI 分析</b><small>{{ taskState === 'succeeded' ? '已生成待审阅报告' : executing ? '执行请求已提交，正在等待结果' : taskState === 'running' ? '正在执行' : sendAuthorized ? '等待你单独点击开始；这一步才会调用模型' : '第一阶段完成后才开放' }}</small></div>
      </div>
    </section>

    <section v-if="hasPreparation && contextFindings.length" class="unfinished-card" aria-label="本地内容风险处理">
      <h2>已在本机脱敏或排除的内容</h2>
      <p>以下只展示安全占位片段。继续时使用脱敏后的资料，排除项不会发送；这些内容不能作为已验证实现的证据。</p>
      <ul>
        <li v-for="(finding, index) in contextFindings" :key="index">
          <code>{{ finding.path || finding.target }}</code>
          · {{ finding.line_start ? `${finding.range_kind === 'diff_lines' ? 'Diff ' : ''}第 ${finding.line_start}–${finding.line_end} 行` : '整个目标' }}
          · {{ finding.rule_id }} · {{ finding.risk_level === 'high' ? '高风险' : '提示' }}
          · {{ finding.action === 'excluded' ? '已排除' : finding.action === 'isolated' ? '已隔离' : '已脱敏' }}
          <code>{{ finding.safe_snippet }}</code>
        </li>
      </ul>
      <p v-if="localReady">这些提示已经在本机完成脱敏、隔离或排除，不需要额外确认；后续只会使用上方显示的安全范围。</p>
      <button v-if="!sendAuthorized && !executionAttempted" class="secondary" type="button" @click="resetSendGate">取消本次发送</button>
    </section>

    <section v-if="hasTask && taskActionable && localReady && scopeVisible" class="send-authorization-card" aria-label="真实 AI 发送授权">
      <div class="send-card-head">
        <div>
          <span class="send-tag">真实发送授权</span>
          <h2>确认这次要交给 AI 的资料</h2>
          <p>这里只确认本次冻结研发证据的精确范围。授权与真正调用 AI 是两个动作。</p>
        </div>
        <strong v-if="sendAuthorized" class="authorized-badge">已授权一次</strong>
      </div>

      <div v-if="scopeLoading" class="scope-loading">正在本机重新核对发送范围…</div>

      <template v-else-if="scopePreview">
        <div class="scope-grid">
          <div><span>本次将进入模型上下文</span><strong>{{ scopePreview.admitted_target_count ?? 0 }} 项研发证据</strong></div>
          <div><span>当前 AI</span><strong>DeepSeek V4 Flash</strong></div>
          <div><span>授权有效期</span><strong>{{ scopePreview.permit_ttl_seconds ? `${Math.round(scopePreview.permit_ttl_seconds / 60)} 分钟 / 仅一次` : '授权后 5 分钟 / 仅一次' }}</strong></div>
          <div><span>本次范围指纹</span><code>{{ `${scopePreview.data_scope_hash?.slice(0, 12)}…${scopePreview.data_scope_hash?.slice(-8)}` }}</code></div>
        </div>
        <p class="scope-note">授权只对这个范围指纹有效；范围变化就必须重新确认。本卡不会读取或展示 API Key，也不会把准备完成冒充成 AI 已执行。</p>

        <div v-if="!sendAuthorized" class="send-confirm">
          <span><b>点击下方按钮即授权这个精确范围</b><small v-if="batchPlan">本次授权最多 {{ remainingCalls }} 次模型调用，逐批处理尚未发送的资料，可能产生相应模型费用。已完成批次不会重发；任何结果不确定都会停止。授权本身仍不会调用 AI。</small><small v-else>下一步真正执行会调用 DeepSeek，并可能产生模型费用。授权本身仍不会调用 AI。</small></span>
        </div>
        <button v-if="!sendAuthorized" class="primary" type="button" :disabled="!canAuthorizeSend" @click="authorizeSend">
          {{ authorizing ? '正在记录授权…' : '授权本次发送（尚不调用 AI）' }}
        </button>

        <div v-else class="authorized-action">
          <div v-if="executing"><b>执行请求已提交，正在等待结果</b><span>请勿重复操作。实际发送情况和分析结果以服务端返回为准；软件不会自动重发。</span></div>
          <div v-else><b>本次授权已经记录，待发送资料尚未发送</b><span>只有下面这个按钮会真正进入模型执行。刷新、返回或普通页面加载都不会自动发送。</span></div>
          <button class="primary execute-primary" type="button" :disabled="!canExecute" @click="executeReport">{{ executing ? '正在调用 AI…' : '开始 AI 分析 · 会调用模型' }}</button>
        </div>
      </template>

      <div v-else class="scope-unavailable">
        <b>当前浏览器没有可用的真实发送会话</b>
        <span>请从 Windows 的 <code>Start-AnxinBoard.cmd</code> 打开软件。直接输入 localhost 地址只允许浏览和本地准备，不开放真实 AI 发送。</span>
        <button class="secondary" type="button" @click="openSendAuthorization">重新检查本地会话</button>
      </div>
    </section>

    <section v-if="hasTask && taskActionable" class="unfinished-card">
      <div v-if="executing">
        <span class="unfinished-tag">正在等待分析结果</span>
        <h2>已提交执行请求，正在等待分析结果</h2>
        <p>实际发送情况和分析结果以服务端返回为准；请勿重复操作，软件不会自动重发。</p>
      </div>
      <div v-else-if="!hasPreparation">
        <span class="unfinished-tag">真实链路继续推进</span>
        <h2>分析任务已经建立，下一步先准备正式 AI 调用</h2>
        <p>准备阶段会把任务、冻结证据和 DeepSeek 模型身份正式绑定，并在本机完成脱敏、敏感路径检查和预算检查。不会触发模型费用。</p>
      </div>
      <div v-else-if="localReady && sendAuthorized">
        <span class="unfinished-tag">已授权 · 尚未发送</span>
        <h2>本次精确范围已经由你授权，仍需要再次点击才会真正调用 AI</h2>
        <p>授权 5 分钟内有效并且只允许消费一次。系统不会因为刷新页面、恢复状态或后台任务而自动调用模型。</p>
      </div>
      <div v-else-if="localReady">
        <span class="unfinished-tag">已到真实发送边界</span>
        <h2>本次待发送范围的本地检查已经通过</h2>
        <p>点击“确认本次发送范围”后，你会先看到本次范围摘要和指纹；只有明确授权并再次点击“开始 AI 分析”才会真正发送。</p>
      </div>
      <div v-else-if="localContextBlocked">
        <span class="unfinished-tag">本地检查已阻止发送</span>
        <h2>发现不能进入模型上下文的内容</h2>
        <p>已局部排除 {{ preparation?.denied_target_count || 0 }} 项不适合发送的内容，但剩余证据不足以可信分析。请补充安全的源码证据。当前没有向模型发送数据。</p>
      </div>
      <div v-else-if="localBudgetBlocked">
        <span class="unfinished-tag">本地检查已阻止发送</span>
        <h2>本轮证据超过当前安全预算</h2>
        <p>软件已经在本机停止，没有截断后偷偷发送，也没有把不完整上下文当成完整报告依据。</p>
      </div>
      <div v-else>
        <span class="unfinished-tag">状态暂时无法确认</span>
        <h2>准备记录存在，但本地发送范围状态不完整</h2>
        <p>软件不会继续执行真实 AI 调用，避免把未知状态当作可发送状态。</p>
      </div>
    </section>

    <details class="advanced-details">
      <summary>高级详情</summary>
      <div class="advanced-grid">
        <div><span>分析任务 ID</span><code>{{ task?.local_task_id || localTaskId || '尚未创建' }}</code></div>
        <div><span>证据快照</span><code>{{ evidenceSnapshotId || '尚未冻结' }}</code></div>
        <div><span>Model Call ID</span><code>{{ preparation?.model_call_id || '尚未准备' }}</code></div>
        <div><span>Budget Profile</span><code>{{ preparation?.budget_profile_hash || '尚未准备' }}</code></div>
        <div><span>Final Context Manifest</span><code>{{ preparation?.final_context_manifest_hash || '尚未准备' }}</code></div>
        <div><span>Framed Payload</span><code>{{ preparation?.framed_payload_hash || '尚未准备' }}</code></div>
        <div><span>发送范围指纹</span><code>{{ scopePreview?.data_scope_hash || '尚未授权' }}</code></div>
        <div><span>允许 / 拦截项</span><strong>{{ hasPreparation ? `${preparation?.admitted_target_count ?? 0} / ${preparation?.denied_target_count ?? 0}` : '—' }}</strong></div>
        <div><span>本地发送范围状态</span><strong>{{ preparation?.local_request_readiness_state || '尚未检查' }}</strong></div>
        <div><span>创建时间</span><strong>{{ task?.created_at ? formatTime(task.created_at) : '—' }}</strong></div>
        <div><span>最近更新</span><strong>{{ task?.updated_at ? formatTime(task.updated_at) : '—' }}</strong></div>
      </div>
      <p>如果外部调用结果无法确认，系统不会自动重试。这些技术信息主要用于排查问题，普通使用不需要关注。</p>
    </details>
  </section>
</template>

<style scoped>
.batch-card{padding:20px;border:1px solid #ccd7f5;border-radius:16px;background:#f7f9ff}.batch-card h2{margin:0 0 10px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.batch-card p,.batch-card li{font-size:var(--ui-text-helper);line-height:1.8;color:#49617c}.batch-card ol{max-height:240px;overflow:auto;padding-left:24px}
.task-page{display:grid;gap:18px;color:#172033}.page-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.page-head h1{margin:5px 0 7px;font-size:var(--ui-text-page);font-weight:var(--ui-weight-strong);line-height:1.4}.page-head p{max-width:760px;margin:0;color:#6d788c;font-size:var(--ui-text-helper);line-height:1.6}.eyebrow{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.08em;color:#6677cc}.primary,.secondary{min-height:40px;border-radius:10px;padding:9px 14px;font:inherit;font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong)}.primary{border:1px solid #4f6ef7;background:#4f6ef7;color:#fff;box-shadow:0 7px 16px rgba(79,110,247,.2)}.secondary{border:1px solid #d7ddea;background:#fff;color:#44516a}.execute-primary{border-color:#28365f;background:#28365f;box-shadow:0 7px 16px rgba(40,54,95,.2)}.primary:disabled,.secondary:disabled{opacity:.48;cursor:not-allowed;box-shadow:none}.hero-card{display:grid;grid-template-columns:58px minmax(0,1fr) auto;gap:18px;align-items:center;padding:22px;border:1px solid #e0e5ee;border-radius:18px;background:#fff;box-shadow:0 12px 30px rgba(31,45,91,.06)}.hero-card.info{border-color:#d9e1ff;background:linear-gradient(145deg,#fff,#f8f9ff)}.hero-card.success{border-color:#cce8df;background:linear-gradient(145deg,#fff,#f5fbf8)}.hero-card.danger{border-color:#f0d1d1}.hero-card.warning{border-color:#f0dfb9;background:#fffaf0}.hero-icon{width:54px;height:54px;display:grid;place-items:center;border-radius:17px;background:#eef2ff;color:#4b61d0;font-size:16px;font-weight:var(--ui-weight-strong)}.hero-copy>span{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);color:#7c8798}.hero-copy h2{margin:4px 0 5px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.hero-copy p{margin:0;color:#6f7a8c;font-size:var(--ui-text-helper);line-height:1.6}.send-phase-card{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px;border:1px solid #e3e7ee;border-radius:15px;background:#fff}.send-phase{display:grid;grid-template-columns:32px 1fr;gap:10px;align-items:center;padding:11px 12px;border-radius:11px;background:#f8fafc;color:#6e7a8e}.send-phase>span{width:30px;height:30px;display:grid;place-items:center;border-radius:9px;background:#e9edf4;color:#66758a;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.send-phase>div{display:grid;gap:3px}.send-phase b{font-size:var(--ui-text-body);color:#354157}.send-phase small{font-size:var(--ui-text-caption);line-height:1.5}.send-phase.current{background:#f1f4ff}.send-phase.current>span{background:#4f6ef7;color:#fff}.send-phase.done{background:#f2faf7}.send-phase.done>span{background:#dff3eb;color:#24705d}.preparation-consent{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border:1px solid #d8e3f8;border-radius:14px;background:#f7f9ff;color:#49617c}.preparation-consent input{margin-top:3px}.preparation-consent span{display:grid;gap:4px}.preparation-consent b{font-size:var(--ui-text-body);color:#334564}.preparation-consent small{font-size:var(--ui-text-caption);line-height:1.65}.send-authorization-card{display:grid;gap:16px;padding:22px;border:1px solid #ccd7f5;border-radius:18px;background:linear-gradient(145deg,#fff,#f7f9ff);box-shadow:0 12px 28px rgba(44,61,112,.06)}.send-card-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.send-card-head h2{margin:7px 0 5px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.send-card-head p,.scope-note{margin:0;color:#68758a;font-size:var(--ui-text-caption);line-height:1.65}.send-tag,.authorized-badge{display:inline-flex;padding:4px 8px;border-radius:999px;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.send-tag{background:#edf1ff;color:#5269d0}.authorized-badge{background:#e2f6ee;color:#16735c}.scope-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.scope-grid>div{display:grid;gap:5px;padding:12px;border:1px solid #e2e7f1;border-radius:12px;background:#fff}.scope-grid span{font-size:var(--ui-text-caption);color:#8a94a4}.scope-grid strong,.scope-grid code{font-size:var(--ui-text-body);color:#33415a;overflow-wrap:anywhere}.send-confirm{display:flex;gap:11px;align-items:flex-start;padding:13px 14px;border:1px solid #e2d6b6;border-radius:12px;background:#fffbf2}.send-confirm input{margin-top:3px}.send-confirm span{display:grid;gap:4px}.send-confirm b{font-size:var(--ui-text-body);color:#5a4b2c}.send-confirm small{font-size:var(--ui-text-caption);line-height:1.6;color:#807157}.authorized-action{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:14px;border:1px solid #cfe7dd;border-radius:13px;background:#f4fbf8}.authorized-action>div{display:grid;gap:4px}.authorized-action b{font-size:var(--ui-text-body);color:#225f50}.authorized-action span{font-size:var(--ui-text-caption);line-height:1.55;color:#5d7a71}.scope-loading,.scope-unavailable{padding:16px;border-radius:12px;background:#fff;color:#667085;font-size:var(--ui-text-caption)}.scope-unavailable{display:grid;gap:8px;border:1px dashed #d5dbe7}.scope-unavailable b{color:#39465b}.scope-unavailable code{font-size:var(--ui-text-caption)}.unfinished-card{padding:22px;border:1px solid #efdeb3;border-radius:16px;background:#fffaf0}.unfinished-tag{display:inline-flex;padding:4px 8px;border-radius:999px;background:#fff0c7;color:#8b6417;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.unfinished-card h2{margin:10px 0 7px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.unfinished-card p{margin:0;max-width:900px;color:#796e58;font-size:var(--ui-text-helper);line-height:1.65}.advanced-details{border:1px solid #e1e5ec;border-radius:14px;background:#fff}.advanced-details summary{padding:14px 16px;cursor:pointer;color:#5b6679;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.advanced-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;padding:0 16px}.advanced-grid>div{display:grid;gap:4px;padding:11px;border-radius:10px;background:#f8fafc}.advanced-grid span{font-size:var(--ui-text-caption);color:#8a94a4}.advanced-grid code,.advanced-grid strong{overflow-wrap:anywhere;font-size:var(--ui-text-body)}.advanced-details>p{margin:10px 16px 16px;color:#8a94a4;font-size:var(--ui-text-caption);line-height:1.6}
@media(max-width:1000px){.scope-grid{grid-template-columns:1fr 1fr}.authorized-action{align-items:flex-start;flex-direction:column}}
@media(max-width:900px){.hero-card{grid-template-columns:48px 1fr}.hero-action{grid-column:1/-1}.send-phase-card{grid-template-columns:1fr}}
@media(max-width:620px){.page-head,.send-card-head{flex-direction:column}.advanced-grid,.scope-grid{grid-template-columns:1fr}.hero-card{grid-template-columns:1fr}.hero-icon{width:46px;height:46px}}
</style>
