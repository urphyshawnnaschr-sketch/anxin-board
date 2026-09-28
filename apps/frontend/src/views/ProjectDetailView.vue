<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getProject, updateProject } from '../api/projects.js'
import { listPrdVersions } from '../api/prd.js'
import { getProjectProfile, getProjectStateBaselineStatus, listProjectProfiles } from '../api/projectProfiles.js'
import { projectBaselineNextAction } from '../features/project-profile/projectBaselineNextAction.js'
import { formatTime, statusLabel } from '../utils/projectFormat.js'
import GitConnectionPanel from './GitConnectionPanel.vue'
import PrdPanel from './PrdPanel.vue'
import ProjectProfilePanel from './ProjectProfilePanel.vue'

const props = defineProps({
  projectId: { type: [Number, String], default: null },
  section: { type: String, default: 'home' }
})

const detailState = ref('loading')
const detail = ref(null)
const editName = ref('')
const editGitUrl = ref('')
const editBranch = ref('')
const saving = ref(false)
const conflict = ref(false)
const message = ref('')
const messageType = ref('')
const summaryState = ref('loading')
const activePrd = ref(null)
const activeProfile = ref(null)
const baselineStatus = ref(null)
const summaryAvailability = ref({ prd: false, profile: false, baseline: false })
let detailGeneration = 0
let summaryGeneration = 0
let disposed = false

function sameProject(id) {
  return !disposed && String(id) === String(props.projectId)
}

function clearSummary() {
  summaryState.value = 'loading'
  activePrd.value = null
  activeProfile.value = null
  baselineStatus.value = null
  summaryAvailability.value = { prd: false, profile: false, baseline: false }
}

const currentSection = computed(() => ['home', 'setup', 'modules'].includes(props.section) ? props.section : 'home')
const projectConfigured = computed(() => Boolean(detail.value && detail.value.git_url && detail.value.branch))
const setupReady = computed(() => projectConfigured.value && activePrd.value)
const moduleReady = computed(() => Boolean(activeProfile.value))
const nextAction = computed(() => projectBaselineNextAction({
  setupReady: setupReady.value,
  moduleReady: moduleReady.value,
  baselineStatus: baselineStatus.value?.status || 'unknown',
  baselineProfileId: baselineStatus.value?.profile_id,
  confirmedProfile: activeProfile.value
}))
const nextSection = computed(() => nextAction.value.section)
const nextActionLabel = computed(() => nextAction.value.label)


