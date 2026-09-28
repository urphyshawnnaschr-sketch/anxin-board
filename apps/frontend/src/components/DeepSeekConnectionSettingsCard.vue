<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  configureDeepSeekCredential,
  getDeepSeekStatus,
  selectDeepSeekModel,
  testDeepSeekConnection
} from '../api/modelSettings.js'

const props = defineProps({ compact: { type: Boolean, default: false } })
const keyValue = ref('')
const configured = ref(false)
const connected = ref(false)
const availableModels = ref([])
const selectedModel = ref(null)
const choice = ref('')
const state = ref('idle')
const message = ref('')

const badge = computed(() => {
  if (selectedModel.value) return { text: `已选择 ${selectedModel.value}`, cls: 'ok' }
  if (connected.value) return { text: '连接已测试', cls: 'ok' }
  if (configured.value) return { text: 'Key 已保存·待测试', cls: 'pending' }
  return { text: '未配置', cls: 'neutral' }
})

function setError(body, fallback) {
  state.value = 'failed'
  message.value = body?.detail?.message || fallback
}

async function loadStatus() {
  try {
    const { ok, body } = await getDeepSeekStatus()
    if (!ok) return
    configured.value = body?.configured === true
    selectedModel.value = typeof body?.selected_model === 'string' ? body.selected_model : null
    choice.value = selectedModel.value || ''
  } catch {}
}

async function saveKey() {
  if (!keyValue.value.trim() || state.value === 'saving') return
  state.value = 'saving'; message.value = ''
  try {
    const { ok, body } = await configureDeepSeekCredential(keyValue.value)
    if (!ok) return setError(body, '保存 DeepSeek API Key 失败。')
    keyValue.value = ''
    configured.value = true
    connected.value = false
    selectedModel.value = null
    choice.value = ''
    availableModels.value = []
    state.value = 'saved'
    message.value = 'API Key 已安全保存。下一步请点“测试连接”，软件会读取你账号当前真实可用模型。'
  } catch (error) {
    setError(null, error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? '请从安心看板本地启动器打开页面后再保存。' : '保存失败，请确认本地服务正在运行。')
  }
}

async function testConnection() {
  if (state.value === 'testing') return
  state.value = 'testing'; message.value = ''
  try {
    const { ok, body } = await testDeepSeekConnection()
    if (!ok) return setError(body, 'DeepSeek 连接测试失败。')
    availableModels.value = Array.isArray(body?.available_models) ? body.available_models : []
    connected.value = body?.connected === true
    selectedModel.value = typeof body?.selected_model === 'string' ? body.selected_model : null
    if (!availableModels.value.includes(choice.value)) choice.value = selectedModel.value || availableModels.value[0] || ''
    state.value = 'tested'
    message.value = availableModels.value.length
      ? `连接成功。检测到 ${availableModels.value.length} 个当前可用模型，请选择一个并保存。`
      : '连接成功，但没有检测到可用模型。'
  } catch (error) {
    setError(null, error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? '请从安心看板本地启动器打开页面后再测试。' : '连接测试失败，请确认本地服务正在运行。')
  }
}

async function saveModel() {
  if (!choice.value || state.value === 'selecting') return
  state.value = 'selecting'; message.value = ''
  try {
    const { ok, body } = await selectDeepSeekModel(choice.value)
    if (!ok) return setError(body, '保存模型选择失败。')
    selectedModel.value = body?.selected_model || null
    connected.value = body?.connected === true
    availableModels.value = Array.isArray(body?.available_models) ? body.available_models : availableModels.value
    state.value = 'selected'
    message.value = `已选择 ${selectedModel.value}。现在可以进行真实 AI 生成功能。`
  } catch (error) {
    setError(null, error?.message === 'LOCAL_SESSION_UNAVAILABLE' ? '请从安心看板本地启动器打开页面后再保存模型。' : '保存模型选择失败。')
  }
}

onMounted(loadStatus)
</script>

