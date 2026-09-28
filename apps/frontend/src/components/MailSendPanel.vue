<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getLatestAnxinBoardReport } from '../api/anxinBoardReports.js'
import { getMailTransportSettings } from '../api/mailSettings.js'
import { getRecipientConfig } from '../api/recipientSettings.js'
import { sendApprovedReportMail } from '../api/mailSend.js'

const props = defineProps({
  projectId: {
    type: [Number, String],
    required: true
  },
  displayedReportVersionId: { type: Number, default: null },
  displayedReportHash: { type: String, default: null },
  moduleNarrativeState: { type: String, default: 'idle' },
  displayedModuleNarrativeHash: { type: String, default: null },
  displayedGitMetricsState: { type: String, default: 'idle' }
})

const loadState = ref('loading')
const sendState = ref('idle')
const report = ref(null)
const transport = ref(null)
const recipients = ref(null)
const message = ref('')
let generation = 0

const reportReady = computed(() => (
  report.value?.schema_version === 'anxin_board_report_v3' &&
  Number.isInteger(report.value?.report_version_id) &&
  report.value.report_version_id > 0
))
const transportReady = computed(() => transport.value?.configured === true && !!transport.value?.profile?.from_identity)
const recipientReady = computed(() => recipients.value?.configured === true && Number.isInteger(recipients.value?.version_no) && recipients.value.version_no > 0 && Array.isArray(recipients.value?.to_recipients) && recipients.value.to_recipients.length > 0)
const isHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const displayedReportMatches = computed(() => reportReady.value &&
  props.displayedReportVersionId === report.value.report_version_id &&
  isHash(props.displayedReportHash) && props.displayedReportHash === report.value.anxin_board_report_hash)
const displayedModulesReady = computed(() =>
  (props.moduleNarrativeState === 'loaded' && isHash(props.displayedModuleNarrativeHash)) ||
  (props.moduleNarrativeState === 'absent' && props.displayedModuleNarrativeHash === null))
const canSend = computed(() => loadState.value === 'ready' && reportReady.value &&
  displayedReportMatches.value && displayedModulesReady.value && props.displayedGitMetricsState === 'loaded' &&
  transportReady.value && recipientReady.value && sendState.value === 'idle')
const fromIdentity = computed(() => transport.value?.profile?.from_identity || '未配置')
const toDisplay = computed(() => recipientReady.value ? recipients.value.to_recipients.join('、') : '未配置')
const reportLabel = computed(() => reportReady.value ? `${report.value.report_date} · 报告 #${report.value.report_version_id}` : '没有可发送的正式确认版')

function safeDetail(body) {
  const detail = body?.detail
  if (typeof detail?.message === 'string' && detail.message) return detail.message
  return '邮件发送没有完成，请刷新状态后再确认。'
}

async function load() {
  const current = ++generation
  loadState.value = 'loading'
  sendState.value = 'idle'
  message.value = ''
  report.value = null
  transport.value = null
  recipients.value = null
  try {
    const [reportResponse, transportResponse, recipientResponse] = await Promise.all([
      getLatestAnxinBoardReport(props.projectId),
      getMailTransportSettings(),
      getRecipientConfig(props.projectId)
    ])
    if (current !== generation) return
    const noReport = reportResponse.status === 404 && reportResponse.body?.detail?.code === 'ANXIN_BOARD_REPORT_NOT_AVAILABLE'
    if ((!reportResponse.ok && !noReport) || !transportResponse.ok || !recipientResponse.ok) throw new Error('mail prerequisites load failed')
    report.value = noReport ? null : reportResponse.body
    transport.value = transportResponse.body
    recipients.value = recipientResponse.body
    loadState.value = 'ready'
  } catch {
    if (current === generation) loadState.value = 'failed'
  }
}

async function sendMail() {
  if (!canSend.value) return
  // Capture what the parent actually displayed. Never fetch a newer annotation at send time.
  const intended = {
    projectId: props.projectId,
    reportId: props.displayedReportVersionId,
    reportHash: props.displayedReportHash,
    moduleHash: props.displayedModuleNarrativeHash,
    recipientVersion: recipients.value.version_no
  }
  const confirmation = [
    '确认发送最新正式安心看板？',
    '',
    `发件人：${fromIdentity.value}`,
    `收件人：${toDisplay.value}`,
    `报告：${reportLabel.value}`,
    intended.moduleHash ? '包含已确认的模块说明修订。' : '使用当前已展示的报告内容。',
    '',
    '本次只提交这一封。若发送结果无法确认，系统不会自动重发。'
  ].join('\n')
  if (!window.confirm(confirmation)) return
  if (!canSend.value || intended.projectId !== props.projectId ||
      intended.reportId !== props.displayedReportVersionId || intended.reportHash !== props.displayedReportHash ||
      intended.moduleHash !== props.displayedModuleNarrativeHash || intended.recipientVersion !== recipients.value.version_no) return

  const current = generation
  sendState.value = 'sending'
  message.value = '正在提交邮件，请勿重复点击。'
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'local'
  const utcOffsetMinutes = -new Date().getTimezoneOffset()
  try {
    const response = await sendApprovedReportMail(intended.projectId, {
      expected_report_version_id: intended.reportId,
      expected_module_narrative_hash: intended.moduleHash,
      expected_recipient_config_version_no: intended.recipientVersion,
      confirmed_timezone: timezone,
      confirmed_utc_offset_minutes: utcOffsetMinutes,
      human_confirmed: true
    })
    if (current !== generation) return
    if (!response.ok) {
      sendState.value = 'blocked'
      message.value = safeDetail(response.body)
      return
    }
    const state = response.body?.state
    if (state === 'sent') {
      sendState.value = 'sent'
      message.value = 'SMTP 服务器已接受这封邮件。'
    } else if (state === 'partial') {
      sendState.value = 'partial'
      message.value = '部分收件人被 SMTP 接受，部分被明确拒绝；系统不会自动重发。'
    } else if (state === 'unknown') {
      sendState.value = 'unknown'
      message.value = '发送结果无法确认。为避免重复邮件，系统不会自动重发。'
    } else if (state === 'failed') {
      sendState.value = 'failed'
      message.value = '邮件在可确认的发送前阶段失败，没有进入自动重试。'
    } else {
      sendState.value = 'blocked'
      message.value = '邮件状态无法确认，请不要重复发送。'
    }
  } catch {
    if (current !== generation) return
    sendState.value = 'blocked'
    message.value = '本地发送请求没有完成，请先刷新状态；不要连续重复点击。'
  }
}