// These labels describe loaded facts only; project management status is separate.
const homePreparation = computed(() => {
  const loading = summaryState.value === 'loading'
  const unavailable = loading ? '读取中' : '暂不可用'
  const baselineLabels = {
    established: '已确认', candidate_pending: '候选待确认',
    missing: '尚无完整结果', upgrade_required: '需要升级', unknown: '暂不可用'
  }
  return [
    { key: 'repository', title: '代码仓库', status: projectConfigured.value ? '已配置' : '待补充',
      ready: projectConfigured.value, description: detail.value?.git_url
        ? [detail.value.git_url, detail.value.branch ? '分支：' + detail.value.branch : '尚未设置分支'].join(' · ')
        : '尚未填写仓库地址和研发分支。' },
    { key: 'prd', title: '需求文档', status: !summaryAvailability.value.prd ? unavailable : activePrd.value ? '已确认' : '待确认',
      ready: summaryAvailability.value.prd && Boolean(activePrd.value), description: !summaryAvailability.value.prd
        ? loading ? '正在读取需求文档。' : '无法确定当前需求文档的确认状态。'
        : activePrd.value ? '当前已确认版本：V' + activePrd.value.version_no : '尚无已确认的 PRD，请在项目资料中上传并确认。' },
    { key: 'profile', title: '功能模块', status: !summaryAvailability.value.profile ? unavailable : activeProfile.value ? '已确认' : '待确认',
      ready: summaryAvailability.value.profile && Boolean(activeProfile.value), description: !summaryAvailability.value.profile
        ? loading ? '正在读取功能模块。' : '无法确定当前功能模块的确认状态。'
        : activeProfile.value ? '当前已确认版本：V' + activeProfile.value.version_no : '尚无已确认的功能模块。' },
    { key: 'baseline', title: '当前代码分析', status: !summaryAvailability.value.baseline ? unavailable : nextAction.value.key === 'full-analysis' ? '全部待核实' : baselineLabels[baselineStatus.value?.status] || '暂不可用',
      ready: summaryAvailability.value.baseline && baselineStatus.value?.status === 'established' && nextAction.value.key !== 'full-analysis', description: loading
        ? '正在读取代码分析状态。' : !summaryAvailability.value.baseline || !baselineLabels[baselineStatus.value?.status] || baselineStatus.value?.status === 'unknown'
          ? '暂时无法确定当前代码的分析状态。'
          : nextAction.value.key === 'full-analysis' ? '历史记录已确认，但所有模块的实现情况仍待核实。'
          : baselineStatus.value?.status === 'established' ? '当前实现结果已确认，可继续选择本轮研发范围。'
          : baselineStatus.value?.status === 'candidate_pending' ? '已有分析结果，等待你检查并确认。'
          : baselineStatus.value?.status === 'upgrade_required' ? '请先更新功能模块，再分析当前代码。'
          : '尚无完整代码分析结果；完成分析后需要你检查并确认。' }
  ]
})
const homeAction = computed(() => {
  if (summaryState.value === 'loading') return { title: '正在核对准备情况', description: '正在读取需求、功能模块和代码分析状态。', label: '读取中…', section: null, disabled: true }
  if (summaryState.value === 'failed' || !summaryAvailability.value.prd || !summaryAvailability.value.profile || !summaryAvailability.value.baseline) {
    return { title: '准备情况暂不可用', description: '部分资料未能可靠读取，暂时无法判断下一步。请重新读取后继续。', label: '重新读取准备情况', section: null }
  }
  if (setupReady.value && moduleReady.value && !['established', 'candidate_pending', 'missing', 'upgrade_required'].includes(baselineStatus.value?.status)) {
    return { title: '查看代码分析状态', description: '需求与模块已确认，请先查看已有代码分析记录。', label: '查看分析记录 →', section: 'modules' }
  }
  const analysisCopy = {
    missing: { title: '了解当前实现情况', description: '对照需求检查当前代码，结果需由你确认。', label: '分析当前代码 →' },
    candidate_pending: { title: '检查实现结果', description: '查看代码分析结果，确认无误后继续研发分析。', label: '检查实现结果 →' },
    upgrade_required: { title: '更新功能模块', description: '更新当前功能模块后，再检查代码的实现情况。', label: '更新功能模块 →' }
  }
  return nextAction.value.key === 'baseline'
    ? { ...nextAction.value, ...analysisCopy[baselineStatus.value?.status] }
    : nextAction.value
})

function showMessage(text, type) { message.value = text; messageType.value = type }
function clearMessage() { message.value = ''; messageType.value = '' }

