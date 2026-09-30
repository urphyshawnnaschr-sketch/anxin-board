<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { createWechatPreview, getWechatHistory, getWechatImage, getWechatSettings, saveWechatSettings, sendWechatPreview } from '../api/wechatDelivery.js'
import WechatBindingPanel from './WechatBindingPanel.vue'

const props = defineProps({
  projectId: { type: [Number, String], required: true },
  displayedReportVersionId: { type: Number, default: null },
  displayedReportHash: { type: String, default: null },
  moduleNarrativeState: { type: String, default: 'idle' },
  displayedModuleNarrativeHash: { type: String, default: null },
  displayedGitMetricsState: { type: String, default: 'idle' }
})
const fields = ['gateway_url', 'account_id', 'target', 'recipient_label', 'session_key']
const defaults = () => ({ gateway_url: '', account_id: '', target: '', recipient_label: '', session_key: 'main', token: '' })
const form = reactive(defaults())
const settings = ref(null)
const loading = ref(true)
const busy = ref('')
const configOpen = ref(false)
const bindingPanel = ref(null)
const bindingBusy = ref(false)
const message = ref('')
const history = ref([])
const preview = ref(null)
const pictures = ref([])
const submitted = ref(false)
let generation = 0
let reportGeneration = 0
let historyGeneration = 0
let settingsGeneration = 0
let live = true

const isHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const reportReady = computed(() => Number.isInteger(props.displayedReportVersionId) && props.displayedReportVersionId > 0 &&
  isHash(props.displayedReportHash) && props.displayedGitMetricsState === 'loaded' &&
  ((props.moduleNarrativeState === 'loaded' && isHash(props.displayedModuleNarrativeHash)) ||
   (props.moduleNarrativeState === 'absent' && props.displayedModuleNarrativeHash === null)))
const dirty = computed(() => !!form.token || fields.some(key => form[key].trim() !== (settings.value?.[key] ?? (key === 'session_key' ? 'main' : ''))))
const configured = computed(() => settings.value?.configured === true && settings.value?.token_configured === true)
const canPreview = computed(() => !loading.value && !busy.value && !bindingBusy.value && reportReady.value && configured.value && !dirty.value)
const canSend = computed(() => canPreview.value && !!preview.value && !submitted.value && pictures.value.length === preview.value.pages.length)

function clearPreview() {
  pictures.value.forEach(item => URL.revokeObjectURL(item.src))
  pictures.value = []
  preview.value = null
  submitted.value = false
}

function safeError(response, fallback) {
  const code = response?.body?.detail?.code
  // Never echo arbitrary gateway bodies or token validation input into the UI.
  if (typeof code === 'string' && code.includes('STALE')) return '报告或微信配置已变化，请刷新后重新生成图片预览。'
  if (typeof code === 'string' && code.includes('SCREENSHOT')) return '图片生成未完成，请检查截图运行组件后重试。没有发送微信。'
  if (typeof code === 'string' && code.includes('TOKEN')) return '访问令牌未保存或不可用，请重新填写。'
  if (typeof code === 'string' && code.includes('LOCAL_SESSION')) return '请从安心看板启动器重新打开页面，再执行此操作。'
  return fallback
}

function applySettings(value) {
  settings.value = { ...value, session_key: value?.session_key || 'main' }
  Object.assign(form, defaults(), Object.fromEntries(fields.map(key => [key, settings.value[key] ?? ''])))
}

async function load() {
  const current = ++generation
  settingsGeneration++
  historyGeneration++
  clearPreview()
  loading.value = true
  busy.value = ''
  message.value = ''
  history.value = []
  settings.value = null
  Object.assign(form, defaults())
  try {
    const [config, records] = await Promise.all([getWechatSettings(props.projectId), getWechatHistory(props.projectId)])
    if (!live || current !== generation) return
    if (!config.ok || !records.ok || !Array.isArray(records.body)) throw new Error('load failed')
    applySettings(config.body)
    history.value = records.body
    configOpen.value = false
  } catch {
    if (live && current === generation) message.value = '微信配置读取失败，请刷新状态。'
  } finally {
    if (live && current === generation) loading.value = false
  }
}

