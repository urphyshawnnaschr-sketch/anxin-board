<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import GitConnectionPanel from './GitConnectionPanel.vue'
import { getProject } from '../api/projects.js'
import { getGitStatus } from '../api/git.js'
import { getAnalysisLineage, createAnalysisLineage, refreshRangeCandidate } from '../api/analysisLineages.js'
import { confirmGitSnapshot } from '../api/gitSnapshots.js'
import { listPrdVersions } from '../api/prd.js'
import { listProjectProfiles } from '../api/projectProfiles.js'
import { confirmEvidenceSnapshot } from '../api/evidenceSnapshots.js'
import { formatTime } from '../utils/projectFormat.js'

const props = defineProps({ projectId: { type: [Number, String], default: null } })

const project = ref(null)
const gitStatus = ref(null)
const lineage = ref(null)
const candidate = ref(null)
const gitSnapshot = ref(null)
const evidenceSnapshot = ref(null)
const activePrd = ref(null)
const activeProfile = ref(null)
const loading = ref(false)
const refreshing = ref(false)
const freezing = ref(false)
const establishing = ref(false)
const loadState = ref('loading')
const message = ref('')
const messageType = ref('info')

const continuityLabel = computed(() => ({
  continuous: '范围连续',
  no_new_commit: '没有新提交',
  checkpoint_unreachable: '历史发生变化',
  unknown: '状态未知'
}[candidate.value?.continuity] || (candidate.value ? candidate.value.continuity : '未读取')))

const capacityLabel = computed(() => candidate.value?.capacity === 'capacity_exceeded' ? '超过资源硬上限' : candidate.value?.batch_required ? '大范围分析' : candidate.value ? '大小合适' : '未读取')
const canFreeze = computed(() => Boolean(
  candidate.value && lineage.value && activePrd.value && activeProfile.value &&
  candidate.value.continuity === 'continuous' &&
  candidate.value.capacity !== 'capacity_exceeded' &&
  candidate.value.baseline_commit && candidate.value.remote_head && lineage.value.id &&
  !refreshing.value && !freezing.value
))
const commits = computed(() => candidate.value?.commits || gitSnapshot.value?.commits || [])
const fromCommit = computed(() => candidate.value?.baseline_commit || gitSnapshot.value?.from_commit || lineage.value?.baseline_commit || null)
const toCommit = computed(() => candidate.value?.remote_head || gitSnapshot.value?.to_commit || gitStatus.value?.remote_head || null)

function shortHash(value) { return value ? value.slice(0, 8) : '—' }
function bytes(value) {
  if (!Number.isFinite(value)) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}
function showMessage(text, type = 'info') { message.value = text; messageType.value = type }
function detailMessage(result, fallback) { return result?.body?.detail?.message || fallback }
function rememberEvidenceSnapshot(id) {
  try { sessionStorage.setItem(`rd-agent:project:${props.projectId}:evidence-snapshot-id`, String(id)) } catch {}
}

async function loadBaseFacts() {
  if (!props.projectId) return
  loading.value = true
  loadState.value = 'loading'
  message.value = ''
  try {
    const [projectResult, statusResult, lineageResult, prdResult, profileResult] = await Promise.all([
      getProject(props.projectId),
      getGitStatus(props.projectId),
      getAnalysisLineage(props.projectId),
      listPrdVersions(props.projectId),
      listProjectProfiles(props.projectId)
    ])
    if (!projectResult.ok) throw new Error(detailMessage(projectResult, '读取项目信息失败。'))
    project.value = projectResult.body
    gitStatus.value = statusResult.ok ? statusResult.body : null
    if (!lineageResult.ok) throw new Error(detailMessage(lineageResult, '读取分析起点失败。'))
    lineage.value = lineageResult.ok && lineageResult.body?.lineage ? lineageResult.body.lineage : null
    activePrd.value = prdResult.ok && Array.isArray(prdResult.body) ? prdResult.body.find(item => item.status === 'parse_confirmed') || null : null
    activeProfile.value = profileResult.ok && Array.isArray(profileResult.body) ? profileResult.body.find(item => item.status === 'confirmed') || null : null
    loadState.value = 'ready'
  } catch (error) {
    loadState.value = 'failed'
    showMessage(error.message || '当前项目状态读取失败，请稍后重试。', 'error')
  } finally {
    loading.value = false
  }
}

