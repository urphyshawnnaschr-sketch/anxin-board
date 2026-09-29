<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { cancelWechatLogin, disconnectWechatBinding, getWechatBinding, getWechatLoginQR, pollWechatLogin, refreshWechatBinding, startWechatLogin } from '../api/wechatDelivery.js'

const props = defineProps({ projectId: { type: [Number, String], required: true }, disabled: Boolean })
const emit = defineEmits(['active-change', 'changed', 'starting'])
const binding = ref(null)
const status = ref('idle')
const busy = ref('loading')
const flow = ref(null)
const qrSource = ref('')
const verificationCode = ref('')
const validVerificationCode = computed(() => /^[0-9]{4,12}$/.test(verificationCode.value.trim()))
const notice = ref('')
let generation = 0
let live = true
let pollTimer = null
let expiryTimer = null
let controller = null
const active = computed(() => !!busy.value || !!flow.value)
const ready = computed(() => binding.value?.transport === 'direct' && binding.value?.binding_state === 'ready' && binding.value?.context_ready === true)
const awaitingMessage = computed(() => status.value === 'awaiting_message' || binding.value?.transport === 'direct' && binding.value?.binding_state === 'awaiting_message')
const validFlow = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(value)

function bindingError(response, fallback) {
  return {
    WECHAT_LOGIN_BUSY: '已有扫码操作正在进行，请稍后重新读取绑定状态。',
    WECHAT_BINDING_BUSY: '微信操作正在进行，请稍后重新读取绑定状态。',
    WECHAT_LOGIN_STALE: '本次扫码已失效，请重新绑定微信。',
    WECHAT_LOGIN_EXPIRED: '二维码已过期，请重新点击绑定微信。',
    WECHAT_LOGIN_ALREADY_BOUND: '微信未提供新的绑定凭据，请重新扫码，或使用高级 OpenClaw 接入。',
    WECHAT_CONFIG_STALE: '微信配置已变化，请重新读取绑定状态。',
    WECHAT_BINDING_REQUIRED: '请先扫码绑定微信。',
    WECHAT_BINDING_CONTEXT_REQUIRED: '请用刚扫码的微信给 ClawBot 发一句话，再检查绑定。',
    WECHAT_CREDENTIAL_UNAVAILABLE: '本机微信凭据不可用，请重新绑定微信。',
    WECHAT_CREDENTIAL_REVOKE_FAILED: '本机旧绑定凭据未能完整清理，请重新读取状态后再操作。',
    ILINK_AUTH_REJECTED: '微信未接受当前绑定凭据，请重新绑定微信。',
    ILINK_SESSION_EXPIRED: '微信会话已失效，请重新绑定微信。',
    ILINK_RATE_LIMITED: '微信暂时限制了操作频率，请稍后再试。',
    ILINK_NETWORK_ERROR: '暂时无法连接微信，请检查网络后重新操作。',
    ILINK_TIMEOUT: '等待微信响应超时，请稍后重新操作。'
  }[response?.body?.detail?.code] || fallback
}

function safeBinding(value) {
  if (!value || !['direct', 'openclaw'].includes(value.transport) || !['unbound', 'awaiting_message', 'ready', 'disconnected'].includes(value.binding_state) || !Number.isInteger(value.version_no) || value.version_no < 0) throw new Error('invalid binding')
  return { transport: value.transport, binding_state: value.binding_state, version_no: value.version_no,
    recipient_label: typeof value.recipient_label === 'string' ? value.recipient_label.slice(0, 200) : '', context_ready: value.context_ready === true }
}

function clearQR() {
  if (qrSource.value) URL.revokeObjectURL(qrSource.value)
  qrSource.value = ''
  verificationCode.value = ''
}

function stopLocal() {
  clearTimeout(pollTimer); pollTimer = null
  clearTimeout(expiryTimer); expiryTimer = null
  controller?.abort(); controller = null
  clearQR()
  flow.value = null
  busy.value = ''
}

function discard(projectId, flowId) {
  if (validFlow(flowId)) void cancelWechatLogin(projectId, flowId).catch(() => {})
}

function fail(current, message) {
  if (!live || current !== generation) return
  const old = flow.value
  generation++
  stopLocal()
  status.value = 'error'
  notice.value = message
  discard(old?.projectId, old?.id)
  emit('changed')
}

function expire(current) {
  if (!live || current !== generation) return
  const old = flow.value
  generation++
  stopLocal()
  status.value = 'expired'
  notice.value = '二维码已过期，请重新点击绑定微信。'
  discard(old?.projectId, old?.id)
  emit('changed')
}

function setExpiry(value, current) {
  const deadline = Date.parse(value)
  if (!Number.isFinite(deadline)) throw new Error('invalid expiry')
  clearTimeout(expiryTimer)
  if (deadline <= Date.now()) { expire(current); return false }
  expiryTimer = setTimeout(() => expire(current), Math.min(deadline - Date.now(), 2147483647))
  return true
}