async function refreshSettings() {
  const current = generation
  const settingsCurrent = ++settingsGeneration
  clearPreview()
  try {
    const response = await getWechatSettings(props.projectId)
    if (!live || current !== generation || settingsCurrent !== settingsGeneration) return
    if (!response.ok) throw new Error('settings read failed')
    applySettings(response.body)
  } catch {
    if (live && current === generation && settingsCurrent === settingsGeneration) {
      settings.value = null
      message.value = '微信状态读取失败，请刷新后再生成预览。'
    }
  }
}

async function save() {
  if (busy.value || bindingBusy.value || loading.value) return
  const current = generation
  const projectId = props.projectId
  const payload = Object.fromEntries(fields.map(key => [key, form[key].trim()]))
  payload.expected_version_no = settings.value?.version_no ?? 0
  if (form.token) payload.token = form.token
  busy.value = 'saving'
  clearPreview()
  message.value = ''
  try {
    const response = await saveWechatSettings(projectId, payload)
    if (!live || current !== generation) return
    if (!response.ok) { message.value = safeError(response, '配置未保存，请检查网关地址、绑定账号、接收标识和令牌。'); return }
    applySettings(response.body)
    void bindingPanel.value?.reload()
    message.value = '配置已保存；微信实际收件尚需通过发送验收。'
  } catch {
    if (live && current === generation) message.value = '配置保存结果无法确认，请刷新状态后核对。'
  } finally {
    // Keep the credential out of component state after either response path.
    delete payload.token
    if (live && current === generation) { form.token = ''; busy.value = '' }
  }
}

async function makePreview() {
  if (!canPreview.value) return
  const current = generation
  const reportCurrent = reportGeneration
  const projectId = props.projectId
  const payload = { expected_report_version_id: props.displayedReportVersionId, expected_report_hash: props.displayedReportHash,
    expected_module_narrative_hash: props.displayedModuleNarrativeHash, expected_config_version_no: settings.value.version_no }
  clearPreview()
  busy.value = 'preview'
  message.value = '正在生成正式报告图片…'
  const created = []
  try {
    const response = await createWechatPreview(projectId, payload)
    if (!live || current !== generation || reportCurrent !== reportGeneration) return
    if (!response.ok) { message.value = safeError(response, '无法生成这份正式报告的图片，请核对报告与配置。'); return }
    const value = response.body
    if (!Array.isArray(value.pages) || value.pages.length < 1 || value.pages.length > 24 || value.report_version_id !== payload.expected_report_version_id || value.config_version_no !== payload.expected_config_version_no) throw new Error('invalid preview')
    for (const [position, item] of value.pages.entries()) {
      if (item.index !== position + 1 || !isHash(item.sha256)) throw new Error('invalid page')
      const blob = await getWechatImage(projectId, value.preview_id, item.index)
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()))
      if (Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('') !== item.sha256) throw new Error('image content changed')
      const bitmap = await createImageBitmap(blob)
      const dimensionsMatch = bitmap.width === item.width && bitmap.height === item.height
      bitmap.close()
      if (!dimensionsMatch) throw new Error('image dimensions changed')
      if (!live || current !== generation || reportCurrent !== reportGeneration) return
      created.push({ index: item.index, src: URL.createObjectURL(blob) })
    }
    pictures.value = created.splice(0)
    preview.value = value
    message.value = `已生成 ${value.pages.length} 张图片，请核对内容和接收人后确认发送。`
  } catch {
    if (live && current === generation && reportCurrent === reportGeneration) message.value = '图片预览没有完整加载，微信没有发送。请重新生成预览。'
  } finally {
    created.forEach(item => URL.revokeObjectURL(item.src))
    if (live && current === generation) busy.value = ''
  }
}