async function establishBaseline() {
  if (!props.projectId || establishing.value || lineage.value) return
  establishing.value = true
  try {
    const result = await createAnalysisLineage(props.projectId)
    if (!result.ok || !result.body?.lineage) {
      showMessage(detailMessage(result, '建立分析起点失败，请检查仓库连接。'), 'error')
      return
    }
    lineage.value = result.body.lineage
    await refreshRange()
  } catch {
    showMessage('分析起点建立结果暂时无法确认，请重新读取状态。', 'error')
  } finally { establishing.value = false }
}

async function refreshRange() {
  if (!props.projectId || refreshing.value) return
  refreshing.value = true
  message.value = ''
  candidate.value = null
  gitSnapshot.value = null
  evidenceSnapshot.value = null
  try {
    const result = await refreshRangeCandidate(props.projectId)
    if (!result.ok) {
      showMessage(detailMessage(result, '读取本次研发范围失败。'), 'error')
      return
    }
    candidate.value = result.body?.candidate || null
    const status = result.body?.status
    if (status === 'continuous') showMessage(candidate.value?.batch_required ? '大范围：完整保留证据，后续按模型预算检查。超预算不会截断发送，请核对后确认范围。' : '已读取本次代码变化，请核对后确认范围。', 'success')
    else if (status === 'no_new_commit') showMessage('当前没有新提交，本轮暂时不需要生成新报告。', 'info')
    else if (status === 'capacity_exceeded') showMessage(`范围超过本地资源硬上限（${(candidate.value?.capacity_reasons || []).map(reason => ({ changed_files_hard_limit: '变化文件数', line_changes_hard_limit: '变化行数', commits_hard_limit: '提交数', diff_bytes_hard_limit: '差异字节数' })[reason] || reason).join('、')}）。文件上限 ${candidate.value?.capacity_limits?.changed_files}，行数上限 ${candidate.value?.capacity_limits?.line_changes}，提交数上限 ${candidate.value?.capacity_limits?.commits}，差异字节上限 ${candidate.value?.capacity_limits?.diff_bytes}。当前范围未获准冻结或发送，请缩小范围。`, 'warning')
    else if (status === 'checkpoint_unreachable') showMessage('代码历史发生变化，暂时不能沿用上次分析位置，请先检查仓库状态。', 'error')
    else if (status === 'git_operation_in_progress') showMessage('这个项目正在读取代码仓库，请稍后再试。', 'warning')
    else if (status === 'git_failed') showMessage(result.body?.error_summary || '代码仓库读取失败。', 'error')
    else if (status === 'no_lineage') showMessage('尚未建立分析起点。请使用本页的“建立分析起点并读取”继续。', 'info')
    else showMessage('当前研发范围暂时无法确认，请稍后重试。', 'warning')
  } catch {
    showMessage('无法连接本地服务，当前研发范围暂时无法确认。', 'error')
  } finally {
    refreshing.value = false
  }
}

async function freezeEvidence() {
  if (!canFreeze.value) return
  freezing.value = true
  message.value = ''
  try {
    const gitResult = await confirmGitSnapshot(props.projectId, {
      expected_lineage_id: lineage.value.id,
      expected_from_commit: candidate.value.baseline_commit,
      expected_to_commit: candidate.value.remote_head
    })
    if (!gitResult.ok || !gitResult.body?.snapshot) {
      showMessage(detailMessage(gitResult, '本次研发范围确认失败，没有继续生成报告。'), gitResult.status === 409 ? 'warning' : 'error')
      return
    }
    gitSnapshot.value = gitResult.body.snapshot

    const evidenceResult = await confirmEvidenceSnapshot(props.projectId, {
      expected_git_snapshot_id: gitSnapshot.value.id,
      expected_lineage_id: lineage.value.id,
      expected_prd_id: activePrd.value.id,
      expected_profile_id: activeProfile.value.id
    })
    if (!evidenceResult.ok || !evidenceResult.body?.snapshot) {
      showMessage(detailMessage(evidenceResult, '研发范围已记录，但完整分析资料还没有确认完成，请先不要继续。'), evidenceResult.status === 409 ? 'warning' : 'error')
      return
    }
    evidenceSnapshot.value = evidenceResult.body.snapshot
    rememberEvidenceSnapshot(evidenceSnapshot.value.id)
    showMessage('本次研发范围已经确认。之后的新提交不会混入这份报告。', 'success')
  } catch {
    showMessage('范围确认结果暂时无法确认。请先刷新当前状态，不要重复操作。', 'error')
  } finally {
    freezing.value = false
  }
}

