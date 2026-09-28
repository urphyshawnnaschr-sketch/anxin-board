<script setup>
import { onMounted, ref } from 'vue'
import { createProject, listProjects } from '../api/projects.js'
import { formatTime, statusLabel } from '../utils/projectFormat.js'

const projectName = ref('')
const projects = ref([])
const message = ref('')
const messageType = ref('')
const listState = ref('loading')
const creating = ref(false)
const showCreate = ref(false)

const firstRun = ref(false)

function showMessage(text, type) {
  message.value = text
  messageType.value = type
}

async function loadProjects() {
  listState.value = 'loading'
  try {
    projects.value = await listProjects()
    listState.value = 'loaded'
    firstRun.value = projects.value.length === 0
    if (projects.value.length === 0) showCreate.value = true
  } catch {
    listState.value = 'failed'
    firstRun.value = false
    showMessage('加载项目列表失败，请检查本地服务后重试。', 'error')
  }
}

async function createProjectHandler() {
  if (creating.value) return
  const name = projectName.value.trim()
  if (!name) {
    showMessage('先给这个项目起一个名字。', 'error')
    return
  }
  creating.value = true
  try {
    const created = await createProject({ name })
    projectName.value = ''
    showCreate.value = false
    await loadProjects()
    showMessage('项目已创建，现在从“准备项目”开始。', 'success')
    if (created && created.id) window.location.hash = `#/projects/${created.id}/setup`
  } catch (error) {
    showMessage(error.message || '创建失败', 'error')
  } finally {
    creating.value = false
  }
}

function openDetail(id) {
  window.location.hash = `#/projects/${id}`
}

onMounted(loadProjects)
</script>

<template>
  <section class="page-stack lane-page" aria-label="项目列表内容">
    <template v-if="firstRun">
      <section class="welcome-card">
        <div class="welcome-copy">
          <h1>从一个真实项目开始</h1>
          <p>连接代码仓库，整理需求，了解每项功能的实现依据。</p>
          <form class="welcome-create" @submit.prevent="createProjectHandler">
            <label for="first-project-name">项目名称</label>
            <div>
              <input id="first-project-name" v-model="projectName" maxlength="100" placeholder="例如：研发进度 AI Agent" autofocus />
              <button class="primary-button" type="submit" :disabled="creating">{{ creating ? '创建中…' : '创建并准备项目 →' }}</button>
            </div>
          </form>
          <small class="welcome-note">创建后直接进入“准备项目”，不会自动开始 AI 分析或其它操作。</small>
        </div>
      </section>
      <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>
    </template>

    <template v-else>
      <div class="lane-page-head">
        <div>
          <h2>项目列表</h2>
          <p>选择项目，继续当前工作。</p>
        </div>
        <button class="primary-button" type="button" @click="showCreate = !showCreate">
          {{ showCreate ? '收起' : '＋ 新建项目' }}
        </button>
      </div>

      <p v-if="message" :class="['message', messageType]" role="status">{{ message }}</p>

      <form v-if="showCreate" class="panel-card lane-create-card" @submit.prevent="createProjectHandler">
        <div>
          <h3>新建项目</h3>
          <p>创建后直接进入“准备项目”。</p>
        </div>
        <div class="lane-create-controls">
          <div class="field">
            <label for="project-name">项目名称</label>
            <input id="project-name" v-model="projectName" maxlength="100" placeholder="请输入项目名称" />
          </div>
          <button class="primary-button" type="submit" :disabled="creating">
            {{ creating ? '创建中...' : '创建并开始 →' }}
          </button>
        </div>
      </form>

      <section class="panel-card lane-table-card" aria-labelledby="project-list-title">
        <div class="lane-card-head">
          <div>
            <h3 id="project-list-title">全部项目</h3>
          </div>
        </div>

        <div v-if="listState === 'loading'" class="lane-state" role="status">
          <div class="lane-spinner" aria-hidden="true"></div>
          <b>正在加载项目...</b>
        </div>

        <div v-else-if="listState === 'failed'" class="lane-state error-state">
          <b>项目列表暂时无法显示</b>
          <span>请检查本地服务后重新加载。</span>
          <button class="secondary-button" type="button" @click="loadProjects">重新加载</button>
        </div>

        <div v-else-if="projects.length" class="lane-table-wrap">
          <table class="lane-table">
            <thead>
              <tr>
                <th>项目</th>
                <th>分支</th>
                <th>最近更新</th>
                <th>管理状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="project in projects" :key="project.id">
                <td><b>{{ project.name }}</b><span>{{ project.git_url || '还没有连接 Git 仓库' }}</span></td>
                <td><code>{{ project.branch || '未设置' }}</code></td>
                <td>{{ formatTime(project.updated_at) }}</td>
                <td><span class="status-badge status-draft">{{ statusLabel(project.status) }}</span></td>
                <td><button class="lane-link-button" type="button" @click="openDetail(project.id)">进入项目 →</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.lane-page {
  gap: 16px;
}