<template>
  <section :class="['deepseek-card', { compact: props.compact }]">
    <div class="card-head">
      <div><span class="kicker">AI 模型</span><h3>DeepSeek</h3><p>保存 Key 不等于模型已接通。测试连接会调用只读 <code>/models</code>，不会发送 PRD、代码或报告内容。</p></div>
      <span :class="['badge', badge.cls]">{{ badge.text }}</span>
    </div>

    <form class="key-row" @submit.prevent="saveKey">
      <label for="deepseek-live-key">API Key</label>
      <div>
        <input id="deepseek-live-key" v-model="keyValue" type="password" autocomplete="new-password" maxlength="1024" placeholder="粘贴你的 DeepSeek API Key" :disabled="state === 'saving'" />
        <button class="primary" type="submit" :disabled="state === 'saving' || !keyValue.trim()">{{ state === 'saving' ? '保存中…' : configured ? '更新 API Key' : '保存 API Key' }}</button>
      </div>
    </form>

    <div class="test-row">
      <div><b>连接与模型</b><span>测试成功后，以 DeepSeek 当前账号真实返回的模型列表为准。</span></div>
      <button class="secondary" type="button" :disabled="!configured || state === 'testing'" @click="testConnection">{{ state === 'testing' ? '测试中…' : '测试连接' }}</button>
    </div>

    <div v-if="availableModels.length" class="model-row">
      <label for="deepseek-live-model">当前可用模型</label>
      <div>
        <select id="deepseek-live-model" v-model="choice" :disabled="state === 'selecting'">
          <option v-for="model in availableModels" :key="model" :value="model">{{ model }}</option>
        </select>
        <button class="primary" type="button" :disabled="!choice || state === 'selecting'" @click="saveModel">{{ state === 'selecting' ? '保存中…' : '使用此模型' }}</button>
      </div>
    </div>

    <p v-if="message" :class="['result', state === 'failed' ? 'error' : 'success']">{{ message }}</p>
    <small>API Key 只保存在本机 Windows Credential Manager，页面不会读取或回显旧 Key；更换 Key 会清除旧模型选择，要求重新测试。</small>
  </section>
</template>

<style scoped>
.deepseek-card{font-size:var(--ui-text-body);line-height:1.6;padding:18px;border:1px solid #d9e0fb;border-radius:16px;background:linear-gradient(145deg,#fff,#f8f9ff);display:grid;gap:14px}.deepseek-card.compact{padding:14px;border-radius:12px}.card-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.card-head h3{margin:3px 0 5px;font-size:var(--ui-text-section)}.card-head p{margin:0;max-width:760px;color:#727d8f;font-size:var(--ui-text-helper);line-height:1.55}.kicker{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.08em;color:#6676c7}.badge{display:inline-flex;align-items:center;min-height:26px;padding:4px 9px;border-radius:999px;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);white-space:nowrap}.badge.ok{color:#14735d;background:#e8f8f3}.badge.pending{color:#8a6417;background:#fff5dd}.badge.neutral{color:#6f7888;background:#f1f3f6}.key-row,.model-row{display:grid;gap:7px}.key-row label,.model-row label{font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong);color:#586478}.key-row>div,.model-row>div{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:9px}.key-row input,.model-row select{min-height:42px;padding:0 12px;border:1px solid #cfd7e4;border-radius:10px;background:#fff;font:inherit}.test-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px;border-radius:11px;background:#f8fafc}.test-row>div{display:grid;gap:3px}.test-row b{font-size:var(--ui-text-body)}.test-row span{color:#7a8597;font-size:var(--ui-text-helper)}.primary,.secondary{min-height:40px;padding:0 14px;border-radius:10px;font:inherit;font-size:var(--ui-text-body);font-weight:var(--ui-weight-strong);cursor:pointer}.primary{border:0;background:#536bd7;color:#fff}.secondary{border:1px solid #d6dce6;background:#fff;color:#45516a}.primary:disabled,.secondary:disabled{opacity:.5;cursor:not-allowed}.result{margin:0;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.result.success{color:#14735d}.result.error{color:#a43b3b}.deepseek-card>small{color:#8791a2;font-size:var(--ui-text-helper);line-height:1.55}.card-head code{font-family:Consolas,var(--font-ui);font-size:var(--ui-text-helper)}@media(max-width:760px){.card-head{flex-direction:column}.key-row>div,.model-row>div,.test-row{grid-template-columns:1fr}.test-row .secondary{justify-self:start}}
</style>
