<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import SideNavigation from './SideNavigation.vue'
import GuidedWorkflow from './GuidedWorkflow.vue'
import ModelQuickSetup from './ModelQuickSetup.vue'
import OnboardingTour from './OnboardingTour.vue'
import { getProject } from '../api/projects.js'
import { isOnboardingComplete, ONBOARDING_REPLAY_EVENT } from '../onboarding.js'

const props = defineProps({
  currentView: {
    type: String,
    required: true
  },
  currentProjectId: {
    type: [Number, String],
    default: null
  }
})

const pageMeta = {
  projects: { title: '项目列表' },
  home: { title: '项目概览' },
  setup: { title: '准备项目' },
  modules: { title: '确认功能模块' },
  git: { title: '选择研发范围' },
  task: { title: '生成研发报告' },
  review: { title: '审阅报告' },
  board: { title: '安心看板' },
  history: { title: '历史记录' },
  settings: { title: '设置' }
}

const mobileNavigationQuery = window.matchMedia('(max-width: 820px)')
const drawerOpen = ref(false)
const isMobileNavigation = ref(mobileNavigationQuery.matches)
const menuButton = ref(null)
const closeButton = ref(null)
const currentProject = ref(null)
const tourOpen = ref(false)
let drawerFocusTimer = null
let projectReadToken = 0

const currentMeta = computed(() => pageMeta[props.currentView] || pageMeta.projects)
const pageTitle = computed(() => currentMeta.value.title)
const projectContextName = computed(() => (
  currentProject.value?.name || (props.currentProjectId != null ? `项目 #${props.currentProjectId}` : '项目')
))
const showModelQuickSetup = computed(() => props.currentProjectId != null && ['modules', 'task'].includes(props.currentView))
const showOnboarding = computed(() => tourOpen.value && props.currentView !== 'board')

async function loadProjectContext(projectId) {
  const token = ++projectReadToken
  if (projectId == null) {
    currentProject.value = null
    return
  }

  try {
    const { ok, body } = await getProject(projectId)
    if (token === projectReadToken) currentProject.value = ok ? body : null
  } catch {
    if (token === projectReadToken) currentProject.value = null
  }
}

async function openDrawer() {
  drawerOpen.value = true
  await nextTick()
  drawerFocusTimer = window.setTimeout(() => {
    if (drawerOpen.value) closeButton.value?.focus()
  }, 240)
}

async function closeDrawer() {
  const shouldRestoreFocus = drawerOpen.value && isMobileNavigation.value
  window.clearTimeout(drawerFocusTimer)
  drawerOpen.value = false
  if (shouldRestoreFocus) {
    await nextTick()
    window.setTimeout(() => menuButton.value?.focus(), 0)
  }
}

function closeOnboarding() {
  tourOpen.value = false
}

function replayOnboarding() {
  tourOpen.value = true
}

function maybeOpenFirstRun() {
  if (props.currentView === 'projects' && !isOnboardingComplete()) tourOpen.value = true
}

function handleKeydown(event) {
  if (event.key === 'Escape' && drawerOpen.value) closeDrawer()
}

function handleViewportChange(event) {
  isMobileNavigation.value = event.matches
  if (!event.matches) drawerOpen.value = false
}

watch(
  () => props.currentProjectId,
  (projectId) => loadProjectContext(projectId),
  { immediate: true }
)

watch(
  () => props.currentView,
  (view) => {
    closeDrawer()
    if (view === 'projects') maybeOpenFirstRun()
  }
)

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  window.addEventListener(ONBOARDING_REPLAY_EVENT, replayOnboarding)
  mobileNavigationQuery.addEventListener('change', handleViewportChange)
  maybeOpenFirstRun()
})

onBeforeUnmount(() => {
  projectReadToken += 1
  window.clearTimeout(drawerFocusTimer)
  window.removeEventListener('keydown', handleKeydown)
  window.removeEventListener(ONBOARDING_REPLAY_EVENT, replayOnboarding)
  mobileNavigationQuery.removeEventListener('change', handleViewportChange)
})
</script>

<template>
  <div class="app-shell" :class="{ 'is-frozen-client-board': currentView === 'board' }">
    <OnboardingTour v-if="showOnboarding" @close="closeOnboarding" />

    <div
      v-if="drawerOpen"
      class="navigation-backdrop"
      aria-hidden="true"
      @click="closeDrawer"
    ></div>

    <aside
      id="primary-navigation"
      class="shell-sidebar"
      :class="{ 'is-open': drawerOpen }"
      :aria-hidden="isMobileNavigation && !drawerOpen ? 'true' : undefined"
      :inert="isMobileNavigation && !drawerOpen"
    >
      <div class="mobile-navigation-heading">
        <span>菜单</span>
        <button ref="closeButton" class="icon-button navigation-close" type="button" aria-label="关闭主菜单" @click="closeDrawer">
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <SideNavigation
        :current-view="currentView"
        :current-project-id="currentProjectId"
        :current-project="currentProject"
        @navigate="closeDrawer"
      />
    </aside>

    <div class="shell-workspace">
      <header class="shell-topbar">
        <div class="topbar-left">
          <button
            ref="menuButton"
            class="icon-button menu-button"
            type="button"
            aria-label="打开主菜单"
            aria-controls="primary-navigation"
            :aria-expanded="drawerOpen"
            @click="openDrawer"
          >
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
          </button>
          <div class="breadcrumb" aria-label="当前位置" data-tour="project-context">
            <span>{{ projectContextName }}</span>
            <span class="slash">/</span>
            <strong>{{ pageTitle }}</strong>
          </div>
        </div>

        <div class="top-actions" aria-label="当前运行状态">
          <ModelQuickSetup v-if="showModelQuickSetup" />
          <span class="runtime">本地运行</span>
        </div>
      </header>

      <main class="shell-main">
        <div class="content-frame">
          <GuidedWorkflow v-if="currentView !== 'home'" :current-view="currentView" :current-project-id="currentProjectId" />
          <slot></slot>
        </div>
      </main>
    </div>
  </div>
</template>