async function reload() {
  const previous = flow.value
  const current = ++generation
  stopLocal()
  discard(previous?.projectId, previous?.id)
  binding.value = null
  status.value = 'idle'
  notice.value = ''
  busy.value = 'loading'
  try {
    const response = await getWechatBinding(props.projectId)
    if (!live || current !== generation) return
    if (!response.ok) throw new Error('binding read failed')
    binding.value = safeBinding(response.body)
  } catch {
    if (live && current === generation) notice.value = '绑定状态读取失败，请重新读取。'
  } finally { if (live && current === generation) busy.value = '' }
}

async function start() {
  if (props.disabled || active.value || !binding.value) return
  const current = ++generation
  const projectId = props.projectId
  const version = binding.value.version_no
  emit('starting')
  stopLocal()
  busy.value = 'starting'
  status.value = 'starting'
  notice.value = ''
  try {
    const response = await startWechatLogin(projectId, version)
    if (!live || current !== generation) { discard(projectId, response.body?.flow_id); return }
    if (!response.ok) { fail(current, bindingError(response, '暂时无法生成绑定二维码，请稍后重新尝试。')); return }
    if (!validFlow(response.body?.flow_id) || response.body.status !== 'wait') throw new Error('start failed')
    flow.value = { id: response.body.flow_id, projectId }
    status.value = 'wait'
    if (!setExpiry(response.body.expires_at, current)) return
    controller = new AbortController()
    const blob = await getWechatLoginQR(projectId, flow.value.id, controller.signal)
    const bitmap = await createImageBitmap(blob)
    bitmap.close()
    if (!live || current !== generation) return
    qrSource.value = URL.createObjectURL(blob)
    busy.value = ''
    schedule(current)
  } catch { fail(current, '暂时无法生成绑定二维码，请稍后重新尝试。') }
}

function schedule(current) {
  clearTimeout(pollTimer)
  pollTimer = setTimeout(() => poll(current), 2000)
}

async function poll(current, code = '') {
  if (!live || current !== generation || !flow.value || busy.value) return
  const intended = flow.value
  busy.value = 'polling'
  controller = new AbortController()
  try {
    const response = await pollWechatLogin(intended.projectId, intended.id, code, controller.signal)
    if (!live || current !== generation) return
    const value = response.body
    if (!response.ok) { fail(current, bindingError(response, '暂时无法确认绑定状态，请重新读取后再操作。')); return }
    if (value?.flow_id !== intended.id || !['wait', 'scaned', 'need_verifycode', 'verify_code_blocked', 'expired', 'awaiting_message', 'ready'].includes(value.status)) throw new Error('poll failed')
    if (value.binding) binding.value = safeBinding(value.binding)
    status.value = value.status
    if (value.status === 'ready') {
      if (!ready.value) throw new Error('unverified binding')
      stopLocal()
      emit('changed')
      return
    }
    if (value.status === 'expired') { expire(current); return }
    if (!setExpiry(value.expires_at, current)) return
    if (['scaned', 'need_verifycode', 'verify_code_blocked', 'awaiting_message'].includes(value.status)) clearQR()
    if (value.status === 'awaiting_message') emit('changed')
    if (value.status === 'verify_code_blocked') {
      fail(current, '微信暂时不允许继续验证，请稍后重新绑定。')
      return
    }
    if (value.status !== 'need_verifycode') schedule(current)
  } catch { fail(current, '暂时无法确认绑定状态，请重新读取后再操作。') }
  finally { code = ''; if (live && current === generation) { verificationCode.value = ''; busy.value = '' } }
}

function submitCode() {
  if (status.value !== 'need_verifycode' || busy.value || !validVerificationCode.value) return
  const code = verificationCode.value.trim()
  verificationCode.value = ''
  void poll(generation, code)
}

async function cancel() {
  const old = flow.value
  const current = ++generation
  stopLocal()
  status.value = 'idle'
  notice.value = '已取消本次扫码。'
  if (!old) return
  busy.value = 'cancelling'
  try {
    const response = await cancelWechatLogin(old.projectId, old.id)
    if (!live || current !== generation) return
    if (!response.ok || response.body?.cancelled !== true) throw new Error('cancel failed')
    binding.value = safeBinding(response.body.binding)
  } catch { if (live && current === generation) notice.value = '本页已停止扫码，请重新读取绑定状态。' }
  finally { if (live && current === generation) { busy.value = ''; emit('changed') } }
}

async function checkMessage() {
  if (props.disabled || active.value || !binding.value) return
  const current = generation
  busy.value = 'refreshing'
  notice.value = ''
  try {
    const response = await refreshWechatBinding(props.projectId, binding.value.version_no)
    if (!live || current !== generation) return
    if (!response.ok) { notice.value = bindingError(response, '暂时无法确认绑定状态，请稍后再检查。'); return }
    binding.value = safeBinding(response.body)
    status.value = 'idle'
    if (!ready.value) notice.value = '还没有收到确认消息。请用刚扫码的微信给 ClawBot 发一句话，再检查绑定。'
    emit('changed')
  } catch { if (live && current === generation) notice.value = '暂时无法确认绑定状态，请稍后再检查。' }
  finally { if (live && current === generation) busy.value = '' }
}