async function loadSummary(id) {
  if (!sameProject(id)) return
  const generation = ++summaryGeneration
  const isCurrent = () => sameProject(id) && generation === summaryGeneration
  clearSummary()
  try {
    const [prdResult, profileResult, baselineResult] = await Promise.all([
      listPrdVersions(id),
      listProjectProfiles(id),
      getProjectStateBaselineStatus(id)
    ])
    if (!isCurrent()) return
    const availability = {
      prd: prdResult.ok && Array.isArray(prdResult.body),
      profile: profileResult.ok && Array.isArray(profileResult.body),
      baseline: Boolean(baselineResult.ok && baselineResult.body?.status)
    }
    const confirmed = availability.profile
      ? profileResult.body.find(item => item.status === 'confirmed') || null
      : null
    let resolvedProfile = null
    if (confirmed) {
      // Lists contain metadata only; bind the detail to that exact confirmed record.
      const metadataValid = confirmed.id != null && String(confirmed.project_id) === String(id)
        && typeof confirmed.content_hash === 'string' && /^[0-9a-f]{64}$/.test(confirmed.content_hash)
      const result = metadataValid ? await getProjectProfile(confirmed.id) : null
      if (!isCurrent()) return
      const profile = result?.body
      const detailMatches = result?.ok && profile?.id != null
        && String(profile.id) === String(confirmed.id) && String(profile.project_id) === String(id)
        && profile.status === 'confirmed' && profile.content_hash === confirmed.content_hash
        && profile.content && typeof profile.content === 'object' && !Array.isArray(profile.content)
        && typeof profile.content.schema_version === 'string'
      if (detailMatches) {
        // Keep mapping identity/status only, without source evidence or profile text.
        resolvedProfile = { id: confirmed.id, project_id: confirmed.project_id, status: confirmed.status,
          version_no: confirmed.version_no, content_hash: confirmed.content_hash,
          content: {
            schema_version: profile.content.schema_version,
            planned_modules: Array.isArray(profile.content.planned_modules)
              ? profile.content.planned_modules.map(module => ({ client_id: module?.client_id })) : null,
            implementation_mappings: Array.isArray(profile.content.implementation_mappings)
              ? profile.content.implementation_mappings.map(mapping => ({ planned_module_id: mapping?.planned_module_id, status: mapping?.status })) : null
          }
        }
      } else {
        availability.profile = false
        availability.baseline = false
      }
    }
    if (!isCurrent()) return
    summaryAvailability.value = availability
    activePrd.value = availability.prd ? prdResult.body.find(item => item.status === 'parse_confirmed') || null : null
    activeProfile.value = resolvedProfile
    baselineStatus.value = availability.baseline ? baselineResult.body : { status: 'unknown', has_confirmed_baseline: false }
    summaryState.value = 'loaded'
  } catch {
    if (!isCurrent()) return
    summaryState.value = 'failed'
  }
}

async function loadDetail(id) {
  const generation = ++detailGeneration
  const isCurrent = () => sameProject(id) && generation === detailGeneration
  ++summaryGeneration
  clearSummary()
  saving.value = false
  detailState.value = 'loading'
  detail.value = null
  conflict.value = false
  clearMessage()
  try {
    const { status, ok, body } = await getProject(id)
    if (!isCurrent()) return
    if (status === 404) {
      detailState.value = 'not-found'
      showMessage('项目不存在或已被删除', 'error')
      return
    }
    if (!ok) throw new Error('加载失败')
    detail.value = body
    editName.value = body.name
    editGitUrl.value = body.git_url || ''
    editBranch.value = body.branch || ''
    detailState.value = 'loaded'
    await loadSummary(id)
  } catch {
    if (!isCurrent()) return
    detailState.value = 'failed'
    showMessage('加载项目详情失败', 'error')
  }
}

async function saveProject() {
  if (saving.value || !detail.value) return
  const id = props.projectId
  const generation = detailGeneration
  const isCurrent = () => sameProject(id) && generation === detailGeneration
  saving.value = true
  clearMessage()
  try {
    const { status, ok, body } = await updateProject(id, {
      name: editName.value,
      git_url: editGitUrl.value,
      branch: editBranch.value,
      version: detail.value.version
    })
    if (!isCurrent()) return
    if (status === 409) {
      conflict.value = true
      if (body.detail && body.detail.current) detail.value = body.detail.current
      showMessage('项目已在其他操作中更新，请重新加载最新内容', 'error')
      return
    }
    if (!ok) {
      showMessage(status === 422 ? '提交内容格式不正确' : (body.detail && body.detail.message) || '保存失败，原数据未改变', 'error')
      return
    }
    if (typeof body.changed !== 'boolean' || !body.project) throw new Error('保存响应格式不正确')
    detail.value = body.project
    editName.value = body.project.name
    editGitUrl.value = body.project.git_url || ''
    editBranch.value = body.project.branch || ''
    showMessage(body.changed ? '项目配置已保存' : '内容未变化', body.changed ? 'success' : 'info')
    await loadSummary(id)
  } catch (error) {
    if (!isCurrent()) return
    showMessage(error.message || '保存失败，原数据未改变', 'error')
  } finally {
    if (isCurrent()) saving.value = false
  }
}