const stateLabel = state => ({ sending: '正在提交', accepted: '已提交微信，待核对收件', partial: '部分图片已提交', failed: '发送失败', unknown: '发送结果无法确认' }[state] || '状态待核对')
const failureReason = code => ({
  ILINK_INPUT_INVALID: '图片或微信绑定信息未通过检查，请重新读取绑定状态。',
  ILINK_UPLOAD_FAILED: '图片上传未完成，本页尚未提交微信。请核对网络与发送记录。',
  ILINK_AUTH_REJECTED: '微信未接受当前绑定凭据，请重新绑定微信。',
  ILINK_SESSION_EXPIRED: '微信会话已失效，请重新绑定微信。',
  ILINK_RATE_LIMITED: '微信暂时限制了发送频率，请稍后核对发送记录。',
  ILINK_REQUEST_REJECTED: '微信拒绝了本页发送。可用绑定的微信给 ClawBot 发一句话，再点击“刷新会话（可选）”；本次不会自动重发。',
  ILINK_RESPONSE_UNVERIFIED: '微信响应无法确认投递结果，请先核对微信，系统不会自动重发。',
  ILINK_SEND_UNCERTAIN: '连接中断或等待超时，图片可能已提交，请先核对微信，系统不会自动重发。',
  GATEWAY_AUTH_REJECTED: '网关拒绝了访问令牌，请核对令牌。',
  GATEWAY_POLICY_REJECTED: '网关未授权发送消息，请检查消息工具权限。',
  GATEWAY_UPLOADS_DISABLED: '网关尚未开启图片上传，请开启后更新配置。',
  GATEWAY_TOOL_UNAVAILABLE: '网关没有提供消息工具，请检查 OpenClaw 配置。',
  GATEWAY_INPUT_INVALID: '发送参数未通过检查，请核对微信绑定标识。',
  GATEWAY_TOOL_REJECTED: '消息工具拒绝了本次发送，请核对微信绑定和会话状态。',
  GATEWAY_IMAGE_TOO_LARGE: '网关拒绝了图片大小。',
  GATEWAY_RATE_LIMITED: '网关暂时限制了发送频率。',
  GATEWAY_REDIRECT_REJECTED: '网关地址发生跳转，请核对正确地址。',
  GATEWAY_REQUEST_REJECTED: '网关拒绝了发送请求。',
  GATEWAY_TIMEOUT: '等待网关响应超时，图片可能已经提交，请先核对微信。',
  GATEWAY_TRANSPORT_UNCERTAIN: '连接中断，图片是否提交尚不确定，请先核对微信。',
  GATEWAY_DELIVERY_UNCERTAIN: '网关尚未确认最终发送结果，请先核对微信。',
  GATEWAY_RESPONSE_UNVERIFIED: '网关响应无法确认投递结果，请先核对微信。',
  GATEWAY_SERVER_UNCERTAIN: '网关发生异常，图片是否提交尚不确定，请先核对微信。',
  WECHAT_INTERRUPTED_UNKNOWN: '应用在发送过程中中断，请先核对微信。'
}[code] || '本页未完成发送，请核对微信与连接配置。')
const pageFailures = item => (item.pages || []).filter(page => ['rejected', 'unknown'].includes(page.state))

async function send() {
  if (!canSend.value) return
  const current = generation
  const intended = preview.value
  const projectId = props.projectId
  if (!window.confirm(`确认将这份正式报告发送到微信？\n\n接收人：${intended.recipient_label}\n报告：${intended.report_label}\n共 ${intended.pages.length} 张图片\n\n发送后请在微信核对收件；结果未知时不会自动重发。`)) return
  if (!canSend.value || current !== generation || intended !== preview.value) return
  busy.value = 'sending'
  historyGeneration++
  submitted.value = true
  message.value = '正在逐张提交图片，请勿重复点击。'
  try {
    const response = await sendWechatPreview(projectId, intended.preview_id)
    if (!live || current !== generation) return
    if (!response.ok) message.value = safeError(response, '发送未完成，请刷新发送记录核对；不要连续重发。')
    else {
      const state = response.body?.state
      message.value = state === 'accepted' ? '全部图片已提交微信，请在客户微信中核对实际收件。' :
        state === 'partial' ? '部分图片已提交，其余没有完成；请先在微信核对，系统不会自动重发。' :
          state === 'failed' ? (response.body.retry_requires_config_change ? '本次未投递图片。修正并保存配置后，可重新生成预览、人工确认发送。' : '图片发送失败。请核对配置与发送记录，系统不会自动重发。') :
            '发送结果无法确认，请先在微信核对；系统不会自动重发。'
      if (response.body?.attempt_id) history.value = [response.body, ...history.value.filter(item => item.attempt_id !== response.body.attempt_id)].slice(0, 20)
    }
  } catch {
    if (live && current === generation) message.value = '发送结果无法确认，请刷新发送记录并在微信核对；不要重复点击发送。'
  } finally {
    if (live && current === generation) busy.value = ''
  }
}

