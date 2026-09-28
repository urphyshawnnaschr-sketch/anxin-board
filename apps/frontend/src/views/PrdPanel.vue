<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { confirmPrd, getPrdReviewChunk, getPrdVersion, listPrdVersions, uploadPrd } from '../api/prd.js'
import { formatTime } from '../utils/projectFormat.js'

const props = defineProps({ projectId: { type: [Number, String], default: null } })
const emit = defineEmits(['changed'])

const fileInput = ref(null)
const selectedFile = ref(null)
const uploading = ref(false)
const confirming = ref(false)
const reviewLoading = ref(false)
const reviewComplete = ref(false)
const reviewText = ref('')
const dragActive = ref(false)
const message = ref('')
const messageType = ref('')
const currentVersion = ref(null)
const activeVersion = ref(null)
const versions = ref([])
const historyState = ref('loading')

const ACCEPTED_FILE_RE = /\.(md|txt|docx|pdf)$/i
const REVIEW_CHUNK_SIZE = 50000
const REVIEW_MAX_CHUNKS = 512

function showMessage(text, type) { message.value = text; messageType.value = type }
function clearMessage() { message.value = ''; messageType.value = '' }
function formatBytes(size) { if (size == null || Number.isNaN(size)) return '-'; if (size < 1024) return `${size} B`; if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`; return `${(size / 1024 / 1024).toFixed(2)} MB` }
function statusLabel(status) { return ({ uploaded:'已上传', parsed:'待人工确认', parse_failed:'解析失败', parse_confirmed:'当前有效', superseded:'已被替换' })[status] || status }
function hasPreview(status) { return ['parsed','parse_confirmed','superseded'].includes(status) }
const viewingActive = computed(() => currentVersion.value && activeVersion.value && currentVersion.value.id === activeVersion.value.id)
const readyForNextStep = computed(() => viewingActive.value && currentVersion.value?.status === 'parse_confirmed')
const mustLoadFullReview = computed(() => currentVersion.value?.status === 'parsed' && currentVersion.value?.preview_truncated && !reviewComplete.value)

function resetReview(version = null) {
  reviewLoading.value = false
  if (version && hasPreview(version.status)) {
    reviewText.value = version.preview || ''
    reviewComplete.value = !version.preview_truncated
  } else {
    reviewText.value = ''
    reviewComplete.value = false
  }
}

function activateVersion(version) {
  currentVersion.value = version
  resetReview(version)
}

function selectFile(file) {
  clearMessage()
  if (!file) { selectedFile.value = null; return }
  if (!(file instanceof File) || !ACCEPTED_FILE_RE.test(file.name || '')) {
    selectedFile.value = null
    return showMessage('仅支持 DOCX / Markdown / TXT / 可提取文字的 PDF', 'error')
  }
  selectedFile.value = file
}

function pickFile(event) { selectFile(event.target.files?.[0] || null) }
function handleDrop(event) {
  dragActive.value = false
  const files = Array.from(event.dataTransfer?.files || [])
  if (files.length !== 1) return showMessage('请一次只拖入一个 PRD 文件', 'error')
  selectFile(files[0])
}

function goModules() {
  if (props.projectId == null) return
  window.location.hash = `#/projects/${props.projectId}/modules`
}

async function viewVersion(versionId) {
  const { status, ok, body } = await getPrdVersion(versionId)
  if (status === 404) return showMessage('PRD 版本不存在或已被删除', 'error')
  if (!ok) return showMessage((body.detail && body.detail.message) || '读取 PRD 版本失败', 'error')
  if (!body || typeof body.status !== 'string' || !body.id) return showMessage('读取响应格式不正确', 'error')
  activateVersion(body)
}
function viewActive() { if (activeVersion.value) viewVersion(activeVersion.value.id) }