.lane-page-head,.lane-card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 18px;
}

.lane-page-head h2,.lane-card-head h3,.lane-create-card h3 {
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

.lane-page-head p,.lane-card-head p,.lane-create-card p {
  margin: 0;
  color: var(--muted);
  font-size: var(--ui-text-helper);
}

.lane-create-card {
  padding: 18px;
  display: grid;
  grid-template-columns: minmax(220px,1fr) minmax(320px,1.4fr);
  gap: 20px;
  align-items: end;
}

.lane-create-controls {
  display: grid;
  grid-template-columns: minmax(0,1fr) auto;
  gap: 12px;
  align-items: end;
}

.lane-table-card {
  padding: 20px;
}

.lane-table-wrap {
  margin-top: 16px;
  overflow: auto;
  border: 0;
  border-radius: 0;
}

.lane-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 720px;
  background: #fff;
}

.lane-table th {
  padding: 12px;
  background: #f5f5f7;
  color: var(--muted);
  font-size: var(--ui-text-caption);
  text-align: left;
  border-bottom: 1px solid var(--border);
  font-weight: 400;
}

.lane-table td {
  padding: 16px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
  font-size: var(--ui-text-body);
}

.lane-table tbody tr:last-child td {
  border-bottom: 0;
}

.lane-table td:first-child b,.lane-table td:first-child span {
  display: block;
}

.lane-table td:first-child b {
  color: var(--title);
  font-size: var(--ui-text-body);
}

.lane-table td:first-child span {
  margin-top: 4px;
  color: var(--muted);
  font-size: var(--ui-text-helper);
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lane-table code {
  font-size: var(--ui-text-caption);
}

.lane-link-button {
  border: 0;
  background: transparent;
  color: var(--primary);
  font-weight: var(--ui-weight-strong);
  font-size: var(--ui-text-body);
  cursor: pointer;
  padding: 6px 0;
  min-height: 38px;
}

.lane-state {
  min-height: 180px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  text-align: center;
}

.lane-state b {
  color: var(--title);
}

.lane-spinner {
  width: 25px;
  height: 25px;
  border: 3px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin .8s linear infinite;
}

.error-state {
  color: var(--danger);
}

.welcome-card {
  min-height: 280px;
  display: grid;
  align-items: center;
  padding: 32px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #fff;
  box-shadow: none;
}

.welcome-copy {
  max-width: 640px;
}

.welcome-kicker {
  font-size: var(--ui-text-helper);
  font-weight: var(--ui-weight-strong);
  letter-spacing: .12em;
  color: #6277d7;
}

.welcome-copy h1 {
  margin: 10px 0 12px;
  color: var(--title);
  font-size: var(--ui-text-page);
  line-height: 1.15;
  font-weight: var(--ui-weight-strong);
}

.welcome-copy>p {
  max-width: 680px;
  margin: 0;
  color: var(--muted);
  font-size: var(--ui-text-body);
  line-height: 1.7;
}

.welcome-create {
  margin-top: 24px;
  display: grid;
  gap: 9px;
}

.welcome-create>label {
  font-size: var(--ui-text-body);
  font-weight: var(--ui-weight-strong);
  color: #475467;
}

.welcome-create>div {
  display: grid;
  grid-template-columns: minmax(0,1fr) auto;
  gap: 10px;
}

.welcome-create input {
  min-height: 38px;
  padding: 0 14px;
  border: 1px solid #cfd8e6;
  border-radius: 12px;
  background: #fff;
  font: inherit;
  color: var(--title);
}

.welcome-note {
  display: block;
  margin-top: 12px;
  color: #7a8597;
  line-height: 1.6;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width:720px) {
  .lane-page-head,.lane-card-head {
    flex-direction: column;
  }
  .lane-create-card,.lane-create-controls,.welcome-create>div {
    grid-template-columns: 1fr;
  }
  .welcome-card {
    padding: 32px;
    min-height: 280px;
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: none;
  }
  .welcome-copy h1 {
    font-size: var(--ui-text-page);
    font-weight: var(--ui-weight-strong);
    line-height: 1.4;
  }
}
</style>
