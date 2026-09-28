import { computed, ref, watch } from 'vue'
import { getGitStatus } from '../../api/git.js'
import {
  createAnalysisLineage,
  getAnalysisLineage,
  refreshRangeCandidate
} from '../../api/analysisLineages.js'
import { confirmGitSnapshot } from '../../api/gitSnapshots.js'

const RANGE_CHANGED_CODES = new Set([
  'ANALYSIS_LINEAGE_MISMATCH',
  'GIT_SNAPSHOT_FROM_COMMIT_STALE',
  'GIT_SNAPSHOT_TO_COMMIT_STALE',
  'GIT_SNAPSHOT_CONFIG_STALE',
  'GIT_SNAPSHOT_EMPTY_RANGE',
  'GIT_SNAPSHOT_CHECKPOINT_UNREACHABLE',
  'GIT_SNAPSHOT_CAPACITY_EXCEEDED'
])

export function useGitBaselinePanel({ projectId, savedGitUrl, savedBranch, api: extApi }) {
  const api = extApi || {
    getGitStatus: getGitStatus,
    getAnalysisLineage: getAnalysisLineage,
    createAnalysisLineage: createAnalysisLineage,
    refreshRangeCandidate: refreshRangeCandidate,
    confirmGitSnapshot: confirmGitSnapshot
  }
  let projectGeneration = 0

  const loading = ref(false)
  const creating = ref(false)
  const refreshing = ref(false)
  const confirming = ref(false)
  const lineage = ref(null)
  const lineageError = ref(false)
  const noLineage = ref(false)
  const rangeCandidate = ref(null)
  const gitConnected = ref(false)
  const configConsistent = ref(true)
  const message = ref('')
  const messageType = ref('')
  const confirmedRange = ref(null)

  const working = computed(
    () => loading.value || creating.value || refreshing.value || confirming.value
  )

  const lineageStatus = computed(() => {
    if (loading.value) return 'loading'
    if (lineageError.value) return 'error'
    if (lineage.value) return 'active'
    if (noLineage.value) return 'no_lineage'
    return 'error'
  })

  watch([savedGitUrl, savedBranch], () => {
    resetState()
    if (projectId.value) {
      loadPanel()
    }
  })

  function clearMessage() {
    message.value = ''
    messageType.value = ''
  }

  function showMessage(text, type) {
    message.value = text
    messageType.value = type
  }

  function resetState() {
    projectGeneration += 1
    lineage.value = null
    lineageError.value = false
    noLineage.value = false
    rangeCandidate.value = null
    gitConnected.value = false
    configConsistent.value = true
    loading.value = false
    creating.value = false
    refreshing.value = false
    confirming.value = false
    message.value = ''
    messageType.value = ''
    confirmedRange.value = null
  }

  async function loadStatus(gen, pid) {
    const result = await api.getGitStatus(pid)
    if (gen !== projectGeneration) return null
    if (result.ok && result.body) {
      return {
        connected: result.body.status === 'connected',
        consistent: !(result.body.status === 'not_tested')
      }
    }
    return { connected: false, consistent: true }
  }

  async function loadPanel() {
    const projectIdValue = projectId.value
    if (!projectIdValue) return
    const gen = projectGeneration
    const pid = projectIdValue
    clearMessage()
    loading.value = true
    try {
      const status = await loadStatus(gen, pid)
      if (status === null) return
      gitConnected.value = status.connected
      configConsistent.value = status.consistent

      const result = await api.getAnalysisLineage(pid)
      if (gen !== projectGeneration) return
      if (result.ok && result.body && result.body.status === 'active' && result.body.lineage) {
        lineage.value = result.body.lineage
        lineageError.value = false
        noLineage.value = false
      } else if (result.ok && result.body && result.body.status === 'no_lineage') {
        lineage.value = null
        lineageError.value = false
        noLineage.value = true
      } else if (result.status === 404) {
        lineage.value = null
        lineageError.value = true
        noLineage.value = false
        rangeCandidate.value = null
        showMessage('项目不存在或已被删除。', 'error')
      } else if (result.status === 409) {
        lineage.value = null
        lineageError.value = true
        noLineage.value = false
        rangeCandidate.value = null
        showMessage((result.body && result.body.detail && result.body.detail.message) || '当前状态不允许读取基线。', 'error')
      } else if (!result.ok) {
        lineage.value = null
        lineageError.value = true
        noLineage.value = false
        rangeCandidate.value = null
        showMessage((result.body && result.body.detail && result.body.detail.message) || '读取基线状态失败，请稍后重试。', 'error')
      } else {
        lineage.value = null
        lineageError.value = true
        noLineage.value = false
        rangeCandidate.value = null
        showMessage('读取基线状态失败，响应格式不正确。', 'error')
      }
    } catch {
      if (gen !== projectGeneration) return
      lineage.value = null
      lineageError.value = true
      noLineage.value = false
      rangeCandidate.value = null
      showMessage('读取 Git 基线状态失败，请稍后重试。', 'error')
    } finally {
      if (gen === projectGeneration) {
        loading.value = false
      }
    }
  }

  async function establishBaseline() {
    const projectIdValue = projectId.value
    if (creating.value) return
    const gen = projectGeneration
    const pid = projectIdValue
    creating.value = true
    clearMessage()
    try {
      const result = await api.createAnalysisLineage(pid)
      if (gen !== projectGeneration) return
      if (result.ok && result.body) {
        lineage.value = result.body.lineage
        lineageError.value = false
        noLineage.value = false
        if (result.body.created === false) {
          showMessage('已存在有效的初始基线，未重复创建。', 'info')
        } else {
          showMessage('初始基线已建立。', 'success')
        }
      } else if (result.status === 409) {
        const detail = result.body && result.body.detail
        showMessage((detail && detail.message) || '当前状态不允许建立基线。', 'error')
      } else if (result.status === 404) {
        showMessage('项目不存在或已被删除。', 'error')
      } else {
        showMessage((result.body && result.body.detail && result.body.detail.message) || '建立基线失败，请稍后重试。', 'error')
      }
    } catch {
      if (gen !== projectGeneration) return
      showMessage('网络请求失败，无法确认基线是否建立。', 'error')
    } finally {
      if (gen === projectGeneration) {
        creating.value = false
      }
    }
  }

  async function refreshRange() {
    const projectIdValue = projectId.value
    if (refreshing.value) return
    const gen = projectGeneration
    const pid = projectIdValue
    refreshing.value = true
    confirmedRange.value = null
    clearMessage()
    try {
      const result = await api.refreshRangeCandidate(pid)
      if (gen !== projectGeneration) return
      if (result.ok && result.body) {
        rangeCandidate.value = result.body.candidate
        const status = result.body.status
        if (status === 'no_new_commit') {
          showMessage('自基线以来没有新提交。', 'info')
        } else if (status === 'continuous') {
          showMessage('存在连续新提交，范围候选已刷新。', 'success')
        } else if (status === 'capacity_exceeded') {
          showMessage('变更范围超过容量限制，未改变基线。', 'warning')
        } else if (status === 'branch_changed') {
          showMessage('活动分支已改变，请先重新测试连接并核对基线。', 'error')
        } else if (status === 'checkpoint_unreachable') {
          showMessage('基线已不在当前历史中，需要人工处理。', 'error')
        } else if (status === 'no_lineage') {
          showMessage('还没有建立初始基线。', 'info')
        } else if (status === 'git_operation_in_progress') {
          showMessage('已有其他 Git 工作区操作正在进行，请稍后刷新。', 'warning')
        } else if (status === 'git_failed') {
          const detail = result.body
          showMessage((detail && detail.error_summary) || 'Git 操作失败，请稍后重试。', 'error')
        }
      } else {
        showMessage((result.body && result.body.detail && result.body.detail.message) || '刷新提交范围失败，请稍后重试。', 'error')
      }
    } catch {
      if (gen !== projectGeneration) return
      showMessage('网络请求失败，无法刷新提交范围。', 'error')
    } finally {
      if (gen === projectGeneration) {
        refreshing.value = false
      }
    }
  }

  async function confirmRange() {
    const projectIdValue = projectId.value
    const lineageAtClick = lineage.value
    const candidateAtClick = rangeCandidate.value
    if (
      working.value ||
      !projectIdValue ||
      !lineageAtClick ||
      !candidateAtClick ||
      candidateAtClick.continuity !== 'continuous' ||
      candidateAtClick.capacity === 'capacity_exceeded' ||
      !lineageAtClick.id ||
      !candidateAtClick.baseline_commit ||
      !candidateAtClick.remote_head
    ) {
      return
    }

    const payload = {
      expected_lineage_id: lineageAtClick.id,
      expected_from_commit: candidateAtClick.baseline_commit,
      expected_to_commit: candidateAtClick.remote_head
    }
    const gen = projectGeneration
    const pid = projectIdValue
    confirming.value = true
    confirmedRange.value = null
    clearMessage()
    try {
      const result = await api.confirmGitSnapshot(pid, payload)
      if (gen !== projectGeneration) return
      if (result.ok && result.body) {
        confirmedRange.value = {
          fromCommit: payload.expected_from_commit,
          toCommit: payload.expected_to_commit
        }
        if (result.body.created === false) {
          showMessage('该分析范围已经确认。', 'success')
        } else {
          showMessage('本次分析范围已确认。', 'success')
        }
      } else {
        const detail = result.body && result.body.detail
        if (detail && RANGE_CHANGED_CODES.has(detail.code)) {
          showMessage('当前提交范围已经发生变化，请重新刷新提交范围后再确认。', 'warning')
        } else {
          showMessage((detail && detail.message) || '确认本次分析范围失败，请稍后重试。', 'error')
        }
      }
    } catch {
      if (gen !== projectGeneration) return
      showMessage('网络请求失败，无法确认本次分析范围。', 'error')
    } finally {
      if (gen === projectGeneration) {
        confirming.value = false
      }
    }
  }

  async function active() {
    const projectIdValue = projectId.value
    if (!projectIdValue) return
    resetState()
    await loadPanel()
  }

  async function reload() {
    resetState()
    await loadPanel()
  }

  const baselineDisabled = computed(
    () => working.value ||
      lineageStatus.value === 'loading' ||
      lineageStatus.value === 'error' ||
      !gitConnected.value ||
      !configConsistent.value
  )

  const refreshDisabled = computed(
    () => working.value ||
      lineageStatus.value === 'loading' ||
      lineageStatus.value === 'error' ||
      lineageStatus.value !== 'active' ||
      !gitConnected.value ||
      !configConsistent.value
  )

  const confirmDisabled = computed(() => {
    const candidate = rangeCandidate.value
    return working.value ||
      lineageStatus.value !== 'active' ||
      !lineage.value ||
      !lineage.value.id ||
      !candidate ||
      candidate.continuity !== 'continuous' ||
      candidate.capacity === 'capacity_exceeded' ||
      !candidate.baseline_commit ||
      !candidate.remote_head ||
      !gitConnected.value ||
      !configConsistent.value
  })

  return {
    loading,
    creating,
    refreshing,
    confirming,
    working,
    lineageStatus,
    lineage,
    lineageError,
    noLineage,
    rangeCandidate,
    gitConnected,
    configConsistent,
    message,
    messageType,
    confirmedRange,
    active,
    reload,
    establishBaseline,
    refreshRange,
    confirmRange,
    hasValidProject: computed(() => !!projectId.value && Number(projectId.value) > 0),
    baselineDisabled,
    refreshDisabled,
    confirmDisabled
  }
}