async function loadFullReview() {
  const version = currentVersion.value
  if (!version?.id || !version.preview_truncated || reviewLoading.value) return
  reviewLoading.value = true
  clearMessage()
  try {
    let offset = 0
    let expectedHash = version.parsed_hash || ''
    const parts = []
    for (let index = 0; index < REVIEW_MAX_CHUNKS; index += 1) {
      const { ok, body } = await getPrdReviewChunk(version.id, { offset, limit: REVIEW_CHUNK_SIZE })
      if (!ok) throw new Error((body.detail && body.detail.message) || '完整解析文本读取失败')
      if (body?.schema_version !== 'prd_parsed_review_v1' || body.version_id !== version.id) throw new Error('完整解析文本身份校验失败')
      if (typeof body.parsed_hash !== 'string' || !body.parsed_hash) throw new Error('完整解析文本缺少版本指纹')
      if (!expectedHash) expectedHash = body.parsed_hash
      if (body.parsed_hash !== expectedHash) throw new Error('完整解析文本版本在审阅过程中发生变化')
      if (body.offset !== offset || typeof body.content !== 'string') throw new Error('完整解析文本分块顺序异常')
      if (!Number.isInteger(body.next_offset) || body.next_offset < offset) throw new Error('完整解析文本游标异常')
      parts.push(body.content)
      if (body.complete === true) {
        const joined = parts.join('')
        if (Number.isInteger(body.total_chars) && joined.length !== body.total_chars) throw new Error('完整解析文本长度校验失败')
        if (currentVersion.value?.id !== version.id) throw new Error('当前查看的 PRD 版本已经切换')
        reviewText.value = joined
        reviewComplete.value = true
        showMessage('完整解析文本已加载，请核对后再确认这一版。', 'success')
        return
      }
      if (body.next_offset <= offset) throw new Error('完整解析文本读取没有继续推进')
      offset = body.next_offset
    }
    throw new Error('完整解析文本过大，未能在安全分块上限内读完')
  } catch (error) {
    reviewComplete.value = false
    showMessage(error.message || '完整解析文本读取失败', 'error')
  } finally {
    reviewLoading.value = false
  }
}

async function handleUpload() {
  if (!selectedFile.value) return showMessage('请先选择要导入的 PRD 文件', 'error')
  if (uploading.value) return
  uploading.value = true; clearMessage()
  try {
    const { status, ok, body } = await uploadPrd(props.projectId, selectedFile.value)
    if (status === 404) return showMessage('项目不存在或已被删除', 'error')
    if (!ok) return showMessage((body.detail && body.detail.message) || 'PRD 导入失败', 'error')
    if (!body || typeof body.status !== 'string' || !body.id) return showMessage('导入响应格式不正确', 'error')
    activateVersion(body)
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    showMessage(body.status === 'parsed' ? (body.preview_truncated ? 'PRD 已解析；确认前请先加载并核对完整解析文本。' : 'PRD 已解析，请人工确认解析结果。') : 'PRD 解析失败，未产生有效版本。', body.status === 'parsed' ? 'success' : 'error')
    await loadHistory(); emit('changed')
  } catch (error) { showMessage(error.message || 'PRD 导入失败', 'error') }
  finally { uploading.value = false }
}

async function handleConfirm(version) {
  if (confirming.value) return
  if (version.preview_truncated && !reviewComplete.value) return showMessage('请先加载并核对完整解析文本，再确认这一版。', 'error')
  confirming.value = true; clearMessage()
  try {
    const { status, ok, body } = await confirmPrd(version.id, 'local')
    if (status === 404) return showMessage('PRD 版本不存在或已被删除', 'error')
    if (!ok) return showMessage((body.detail && body.detail.message) || '确认失败', 'error')
    if (!body || body.status !== 'parse_confirmed' || !body.id) return showMessage('确认响应格式不正确', 'error')
    activateVersion(body)
    showMessage(`V${body.version_no} 已确认为当前有效 PRD`, 'success')
    await loadHistory(); emit('changed')
  } catch (error) { showMessage(error.message || '确认失败', 'error') }
  finally { confirming.value = false }
}

async function loadHistory() {
  if (!props.projectId) return
  historyState.value = 'loading'
  try {
    const { ok, body } = await listPrdVersions(props.projectId)
    if (!ok || !Array.isArray(body)) { historyState.value = 'failed'; return }
    versions.value = body
    activeVersion.value = body.find((version) => version.status === 'parse_confirmed') || null
    historyState.value = 'loaded'
  } catch { historyState.value = 'failed' }
}

async function restoreView() {
  clearMessage(); await loadHistory()
  if (activeVersion.value) await viewVersion(activeVersion.value.id)
  else if (versions.value.length) await viewVersion(versions.value[0].id)
  else { currentVersion.value = null; resetReview() }
}

onMounted(() => { if (props.projectId != null) restoreView() })
watch(() => props.projectId, (newId) => { currentVersion.value=null; activeVersion.value=null; versions.value=[]; selectedFile.value=null; dragActive.value=false; resetReview(); if(fileInput.value) fileInput.value.value=''; clearMessage(); if(newId!=null) restoreView() })
</script>