onMounted(load)
watch(() => props.projectId, load)
onUnmounted(() => { generation += 1 })
</script>

<template>
  <section class="mail-send-panel" aria-label="发送安心看板邮件" data-testid="mail-send-panel">
    <div class="mail-send-main">
      <div>
        <div class="mail-send-kicker">邮件发送</div>
        <strong class="mail-send-title">发送最新正式安心看板</strong>
      </div>
      <button
        type="button"
        class="mail-send-button"
        :disabled="!canSend"
        data-testid="mail-send-button"
        @click="sendMail"
      >{{ sendState === 'sending' ? '发送中…' : '发送邮件' }}</button>
    </div>

    <div v-if="loadState === 'loading'" class="mail-send-status">正在读取发送准备信息…</div>
    <div v-else-if="loadState === 'failed'" class="mail-send-status error">发送准备信息读取失败，请刷新后重试。</div>
    <template v-else>
      <div class="mail-send-facts">
        <span><b>发件人</b><em data-testid="mail-send-from">{{ fromIdentity }}</em></span>
        <span><b>收件人</b><em data-testid="mail-send-to">{{ toDisplay }}</em></span>
        <span><b>报告</b><em data-testid="mail-send-report">{{ reportLabel }}</em></span>
      </div>
      <div v-if="!report" class="mail-send-status warning">尚无已确认报告，请先审阅并确认</div>
      <div v-else-if="!reportReady || !transportReady || !recipientReady" class="mail-send-status warning">
        需要正式确认版报告、已保存 SMTP 配置和至少 1 个 To 收件人后才能发送。
      </div>
      <div v-else-if="!displayedReportMatches" class="mail-send-status warning" data-testid="mail-display-binding">
        请先查看当前最新的完整报告，再确认发送。
      </div>
      <div v-else-if="!displayedModulesReady" class="mail-send-status warning" data-testid="mail-display-binding">
        模块说明尚未完成读取或验证，请在下方核对后再发送。
      </div>
      <div v-else-if="displayedGitMetricsState !== 'loaded'" class="mail-send-status warning" data-testid="mail-display-binding">
        代码变化统计尚未完成读取或验证，请在下方核对后再发送。
      </div>
      <div v-else-if="displayedModuleNarrativeHash" class="mail-send-status" data-testid="mail-display-binding">
        本次邮件包含当前已展示并确认的模块说明修订。
      </div>
    </template>
    <div v-if="message" class="mail-send-status" :class="sendState" data-testid="mail-send-result">{{ message }}</div>
  </section>
</template>

<style scoped>
.mail-send-panel{width:min(1060px,calc(100% - 28px));margin:26px auto 0;padding:16px 18px;border:1px solid #dfe5f1;border-radius:14px;background:#fff;box-shadow:0 8px 22px rgba(15,23,42,.05);color:#334155}.mail-send-main{display:flex;align-items:center;justify-content:space-between;gap:16px}.mail-send-kicker{font-size:11px;font-weight:800;color:#16866f}.mail-send-title{display:block;margin-top:2px;font-size:16px;color:#1f2a3d}.mail-send-button{border:0;border-radius:10px;background:#16866f;color:#fff;padding:10px 18px;font:inherit;font-size:13px;font-weight:800;cursor:pointer}.mail-send-button:disabled{cursor:not-allowed;opacity:.45}.mail-send-facts{display:grid;grid-template-columns:1fr 1.4fr 1fr;gap:10px;margin-top:13px}.mail-send-facts span{min-width:0;padding:9px 10px;border-radius:9px;background:#f8fafc;font-size:12px;overflow-wrap:anywhere}.mail-send-facts b{display:block;margin-bottom:2px;color:#64748b;font-size:10px}.mail-send-facts em{display:block;color:#334155;font-style:normal}.mail-send-status{margin-top:10px;padding:9px 11px;border-radius:9px;background:#f1f5f9;color:#475569;font-size:12px}.mail-send-status.sent{background:#eaf9f4;color:#166f56}.mail-send-status.unknown,.mail-send-status.partial,.mail-send-status.warning{background:#fff7e8;color:#8a5b12}.mail-send-status.failed,.mail-send-status.blocked,.mail-send-status.error{background:#fff1f2;color:#a33a3a}@media(max-width:760px){.mail-send-facts{grid-template-columns:1fr}.mail-send-main{align-items:flex-start}.mail-send-button{flex:0 0 auto}}
</style>
