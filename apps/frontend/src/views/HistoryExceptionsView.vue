<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getAnalysisLineage } from '../api/analysisLineages.js'
import { getAnxinBoardReportHistory } from '../api/anxinBoardReports.js'
import { getHistoryTaskStatus } from '../api/historyTaskStatus.js'

const props = defineProps({
  projectId: { type: [Number, String], required: true }
})

const historyState = ref('idle')
const historyRecords = ref([])
const historyMessage = ref('')
const lineageState = ref('idle')
const lineage = ref(null)
const lineageMessage = ref('')
const taskId = ref('')
const taskState = ref('idle')
const taskRecord = ref(null)
const taskMessage = ref('')

const latestHistoryId = computed(() => {
  const ids = historyRecords.value
    .map((item) => Number(item?.id))
    .filter((value) => Number.isFinite(value))
  return ids.length ? Math.max(...ids) : null
})

const taskStateValue = computed(() => {
  const value = taskRecord.value?.state
  return typeof value === 'string' ? value.toLowerCase() : ''
})
const taskIsUnknown = computed(() => taskStateValue.value === 'unknown')

function text(value, fallback = '未记录') {
  return value === null || value === undefined || value === '' ? fallback : String(value)
}

function shortHash(value) {
  const result = text(value, '—')
  return result.length > 18 ? `${result.slice(0, 10)}…${result.slice(-6)}` : result
}

function formatTime(value) {
  if (!value) return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString('zh-CN', { hour12: false })
}

function rangeText(record) {
  const report = record?.report || {}
  const values = [
    report.git_range,
    report.source_range,
    report.range,
    report.evidence?.git_range,
    report.evidence_snapshot?.git_range
  ]
  return values.find((value) => typeof value === 'string' && value.trim()) || '未记录'
}

function errorText(body, fallback) {
  return body?.detail?.message || body?.message || fallback
}

async function loadHistory() {
  historyState.value = 'loading'
  historyMessage.value = ''
  try {
    const { ok, body } = await getAnxinBoardReportHistory(props.projectId, { limit: 20 })
    if (!ok) {
      historyRecords.value = []
      historyState.value = 'failed'
      historyMessage.value = errorText(body, '报告历史读取失败')
      return
    }
    if (!Array.isArray(body)) {
      historyRecords.value = []
      historyState.value = 'failed'
      historyMessage.value = '历史接口返回格式无法确认'
      return
    }
    historyRecords.value = body
    historyState.value = 'loaded'
  } catch {
    historyRecords.value = []
    historyState.value = 'failed'
    historyMessage.value = '报告历史读取失败'
  }
}

async function loadLineage() {
  lineageState.value = 'loading'
  lineage.value = null
  lineageMessage.value = ''
  try {
    const { ok, body } = await getAnalysisLineage(props.projectId)
    if (!ok) {
      lineageState.value = 'failed'
      lineageMessage.value = errorText(body, '分析链路读取失败')
      return
    }
    if (body?.status === 'no_lineage' || !body?.lineage) {
      lineageState.value = 'empty'
      lineageMessage.value = '当前还没有活动分析链路'
      return
    }
    lineage.value = body.lineage
    lineageState.value = 'loaded'
  } catch {
    lineageState.value = 'failed'
    lineageMessage.value = '分析链路读取失败'
  }
}

async function refreshPage() {
  await Promise.all([loadHistory(), loadLineage()])
}

async function inspectTask() {
  if (taskState.value === 'loading' || !taskId.value.trim()) return
  taskState.value = 'loading'
  taskRecord.value = null
  taskMessage.value = ''
  try {
    const { ok, status, body } = await getHistoryTaskStatus(props.projectId, taskId.value)
    if (!ok) {
      taskState.value = status === 404 ? 'not-found' : 'failed'
      taskMessage.value = errorText(
        body,
        status === 404 ? '未找到这个报告生成任务' : '任务状态读取失败'
      )
      return
    }
    taskRecord.value = body
    taskState.value = 'loaded'
  } catch {
    taskState.value = 'failed'
    taskMessage.value = '任务状态读取失败'
  }
}