<template>
  <section class="panel-card prd-panel lane-prd" aria-labelledby="prd-panel-title">
    <div class="lane-card-head">
      <div><span class="section-kicker">PRD</span><h3 id="prd-panel-title">当前 PRD</h3><p>上传后先看完整解析结果，人工确认后才成为后续分析使用的当前版本。</p></div>
      <span :class="['status-badge', activeVersion ? 'status-active' : 'status-info']">{{ activeVersion ? `V${activeVersion.version_no} 已确认` : '等待确认' }}</span>
    </div>

    <div
      :class="['lane-file-zone', { 'drag-active': dragActive }]"
      @dragenter.prevent="dragActive = true"
      @dragover.prevent="dragActive = true"
      @dragleave.prevent="dragActive = false"
      @drop.prevent="handleDrop"
    >
      <div class="lane-upload-icon">⇧</div>
      <b>{{ selectedFile ? selectedFile.name : currentVersion ? currentVersion.original_filename : '选择或拖入 PRD 文件' }}</b>
      <p v-if="currentVersion">{{ formatBytes(currentVersion.size_bytes) }} · {{ statusLabel(currentVersion.status) }}</p>
      <p v-else>支持 DOCX / Markdown / TXT / 可提取文字的 PDF；拖入只选择文件，不会自动上传。</p>
      <label class="secondary-button lane-file-button">{{ currentVersion ? '更换文件' : '选择文件' }}<input ref="fileInput" type="file" accept=".md,.txt,.docx,.pdf" @change="pickFile" /></label>
      <button class="primary-button" type="button" :disabled="uploading || !selectedFile" @click="handleUpload">{{ uploading ? '上传解析中...' : '上传并解析' }}</button>
    </div>

    <p class="lane-prd-hint">扫描件或图片型 PDF 不支持 OCR；解析失败不会产生有效 PRD。</p>
    <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>

    <div v-if="currentVersion" class="lane-prd-current">
      <div v-if="currentVersion.status === 'parsed'" class="lane-alert warning-alert"><b>需要人工确认解析结果</b><span>{{ mustLoadFullReview ? '当前只显示截断预览；必须先加载并核对完整解析文本。' : '请核对下面的解析文本是否正确，再确认这一版。' }}</span></div>
      <div v-if="currentVersion.status === 'parse_failed'" class="lane-alert error-alert"><b>解析失败</b><span>请检查文件格式后重新上传；不会沿用演示或旧解析结果。</span></div>

      <template v-if="hasPreview(currentVersion.status)">
        <details class="lane-preview" :open="currentVersion.status === 'parsed'">
          <summary>{{ reviewComplete && currentVersion.preview_truncated ? '查看完整解析结果' : '查看解析结果' }}</summary>
          <pre>{{ reviewText || currentVersion.preview || '（无预览内容）' }}</pre>
          <p v-if="currentVersion.preview_truncated && !reviewComplete">当前仅显示前置预览，完整解析文本尚未审阅。</p>
          <p v-else-if="currentVersion.preview_truncated && reviewComplete">完整解析文本已加载并通过版本指纹校验。</p>
        </details>
        <div class="lane-actions">
          <button v-if="currentVersion.preview_truncated && !reviewComplete" class="secondary-button" type="button" :disabled="reviewLoading" @click="loadFullReview">{{ reviewLoading ? '正在加载完整解析文本...' : '加载完整解析文本（确认前必读）' }}</button>
          <button v-if="currentVersion.status === 'parsed'" class="primary-button" type="button" :disabled="confirming || mustLoadFullReview" @click="handleConfirm(currentVersion)">{{ confirming ? '确认中...' : '确认解析版本' }}</button>
          <button v-if="activeVersion && !viewingActive" class="secondary-button" type="button" @click="viewActive">返回当前有效版本</button>
        </div>
      </template>

      <section v-if="readyForNextStep" class="next-step-card">
        <div class="next-step-check">✓</div>
        <div>
          <span>当前 PRD 已确认</span>
          <h4>可以继续确认功能模块</h4>
          <p>后续模块建议只基于已确认 PRD 和受控 Git 信息生成，仍需要人工确认后才生效。</p>
        </div>
        <button class="next-step-button" type="button" @click="goModules">下一步：确认功能模块 →</button>
      </section>

      <details class="prd-technical">
        <summary>解析技术信息</summary>
        <div class="lane-meta-grid">
          <div><span>文件指纹</span><b class="hash">{{ currentVersion.source_hash || '暂无法确认' }}</b></div>
          <div><span>解析版本</span><b>{{ currentVersion.parser_version || '暂无法确认' }} · {{ statusLabel(currentVersion.status) }}</b></div>
        </div>
      </details>
    </div>

    <details class="history-details">
      <summary>PRD 历史 · {{ historyState === 'loaded' ? `${versions.length} 个版本` : '读取中' }}</summary>
      <p v-if="historyState === 'loading'" class="empty state-placeholder">正在加载版本历史...</p>
      <p v-else-if="historyState === 'failed'" class="message error">加载版本历史失败</p>
      <ul v-else-if="versions.length" class="lane-history-list">
        <li v-for="version in versions" :key="version.id" :class="{ active: activeVersion && activeVersion.id === version.id }">
          <div><b>V{{ version.version_no }} · {{ version.original_filename }}</b><span>{{ statusLabel(version.status) }} · {{ formatTime(version.created_at) }}</span></div>
          <button class="lane-link-button" type="button" @click="viewVersion(version.id)">查看</button>
        </li>
      </ul>
      <p v-else class="empty state-placeholder">暂无 PRD 版本</p>
    </details>
  </section>