function reloadLatest() { loadDetail(props.projectId) }
function goList() { window.location.hash = '#/projects' }
function goSection(section) {
  if (!props.projectId) return
  const suffix = section === 'home' ? '' : `/${section}`
  window.location.hash = `#/projects/${props.projectId}${suffix}`
}

onMounted(() => { if (props.projectId != null) loadDetail(props.projectId) })
onUnmounted(() => { disposed = true; ++detailGeneration; ++summaryGeneration })
watch(() => props.projectId, (newId) => { if (newId != null) loadDetail(newId) })
</script>

<template>
  <section class="page-stack lane-page" :class="{ 'home-overview': currentSection === 'home' }" aria-label="项目操作内容">
    <div class="lane-context-row">
      <button class="back-button" type="button" @click="goList"><span aria-hidden="true">←</span> 返回项目列表</button>
      <span v-if="detailState === 'loaded' && detail" class="lane-project-name">{{ detail.name }}</span>
    </div>

    <section v-if="detailState === 'loading'" class="panel-card lane-state-card">
      <div class="lane-spinner" aria-hidden="true"></div>
      <b>正在加载项目...</b>
    </section>

    <template v-else-if="detailState === 'loaded' && detail">
      <template v-if="currentSection === 'home'">
        <div class="lane-page-head">
          <div><h2>项目工作台</h2></div>
          <button class="secondary-button" type="button" @click="goSection('setup')">项目资料</button>
        </div>

        <section class="home-next-action" aria-label="推荐下一步">
          <div class="home-next-copy">
            <span class="home-section-label">下一步</span>
            <h3>{{ homeAction.title }}</h3>
            <p>{{ homeAction.description }}</p>
          </div>
          <button class="primary-button" type="button" :disabled="homeAction.disabled"
            @click="homeAction.section ? goSection(homeAction.section) : reloadLatest()">
            {{ homeAction.label }}
          </button>
        </section>

        <section class="home-preparation" aria-label="项目准备情况">
          <div class="home-preparation-head">
            <h3>准备情况</h3>
            <span>以已保存、已确认的资料为准</span>
          </div>
          <ul class="home-readiness-list">
            <li v-for="item in homePreparation" :key="item.key" class="home-readiness-row">
              <div class="home-readiness-title">
                <strong>{{ item.title }}</strong>
                <span :class="['home-readiness-status', { ready: item.ready }]">{{ item.status }}</span>
              </div>
              <p>{{ item.description }}</p>
            </li>
          </ul>
          <dl class="home-project-meta">
            <div><dt>管理状态</dt><dd>{{ statusLabel(detail.status) }}</dd></div>
            <div><dt>资料版本</dt><dd>{{ detail.version != null ? `V${detail.version}` : '暂无记录' }}</dd></div>
            <div><dt>最近更新</dt><dd>{{ detail.updated_at ? formatTime(detail.updated_at) : '暂无记录' }}</dd></div>
          </dl>
        </section>
      </template>

      <template v-else-if="currentSection === 'setup'">
        <div class="lane-page-head">
          <div><h2>准备项目</h2><p>连接代码仓库，上传并确认需求文档。</p></div>
          <div class="lane-actions"><button class="secondary-button" type="button" @click="goSection('home')">返回工作台</button></div>
        </div>
        <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>

        <div class="lane-two-grid lane-setup-grid">
          <form class="panel-card lane-card" @submit.prevent="saveProject">
            <div class="lane-card-head"><div><h3>项目资料</h3><p>一个项目绑定一个代码仓库和一个当前研发分支。</p></div><span class="status-badge status-active">{{ detail.git_url && detail.branch ? '已保存' : '待补充' }}</span></div>
            <div class="lane-form-grid">
              <div class="field"><label for="edit-name">项目名称</label><input id="edit-name" v-model="editName" maxlength="100" /></div>
              <div class="field"><label for="edit-branch">当前研发分支</label><input id="edit-branch" v-model="editBranch" maxlength="255" placeholder="main" /></div>
              <div class="field full"><label for="edit-git-url">代码仓库地址</label><input id="edit-git-url" v-model="editGitUrl" maxlength="500" placeholder="https://... / ssh://... / git@host:path" /><small>保存以后，可以在下方检查仓库是否能正常读取。</small></div>
            </div>
            <div class="lane-actions"><button class="primary-button" type="submit" :disabled="saving">{{ saving ? '保存中...' : '保存项目资料' }}</button><button v-if="conflict" class="secondary-button" type="button" @click="reloadLatest">重新加载最新内容</button></div>
          </form>

          <PrdPanel :project-id="detail.id" @changed="loadSummary(detail.id)" />
        </div>

        <section class="panel-card lane-card">
          <div class="lane-card-head"><div><h3>仓库连接检查</h3><p>确认仓库地址和当前分支能够正常读取。</p></div></div>
          <GitConnectionPanel :project-id="detail.id" :saved-git-url="detail.git_url || ''" :saved-branch="detail.branch || ''" :draft-git-url="editGitUrl" :draft-branch="editBranch" />
        </section>
      </template>

      <template v-else>
        <div class="lane-page-head">
          <div><h2>需求与代码对账</h2><p>逐项查看实现情况与依据，核对后再确认使用。</p></div>
          <button class="secondary-button" type="button" @click="goSection('home')">返回工作台</button>
        </div>
        <ProjectProfilePanel :project-id="detail.id" />
      </template>
    </template>

    <section v-else-if="detailState === 'not-found'" class="panel-card lane-state-card error-state">
      <b>未找到项目</b><span>项目不存在或已被删除。</span><button class="secondary-button" type="button" @click="goList">返回项目列表</button>
    </section>
    <section v-else class="panel-card lane-state-card error-state">
      <b>项目详情加载失败</b><span>当前没有可确认的项目数据。</span><div class="lane-actions"><button class="primary-button" type="button" @click="reloadLatest">重新加载</button><button class="secondary-button" type="button" @click="goList">返回项目列表</button></div>
    </section>
  </section>
