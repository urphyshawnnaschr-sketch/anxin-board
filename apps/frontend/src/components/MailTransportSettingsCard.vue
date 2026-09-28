<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { configureMailTransport, getMailTransportSettings, testMailTransportConnection } from '../api/mailSettings.js'
import {
  MAIL_PROVIDER_PRESETS,
  detectMailProvider,
  getMailProviderPreset,
  isMailTransportConnectionTestSupported
} from '../api/mailProviderPresets.js'

const loadState = ref('loading')
const saveState = ref('idle')
const testState = ref('idle')
const message = ref('')
const testMessage = ref('')
const configured = ref(false)
const profileVersion = ref(0)
const providerKey = ref('qq')
const savedFingerprint = ref('')

const form = reactive({
  host: '',
  port: 587,
  security: 'starttls',
  username: '',
  from_identity: '',
  password: '',
  timeout_seconds: 30
})

const selectedProvider = computed(() => getMailProviderPreset(providerKey.value))
const providerSupported = computed(() => selectedProvider.value?.supported !== false)
const providerHelpAvailable = computed(() => Boolean(selectedProvider.value?.helpUrl))
const transportTestSupported = computed(() => isMailTransportConnectionTestSupported(form))
const transportTestBlocked = computed(() => !transportTestSupported.value)

const statusLabel = computed(() => {
  if (loadState.value === 'loading') return '检查中'
  if (loadState.value === 'failed') return '状态未知'
  return configured.value ? '已配置' : '待配置'
})

const statusClass = computed(() => {
  if (loadState.value === 'failed') return 'danger'
  return configured.value ? 'ok' : 'pending'
})

function transportFingerprint(value = form) {
  return JSON.stringify({
    host: String(value.host || '').trim(),
    port: Number(value.port),
    security: value.security,
    username: String(value.username || '').trim(),
    from_identity: String(value.from_identity || '').trim(),
    timeout_seconds: Number(value.timeout_seconds)
  })
}

const hasUnsavedChanges = computed(() => (
  Boolean(form.password)
  || !configured.value
  || transportFingerprint() !== savedFingerprint.value
))

const canSave = computed(() => (
  providerSupported.value
  && saveState.value !== 'saving'
  && testState.value !== 'testing'
  && form.host.trim()
  && Number(form.port) > 0
  && form.username.trim()
  && form.from_identity.trim()
  && form.password
))

const canTest = computed(() => (
  configured.value
  && providerSupported.value
  && transportTestSupported.value
  && !hasUnsavedChanges.value
  && saveState.value !== 'saving'
  && testState.value !== 'testing'
))

function applyProviderPreset() {
  const preset = selectedProvider.value
  testMessage.value = ''
  testState.value = 'idle'
  form.password = ''
  if (!preset || preset.key === 'custom') return
  form.host = preset.host
  form.port = preset.port
  form.security = preset.security
}

function fillSenderFromUsername() {
  if (!form.from_identity.trim() && form.username.trim()) {
    form.from_identity = form.username.trim()
  }
}

function applySettings(body) {
  configured.value = body?.configured === true
  const profile = body?.profile
  profileVersion.value = Number.isInteger(profile?.version_no) ? profile.version_no : 0
  if (!profile) {
    providerKey.value = 'qq'
    applyProviderPreset()
    savedFingerprint.value = ''
    return
  }
  form.host = profile.host || ''
  form.port = Number(profile.port) || 587
  form.security = profile.security === 'implicit_tls' ? 'implicit_tls' : 'starttls'
  form.username = profile.username || ''
  form.from_identity = profile.from_identity || ''
  form.timeout_seconds = Number(profile.timeout_seconds) || 30
  providerKey.value = detectMailProvider(profile)
  savedFingerprint.value = transportFingerprint(profile)
}

async function loadSettings() {
  loadState.value = 'loading'
  message.value = ''
  testMessage.value = ''
  try {
    const { ok, body } = await getMailTransportSettings()
    if (!ok) {
      loadState.value = 'failed'
      message.value = body?.detail?.message || '邮件发送配置状态暂时无法读取。'
      return
    }
    applySettings(body)
    loadState.value = 'loaded'
  } catch {
    loadState.value = 'failed'
    message.value = '无法连接本地服务，邮件发送配置保持关闭。'
  }
}