</template>

<style scoped>
.lane-prd{padding:18px}.lane-card-head,.lane-actions{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.lane-card-head h3{margin:4px 0 5px;color:var(--title)}.lane-card-head p{margin:0;color:var(--muted);font-size:12px}.lane-file-zone{margin-top:15px;padding:18px;border:1.5px dashed #cbd5e1;border-radius:12px;background:#f8fafc;display:grid;grid-template-columns:40px 1fr auto auto;gap:8px 10px;align-items:center;transition:border-color .15s ease,background .15s ease}.lane-file-zone.drag-active{border-color:var(--primary);background:var(--primary-light)}.lane-upload-icon{grid-row:1/3;width:36px;height:36px;display:grid;place-items:center;border-radius:10px;background:var(--primary-light);color:var(--primary);font-size:18px}.lane-file-zone b{color:var(--title);font-size:12px}.lane-file-zone p{grid-column:2;margin:0;color:var(--muted);font-size:10px}.lane-file-button{position:relative;overflow:hidden}.lane-file-button input{position:absolute;inset:0;opacity:0;cursor:pointer}.lane-prd-hint{margin:8px 0 0;color:var(--muted);font-size:10px}.lane-prd-current{margin-top:14px}.lane-meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.lane-meta-grid>div{padding:10px;border-radius:9px;background:#f8fafc}.lane-meta-grid span,.lane-meta-grid b{display:block}.lane-meta-grid span{color:var(--muted);font-size:9px}.lane-meta-grid b{margin-top:4px;color:var(--title);font-size:10px;word-break:break-all}.lane-alert{margin-top:10px;padding:11px 12px;border-radius:9px}.lane-alert b,.lane-alert span{display:block}.lane-alert b{font-size:11px}.lane-alert span{margin-top:3px;font-size:10px}.warning-alert{background:#fff8eb;color:#8a5a16;border:1px solid #f3d39d}.error-alert{background:#fff2f2;color:#9b3c3c;border:1px solid #efc2c2}.lane-preview{margin:10px 0;border:1px solid var(--border);border-radius:9px;overflow:hidden}.lane-preview summary,.prd-technical summary,.history-details summary{padding:10px 12px;background:#f8fafc;color:var(--title);font-size:11px;font-weight:700;cursor:pointer}.lane-preview pre{max-height:420px;overflow:auto;margin:0;padding:12px;white-space:pre-wrap;font-size:10px;background:#fff}.lane-preview p{margin:0;padding:8px 12px;color:var(--muted);font-size:10px}.next-step-card{margin-top:14px;display:grid;grid-template-columns:40px minmax(0,1fr) auto;gap:13px;align-items:center;padding:15px;border:1px solid #cfe6dc;border-radius:13px;background:linear-gradient(145deg,#f8fdfb,#f2faf7)}.next-step-check{width:38px;height:38px;display:grid;place-items:center;border-radius:12px;background:#dff4ec;color:#14735d;font-size:18px;font-weight:900}.next-step-card span{font-size:9px;font-weight:900;letter-spacing:.08em;color:#4e8a79}.next-step-card h4{margin:3px 0 4px;color:#23453c;font-size:13px}.next-step-card p{margin:0;color:#668176;font-size:10px;line-height:1.55}.next-step-button{min-height:40px;padding:8px 13px;border:1px solid #2d8069;border-radius:10px;background:#2d8069;color:#fff;font:inherit;font-size:11px;font-weight:900;cursor:pointer}.prd-technical,.history-details{margin-top:12px;border:1px solid var(--border);border-radius:9px;overflow:hidden}.prd-technical .lane-meta-grid{padding:10px}.lane-history-list{list-style:none;margin:0;padding:10px;display:flex;flex-direction:column;gap:7px}.lane-history-list li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px;border:1px solid var(--border);border-radius:9px}.lane-history-list li.active{border-color:#b7c3ff;background:#f7f8ff}.lane-history-list b,.lane-history-list span{display:block}.lane-history-list b{font-size:11px;color:var(--title)}.lane-history-list span{margin-top:3px;font-size:9px;color:var(--muted)}.lane-link-button{border:0;background:transparent;color:var(--primary);font-size:10px;font-weight:800;cursor:pointer}@media(max-width:760px){.lane-card-head{flex-direction:column}.lane-file-zone{grid-template-columns:40px 1fr}.lane-file-zone .secondary-button,.lane-file-zone .primary-button{grid-column:1/-1}.lane-meta-grid{grid-template-columns:1fr}.next-step-card{grid-template-columns:40px 1fr}.next-step-button{grid-column:1/-1}}
</style>