async function reloadPageFacts() {
  candidate.value = null
  gitSnapshot.value = null
  evidenceSnapshot.value = null
  await loadBaseFacts()
  if (loadState.value === 'ready' && lineage.value && gitStatus.value?.status === 'connected') await refreshRange()
}

function goTask() {
  window.location.hash = `#/projects/${props.projectId}/task`
}

onMounted(reloadPageFacts)
watch(() => props.projectId, reloadPageFacts)
</script>

<template>
  <section class="git-page" aria-labelledby="git-evidence-title">
    <div class="page-head">
      <div>
        <span class="eyebrow">研发分析</span>
        <h1 id="git-evidence-title">确认本次研发范围</h1>
        <p>先确认这次报告要分析哪些真实代码变化。确认以后，新提交不会混入本轮报告，这一页也不会启动 AI。</p>
      </div>
      <div class="actions">
        <button class="secondary" type="button" :disabled="refreshing || loading" @click="reloadPageFacts">{{ refreshing ? '读取中…' : '重新读取' }}</button>
        <button class="primary" type="button" :disabled="!canFreeze" @click="freezeEvidence">{{ freezing ? '确认中…' : '确认本次范围' }}</button>
      </div>
    </div>

    <div v-if="loadState === 'loading'" class="state-card">正在读取代码仓库、PRD 和功能模块…</div>
    <div v-else-if="loadState === 'failed'" class="state-card error">当前项目数据读取失败，请稍后重试。</div>

    <template v-else>
      <section v-if="!lineage" class="state-card" aria-label="建立分析起点">
        <p>首次分析需要先建立起点。将使用已验证仓库的提交 {{ shortHash(gitStatus?.remote_head) }} 作为起点，之后的提交作为本轮变化；这不会调用 AI。</p>
        <button type="button" :disabled="establishing || refreshing || gitStatus?.status !== 'connected'" @click="establishBaseline">{{ establishing ? '建立中…' : '建立分析起点并读取' }}</button>
      </section>
      <section class="range-overview" aria-label="本次研发范围摘要">
        <article><span>本轮提交</span><strong>{{ candidate?.commit_count ?? '—' }}</strong><small>{{ candidate ? '会进入这次报告' : '等待读取' }}</small></article>
        <article><span>变化文件</span><strong>{{ candidate?.statistics_complete === false ? `已读取 ${candidate.changed_file_count}` : candidate?.changed_file_count ?? '—' }}</strong><small>{{ candidate?.statistics_complete === false ? '统计未完整，不代表总量' : candidate ? '真实代码变化' : '等待读取' }}</small></article>
        <article><span>范围状态</span><strong class="text-value">{{ evidenceSnapshot ? '已确认' : continuityLabel }}</strong><small>{{ evidenceSnapshot ? '之后的新提交不会混入' : capacityLabel }}</small></article>
      </section>

      <section class="card readiness-card">
        <div class="card-head">
          <div><h2>确认前检查</h2><p>这三项都满足时，才允许冻结本轮范围。</p></div>
          <span :class="['pill', canFreeze ? 'ok' : 'neutral']">{{ evidenceSnapshot ? '已确认' : canFreeze ? '可以确认' : '还不能确认' }}</span>
        </div>
        <div class="readiness-list">
          <div><i :class="gitStatus?.status === 'connected' ? 'ok' : ''"></i><span><b>代码仓库</b><small>{{ gitStatus?.status === 'connected' ? `已连接 · ${lineage?.branch || project?.branch || '当前分支'}` : '连接状态未确认' }}</small></span></div>
          <div><i :class="activePrd ? 'ok' : ''"></i><span><b>当前 PRD</b><small>{{ activePrd ? `V${activePrd.version_no || activePrd.id} · 已确认` : '还没有确认当前 PRD' }}</small></span></div>
          <div><i :class="activeProfile ? 'ok' : ''"></i><span><b>功能模块</b><small>{{ activeProfile ? `V${activeProfile.version_no || activeProfile.id} · 已确认` : '还没有确认功能模块' }}</small></span></div>
        </div>
      </section>

      <article class="card commits-card">
        <div class="card-head"><div><h2>本次代码变化</h2><p>这里列出的提交会进入本轮研发报告分析。</p></div><span :class="['pill', candidate?.continuity === 'continuous' ? 'ok' : 'neutral']">{{ continuityLabel }}</span></div>
        <div v-if="commits.length" class="commit-list">
          <div v-for="(commit, index) in commits" :key="commit" class="commit-row">
            <span class="lock">✓</span><div><b>提交 {{ index + 1 }}</b><span>纳入本轮分析</span></div><code>{{ shortHash(commit) }}</code>
          </div>
        </div>
        <div v-else class="empty">{{ refreshing ? '正在读取本次代码变化…' : '当前没有可展示的代码变化。' }}</div>
      </article>

      <p v-if="!activePrd || !activeProfile" class="message warning">开始分析前，需要先确认 PRD 和功能模块。缺少任一项时，“确认本次范围”会保持不可用。</p>
      <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>

      <div v-if="evidenceSnapshot" class="next-action"><div><b>本次研发范围已经确认</b><span>下一步开始生成研发报告；进入下一页仍不会自动发送给 AI。</span></div><button class="primary" type="button" @click="goTask">下一步：生成研发报告 →</button></div>

      <details class="technical-details">
        <summary>仓库连接与技术详情</summary>
        <div class="technical-content">
          <GitConnectionPanel
            v-if="project"
            :project-id="project.id"
            :saved-git-url="project.git_url || ''"
            :saved-branch="project.branch || ''"
            :draft-git-url="project.git_url || ''"
            :draft-branch="project.branch || ''"
            @git-connected="reloadPageFacts"
          />
          <div class="technical-grid">
            <div><span>当前分支</span><code>{{ lineage?.branch || project?.branch || '—' }}</code></div>
            <div><span>上次分析到 · 完整 SHA</span><code>{{ fromCommit || '—' }}</code></div>
            <div><span>仓库最新提交 · 完整 SHA</span><code>{{ toCommit || '—' }}</code></div>
            <div><span>最近 Git 检查</span><b>{{ gitStatus?.last_checked_at ? formatTime(gitStatus.last_checked_at) : '尚未读取' }}</b></div>
            <div><span>变化大小</span><b>{{ bytes(candidate?.diff_bytes) }}</b></div>
            <div><span>范围容量</span><b>{{ capacityLabel }}</b></div>
          </div>
          <div v-if="commits.length" class="technical-commits" aria-label="本轮完整提交 SHA">
            <span>本轮完整提交 SHA</span>
            <code v-for="commit in commits" :key="`full-${commit}`">{{ commit }}</code>
          </div>
          <p class="technical-note">完整 SHA、连接诊断和变化大小用于排障；不会把提交数量、字节数或路径数量解释成项目完成度。</p>
        </div>
      </details>
    </template>
  </section>