async function saveSettings() {
  if (!canSave.value) return
  saveState.value = 'saving'
  message.value = ''
  testMessage.value = ''
  testState.value = 'idle'
  try {
    const { ok, body } = await configureMailTransport({
      host: form.host.trim(),
      port: Number(form.port),
      security: form.security,
      username: form.username.trim(),
      from_identity: form.from_identity.trim(),
      password: form.password,
      timeout_seconds: Number(form.timeout_seconds),
      expected_version_no: profileVersion.value
    })
    if (!ok) {
      saveState.value = 'failed'
      message.value = body?.detail?.message || '邮件发送配置保存失败，请检查后重试。'
      if (body?.detail?.code === 'MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT') await loadSettings()
      return
    }
    form.password = ''
    applySettings(body)
    loadState.value = 'loaded'
    saveState.value = 'saved'
    message.value = '邮箱配置已保存。下一步点“测试连接”确认账号和授权码是否可用；测试不会发送邮件。'
  } catch (error) {
    saveState.value = 'failed'
    message.value = error?.message === 'LOCAL_SESSION_UNAVAILABLE'
      ? '请从安心看板本地启动器打开当前页面后再保存 SMTP 配置。'
      : '保存失败，请确认本地服务正在运行。'
  }
}

async function runConnectionTest() {
  if (!canTest.value) return
  testState.value = 'testing'
  testMessage.value = ''
  try {
    const { ok, body } = await testMailTransportConnection(profileVersion.value)
    if (!ok) {
      if (body?.detail?.code === 'MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT') {
        await loadSettings()
        testState.value = 'failed'
        testMessage.value = '邮件发送配置已在其他窗口变化，已刷新到最新版本；请确认后重新测试。'
        return
      }
      testState.value = 'failed'
      testMessage.value = body?.detail?.message || '连接失败：请先确认邮箱已开启 SMTP/客户端协议，并检查授权码或应用专用密码。'
      return
    }
    testState.value = 'passed'
    testMessage.value = '连接成功：邮箱账号、授权码、SMTP 和 TLS 均可用；本次测试没有发送任何邮件。'
  } catch (error) {
    testState.value = 'failed'
    testMessage.value = error?.message === 'LOCAL_SESSION_UNAVAILABLE'
      ? '请从安心看板本地启动器打开当前页面后再测试连接。'
      : '连接测试失败，请确认本地服务正在运行。'
  }
}

onMounted(loadSettings)
</script>

