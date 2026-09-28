<script setup>
import { computed, onMounted, watch } from 'vue'
import { formatTime } from '../../utils/projectFormat.js'
import { useGitBaselinePanel } from './useGitBaselinePanel.js'
import { lineageStatusText, lineageStatusClass } from './baselineStatusDisplay.js'

const props = defineProps({
  projectId: { type: [Number, String], default: null },
  savedGitUrl: { type: String, default: '' },
  savedBranch: { type: String, default: '' }
})

const projectIdRef = computed(() => props.projectId)

const panel = useGitBaselinePanel({
  projectId: projectIdRef,
  savedGitUrl: computed(() => props.savedGitUrl),
  savedBranch: computed(() => props.savedBranch)
})

const statusText = computed(() => lineageStatusText(panel.lineageStatus.value))
const statusClass = computed(() => lineageStatusClass(panel.lineageStatus.value))
const continuityText = computed(() => {
  const candidate = panel.rangeCandidate.value
  if (!candidate) return '—'
  const map = {
    no_new_commit: '无新提交',
    continuous: '存在连续新提交',
    checkpoint_unreachable: '基线不可达',
    capacity_exceeded: '容量超限'
  }
  return map[candidate.continuity] || candidate.continuity
})
const capacityText = computed(() => {
  const candidate = panel.rangeCandidate.value
  if (!candidate) return '—'
  return candidate.capacity === 'capacity_exceeded' ? '超限' : '正常'
})

function shortHead(value) {
  return value ? value.slice(0, 8) : '—'
}

function reloadPanel() {
  panel.reload()
}

defineExpose({ reload: reloadPanel })

onMounted(() => {
  panel.active()
})

watch(
  () => props.projectId,
  () => panel.active()
)
</script>

<template>
  <section class="panel-card git-baseline-card" aria-labelledby="git-baseline-title">
    <div class="card-heading">
      <div>
        <span class="section-kicker">Git 基线与提交范围</span>
        <h2 id="git-baseline-title">本次分析范围</h2>
        <p>刷新提交范围，核对本次分析的起点和终点后再确认；当前版本不会生成日报。</p>
      </div>
      <span :class="['status-badge', statusClass]">{{ statusText }}</span>
    </div>

    <dl class="lineage-meta-grid">
      <div><dt>本次分析起点</dt><dd><code>{{ shortHead(panel.rangeCandidate.value ? panel.rangeCandidate.value.baseline_commit : panel.lineage.value && panel.lineage.value.baseline_commit) }}</code></dd></div>
      <div><dt>基线分支</dt><dd>{{ (panel.lineage.value && panel.lineage.value.branch) || '—' }}</dd></div>
      <div><dt>建立时间</dt><dd>{{ panel.lineage.value && panel.lineage.value.created_at ? formatTime(panel.lineage.value.created_at) : '—' }}</dd></div>
      <div><dt>本次分析终点</dt><dd><code>{{ panel.rangeCandidate.value ? shortHead(panel.rangeCandidate.value.remote_head) : '—' }}</code></dd></div>
      <div><dt>提交数量</dt><dd>{{ panel.rangeCandidate.value ? panel.rangeCandidate.value.commit_count : '—' }}</dd></div>
      <div><dt>变更文件</dt><dd>{{ panel.rangeCandidate.value ? panel.rangeCandidate.value.changed_file_count : '—' }}</dd></div>
      <div><dt>新增 / 删除</dt><dd>{{ panel.rangeCandidate.value ? `${panel.rangeCandidate.value.added_lines} / ${panel.rangeCandidate.value.deleted_lines}` : '—' }}</dd></div>
      <div><dt>连续状态</dt><dd>{{ continuityText }}</dd></div>
      <div><dt>容量状态</dt><dd>{{ capacityText }}</dd></div>
    </dl>

    <p v-if="!panel.gitConnected.value" class="message info" role="status">
      请先测试 Git 连接并确认连接成功，再建立初始基线。
    </p>
    <p v-else-if="!panel.configConsistent.value" class="message info" role="status">
      Git 配置与成功连接记录不一致，请重新测试连接。
    </p>
    <p class="message success" role="status">建立初始基线不会生成日报，也不会推送或修改远端。</p>
    <p v-if="panel.message.value" :class="['message', panel.messageType.value]" role="status">
      <span>{{ panel.message.value }}</span>
      <span v-if="panel.confirmedRange.value">
        起点 <code>{{ shortHead(panel.confirmedRange.value.fromCommit) }}</code>，
        终点 <code>{{ shortHead(panel.confirmedRange.value.toCommit) }}</code>。
      </span>
    </p>

    <div class="action-row">
      <button
        class="primary-button"
        type="button"
        :disabled="panel.baselineDisabled.value"
        @click="panel.establishBaseline()"
      >
        {{ panel.creating.value ? '建立中...' : '建立初始基线' }}
      </button>
      <button
        class="secondary-button"
        type="button"
        :disabled="panel.refreshDisabled.value"
        @click="panel.refreshRange()"
      >
        {{ panel.refreshing.value ? '刷新中...' : '刷新提交范围' }}
      </button>
      <button
        class="primary-button"
        type="button"
        :disabled="panel.confirmDisabled.value"
        @click="panel.confirmRange()"
      >
        {{ panel.confirming.value ? '确认中...' : '确认本次分析范围' }}
      </button>
    </div>
  </section>
</template>