async function refreshHistory() {
  const current = generation
  const currentHistory = ++historyGeneration
  try {
    const response = await getWechatHistory(props.projectId)
    if (!live || current !== generation || currentHistory !== historyGeneration) return
    if (!response.ok || !Array.isArray(response.body)) throw new Error('history failed')
    history.value = response.body
  } catch { if (live && current === generation && currentHistory === historyGeneration) message.value = '发送记录读取失败，请稍后刷新。' }
}

onMounted(load)
watch(() => props.projectId, load)
watch(() => [props.displayedReportVersionId, props.displayedReportHash, props.displayedModuleNarrativeHash, props.moduleNarrativeState, props.displayedGitMetricsState], () => {
  reportGeneration++
  clearPreview()
  if (busy.value !== 'sending') message.value = ''
})
onUnmounted(() => { live = false; generation++; clearPreview(); form.token = '' })
</script>

<template>
  <section class="wechat-panel" data-testid="wechat-panel" aria-label="微信发送正式报告">
    <div class="wechat-heading">
      <div><span class="wechat-kicker">微信 ClawBot</span><h2>把正式报告作为图片发到微信</h2><p>图片直接显示在聊天中，长报告按内容分页。</p></div>
      <button type="button" class="wechat-primary" :disabled="!canPreview" @click="makePreview">{{ busy === 'preview' ? '正在生成图片…' : '生成图片预览' }}</button>
    </div>
    <p v-if="loading">正在读取微信配置…</p>
    <template v-else>
      <WechatBindingPanel ref="bindingPanel" :project-id="projectId" :disabled="!!busy || loading" @active-change="bindingBusy = $event" @starting="clearPreview" @changed="refreshSettings" />
      <div v-if="configured" class="wechat-recipient"><b>接收人：{{ settings.recipient_label }}</b><span v-if="settings.transport !== 'direct'">微信接收标识：{{ settings.target }}</span></div>
      <details :open="configOpen" @toggle="configOpen = $event.target.open">
        <summary>高级接入：使用已有 OpenClaw</summary>
        <form class="wechat-config" @submit.prevent="save">
          <label>OpenClaw 网关地址<input v-model="form.gateway_url" type="url" placeholder="https://your-gateway.example" required :disabled="!!busy || bindingBusy" autocomplete="off"></label>
          <label>接收人名称<input v-model="form.recipient_label" required maxlength="128" :disabled="!!busy || bindingBusy" placeholder="客户或项目负责人"></label>
          <label>绑定账号标识<input v-model="form.account_id" required maxlength="200" :disabled="!!busy || bindingBusy" autocomplete="off"></label>
          <label>微信接收标识<input v-model="form.target" required maxlength="190" :disabled="!!busy || bindingBusy" placeholder="由已有 OpenClaw 微信会话提供" autocomplete="off"></label>
          <label>OpenClaw 会话<input v-model="form.session_key" required maxlength="200" :disabled="!!busy || bindingBusy" autocomplete="off"></label>
          <label>访问令牌<input v-model="form.token" type="password" :required="!configured || settings?.transport === 'direct'" :disabled="!!busy || bindingBusy" autocomplete="new-password" :placeholder="configured && settings?.transport !== 'direct' ? '已保存；留空保持原令牌' : '填写 OpenClaw 网关访问令牌'"></label>
          <p class="wechat-help">使用客户已绑定微信的 OpenClaw。远程地址需要 HTTPS；网关须允许消息工具和图片上传。修改网关地址时请重新填写令牌。</p>
          <button type="submit" :disabled="!!busy || bindingBusy || (!dirty && configured)">{{ busy === 'saving' ? '正在保存…' : '保存微信配置' }}</button>
        </form>
      </details>
      <p v-if="!reportReady" class="wechat-help">请先打开当前正式确认版报告，待模块说明与代码统计加载完成后生成图片。</p>
      <p v-if="dirty && configured" class="wechat-help">微信配置有未保存的修改，请先保存后重新核对预览。</p>
    </template>
    <p v-if="message" role="status" class="wechat-status" data-testid="wechat-status">{{ message }}</p>
    <div v-if="preview" data-testid="wechat-preview" class="wechat-preview">
      <div class="wechat-heading"><div><strong>{{ preview.report_label }}</strong><p>发送给 {{ preview.recipient_label }} · 共 {{ preview.pages.length }} 张</p></div><button type="button" class="wechat-primary" :disabled="!canSend" @click="send">{{ busy === 'sending' ? '正在发送…' : '确认发送图片' }}</button></div>
      <div class="wechat-pictures"><figure v-for="item in pictures" :key="item.index"><a :href="item.src" target="_blank" rel="noopener"><img :src="item.src" :alt="`正式报告第 ${item.index} 页`"></a><figcaption>第 {{ item.index }} / {{ pictures.length }} 页 · 点击查看大图</figcaption></figure></div>
    </div>
    <div v-if="!loading" class="wechat-history">
      <div class="wechat-heading"><strong>微信发送记录</strong><button type="button" :disabled="!!busy" @click="refreshHistory">刷新发送记录</button></div>
      <p v-if="!history.length" class="wechat-help">暂无微信发送记录。</p>
      <ul v-else><li v-for="item in history" :key="item.attempt_id">
        <span>报告 #{{ item.report_version_id }} · {{ item.recipient_label }}</span>
        <b>{{ stateLabel(item.state) }}</b>
        <small v-if="item.pages">{{ item.pages.filter(p => p.state === 'accepted').length }} / {{ item.pages.length }} 张已提交</small>
        <p v-for="page in pageFailures(item)" :key="page.index" class="wechat-page-reason">第 {{ page.index }} 页：{{ failureReason(page.code) }}</p>
        <p v-if="item.retry_requires_config_change" class="wechat-page-reason">本次明确未投递。修正并保存配置后，可重新生成预览、人工确认发送。</p>
      </li></ul>
    </div>
  </section>