function exportHistory() {
  if (!historyRecords.value.length) return
  const payload = {
    project_id: Number(props.projectId),
    exported_at: new Date().toISOString(),
    note: '当前页面已加载的只读安心看板历史；不是数据库备份。',
    reports: historyRecords.value.map((item) => ({
      id: item?.id ?? null,
      schema_version: item?.schema_version ?? null,
      report_date: item?.report_date ?? null,
      report_hash: item?.report_hash ?? null,
      created_at: item?.created_at ?? null
    }))
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `anxin-history-project-${props.projectId}.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

onMounted(refreshPage)
watch(() => props.projectId, refreshPage)
</script>

<template>
  <section class="history-page" aria-label="历史与异常">
    <header class="page-head">
      <div>
        <span class="eyebrow">历史记录</span>
        <h1>报告历史</h1>
        <p>先看每一版正式报告的只读记录。排障所需的分析链路和任务状态放在下方高级诊断。</p>
      </div>
      <div class="actions">
        <button type="button" class="btn" :disabled="historyState === 'loading'" @click="refreshPage">
          {{ historyState === 'loading' ? '刷新中…' : '重新检查' }}
        </button>
        <button type="button" class="btn primary" :disabled="!historyRecords.length" @click="exportHistory">
          导出历史清单
        </button>
      </div>
    </header>

    <section class="card history-card">
      <div class="card-head">
        <div><h2>全部可读版本</h2><p>历史版本只读，不静默覆盖；“当前可读”只表示最新记录，不代表重新确认。</p></div>
        <span class="pill neutral">{{ historyRecords.length }} 条</span>
      </div>

      <div v-if="historyState === 'loading'" class="empty">正在读取报告历史…</div>
      <div v-else-if="historyState === 'failed'" class="empty error">{{ historyMessage }}</div>
      <div v-else-if="!historyRecords.length" class="empty">当前没有可读取的报告历史。</div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>报告</th><th>范围</th><th>状态</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="record in historyRecords" :key="record.id">
              <td><strong>REP-{{ record.id }}</strong><small>{{ text(record.schema_version, 'schema 未记录') }}</small></td>
              <td class="mono">{{ rangeText(record) }}</td>
              <td><span :class="['pill', Number(record.id) === latestHistoryId ? 'ok' : 'neutral']">{{ Number(record.id) === latestHistoryId ? '当前可读' : '历史只读' }}</span></td>
              <td><strong>{{ text(record.report_date, '日期未记录') }}</strong><small>{{ formatTime(record.created_at) }}</small></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div class="safety-note">
      <b>结果未知时禁止自动重做。</b>
      <span>高级诊断也只读取状态；不会自动 retry、void、创建替代任务或触发模型调用。</span>
    </div>

    <details class="diagnostics">
      <summary>高级诊断 · 分析链路与任务状态</summary>
      <div class="diagnostic-grid">
        <section class="diagnostic-card">
          <div class="card-head">
            <div><h2>分析链路</h2><p>用于排查当前活动链路和可证明的链路身份。</p></div>
            <span v-if="lineageState === 'loaded'" class="pill ok">连续</span>
            <span v-else-if="lineageState === 'failed'" class="pill danger">异常</span>
            <span v-else class="pill neutral">{{ lineageState === 'loading' ? '读取中' : '未建立' }}</span>
          </div>
          <div v-if="lineageState === 'loaded' && lineage" class="timeline">
            <div><i></i><span><small>活动分支</small><b class="mono">{{ text(lineage.branch) }}</b></span></div>
            <div><i></i><span><small>基线提交</small><b class="mono">{{ shortHash(lineage.baseline_commit) }}</b></span></div>
            <div><i></i><span><small>链路编号</small><b>#{{ lineage.sequence_no }}</b></span></div>
            <div><i></i><span><small>最近 Git 检查</small><b>{{ formatTime(lineage.source_git_checked_at) }}</b></span></div>
            <div v-if="lineage.break_reason" class="warning"><i></i><span><small>中断原因</small><b>{{ lineage.break_reason }}</b></span></div>
          </div>
          <div v-else class="empty" :class="{ error: lineageState === 'failed' }">{{ lineageMessage || '正在读取分析链路…' }}</div>
        </section>

        <section class="diagnostic-card">
          <div class="card-head"><div><h2>按任务 ID 查询</h2><p>当前没有异常任务列表 API；只能精确读取已知 <code>local_task_id</code>。</p></div><span class="pill neutral">只读</span></div>
          <form class="query" @submit.prevent="inspectTask">
            <label for="history-task-id">任务 ID</label>
            <div><input id="history-task-id" v-model="taskId" type="text" autocomplete="off" placeholder="输入 local_task_id" /><button class="btn primary" type="submit" :disabled="taskState === 'loading' || !taskId.trim()">{{ taskState === 'loading' ? '检查中…' : '检查任务' }}</button></div>
          </form>
          <div v-if="taskState === 'idle'" class="empty compact">输入已知任务 ID 后读取 durable 状态。</div>
          <div v-else-if="taskState === 'failed' || taskState === 'not-found'" class="empty compact error">{{ taskMessage }}</div>
          <div v-else-if="taskState === 'loaded' && taskRecord" class="task-result">
            <div class="task-top"><div><small>当前状态</small><strong>{{ text(taskRecord.state, 'unknown').toUpperCase() }}</strong></div><span :class="['pill', taskIsUnknown ? 'danger' : 'ok']">{{ taskIsUnknown ? '结果未知' : '已读取' }}</span></div>
            <dl>
              <div><dt>任务 ID</dt><dd class="mono">{{ text(taskRecord.local_task_id, taskId) }}</dd></div>
              <div><dt>Evidence Snapshot</dt><dd>{{ text(taskRecord.evidence_snapshot_id) }}</dd></div>
              <div><dt>创建时间</dt><dd>{{ formatTime(taskRecord.created_at) }}</dd></div>
              <div><dt>最近更新</dt><dd>{{ formatTime(taskRecord.updated_at) }}</dd></div>
            </dl>
            <div v-if="taskIsUnknown" class="unknown-box"><b>不能确定第三方是否已经处理。</b><span>系统不会自动重试，避免重复模型调用、重复计费或重复报告。</span></div>
            <div class="actions"><button class="btn" type="button" @click="inspectTask">再次读取状态</button></div>
          </div>
        </section>
      </div>
    </details>
  </section>
</template>

<style scoped>
.history-page{display:grid;gap:16px;color:#172033;min-width:0}.page-head,.card-head,.task-top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.page-head h1{margin:4px 0 6px;font-size:30px}.page-head p,.card-head p{margin:0;color:#697386;line-height:1.55}.eyebrow{color:#6655d8;font-size:11px;font-weight:800;letter-spacing:.08em}.actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center}.btn{min-height:38px;padding:0 13px;border:1px solid #d7dce5;border-radius:10px;background:#fff;color:#344054;font:inherit;font-weight:700;cursor:pointer}.btn.primary{background:#6655d8;border-color:#6655d8;color:#fff}.btn:disabled{cursor:not-allowed;opacity:.56}.safety-note{display:flex;gap:9px;padding:11px 14px;border:1px solid #efd89a;border-radius:11px;background:#fff8e8;color:#725d22;line-height:1.5;font-size:11px}.card{padding:18px;border:1px solid #e5e8ef;border-radius:15px;background:#fff;box-shadow:0 7px 22px rgba(29,41,57,.045)}.history-card{min-width:0}.card-head{margin-bottom:13px}.card-head h2{margin:0 0 5px;font-size:18px}.pill{display:inline-flex;align-items:center;min-height:25px;padding:0 8px;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap}.pill.ok{background:#e9f7ef;color:#247a49}.pill.neutral{background:#f2f4f7;color:#596273}.pill.danger{background:#fff0f0;color:#be4040}.table-wrap{min-width:0;overflow-x:auto}table{width:100%;min-width:600px;border-collapse:collapse}th,td{padding:11px 10px;border-bottom:1px solid #edf0f4;text-align:left;vertical-align:top}th{color:#818b9b;font-size:11px}td strong,td small{display:block}td small{margin-top:4px;color:#8a94a4}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}.empty{padding:25px 12px;border-radius:11px;background:#f8f9fb;color:#747e8d;text-align:center}.empty.compact{padding:16px 12px}.empty.error{background:#fff4f4;color:#b23f3f}.diagnostics{border:1px solid #e1e5eb;border-radius:14px;background:#fff}.diagnostics>summary{padding:14px 16px;cursor:pointer;color:#596579;font-size:11px;font-weight:800}.diagnostic-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 16px 16px}.diagnostic-card{padding:14px;border-radius:11px;background:#f8fafc}.timeline{display:grid;gap:0}.timeline>div{display:grid;grid-template-columns:18px 1fr;gap:9px;min-height:48px;position:relative}.timeline>div::after{content:'';position:absolute;left:5px;top:18px;bottom:-2px;width:1px;background:#dfdcea}.timeline>div:last-child::after{display:none}.timeline i{width:11px;height:11px;margin-top:4px;border-radius:50%;background:#7562df}.timeline small,.task-top small{display:block;margin-bottom:3px;color:#8a94a4}.timeline b{display:block;overflow-wrap:anywhere}.timeline .warning b{color:#ae6500}.query{display:grid;gap:7px;margin-bottom:12px}.query label{color:#4e596b;font-size:11px;font-weight:800}.query>div{display:flex;gap:8px}.query input{flex:1 1 220px;min-height:38px;padding:0 11px;border:1px solid #d7dce5;border-radius:10px;font:inherit}.task-result{display:grid;gap:11px}.task-top strong{font-size:18px}dl{margin:0;border:1px solid #edf0f4;border-radius:10px;overflow:hidden;background:#fff}dl>div{display:grid;grid-template-columns:130px 1fr;gap:9px;padding:8px 10px;border-bottom:1px solid #edf0f4}dl>div:last-child{border-bottom:0}dt{color:#7e8898}dd{margin:0;overflow-wrap:anywhere}.unknown-box{display:grid;gap:3px;padding:11px 13px;border:1px solid #efd89a;border-radius:10px;background:#fff8e8;color:#725d22}code{padding:2px 5px;border-radius:5px;background:#f3f4f7;color:#5e4fc6}@media(max-width:900px){.diagnostic-grid{grid-template-columns:1fr}.page-head{flex-direction:column}}@media(max-width:600px){.query>div{flex-direction:column}.safety-note{flex-direction:column}dl>div{grid-template-columns:1fr;gap:3px}.table-wrap{overflow:visible}table{min-width:0}thead{display:none}tbody,tr,td{display:block;width:100%}tr{padding:9px 0;border-bottom:1px solid #edf0f4}tr:last-child{border-bottom:0}td{padding:3px 0;border:0}td:nth-child(2){overflow-wrap:anywhere;color:#465268}td:nth-child(2)::before{content:'范围 · ';color:#8a94a4;font-family:inherit}td:nth-child(3),td:nth-child(4){margin-top:3px}}
</style>