<template>
  <section class="mail-card" aria-labelledby="mail-settings-title">
    <div class="mail-head">
      <div class="mail-icon">✉</div>
      <div>
        <span class="kicker">邮件发送</span>
        <h2 id="mail-settings-title">邮箱发送配置</h2>
        <p>选一个常用邮箱，服务器、端口和加密方式会自动填写；你主要只需要准备邮箱账号和授权码 / 应用专用密码。</p>
      </div>
      <span :class="['status', statusClass]">{{ statusLabel }}</span>
    </div>

    <div class="setup-steps" aria-label="邮箱配置步骤">
      <span><b>1</b> 选邮箱</span>
      <span><b>2</b> 填账号和授权码</span>
      <span><b>3</b> 保存配置</span>
      <span><b>4</b> 测试连接</span>
    </div>

    <form class="mail-form" @submit.prevent="saveSettings">
      <label class="provider-field">
        <span>邮箱服务商</span>
        <select v-model="providerKey" :disabled="saveState === 'saving' || testState === 'testing'" @change="applyProviderPreset">
          <option v-for="provider in MAIL_PROVIDER_PRESETS" :key="provider.key" :value="provider.key">
            {{ provider.label }}{{ provider.supported === false ? '（暂不支持）' : '' }}
          </option>
        </select>
      </label>

      <div :class="['provider-note', providerSupported && transportTestSupported ? 'info' : 'warning']">
        <template v-if="transportTestBlocked">
          <b>连接测试暂不支持</b>
          <span>当前 SMTP 参数需要 OAuth2 / Modern Auth；安心看板 V1 不会用 Basic AUTH 测试这组参数。</span>
        </template>
        <template v-else>
          <b>{{ selectedProvider.credentialLabel }}</b>
          <span>{{ selectedProvider.help }}</span>
        </template>
        <a
          v-if="providerHelpAvailable"
          class="provider-help-link"
          :href="selectedProvider.helpUrl"
          target="_blank"
          rel="noopener noreferrer"
        >{{ selectedProvider.helpLinkLabel }} ↗</a>
      </div>

      <label class="identity-field">
        <span>邮箱账号 / SMTP 用户名</span>
        <input v-model="form.username" autocomplete="username" maxlength="320" placeholder="name@example.com" :disabled="saveState === 'saving' || testState === 'testing'" @blur="fillSenderFromUsername" />
      </label>
      <label class="identity-field">
        <span>发件地址（通常与邮箱账号相同）</span>
        <input v-model="form.from_identity" autocomplete="email" maxlength="320" placeholder="留空时会自动使用上面的邮箱账号" :disabled="saveState === 'saving' || testState === 'testing'" />
      </label>
      <label class="credential-field">
        <span>{{ selectedProvider.credentialLabel }}</span>
        <input v-model="form.password" type="password" autocomplete="new-password" maxlength="1024" :placeholder="providerSupported ? (configured ? '更新配置时请重新输入' : '输入授权码 / 应用专用密码') : '当前版本暂不支持；请勿输入密码'" :disabled="!providerSupported || saveState === 'saving' || testState === 'testing'" />
      </label>

      <details class="advanced" :open="providerKey === 'custom'">
        <summary>高级 SMTP 设置</summary>
        <div class="advanced-grid">
          <label>
            <span>SMTP 服务器</span>
            <input v-model="form.host" autocomplete="off" maxlength="253" placeholder="smtp.example.com" :disabled="saveState === 'saving' || testState === 'testing'" />
          </label>
          <label>
            <span>端口</span>
            <input v-model.number="form.port" type="number" min="1" max="65535" :disabled="saveState === 'saving' || testState === 'testing'" />
          </label>
          <label>
            <span>连接安全</span>
            <select v-model="form.security" :disabled="saveState === 'saving' || testState === 'testing'">
              <option value="starttls">STARTTLS</option>
              <option value="implicit_tls">Implicit TLS</option>
            </select>
          </label>
          <label>
            <span>连接超时（秒）</span>
            <input v-model.number="form.timeout_seconds" type="number" min="1" max="60" :disabled="saveState === 'saving' || testState === 'testing'" />
          </label>
        </div>
      </details>

      <div class="mail-actions">
        <button class="primary-button" type="submit" :disabled="!canSave">
          {{ saveState === 'saving' ? '保存中…' : configured ? '更新邮件发送配置' : '保存邮件发送配置' }}
        </button>
        <button class="secondary-button" type="button" :disabled="!canTest" @click="runConnectionTest">
          {{ testState === 'testing' ? '测试中…' : '测试连接' }}
        </button>
        <span>测试连接只检查账号、授权码、SMTP、TLS 和登录是否正常，不会给任何人发邮件。</span>
      </div>
      <p v-if="configured && hasUnsavedChanges" class="dirty-note">当前表单有未保存修改；请先保存，再测试已保存配置。</p>
      <p v-else-if="configured && transportTestBlocked" class="dirty-note">当前已保存的 SMTP 参数属于本版本不支持的认证模式；测试连接保持关闭。</p>
    </form>

    <p v-if="message" :class="['message', saveState === 'failed' || loadState === 'failed' ? 'error' : 'success']">{{ message }}</p>
    <p v-if="testMessage" :class="['message', testState === 'failed' ? 'error' : 'success']">{{ testMessage }}</p>
    <div class="gate-note">
      <b>测试成功 ≠ 已经发信</b>
      <span>这里仅确认邮箱配置能用。正式发送仍要经过报告确认、收件人绑定和发送准入。</span>
    </div>
  </section>
</template>