</template>

<style scoped>
.wechat-page-reason{flex-basis:100%;margin:0;color:#68798b;line-height:1.7}
.wechat-panel{margin:0 0 20px;padding:22px 26px;border:1px solid #dce8e2;border-radius:18px;background:#fff;color:#203449}.wechat-heading{display:flex;justify-content:space-between;align-items:center;gap:20px}.wechat-kicker{color:#157a51;font-size:12px;font-weight:750;letter-spacing:.08em}.wechat-panel h2{font-size:18px;margin:7px 0}.wechat-heading p,.wechat-help{font-size:13px;line-height:1.7;color:#68798b;margin:7px 0}.wechat-panel button{font:inherit;font-size:13px;font-weight:650;border:1px solid #d4e2db;border-radius:9px;background:#fff;color:#24593f;padding:10px 15px;cursor:pointer;white-space:nowrap}.wechat-panel button.wechat-primary{color:#fff;background:#157a51;border-color:#157a51}.wechat-panel button:disabled{opacity:.45;cursor:not-allowed}.wechat-panel details{margin-top:14px;border-top:1px solid #edf1ee;padding-top:13px}.wechat-panel summary{cursor:pointer;font-size:13px;font-weight:650;color:#286548}.wechat-config{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}.wechat-config label{font-size:12px;display:flex;flex-direction:column;gap:7px;color:#52667a}.wechat-config input{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid #d7e1dc;border-radius:8px;color:#253a4e;background:#f9fbfa;font:inherit;font-size:13px}.wechat-config .wechat-help{grid-column:1/-1}.wechat-recipient{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;padding:13px 0 0}.wechat-recipient span{color:#718192;overflow-wrap:anywhere}.wechat-status{font-size:13px;color:#285943;padding:12px 14px;border-radius:9px;background:#f0f8f3;line-height:1.7}.wechat-preview{margin-top:18px;border-top:1px solid #e5eee9;padding-top:18px}.wechat-pictures{display:flex;gap:16px;overflow:auto;padding:16px 0 6px}.wechat-pictures figure{margin:0;flex:0 0 240px}.wechat-pictures img{display:block;width:100%;height:310px;object-fit:contain;object-position:top;border:1px solid #e1e7e4;background:#f4f6f5;border-radius:8px}.wechat-pictures figcaption{font-size:12px;color:#6d7c88;text-align:center;padding:9px}.wechat-history{margin-top:16px;padding-top:14px;border-top:1px solid #edf1ee;font-size:13px}.wechat-history ul{list-style:none;margin:12px 0 0;padding:0}.wechat-history li{display:flex;gap:15px;flex-wrap:wrap;padding:11px 0;border-top:1px solid #f0f2f1}.wechat-history b{font-weight:600}.wechat-history small{color:#778895}@media(max-width:700px){.wechat-panel{padding:18px}.wechat-heading{align-items:flex-start;flex-direction:column;gap:10px}.wechat-config{grid-template-columns:1fr}.wechat-pictures figure{flex-basis:210px}}
</style>
