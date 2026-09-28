<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getMailReadiness } from '../api/mailReadiness.js'

const props = defineProps({
  projectId: { type: Number, default: null }
})

const state = ref('idle')
const readiness = ref(null)
const message = ref('')

const badgeText = computed(() => {
  if (state.value === 'loading') return '检查中'
  if (state.value === 'failed') return '无法确认'
  if (readiness.value?.candidate_ready) return '可以发送'
  if (readiness.value?.state === 'send_confirmation_required') return '等待发送确认'
  return '尚未就绪'
})

const badgeClass = computed(() => readiness.value?.send_action_available ? 'ok' : state.value === 'failed' ? 'danger' : 'pending')

async function refresh() {
  if (!props.projectId || state.value === 'loading') {
    if (!props.projectId) {
      state.value = 'idle'
      readiness.value = null
      message.value = '请先从具体项目进入设置，才能检查该项目的邮件准备状态。'
    }
    return
  }
  state.value = 'loading'
  message.value = ''
  try {
    const { ok, body } = await getMailReadiness(props.projectId)
    if (!ok) {
      state.value = 'failed'
      readiness.value = null
      message.value = body?.detail?.message || '邮件准备状态无法确认。'
      return
    }
    readiness.value = body
    state.value = 'loaded'
  } catch {
    state.value = 'failed'
    readiness.value = null
    message.value = '无法连接本地服务。'
  }
}

watch(() => props.projectId, refresh)
onMounted(refresh)
</script>

<template>
  <section class="readiness-card" aria-labelledby="mail-readiness-title">
    <div class="card-head">
      <div class="icon">✓</div>
      <div>
        <span class="kicker">发送前检查</span>
        <h2 id="mail-readiness-title">邮件发送准备状态</h2>
        <p>这里只检查已保存的正式报告、收件人和 SMTP 配置是否能安全进入发送流程；不会读取 SMTP 密码，也不会发送邮件。</p>
      </div>
      <span :class="['status', badgeClass]">{{ badgeText }}</span>
    </div>

    <p v-if="message" class="message">{{ message }}</p>

    <div v-if="readiness" class="checks">
      <div v-for="item in readiness.checks" :key="item.code" class="check-row">
        <span :class="['dot', item.ready ? 'ready' : 'blocked']">{{ item.ready ? '✓' : '·' }}</span>
        <span>{{ item.summary }}</span>
      </div>
    </div>

    <div v-if="readiness?.state === 'send_confirmation_required'" class="candidate-note">
      <b>可以进入发送确认</b>
      <span>正式报告、SMTP 配置和当前收件人已经具备发送前提。到安心看板页点击“发送邮件”并确认后，系统才会冻结这份报告与当前收件人的绑定并提交邮件。</span>
    </div>

    <div v-else-if="readiness?.candidate_ready" class="candidate-note">
      <b>发送身份已闭合</b>
      <span>这份正式报告已经与当前收件人、SMTP 配置和邮件正文形成同一份不可变发送身份；实际发送仍需在安心看板页显式点击并确认。</span>
    </div>

    <div class="actions">
      <button type="button" class="secondary-button" :disabled="state === 'loading' || !props.projectId" @click="refresh">
        {{ state === 'loading' ? '检查中…' : '重新检查' }}
      </button>
      <small>正式发送入口在安心看板页；这里仅展示准备状态，不会产生外部邮件。</small>
    </div>
  </section>
</template>

<style scoped>
.readiness-card{padding:20px;border:1px solid #e3e7ee;border-radius:17px;background:#fff;box-shadow:0 10px 28px rgba(29,41,57,.04)}.card-head{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:13px;align-items:start}.icon{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#eef8f4;color:#17725d;font-weight:900}.kicker{font-size:11px;font-weight:900;letter-spacing:.1em;color:#6676c7}.card-head h2{margin:4px 0 5px;font-size:18px}.card-head p{margin:0;color:#727d8f;font-size:12px;line-height:1.55}.status{display:inline-flex;align-items:center;justify-content:center;min-height:26px;padding:4px 9px;border-radius:999px;font-size:10px;font-weight:900;white-space:nowrap}.status.ok{color:#14735d;background:#e8f8f3}.status.pending{color:#8a6417;background:#fff5dd}.status.danger{color:#a43b3b;background:#fff0f0}.checks{display:grid;gap:7px;margin-top:16px}.check-row{display:grid;grid-template-columns:24px minmax(0,1fr);gap:8px;align-items:center;padding:10px 12px;border-radius:10px;background:#f8fafc;color:#596579;font-size:11px;line-height:1.5}.dot{width:22px;height:22px;display:grid;place-items:center;border-radius:50%;font-weight:900}.dot.ready{background:#e8f8f3;color:#14735d}.dot.blocked{background:#fff5dd;color:#8a6417}.candidate-note{display:grid;gap:4px;margin-top:12px;padding:12px;border:1px solid #dceee8;border-radius:10px;background:#f5fcf9}.candidate-note b{font-size:11px;color:#146b58}.candidate-note span{font-size:10px;line-height:1.55;color:#6f7d79}.actions{display:flex;align-items:center;gap:12px;margin-top:14px}.secondary-button{min-height:38px;padding:0 13px;border:1px solid #d6dce6;border-radius:10px;background:#fff;color:#45516a;font-weight:800;cursor:pointer}.secondary-button:disabled{opacity:.5;cursor:not-allowed}.actions small,.message{color:#8791a2;font-size:10px;line-height:1.55}.message{margin:14px 0 0}@media(max-width:760px){.card-head{grid-template-columns:1fr}.status{justify-self:start}.actions{align-items:flex-start;flex-direction:column}}
</style>