<style scoped>
.mail-card{font-size:var(--ui-text-body);line-height:1.6;padding:20px;border:1px solid #dce3f3;border-radius:17px;background:linear-gradient(145deg,#fff,#f8faff);box-shadow:0 10px 28px rgba(29,41,57,.04)}
.mail-head{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:13px;align-items:start}.mail-head h2{margin:4px 0 5px;font-size:var(--ui-text-section)}.mail-head p{margin:0;color:#727d8f;font-size:var(--ui-text-helper);line-height:1.55}.mail-icon{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;color:#4b61ba;background:#eef2ff;font-weight:var(--ui-weight-strong)}.kicker{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.1em;color:#6676c7}.status{display:inline-flex;align-items:center;justify-content:center;min-height:26px;padding:4px 9px;border-radius:999px;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);white-space:nowrap}.status.ok{color:#14735d;background:#e8f8f3}.status.pending{color:#8a6417;background:#fff5dd}.status.danger{color:#a43b3b;background:#fff0f0}
.setup-steps{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.setup-steps span{display:inline-flex;align-items:center;gap:5px;padding:6px 9px;border-radius:999px;background:#f2f5fb;color:#667085;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.setup-steps b{display:inline-grid;place-items:center;width:17px;height:17px;border-radius:50%;background:#536bd7;color:#fff;font-size:var(--ui-text-caption)}
.mail-form{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px;padding:16px;border:1px solid #e5e8ef;border-radius:13px;background:rgba(255,255,255,.92)}.mail-form label{display:grid;gap:6px;min-width:0}.mail-form label span{font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong);color:#586478}.mail-form input,.mail-form select{width:100%;min-height:42px;box-sizing:border-box;padding:0 11px;border:1px solid #cfd7e4;border-radius:10px;background:#fff;color:#172033;font:inherit}.provider-field{grid-column:1}.provider-note{grid-column:2;display:grid;align-content:center;gap:3px;padding:10px 12px;border-radius:10px;font-size:var(--ui-text-helper);line-height:1.5}.provider-note.info{background:#f2f7ff;color:#53627a}.provider-note.warning{background:#fff4e5;color:#8b5b16}.provider-note b{font-size:var(--ui-text-helper)}.provider-help-link{width:max-content;margin-top:3px;color:#4054ad;font-weight:var(--ui-weight-strong);text-decoration:none}.provider-help-link:hover{text-decoration:underline}.identity-field,.credential-field{grid-column:span 1}.credential-field{grid-column:1/-1}.advanced{grid-column:1/-1;border:1px solid #e2e6ef;border-radius:11px;background:#fafbfe}.advanced summary{padding:11px 12px;cursor:pointer;font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong);color:#5f6b80}.advanced-grid{display:grid;grid-template-columns:2fr 110px 150px 130px;gap:10px;padding:0 12px 12px}.mail-actions{grid-column:1/-1;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-top:4px}.mail-actions span{max-width:760px;color:#8791a2;font-size:var(--ui-text-helper);line-height:1.5}.primary-button,.secondary-button{font-size:var(--ui-text-body);min-height:40px;padding:0 14px;border-radius:10px;font-weight:var(--ui-weight-strong);cursor:pointer}.primary-button{border:0;background:#536bd7;color:#fff}.secondary-button{border:1px solid #aeb9df;background:#fff;color:#4054ad}.primary-button:disabled,.secondary-button:disabled{opacity:.5;cursor:not-allowed}.dirty-note{grid-column:1/-1;margin:0;color:#8a6417;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.message{margin:10px 0 0;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.message.success{color:#14735d}.message.error{color:#a43b3b}.gate-note{display:grid;gap:3px;margin-top:12px;padding:12px;border-radius:11px;background:#fff8e8}.gate-note b{font-size:var(--ui-text-body);color:#785818}.gate-note span{font-size:var(--ui-text-helper);line-height:1.55;color:#8b7040}
@media (max-width:900px){.advanced-grid{grid-template-columns:1fr 1fr}.provider-field,.provider-note{grid-column:1/-1}}
@media (max-width:640px){.mail-head,.mail-form,.advanced-grid{grid-template-columns:1fr}.mail-head .status{justify-self:start}.identity-field,.credential-field,.provider-field,.provider-note,.advanced,.mail-actions,.dirty-note{grid-column:1}}
</style>