async function disconnect() {
  if (props.disabled || active.value || !binding.value || !window.confirm('解除这台电脑上的微信绑定？之后需要重新扫码才能发送报告图片。')) return
  const current = generation
  busy.value = 'disconnecting'
  emit('starting')
  try {
    const response = await disconnectWechatBinding(props.projectId, binding.value.version_no)
    if (!live || current !== generation) return
    if (!response.ok) { notice.value = bindingError(response, '解除结果暂时无法确认，请重新读取绑定状态。'); return }
    binding.value = safeBinding(response.body)
    status.value = 'idle'
    notice.value = '已解除这台电脑上的微信绑定。'
    emit('changed')
  } catch { if (live && current === generation) notice.value = '解除结果暂时无法确认，请重新读取绑定状态。' }
  finally { if (live && current === generation) busy.value = '' }
}

watch(active, value => emit('active-change', value), { immediate: true })
watch(() => props.projectId, reload)
onMounted(reload)
onUnmounted(() => { live = false; generation++; const old = flow.value; stopLocal(); discard(old?.projectId, old?.id) })
defineExpose({ reload })
</script>

<template>
  <div class="wechat-binding" data-testid="wechat-binding">
    <div class="binding-title"><strong>微信收件</strong><button v-if="!flow && busy !== 'starting'" type="button" :disabled="disabled || active || !binding" @click="start">{{ ready ? '重新绑定微信' : '绑定微信' }}</button><button v-else type="button" @click="cancel">取消绑定</button></div>
    <p v-if="ready && !flow && status !== 'starting'" class="binding-ready">微信已绑定，可以接收报告图片。</p>
    <p v-else-if="binding?.transport === 'openclaw' && !flow" class="binding-guide">当前使用高级 OpenClaw 接入，也可以在这里扫码绑定微信。</p>
    <p v-else-if="!flow && !awaitingMessage && !busy">用收件人的微信扫码，再给 ClawBot 发一句话，即可绑定。</p>
    <p v-if="!flow && status !== 'starting'" class="binding-note">若这个微信已在其他工具使用 ClawBot，重新绑定可能影响原连接；可改用高级接入。</p>
    <p v-if="busy === 'starting'">正在获取微信二维码…</p>
    <p v-if="busy === 'loading'">正在读取绑定状态…</p>
    <div v-if="qrSource" class="binding-qr"><img :src="qrSource" alt="微信绑定二维码"><p>请用收件人的微信扫一扫。</p></div>
    <p v-if="status === 'scaned'">已扫码，请在微信中确认。</p>
    <p v-if="awaitingMessage">扫码已确认。请用刚扫码的微信给 ClawBot 发一句话，完成收件验证；这里不会自动回复消息。</p>
    <button v-if="awaitingMessage && !flow" type="button" :disabled="disabled || active" @click="checkMessage">我已发送，检查绑定</button>
    <form v-if="status === 'need_verifycode'" class="binding-code" @submit.prevent="submitCode"><label>微信验证码<input v-model="verificationCode" type="password" inputmode="numeric" pattern="[0-9]{4,12}" minlength="4" maxlength="12" autocomplete="one-time-code" :disabled="!!busy" required></label><button type="submit" :disabled="!!busy || !validVerificationCode">提交验证码</button><small>请填写微信要求的 4–12 位数字验证码，提交后会立即清空。</small></form>
    <p v-if="notice" role="status" class="binding-notice">{{ notice }}</p>
    <button v-if="!binding || status === 'error'" type="button" :disabled="active || disabled" @click="reload">重新读取绑定状态</button>
    <button v-if="binding?.transport === 'direct' && ['ready', 'awaiting_message'].includes(binding.binding_state) && !flow" type="button" class="binding-secondary" :disabled="disabled || active" @click="disconnect">解除绑定</button>
  </div>
</template>

<style scoped>
.wechat-binding{margin-top:18px;padding:18px 20px;background:#f5faf7;border:1px solid #e0eee5;border-radius:12px;font-size:13px;line-height:1.7}.binding-title{display:flex;align-items:center;justify-content:space-between;gap:16px}.wechat-binding p{margin:10px 0 0}.binding-ready{color:#157a51;font-weight:650}.binding-note,.binding-guide,.binding-code small{color:#6d7d73;font-size:12px}.binding-qr{padding:18px 0 4px;text-align:center}.binding-qr img{display:block;width:220px;height:220px;max-width:100%;object-fit:contain;margin:auto;background:white;border:10px solid white;border-radius:8px}.wechat-binding button{padding:8px 14px;border:1px solid #d2e3d8;border-radius:8px;color:#17623f;background:white;font:inherit;cursor:pointer}.wechat-binding button:disabled{opacity:.5;cursor:not-allowed}.binding-secondary{margin:12px 0 0 10px}.binding-code{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-top:14px}.binding-code label{display:flex;flex-direction:column;gap:5px}.binding-code input{border:1px solid #cbded1;padding:8px 10px;border-radius:7px;font:inherit;max-width:180px}.binding-code small{flex-basis:100%}.binding-notice{color:#51645a}
</style>
