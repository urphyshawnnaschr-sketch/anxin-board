<script setup>
import { computed, ref, watch } from 'vue'
import { checkGitConnection, getGitStatus } from '../api/git.js'
import { formatTime } from '../utils/projectFormat.js'

const props = defineProps({
  projectId: { type: [Number, String], required: true },
  savedGitUrl: { type: String, default: '' },
  savedBranch: { type: String, default: '' },
  draftGitUrl: { type: String, default: '' },
  draftBranch: { type: String, default: '' }
})

const emit = defineEmits(['git-connected'])

const state = ref(emptyState())
const loading = ref(false)
const requestInFlight = ref(false)
const panelMessage = ref('')

function emptyState() {
  return {
    status: 'not_tested', checked_branch: null, git_version: null,
    remote_head: null, local_head: null, last_checked_at: null,
    error_code: null, error_summary: null
  }
}

const protocolLabel = computed(() => {
  if (!props.savedGitUrl) return '未配置'
  if (props.savedGitUrl.startsWith('https://')) return 'HTTPS'
  return 'SSH / SCP（当前连接检查不启用）'
})
const configDirty = computed(() => (
  props.draftGitUrl.trim() !== props.savedGitUrl || props.draftBranch.trim() !== props.savedBranch
))
const checking = computed(() => requestInFlight.value || state.value.status === 'testing')
const buttonDisabled = computed(() => loading.value || checking.value || !props.savedGitUrl || configDirty.value)
const visualStatus = computed(() => loading.value ? 'loading' : (state.value.status || 'unknown'))
const statusText = computed(() => ({
  loading: '读取中',
  not_tested: '未测试',
  testing: '检查中',
  connected: '已连接',
  failed: '连接失败',
  unknown: '状态未知'
}[visualStatus.value] || '状态未知'))

function shortHead(value) {
  return value ? value.slice(0, 8) : '—'
}

async function loadStatus() {
  loading.value = true
  panelMessage.value = ''
  state.value = { ...emptyState(), checked_branch: props.savedBranch || null }
  try {
    const result = await getGitStatus(props.projectId)
    if (result.ok && result.body && typeof result.body.status === 'string') {
      state.value = result.body
    } else if (result.status === 404) {
      state.value = emptyState()
      panelMessage.value = '项目不存在或已被删除。'
    } else {
      state.value = { ...emptyState(), status: 'unknown', checked_branch: props.savedBranch || null }
      panelMessage.value = result.body?.detail?.message || '读取 Git 连接状态失败，当前状态未知。'
    }
  } catch {
    state.value = { ...emptyState(), status: 'unknown', checked_branch: props.savedBranch || null }
    panelMessage.value = '无法连接后端服务，当前 Git 状态未知。'
  } finally {
    loading.value = false
  }
}

async function runCheck() {
  if (requestInFlight.value || buttonDisabled.value) return
  requestInFlight.value = true
  panelMessage.value = ''
  state.value = { ...emptyState(), status: 'testing', checked_branch: props.savedBranch }
  try {
    const result = await checkGitConnection(props.projectId)
    if (result.ok && result.body && typeof result.body.status === 'string') {
      state.value = result.body
      if (result.body.status === 'connected') emit('git-connected')
    } else if (result.status === 409 && result.body?.detail?.code === 'GIT_CHECK_IN_PROGRESS') {
      if (result.body.detail.current) state.value = result.body.detail.current
      panelMessage.value = '已有 Git 检查正在进行，本次没有启动第二个检查。'
    } else if (result.status === 409 && result.body?.detail?.code === 'GIT_OPERATION_IN_PROGRESS') {
      if (result.body.detail.current) state.value = result.body.detail.current
      panelMessage.value = '该项目另有 Git 工作区操作正在进行，状态未改变。'
    } else if (result.status === 404) {
      state.value = emptyState()
      panelMessage.value = '项目不存在或已被删除。'
    } else {
      state.value = { ...emptyState(), status: 'failed', checked_branch: props.savedBranch || null }
      panelMessage.value = result.body?.detail?.message || 'Git 检查请求失败，未确认连接成功。'
    }
  } catch {
    state.value = { ...emptyState(), status: 'unknown', checked_branch: props.savedBranch || null }
    panelMessage.value = '无法连接后端服务，未确认 Git 连接成功。'
  } finally {
    requestInFlight.value = false
  }
}

watch(
  () => [props.projectId, props.savedGitUrl, props.savedBranch],
  () => loadStatus(),
  { immediate: true }
)
</script>

<template>
  <section class="panel-card git-connection-card" aria-labelledby="git-connection-title">
    <div class="card-heading git-connection-heading">
      <div>
        <span class="section-kicker">受控工作区</span>
        <h2 id="git-connection-title">Git 连接</h2>
        <p>读取并核对当前项目的 Git 连接事实；连接检查不会推送或修改远端。</p>
      </div>
      <span :class="['status-badge', `git-status-${visualStatus}`]">{{ statusText }}</span>
    </div>

    <dl class="git-status-grid">
      <div><dt>仓库协议</dt><dd>{{ protocolLabel }}</dd></div>
      <div><dt>活动分支</dt><dd>{{ state.checked_branch || savedBranch || '—' }}</dd></div>
      <div><dt>Git 版本</dt><dd>{{ state.git_version || '—' }}</dd></div>
      <div><dt>最近检查</dt><dd>{{ state.last_checked_at ? formatTime(state.last_checked_at) : '—' }}</dd></div>
      <div><dt>远端 HEAD</dt><dd><code>{{ shortHead(state.remote_head) }}</code></dd></div>
      <div><dt>本地 HEAD</dt><dd><code>{{ shortHead(state.local_head) }}</code></dd></div>
    </dl>

    <p v-if="configDirty" class="message info" role="status">请先保存 Git 配置，再测试连接。</p>
    <p v-else-if="!savedGitUrl" class="message info" role="status">请先填写并保存 Git 仓库地址。</p>
    <p v-if="state.status === 'connected'" class="message success" role="status">
      已确认连接到 {{ state.checked_branch || savedBranch || '当前分支' }}；冻结范围前仍会重新读取远端事实。
    </p>
    <p v-if="state.status === 'failed' && state.error_summary" class="message error" role="alert">{{ state.error_summary }}</p>
    <p v-if="panelMessage" class="message error" role="alert">{{ panelMessage }}</p>

    <div class="action-row">
      <button class="primary-button" type="button" :disabled="buttonDisabled" @click="runCheck">
        {{ checking ? '检查中...' : '重新检查 Git 连接' }}
      </button>
    </div>
  </section>
</template>