</template>

<style scoped>
.lane-page {
  gap: 16px;
}

.lane-context-row,.lane-page-head,.lane-card-head,.lane-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.lane-context-row {
  justify-content: space-between;
}

.lane-project-name {
  font-size: var(--ui-text-helper);
  color: var(--muted);
  font-weight: var(--ui-weight-strong);
}

.lane-page-head,.lane-card-head {
  justify-content: space-between;
  align-items: flex-start;
}

.lane-page-head h2,.lane-card-head h3 {
  margin: 4px 0 5px;
  color: var(--title);
  font-weight: var(--ui-weight-strong);
  line-height: 1.4;
}

.lane-page-head h2 {
  font-size: var(--ui-text-page);
  font-weight: var(--ui-weight-strong);
  line-height: 1.4;
}

.lane-page-head p,.lane-card-head p {
  margin: 0;
  color: var(--muted);
  font-size: var(--ui-text-helper);
}

.lane-card {
  padding: 20px;
}

.lane-two-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.lane-setup-grid {
  align-items: start;
}

.lane-form-grid {
  margin: 16px 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.lane-form-grid .full {
  grid-column: 1/-1;
}

.field small {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: var(--ui-text-caption);
}

.lane-state-card {
  min-height: 240px;
  padding: 26px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  text-align: center;
  color: var(--muted);
}

.lane-state-card b {
  color: var(--title);
}

.error-state {
  color: var(--danger);
}

.lane-spinner {
  width: 26px;
  height: 26px;
  border: 3px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin .8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width:1120px) {
  .lane-two-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width:720px) {
  .lane-page-head,.lane-card-head,.lane-context-row {
    flex-direction: column;
    align-items: flex-start;
  }
  .lane-form-grid {
    grid-template-columns: 1fr;
  }
  .lane-actions {
    flex-wrap: wrap;
  }
}

/* Home overview: one decision surface followed by a factual preparation list. */

.home-overview {
  gap: 16px;
  font-size: var(--ui-text-body);
  line-height: 1.6;
}

.home-overview .lane-project-name {
  font-size: var(--ui-text-helper);
  font-weight: 400;
  overflow-wrap: anywhere;
}

.home-overview .lane-page-head {
  align-items: center;
}

.home-overview .lane-page-head h2 {
  margin: 0;
  font-size: var(--ui-text-page);
  line-height: 1.4;
  font-weight: var(--ui-weight-strong);
}

.home-overview :is(.primary-button, .secondary-button, .back-button) {
  min-height: 38px;
  padding: 9px 14px;
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  line-height: 20px;
  border-radius: 12px;
  box-shadow: none;
}

.home-next-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 20px;
  border: 1px solid var(--border);
  border-left: 1px solid var(--border);
  border-radius: 12px;
  background: #fff;
}