</template>

<style scoped>
.git-page{display:grid;gap:15px}.page-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.page-head h1{margin:4px 0 7px;font-size:var(--ui-text-page);color:#172033;font-weight:var(--ui-weight-strong);line-height:1.4}.page-head p,.card-head p{max-width:780px;margin:0;color:#6d788c;font-size:var(--ui-text-helper);line-height:1.6}.eyebrow{font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong);letter-spacing:.08em;color:#6477d8}.actions{display:flex;gap:9px;flex-wrap:wrap}.primary,.secondary{min-height:39px;border-radius:10px;padding:9px 14px;font-weight:var(--ui-weight-strong);border:1px solid #d8deea;background:#fff;color:#26334a;cursor:pointer}.primary{border-color:#5268e8;background:#5268e8;color:#fff}.primary:disabled,.secondary:disabled{opacity:.5;cursor:not-allowed}.range-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.range-overview article,.card,.state-card,.next-action{border:1px solid #e1e6ef;border-radius:14px;background:#fff;box-shadow:0 6px 22px rgba(28,39,64,.045)}.range-overview article{padding:16px}.range-overview span{display:block;color:#7a8495;font-size:var(--ui-text-caption)}.range-overview strong{display:block;margin:6px 0;font-size:var(--ui-text-page);color:#172033}.range-overview .text-value{font-size:var(--ui-text-section)}.range-overview small{color:#7a8495;font-size:var(--ui-text-caption)}.card{padding:17px}.card-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}.card h2{margin:0 0 5px;font-size:var(--ui-text-section);color:#1c2738;font-weight:var(--ui-weight-strong);line-height:1.4}.pill{align-self:flex-start;border-radius:999px;padding:5px 9px;font-size:var(--ui-text-caption);background:#eef1f6;color:#637083;font-weight:var(--ui-weight-strong)}.pill.ok{background:#e7f7f1;color:#137257}.readiness-card{background:linear-gradient(145deg,#fff,#f9faff)}.readiness-list{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:13px}.readiness-list>div{display:grid;grid-template-columns:10px 1fr;gap:9px;align-items:start;padding:11px;border-radius:10px;background:#f8fafc}.readiness-list i{width:8px;height:8px;margin-top:5px;border-radius:50%;background:#d5dae3}.readiness-list i.ok{background:#2d9b78}.readiness-list span{display:grid;gap:3px}.readiness-list b{font-size:var(--ui-text-body);color:#344054}.readiness-list small{font-size:var(--ui-text-caption);color:#7a8495}.commit-list{display:grid;margin-top:12px}.commit-row{display:grid;grid-template-columns:28px minmax(130px,1fr) 82px;gap:10px;align-items:center;padding:10px 0;border-top:1px solid #eef1f5}.commit-row:first-child{border-top:0}.commit-row code{justify-self:end;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#526079}.commit-row div{display:grid;gap:3px}.commit-row div b{font-size:var(--ui-text-body)}.commit-row div span{font-size:var(--ui-text-caption);color:#7a8495}.lock{display:grid;place-items:center;width:22px;height:22px;border-radius:50%;background:#e7f7f1;color:#148165;font-size:var(--ui-text-caption)}.message{margin:0;padding:11px 13px;border-radius:10px;font-size:var(--ui-text-helper)}.message.success{background:#eaf8f2;color:#126d54}.message.info{background:#eef3ff;color:#405894}.message.warning{background:#fff6df;color:#885d0c}.message.error{background:#fff0f0;color:#a33a3a}.state-card{padding:18px;color:#68758a}.state-card.error{color:#a33a3a;background:#fff5f5}.empty{padding:24px 10px;text-align:center;color:#7d8798}.next-action{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:15px 17px}.next-action div{display:grid;gap:4px}.next-action span{color:#6d788c;font-size:var(--ui-text-caption)}.technical-details{border:1px solid #e1e6ef;border-radius:13px;background:#fff}.technical-details>summary{padding:13px 15px;cursor:pointer;color:#657185;font-size:var(--ui-text-caption);font-weight:var(--ui-weight-strong)}.technical-content{display:grid;gap:12px;padding:0 15px 15px}.technical-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.technical-grid>div{display:grid;gap:4px;padding:10px;border-radius:9px;background:#f8fafc}.technical-grid span{font-size:var(--ui-text-caption);color:#7a8495}.technical-grid code,.technical-grid b{font-size:var(--ui-text-body);overflow-wrap:anywhere}.technical-commits{display:grid;gap:6px;padding:10px;border-radius:9px;background:#f8fafc}.technical-commits>span{font-size:var(--ui-text-caption);color:#7a8495}.technical-commits code{font-size:var(--ui-text-caption);overflow-wrap:anywhere;user-select:text}.technical-grid code{user-select:text}.technical-note{margin:0;color:#7d8798;font-size:var(--ui-text-caption);line-height:1.6}@media(max-width:900px){.range-overview,.readiness-list,.technical-grid{grid-template-columns:1fr 1fr}}@media(max-width:620px){.page-head,.next-action{flex-direction:column}.range-overview,.readiness-list,.technical-grid{grid-template-columns:1fr}.actions{width:100%}.actions button{flex:1}}
</style>
