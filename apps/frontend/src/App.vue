<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ensureLocalSession } from './api/localSession.js'
import AppShell from './components/AppShell.vue'
import ProjectListView from './views/ProjectListView.vue'
import ProjectDetailView from './views/ProjectDetailView.vue'
import GitEvidenceView from './views/GitEvidenceView.vue'
import TaskExecutionView from './views/TaskExecutionView.vue'
import ReportReviewView from './views/ReportReviewView.vue'
import AnxinBoardView from './views/AnxinBoardView.vue'
import HistoryExceptionsView from './views/HistoryExceptionsView.vue'
import SettingsBackupView from './views/SettingsBackupView.vue'

const projectPages = new Set([
  'home',
  'setup',
  'modules',
  'git',
  'task',
  'review',
  'board',
  'history',
  'settings'
])
const projectDetailPages = new Set(['home', 'setup', 'modules'])

const initialRoute = parseHash()
const currentView = ref(initialRoute.view)
const currentProjectId = ref(initialRoute.id)
const sessionChecked = ref(false)
const sessionAvailable = ref(false)

function parseHash() {
  const rawHash = window.location.hash || '#/projects'
  const path = rawHash.slice(1).split('?')[0] || '/projects'

  if (path === '/projects') {
    return { view: 'projects', id: null }
  }

  const scopedMatch = path.match(/^\/projects\/(\d+)\/(home|setup|modules|git|task|review|board|history|settings)$/)
  if (scopedMatch) {
    return { view: scopedMatch[2], id: Number(scopedMatch[1]) }
  }

  // Backward-compatible routes used by the current Product views.
  const legacyDashboardMatch = path.match(/^\/projects\/(\d+)\/dashboard$/)
  if (legacyDashboardMatch) {
    return { view: 'board', id: Number(legacyDashboardMatch[1]) }
  }

  const legacyDetailMatch = path.match(/^\/projects\/(\d+)$/)
  if (legacyDetailMatch) {
    return { view: 'home', id: Number(legacyDetailMatch[1]) }
  }

  const unscopedMatch = path.match(/^\/(home|setup|modules|git|task|review|board|history|settings)$/)
  if (unscopedMatch && projectPages.has(unscopedMatch[1])) {
    return { view: unscopedMatch[1], id: null }
  }

  return { view: 'projects', id: null }
}

function handleRoute() {
  const route = parseHash()
  currentView.value = route.view
  currentProjectId.value = route.id
}

onMounted(() => {
  void ensureLocalSession().then(session => {
    sessionAvailable.value = Boolean(session)
    sessionChecked.value = true
  })
  window.addEventListener('hashchange', handleRoute)
})

onBeforeUnmount(() => {
  window.removeEventListener('hashchange', handleRoute)
})
</script>

<template>
  <AppShell :current-view="currentView" :current-project-id="currentProjectId">
    <aside v-if="sessionChecked && !sessionAvailable" class="local-session-notice" role="status">
      <strong>当前窗口仅供查看</strong>
      <p>要检查代码或保存操作，请从桌面“安心看板”快捷方式重新打开，并使用它打开的浏览器窗口。已有分析和项目数据会保留。</p>
    </aside>
    <ProjectListView v-if="currentView === 'projects'" />
    <ProjectDetailView
      v-else-if="projectDetailPages.has(currentView) && currentProjectId != null"
      :project-id="currentProjectId"
      :section="currentView"
    />
    <GitEvidenceView
      v-else-if="currentView === 'git' && currentProjectId != null"
      :project-id="currentProjectId"
    />
    <TaskExecutionView
      v-else-if="currentView === 'task' && currentProjectId != null"
      :project-id="currentProjectId"
    />
    <ReportReviewView
      v-else-if="currentView === 'review' && currentProjectId != null"
      :project-id="currentProjectId"
    />
    <template v-else-if="currentView === 'board' && currentProjectId != null">
      <AnxinBoardView :project-id="currentProjectId" />
    </template>
    <HistoryExceptionsView
      v-else-if="currentView === 'history' && currentProjectId != null"
      :project-id="currentProjectId"
    />
    <SettingsBackupView
      v-else-if="currentView === 'settings'"
      :project-id="currentProjectId"
    />
    <ProjectListView v-else />
  </AppShell>
</template>