.home-next-copy {
  min-width: 0;
}

.home-section-label {
  color: var(--muted);
  font-size: var(--ui-text-helper);
}

.home-next-action h3 {
  margin: 6px 0 8px;
  color: var(--title);
  font-size: var(--ui-text-section);
  font-weight: var(--ui-weight-strong);
  line-height: 1.4;
}

.home-next-action p {
  margin: 0;
  color: var(--text);
  overflow-wrap: anywhere;
}

.home-next-action > button {
  flex-shrink: 0;
}

.home-preparation {
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #fff;
}

.home-preparation-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
}

.home-preparation-head h3 {
  margin: 0;
  font-size: var(--ui-text-section);
  font-weight: var(--ui-weight-strong);
  line-height: 1.4;
  color: var(--title);
}

.home-preparation-head > span {
  font-size: var(--ui-text-helper);
  color: var(--muted);
}

.home-readiness-list {
  margin: 16px 0 0;
  padding: 0;
  list-style: none;
}

.home-readiness-row {
  display: grid;
  grid-template-columns: minmax(220px, 0.7fr) minmax(0, 1.3fr);
  gap: 20px;
  padding: 16px 0;
  border-top: 1px solid var(--border);
}

.home-readiness-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
}

.home-readiness-title strong {
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.home-readiness-status {
  flex-shrink: 0;
  color: var(--muted);
  font-size: var(--ui-text-helper);
}

.home-readiness-status.ready {
  color: #18745f;
}

.home-readiness-row p {
  margin: 0;
  color: var(--muted);
  font-size: var(--ui-text-helper);
  overflow-wrap: anywhere;
}

.home-project-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 28px;
  margin: 12px 0 0;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: var(--ui-text-helper);
}

.home-project-meta > div {
  display: flex;
  gap: 8px;
}

.home-project-meta dd {
  margin: 0;
  color: var(--text);
}

@media (max-width: 900px) {
  .home-next-action {
    align-items: flex-start;
    flex-direction: column;
    padding: 20px;
    gap: 20px;
    border: 1px solid var(--border);
    border-left: 1px solid var(--border);
    background: #fff;
  }
  .home-readiness-row {
    grid-template-columns: 1fr;
    gap: 20px;
    padding: 16px 0;
    border-top: 1px solid var(--border);
  }
  .home-readiness-title {
    justify-content: flex-start;
  }
}

@media (max-width: 600px) {
  .home-overview .lane-page-head {
    flex-wrap: wrap;
  }
  .home-next-action, .home-preparation {
    padding: 20px;
  }
  .home-preparation-head {
    flex-direction: column;
    gap: 8px;
  }
}
</style>
