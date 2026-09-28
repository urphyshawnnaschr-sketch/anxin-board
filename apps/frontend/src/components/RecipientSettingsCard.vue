<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getRecipientConfig, saveRecipientConfig } from '../api/recipientSettings.js'

const props = defineProps({
  projectId: { type: Number, default: null }
})

const loadState = ref('idle')
const saveState = ref('idle')
const configured = ref(false)
const versionNo = ref(0)
const recipientsText = ref('')
const message = ref('')

const projectScoped = computed(() => Number.isInteger(props.projectId) && props.projectId > 0)
const recipients = computed(() => (
  recipientsText.value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean)
))
const canSave = computed(() => projectScoped.value && saveState.value !== 'saving' && recipients.value.length > 0)
const statusLabel = computed(() => {
  if (!projectScoped.value) return '请选择项目'
  if (loadState.value === 'loading') return '检查中'
  if (loadState.value === 'failed') return '状态未知'
  return configured.value ? `已配置 ${recipients.value.length} 人` : '待配置'
})
const statusClass = computed(() => {
  if (!projectScoped.value || loadState.value === 'failed') return 'danger'
  return configured.value ? 'ok' : 'pending'
})

function applyConfig(body) {
  configured.value = body?.configured === true
  versionNo.value = Number.isInteger(body?.version_no) ? body.version_no : 0
  const list = Array.isArray(body?.to_recipients) ? body.to_recipients : []
  recipientsText.value = list.join('\n')
}

async function loadConfig() {
  message.value = ''
  saveState.value = 'idle'
  if (!projectScoped.value) {
    loadState.value = 'idle'
    configured.value = false
    versionNo.value = 0
    recipientsText.value = ''
    return
  }
  loadState.value = 'loading'
  try {
    const { ok, body } = await getRecipientConfig(props.projectId)
    if (!ok) {
      loadState.value = 'failed'
      message.value = body?.detail?.message || '收件人配置暂时无法读取。'
      return
    }
    applyConfig(body)
    loadState.value = 'loaded'
  } catch {
    loadState.value = 'failed'
    message.value = '无法连接本地服务，收件人配置状态未知。'
  }
}

async function saveConfig() {
  if (!canSave.value) return
  saveState.value = 'saving'
  message.value = ''
  try {
    const { ok, body } = await saveRecipientConfig(props.projectId, {
      to_recipients: recipients.value,
      expected_version_no: versionNo.value
    })
    if (!ok) {
      saveState.value = 'failed'
      message.value = body?.detail?.message || '收件人配置保存失败。'
      if (body?.detail?.code === 'RECIPIENT_CONFIG_VERSION_CONFLICT') await loadConfig()
      return
    }
    applyConfig(body)
    loadState.value = 'loaded'
    saveState.value = 'saved'
    message.value = '收件人名单已保存为新的不可变版本。修改名单不会改写旧版本。'
  } catch (error) {
    saveState.value = 'failed'
    message.value = error?.message === 'LOCAL_SESSION_UNAVAILABLE'
      ? '请从安心看板本地启动器打开当前项目后再保存收件人。'
      : '保存失败，请确认本地服务正在运行。'
  }
}

onMounted(loadConfig)
watch(() => props.projectId, loadConfig)
</script>

<template>
  <section class="recipient-card" aria-labelledby="recipient-settings-title">
    <div class="recipient-head">
      <div class="recipient-icon">To</div>
      <div>
        <span class="kicker">收件人</span>
        <h2 id="recipient-settings-title">项目收件人</h2>
        <p>这里保存“未来允许发给谁”。名单按项目独立版本化，旧报告不会因为后来改名单而自动换收件人。</p>
      </div>
      <span :class="['status', statusClass]">{{ statusLabel }}</span>
    </div>

    <div v-if="!projectScoped" class="scope-warning">
      请从某个项目内进入“设置”，再配置这个项目的收件人。
    </div>

    <form v-else class="recipient-form" @submit.prevent="saveConfig">
      <label for="recipient-list">To 收件人（每行一个邮箱）</label>
      <textarea
        id="recipient-list"
        v-model="recipientsText"
        rows="5"
        maxlength="33000"
        autocomplete="off"
        placeholder="alice@example.com&#10;bob@example.com"
        :disabled="saveState === 'saving'"
      />
      <div class="recipient-actions">
        <button class="primary-button" type="submit" :disabled="!canSave">
          {{ saveState === 'saving' ? '保存中…' : configured ? '更新收件人名单' : '保存收件人名单' }}
        </button>
        <span>只支持标准 To 邮箱；不提供 CC/BCC、显示名、群组或通配符。</span>
      </div>
    </form>

    <p v-if="message" :class="['message', saveState === 'failed' || loadState === 'failed' ? 'error' : 'success']">{{ message }}</p>
    <div class="gate-note">
      <b>保存名单 ≠ 允许发送</b>
      <span>真正发送前，系统还必须把已正式确认的报告与这份收件人版本明确绑定，并通过发送准入。</span>
    </div>
  </section>
</template>

<style scoped>
.recipient-card{font-size:var(--ui-text-body);line-height:1.6;padding:20px;border:1px solid #dce7df;border-radius:17px;background:linear-gradient(145deg,#fff,#f7fcf9);box-shadow:0 10px 28px rgba(29,41,57,.04)}
.recipient-head{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:13px;align-items:start}.recipient-head h2{margin:4px 0 5px;font-size:var(--ui-text-section)}.recipient-head p{margin:0;color:#727d8f;font-size:var(--ui-text-helper);line-height:1.55}.recipient-icon{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;color:#16735e;background:#e9f8f3;font-weight:var(--ui-weight-strong)}.kicker{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.1em;color:#27806d}.status{display:inline-flex;align-items:center;justify-content:center;min-height:26px;padding:4px 9px;border-radius:999px;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);white-space:nowrap}.status.ok{color:#14735d;background:#e8f8f3}.status.pending{color:#8a6417;background:#fff5dd}.status.danger{color:#a43b3b;background:#fff0f0}
.recipient-form{display:grid;gap:8px;margin-top:18px;padding:16px;border:1px solid #e4ebe6;border-radius:13px;background:rgba(255,255,255,.94)}.recipient-form label{font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong);color:#586478}.recipient-form textarea{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cfd7e4;border-radius:10px;background:#fff;color:#172033;font:inherit;line-height:1.65;resize:vertical}.recipient-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding-top:4px}.recipient-actions span{color:#8791a2;font-size:var(--ui-text-helper);line-height:1.5}.primary-button{font-size:var(--ui-text-body);min-height:40px;padding:0 14px;border:0;border-radius:10px;background:#23806b;color:#fff;font-weight:var(--ui-weight-strong);cursor:pointer}.primary-button:disabled{opacity:.5;cursor:not-allowed}.message{margin:10px 0 0;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.message.success{color:#14735d}.message.error{color:#a43b3b}.gate-note{display:grid;gap:3px;margin-top:12px;padding:12px;border-radius:11px;background:#fff8e8}.gate-note b{font-size:var(--ui-text-body);color:#785818}.gate-note span{font-size:var(--ui-text-helper);line-height:1.55;color:#8b7040}.scope-warning{margin-top:16px;padding:12px;border-radius:11px;background:#fff0f0;color:#8f3e3e;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong);line-height:1.6}
@media (max-width:640px){.recipient-head{grid-template-columns:1fr}.recipient-head .status{justify-self:start}}
</style>
