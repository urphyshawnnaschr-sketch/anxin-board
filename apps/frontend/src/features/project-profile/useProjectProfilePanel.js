import { computed, ref, watch, getCurrentScope, onScopeDispose } from 'vue'
import {
  createEmptyProfileForm,
  createEmptyProfileContent,
  profileContentToForm,
  profileFormToContent,
  canonicalizeProfileContent,
  cloneProfileValue,
  createProfileClientId
} from './projectProfileForm.js'

export function useProjectProfilePanel({ projectId, api }) {
  let projectGeneration = 0
  let disposed = false
  const confirmationPending = ref(null)
  if (getCurrentScope()) onScopeDispose(() => { disposed = true; projectGeneration++ })

  const panelState = ref('loading')
  const viewMode = ref('readonly')
  const serverProfile = ref(null)
  const history = ref([])
  const activeConfirmed = ref(null)
  const currentCandidate = ref(null)
  const historyViewing = ref(null)
  const message = ref('')
  const messageType = ref('')
  const saving = ref(false)
  const confirming = ref(false)
  const creating = ref(false)
  const conflict = ref(false)
  const conflictCurrent = ref(null)
  const missingList = ref([])
  const collapsedModules = ref([])
  const collapsedTerms = ref([])

  const form = ref(createEmptyProfileForm())
  const savedSnapshot = ref('')

  const hasUnsaved = computed(() => {
    if (!savedSnapshot.value) return false
    return canonicalizeProfileContent(profileFormToContent(form.value)) !== savedSnapshot.value
  })

  const busy = computed(() => saving.value || confirming.value || !!confirmationPending.value)
  const formLocked = computed(() => viewMode.value === 'editing' && busy.value)

  const confirmDisabled = computed(() => {
    if (!serverProfile.value) return true
    if (serverProfile.value.status !== 'candidate') return true
    if (saving.value || confirming.value || confirmationPending.value) return true
    if (hasUnsaved.value) return true
    if (typeof serverProfile.value.edit_version !== 'number') return true
    return false
  })

  function showMessage(text, type) {
    message.value = text
    messageType.value = type
  }

  function clearMessage() {
    message.value = ''
    messageType.value = ''
  }

  async function fetchProfile(profileId) {
    try {
      const { status, ok, body } = await api.getProjectProfile(profileId)
      if (status === 404) {
        return { ok: false, errorType: 'not_found', message: '项目档案版本不存在或已被删除' }
      }
      if (!ok) {
        const detail = body && body.detail
        return {
          ok: false,
          errorType: 'http',
          message: (detail && detail.message) || '读取项目档案版本失败'
        }
      }
      if (
        !body ||
        typeof body.id === 'undefined' ||
        typeof body.status !== 'string' ||
        typeof body.edit_version === 'undefined' ||
        !body.content
      ) {
        return { ok: false, errorType: 'format', message: '读取响应格式不正确' }
      }
      return { ok: true, profile: body }
    } catch {
      return { ok: false, errorType: 'network', message: '网络请求失败，请检查连接后重试' }
    }
  }

  function enterEditing(profile) {
    serverProfile.value = profile
    viewMode.value = 'editing'
    conflict.value = false
    conflictCurrent.value = null
    missingList.value = []
    form.value = profileContentToForm(profile.content)
    savedSnapshot.value = canonicalizeProfileContent(profile.content)
    collapsedModules.value = form.value.modules.map((_, index) => index > 0)
    collapsedTerms.value = form.value.domain_glossary.map(() => true)
    panelState.value = 'ready'
  }

  function enterReadOnly(profile) {
    serverProfile.value = profile
    viewMode.value = 'readonly'
    conflict.value = false
    conflictCurrent.value = null
    missingList.value = []
    panelState.value = 'ready'
  }

    async function loadPanel(options = {}) {
    if (!projectId.value || disposed) return
    if (confirmationPending.value) { await reconcileConfirmation(); return }
    const gen = projectGeneration
    const pid = projectId.value
    if (!options.keepMessage) clearMessage()
    panelState.value = 'loading'
    historyViewing.value = null
    try {
      const { ok, body } = await api.listProjectProfiles(pid)
      if (gen !== projectGeneration || pid !== projectId.value) return
      if (!ok || !Array.isArray(body)) {
        panelState.value = 'failed'
        return
      }
      history.value = body
      const candidate = body.find((item) => item.status === 'candidate') || null
      const confirmed = body.find((item) => item.status === 'confirmed') || null
      currentCandidate.value = candidate
      activeConfirmed.value = confirmed
      if (candidate) {
        const result = await fetchProfile(candidate.id)
        if (gen !== projectGeneration || pid !== projectId.value) return
        if (result.ok) {
          currentCandidate.value = result.profile
          enterEditing(result.profile)
        } else if (gen === projectGeneration && pid === projectId.value) {
          showMessage(result.message, 'error')
          panelState.value = 'failed'
        }
      } else if (confirmed) {
        const result = await fetchProfile(confirmed.id)
        if (gen !== projectGeneration || pid !== projectId.value) return
        if (result.ok) {
          activeConfirmed.value = result.profile
          enterReadOnly(result.profile)
        } else if (gen === projectGeneration && pid === projectId.value) {
          showMessage(result.message, 'error')
          panelState.value = 'failed'
        }
      } else if (body.length > 0) {
        const result = await fetchProfile(body[0].id)
        if (gen !== projectGeneration || pid !== projectId.value) return
        if (result.ok) {
          enterReadOnly(result.profile)
        } else if (gen === projectGeneration && pid === projectId.value) {
          showMessage(result.message, 'error')
          panelState.value = 'failed'
        }
      } else {
        serverProfile.value = null
        viewMode.value = 'readonly'
        panelState.value = 'empty'
      }
    } catch {
      if (gen === projectGeneration && pid === projectId.value) panelState.value = 'failed'
    }
  }

  async function handleCreate(content) {
    if (creating.value) return
    creating.value = true
    clearMessage()
    const gen = projectGeneration
    const pid = projectId.value
    try {
      const { status, ok, body } = await api.createProfileCandidate(pid, content)
      if (gen !== projectGeneration || pid !== projectId.value) return
      if (status === 409) {
        const code = body.detail && body.detail.code
        if (code === 'PRD_PARSE_NOT_CONFIRMED') {
          showMessage('请先在上方确认 PRD，再创建项目档案候选', 'error')
          return
        }
        showMessage('创建失败，原数据未改变', 'error')
        return
      }
      if (status === 404) {
        showMessage('项目或版本不存在', 'error')
        return
      }
      if (!ok) {
        let errorMessage = '创建失败，输入格式不正确'
        if (status === 400 || status === 422) {
          errorMessage = (body.detail && body.detail.message) || errorMessage
        }
        showMessage(errorMessage, 'error')
        return
      }
      if (
        !body ||
        typeof body.id === 'undefined' ||
        typeof body.status !== 'string' ||
        typeof body.edit_version === 'undefined' ||
        !body.content
      ) {
        showMessage('创建响应格式不正确，未进入编辑状态', 'error')
        return
      }
      await loadPanel()
    } catch {
      if (gen === projectGeneration && pid === projectId.value) showMessage('创建失败，原数据未改变', 'error')
    } finally {
      if (gen === projectGeneration && pid === projectId.value) creating.value = false
    }
  }

  function createBlank() {
    handleCreate(createEmptyProfileContent())
  }

  function createFromConfirmed() {
    if (!activeConfirmed.value) return
    handleCreate(cloneProfileValue(activeConfirmed.value.content))
  }

  function applyServerProfile(profile) {
    serverProfile.value = profile
    form.value = profileContentToForm(profile.content)
    savedSnapshot.value = canonicalizeProfileContent(profile.content)
    conflict.value = false
    collapsedModules.value = form.value.modules.map((_, index) => index > 0)
    collapsedTerms.value = form.value.domain_glossary.map(() => true)
  }

  async function saveCandidate() {
    if (saving.value || confirming.value || confirmationPending.value || disposed) return
    if (!serverProfile.value || viewMode.value !== 'editing') return
    if (serverProfile.value.status !== 'candidate') return
    saving.value = true
    clearMessage()
    const gen = projectGeneration
    const pid = projectId.value
    const profileId = serverProfile.value.id
    const editVersion = serverProfile.value.edit_version
    const content = profileFormToContent(form.value)
    try {
      const { status, ok, body } = await api.updateProfileCandidate(profileId, editVersion, content)
      if (gen !== projectGeneration || pid !== projectId.value) return
      if (status === 409) {
        const code = body.detail && body.detail.code
        if (code === 'PROJECT_PROFILE_VERSION_CONFLICT') {
          conflict.value = true
          conflictCurrent.value = body.detail.current
          showMessage('候选已被其他操作更新，请重新加载最新内容', 'error')
          return
        }
        if (code === 'PROJECT_PROFILE_INVALID_STATE') {
          showMessage('候选状态已变化，请重新加载历史', 'error')
          await loadPanel({ keepMessage: true })
          return
        }
        showMessage('保存失败，原数据未改变', 'error')
        return
      }
      if (!ok) {
        let errorMessage = '保存失败，原数据未改变'
        if (status === 422) {
          errorMessage = '提交内容格式不正确'
        } else if (body.detail && body.detail.message) {
          errorMessage = body.detail.message
        }
        showMessage(errorMessage, 'error')
        return
      }
      if (typeof body.changed === 'boolean' && body.profile && body.profile.content) {
        applyServerProfile(body.profile)
        if (body.changed === true) {
          showMessage('保存成功', 'success')
        } else {
          showMessage('内容未变化', 'info')
        }
      } else {
        showMessage('保存响应格式不正确，未覆盖本地内容', 'error')
      }
    } catch {
      if (gen === projectGeneration && pid === projectId.value) showMessage('保存失败，原数据未改变', 'error')
    } finally {
      if (gen === projectGeneration && pid === projectId.value) saving.value = false
    }
  }

  async function reloadServerVersion() {
    if (confirmationPending.value) { await reconcileConfirmation(); return }
    if (!serverProfile.value) return
    const gen = projectGeneration
    const pid = projectId.value
    if (conflictCurrent.value && conflictCurrent.value.content) {
      applyServerProfile(conflictCurrent.value)
      conflictCurrent.value = null
      showMessage('已重新加载服务器最新版本', 'info')
      return
    }
    const targetId = conflictCurrent.value && conflictCurrent.value.id
      ? conflictCurrent.value.id
      : serverProfile.value.id
    try {
      const result = await fetchProfile(targetId)
      if (gen !== projectGeneration || pid !== projectId.value) return
      if (result.ok) {
        conflictCurrent.value = null
        applyServerProfile(result.profile)
        showMessage('已重新加载服务器最新版本', 'info')
      } else if (gen === projectGeneration && pid === projectId.value) {
        showMessage(result.message, 'error')
      }
    } catch {
      if (gen === projectGeneration && pid === projectId.value) showMessage('重新加载服务器版本失败，请检查连接后重试', 'error')
    }
  }

  async function reconcileConfirmation() {
    const pending = confirmationPending.value
    if (!pending || disposed) return
    const gen = projectGeneration, pid = projectId.value
    const result = await fetchProfile(pending.id)
    if (disposed || gen !== projectGeneration || pid !== projectId.value || confirmationPending.value !== pending) return
    const profile = result.profile
    if (result.ok && profile.id === pending.id && String(profile.project_id) === String(pid) &&
        typeof pending.content_hash === 'string' && /^[a-f0-9]{64}$/.test(pending.content_hash) &&
        profile.content_hash === pending.content_hash && profile.status === 'confirmed' &&
        Number.isInteger(profile.edit_version) && profile.edit_version > pending.edit_version) {
      confirmationPending.value = null
      currentCandidate.value = null
      activeConfirmed.value = profile
      enterReadOnly(profile)
      showMessage('已核实：项目档案已确认生效。没有重复发送确认请求。', 'success')
      return
    }
    conflict.value = true
    conflictCurrent.value = null
    showMessage('确认结果尚未核实，已暂停再次确认。请重新加载服务器版本核对；不会自动重复提交。', 'error')
  }

  async function confirmCandidate() {
    if (confirming.value || saving.value || confirmationPending.value || disposed) return
    if (!serverProfile.value || viewMode.value !== 'editing') return
    if (serverProfile.value.status !== 'candidate') return
    if (hasUnsaved.value) {
      showMessage('请先保存修改，再确认项目档案', 'error')
      return
    }
    confirming.value = true
    clearMessage()
    const gen = projectGeneration
    const pid = projectId.value
    const profileId = serverProfile.value.id
    const editVersion = serverProfile.value.edit_version
    const contentHash = serverProfile.value.content_hash
    async function reconcileUnknown() {
      if (disposed || gen !== projectGeneration || pid !== projectId.value) return
      confirmationPending.value = { id: profileId, edit_version: editVersion, content_hash: contentHash }
      await reconcileConfirmation()
    }
    try {
      const { status, ok, body } = await api.confirmProfileCandidate(profileId, editVersion, 'local')
      if (gen !== projectGeneration || pid !== projectId.value) return
      if (status === 409) {
        const code = body.detail && body.detail.code
        if (code === 'PROJECT_PROFILE_INCOMPLETE') {
          missingList.value = (body.detail && body.detail.missing) || []
          showMessage('项目档案信息不完整，请补充完整后再继续', 'error')
          return
        }
        if (code === 'PROJECT_PROFILE_SOURCE_PRD_STALE') {
          showMessage('PRD 已更新，请重新创建项目档案候选', 'error')
          return
        }
        if (code === 'PROJECT_PROFILE_VERSION_CONFLICT') {
          conflict.value = true
          conflictCurrent.value = body.detail.current
          showMessage('候选已被其他操作更新，请重新加载最新内容', 'error')
          return
        }
        if (code === 'PROJECT_PROFILE_INVALID_STATE') {
          showMessage('候选状态已变化，请重新加载历史', 'error')
          await loadPanel({ keepMessage: true })
          return
        }
        showMessage('确认失败，原状态未改变', 'error')
        return
      }
      if (!ok) {
        if (!status || status >= 500) { await reconcileUnknown(); return }
        let errorMessage = '确认失败，原状态未改变'
        if (body.detail && body.detail.message) {
          errorMessage = body.detail.message
        }
        showMessage(errorMessage, 'error')
        return
      }
      if (body.status !== 'confirmed' || typeof body.id === 'undefined' || typeof body.edit_version === 'undefined') {
        await reconcileUnknown()
        return
      }
      showMessage('项目档案已确认生效', 'success')
      await loadPanel({ keepMessage: true })
    } catch {
      await reconcileUnknown()
    } finally {
      if (gen === projectGeneration && pid === projectId.value) confirming.value = false
    }
  }

  async function viewHistory(item) {
    clearMessage()
    const gen = projectGeneration
    const pid = projectId.value
    try {
      const result = await fetchProfile(item.id)
      if (gen !== projectGeneration || pid !== projectId.value) return
      if (result.ok) {
        historyViewing.value = result.profile
      } else if (gen === projectGeneration && pid === projectId.value) {
        showMessage(result.message || '加载历史版本失败', 'error')
      }
    } catch {
      if (gen === projectGeneration && pid === projectId.value) showMessage('加载历史版本失败，请检查连接后重试', 'error')
    }
  }

  function returnToCurrent() {
    historyViewing.value = null
    clearMessage()
  }

  function addModule() {
    form.value.modules.push({
      client_id: createProfileClientId(),
      name: '',
      description: '',
      prd_refs_text: '',
      requirements_text: '',
      paths: [],
      exclusions_text: ''
    })
    collapsedModules.value.push(false)
  }

  function removeModule(index) {
    form.value.modules.splice(index, 1)
    collapsedModules.value.splice(index, 1)
  }

  function toggleModule(index) {
    collapsedModules.value[index] = !collapsedModules.value[index]
  }

  function addPath(module) {
    module.paths.push({ type: 'frontend', pattern: '', required: true, note: '' })
  }

  function removePath(module, index) {
    module.paths.splice(index, 1)
  }

  function addTerm() {
    form.value.domain_glossary.push({ term: '', definition: '', aliases_text: '' })
    collapsedTerms.value.push(true)
  }

  function removeTerm(index) {
    form.value.domain_glossary.splice(index, 1)
    collapsedTerms.value.splice(index, 1)
  }

  function toggleTerm(index) {
    collapsedTerms.value[index] = !collapsedTerms.value[index]
  }

  function resetState() {
    confirmationPending.value = null
    panelState.value = 'loading'
    viewMode.value = 'readonly'
    serverProfile.value = null
    history.value = []
    activeConfirmed.value = null
    currentCandidate.value = null
    historyViewing.value = null
    message.value = ''
    messageType.value = ''
    saving.value = false
    confirming.value = false
    creating.value = false
    conflict.value = false
    conflictCurrent.value = null
    missingList.value = []
    collapsedModules.value = []
    collapsedTerms.value = []
    form.value = createEmptyProfileForm()
    savedSnapshot.value = ''
  }

  watch(
    () => projectId.value,
    (newId, oldId) => {
      if (newId === oldId) return
      if (newId != null && oldId != null && String(newId) === String(oldId)) return
      projectGeneration++
      resetState()
      if (newId != null) loadPanel()
    }
  )

  return {
    panelState,
    viewMode,
    serverProfile,
    history,
    activeConfirmed,
    currentCandidate,
    historyViewing,
    message,
    messageType,
    saving,
    confirming,
    creating,
    conflict,
    missingList,
    collapsedModules,
    collapsedTerms,
    form,
    hasUnsaved,
    formLocked,
    confirmDisabled,
    clearMessage,
    loadPanel,
    createBlank,
    createFromConfirmed,
    saveCandidate,
    reloadServerVersion,
    confirmCandidate,
    viewHistory,
    returnToCurrent,
    addModule,
    removeModule,
    toggleModule,
    addPath,
    removePath,
    addTerm,
    removeTerm,
    toggleTerm
  }
}
