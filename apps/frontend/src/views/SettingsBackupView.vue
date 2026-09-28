<script setup>
import { computed, onMounted, ref } from 'vue'
import { exportProductBackup } from '../api/productBackup.js'
import { getRuntimeHealth } from '../api/runtimeStatus.js'
import { requestOnboardingReplay } from '../onboarding.js'
import DeepSeekConnectionSettingsCard from '../components/DeepSeekConnectionSettingsCard.vue'
import MailReadinessCard from '../components/MailReadinessCard.vue'
import MailTransportSettingsCard from '../components/MailTransportSettingsCard.vue'
import RecipientSettingsCard from '../components/RecipientSettingsCard.vue'

const props = defineProps({ projectId: { type: Number, default: null } })
const runtimeState = ref('idle')
const runtimeMessage = ref('')
const runtimeCheckedAt = ref('')
const backupState = ref('idle')
const backupMessage = ref('')
const runtimeHealthy = computed(() => runtimeState.value === 'healthy')

function formatTime(value) {
  if (!value) return '尚未检查'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

async function checkRuntime() {
  if (runtimeState.value === 'loading') return
  runtimeState.value = 'loading'; runtimeMessage.value = ''
  try {
    const { ok, body } = await getRuntimeHealth()
    runtimeCheckedAt.value = new Date().toISOString()
    if (ok && body?.status === 'ok') {
      runtimeState.value = 'healthy'; runtimeMessage.value = '本地服务运行正常'; return
    }
    runtimeState.value = 'failed'; runtimeMessage.value = body?.detail?.message || '本地服务状态无法确认'
  } catch {
    runtimeCheckedAt.value = new Date().toISOString()
    runtimeState.value = 'failed'; runtimeMessage.value = '无法连接本地服务'
  }
}

async function downloadBackup() {
  if (backupState.value === 'loading') return
  backupState.value = 'loading'; backupMessage.value = ''
  try {
    const result = await exportProductBackup()
    if (!result.ok || !(result.blob instanceof Blob)) {
      backupState.value = 'failed'; backupMessage.value = result.body?.detail?.message || '备份导出失败，当前数据没有被修改。'; return
    }
    const url = URL.createObjectURL(result.blob)
    try {
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = result.filename || 'AnxinBoard-backup.zip'; anchor.style.display = 'none'
      document.body.appendChild(anchor); anchor.click(); anchor.remove()
    } finally { URL.revokeObjectURL(url) }
    backupState.value = 'saved'; backupMessage.value = '备份包已导出。请把 ZIP 保存到你能找到的安全位置。'
  } catch {
    backupState.value = 'failed'; backupMessage.value = '备份导出失败，请确认本地服务正在运行。'
  }
}

function replayGuide() { requestOnboardingReplay() }
function goBack() { if (history.length > 1) history.back(); else window.location.hash = '#/projects' }
onMounted(checkRuntime)
</script>

<template>
  <section class="settings-page" aria-label="设置">
    <header class="page-header">
      <div>
        <span class="eyebrow">设置</span>
        <h1>模型、邮件与备份设置</h1>
        <p>模型必须先保存凭据、真实测试连接并选择当前可用模型；“Key 已保存”不再冒充“模型已接通”。邮件和正式发送准入仍独立控制。</p>
      </div>
      <button class="back-button" type="button" @click="goBack">← 返回刚才页面</button>
    </header>

    <DeepSeekConnectionSettingsCard />

    <MailTransportSettingsCard />
    <RecipientSettingsCard :project-id="props.projectId" />
    <MailReadinessCard :project-id="props.projectId" />

    <section class="availability-card" aria-labelledby="backup-title">
      <div class="availability-head">
        <div><span class="setting-kicker">本机数据</span><h2 id="backup-title">备份与恢复</h2><p>备份可以在软件运行时安全导出；恢复会替换本机项目数据库，所以必须先完全关闭安心看板，再从 Windows 的“安心看板 - 恢复备份”入口执行。</p></div>
      </div>
      <div class="availability-list">
        <div class="availability-row backup-action-row">
          <div class="setting-icon data">↓</div>
          <div><b>导出备份</b><span>只导出安心看板本机 SQLite 项目数据，不导出 AI Key、SMTP 密码或其它 Windows Credential Manager 凭据。</span></div>
          <button class="secondary-button" type="button" :disabled="backupState === 'loading'" @click="downloadBackup">{{ backupState === 'loading' ? '导出中…' : '导出备份 ZIP' }}</button>
        </div>
        <div class="availability-row">
          <div class="setting-icon data">↻</div>
          <div><b>恢复备份</b><span>先正常关闭安心看板，再打开 Windows 开始菜单中的“安心看板 - 恢复备份”。恢复工具会校验 ZIP、安装运行时、SQLite 完整性与历史模型/邮件发送记录；可能导致系统忘记已发生外部发送的旧备份会被拒绝。</span></div>
          <span class="status ok">离线可用</span>
        </div>
      </div>
      <p v-if="backupMessage" :class="['result-message', backupState === 'failed' ? 'error' : 'success']">{{ backupMessage }}</p>
    </section>

    <section class="guide-card" aria-labelledby="guide-title">
      <div><span class="setting-kicker">使用帮助</span><h2 id="guide-title">首次使用引导</h2><p>需要时可以重新查看 5 步引导。完成状态只保存在当前浏览器，不上传、不写入项目数据。</p></div>
      <button class="secondary-button" type="button" @click="replayGuide">重新查看 5 步引导</button>
    </section>

    <details class="advanced-card">
      <summary>高级信息与故障排查</summary>
      <div class="advanced-content">
        <div class="advanced-row">
          <div><b>本地服务</b><span>{{ runtimeMessage || '正在检查…' }}</span><small>最近检查：{{ formatTime(runtimeCheckedAt) }}</small></div>
          <span :class="['status', runtimeHealthy ? 'ok' : runtimeState === 'failed' ? 'danger' : 'neutral']">{{ runtimeHealthy ? '正常' : runtimeState === 'failed' ? '异常' : '检查中' }}</span>
          <button class="secondary-button" type="button" :disabled="runtimeState === 'loading'" @click="checkRuntime">重新检查</button>
        </div>
        <div class="advanced-note"><b>技术说明</b><p>AI Key、模型选择和 SMTP 密码保存在 Windows Credential Manager；页面不回显旧凭据。模型连接测试只访问 DeepSeek /models，不发送 PRD、代码或报告。</p></div>
      </div>
    </details>
  </section>
</template>

<style scoped>
.settings-page{display:grid;gap:16px;color:#172033}.page-header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}.page-header h1{margin:5px 0 7px;font-size:var(--ui-text-page);letter-spacing:-.02em;font-weight:var(--ui-weight-strong);line-height:1.4}.page-header p{max-width:820px;margin:0;color:#667085;line-height:1.65}.eyebrow,.setting-kicker{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.1em;color:#6676c7}.availability-card,.guide-card{padding:17px;border:1px solid #e3e7ee;border-radius:15px;background:#fff}.availability-head{margin-bottom:10px}.availability-head h2,.guide-card h2{margin:4px 0 5px;font-size:var(--ui-text-section);font-weight:var(--ui-weight-strong);line-height:1.4}.availability-head p,.guide-card p{margin:0;color:#727d8f;font-size:var(--ui-text-helper);line-height:1.55}.availability-list{display:grid;gap:8px}.availability-row{display:grid;grid-template-columns:42px minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px;border-radius:11px;background:#f8fafc}.availability-row>div:nth-child(2){display:grid;gap:3px}.availability-row b{font-size:var(--ui-text-body)}.availability-row span:not(.status){color:#7a8597;font-size:var(--ui-text-caption);line-height:1.55}.setting-icon{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;font-weight:var(--ui-weight-strong)}.setting-icon.data{color:#16735e;background:#e9f8f3}.status{align-self:start;display:inline-flex;align-items:center;justify-content:center;min-height:26px;padding:4px 9px;border-radius:999px;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);white-space:nowrap}.status.ok{color:#14735d;background:#e8f8f3}.status.danger{color:#a43b3b;background:#fff0f0}.status.neutral{color:#6f7888;background:#f1f3f6}.secondary-button{min-height:38px;padding:0 13px;border:1px solid #d6dce6;border-radius:10px;background:#fff;color:#45516a;font-weight:var(--ui-weight-strong);cursor:pointer}.secondary-button:disabled{opacity:.5;cursor:not-allowed}.result-message{margin:10px 0 0;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.result-message.success{color:#14735d}.result-message.error{color:#a43b3b}.guide-card{display:flex;justify-content:space-between;align-items:center;gap:18px;background:linear-gradient(145deg,#fff,#fafbff)}.advanced-card{border:1px solid #e1e5eb;border-radius:14px;background:#fff}.advanced-card summary{padding:15px 17px;cursor:pointer;color:#526079;font-size:var(--ui-text-helper);font-weight:var(--ui-weight-strong)}.advanced-content{padding:0 17px 17px;display:grid;gap:12px}.advanced-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:12px;align-items:center;padding:14px;border-radius:11px;background:#f8fafc}.advanced-row>div{display:grid;gap:3px}.advanced-row b{font-size:var(--ui-text-body)}.advanced-row span,.advanced-row small{font-size:var(--ui-text-caption);color:#7a8597}.advanced-note{padding:13px;border:1px dashed #d9dfe8;border-radius:11px}.advanced-note b{font-size:var(--ui-text-body)}.advanced-note p{margin:5px 0 0;color:#7a8597;font-size:var(--ui-text-caption);line-height:1.65}@media(max-width:760px){.page-header{display:grid}.availability-row,.advanced-row{grid-template-columns:1fr}.guide-card{display:grid}}
</style>
