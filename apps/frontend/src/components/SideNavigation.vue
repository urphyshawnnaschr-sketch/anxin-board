<script setup>
const props = defineProps({
  currentView: {
    type: String,
    required: true
  },
  currentProjectId: {
    type: [Number, String],
    default: null
  },
  currentProject: {
    type: Object,
    default: null
  }
})

const emit = defineEmits(['navigate'])

const iconPaths = {
  home: 'M3 10 12 3l9 7M5 9v12h5v-7h4v7h5V9',
  setup: 'M4 5h6l2 3h8v12H4V5Zm4 8h8m-8 3h6',
  modules: 'M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z',
  analysis: 'M4 20h17M7 16V9m5 7V4m5 12v-5',
  review: 'M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 14l3 3 5-6',
  board: 'M3 5h18v14H3V5Zm5 0v14m0-9h13',
  projects: 'M4 7h16l-4-4m4 14H4l4 4',
  history: 'M4 7V3m0 4h4M4 7a9 9 0 1 1-1 8m9-9v6l4 2',
  settings: 'M4 7h16M4 17h16M8 4v6m8 4v6'
}

const navigationSections = [
  {
    label: '项目导航',
    items: [
      { key: 'home', label: '项目概览' },
      { key: 'setup', label: '准备项目' },
      { key: 'modules', label: '确认功能模块' },
      { key: 'analysis', route: 'git', views: ['git', 'task'], label: '研发分析' },
      { key: 'review', label: '审阅报告' },
      { key: 'board', label: '安心看板' }
    ]
  },
  {
    label: '工具',
    items: [
      { key: 'projects', label: '切换项目' },
      { key: 'history', label: '历史记录' },
      { key: 'settings', label: '设置' }
    ]
  }
]

function isActive(item) {
  if (Array.isArray(item.views)) return item.views.includes(props.currentView)
  return item.key === props.currentView
}

function routeFor(item) {
  if (item.key === 'projects') return '#/projects'
  const target = item.route || item.key
  if (props.currentProjectId != null) {
    return `#/projects/${props.currentProjectId}/${target}`
  }
  return `#/${target}`
}

function navigate(item) {
  window.location.hash = routeFor(item)
  emit('navigate')
}
</script>

<template>
  <nav class="side-navigation" aria-label="项目导航">
    <div class="brand-block">
      <div class="brand-identity">
        <strong class="brand-title">安心看板</strong>
        <img class="brand-calligraphy" src="../assets/anxin-board-calligraphy.png" alt="非己所安，不加于物" />
      </div>
    </div>

    <div class="project-switch" aria-label="当前项目">
      <small>当前项目</small>
      <strong>
        {{ currentProject?.name || (currentProjectId != null ? `项目 #${currentProjectId}` : '尚未选择项目') }}
      </strong>
      <div class="branch">
        {{ currentProject?.branch || (currentProjectId != null ? '分支尚未设置' : '先创建或进入一个项目') }}
      </div>
    </div>

    <div class="navigation-groups">
      <section v-for="section in navigationSections" :key="section.label" class="navigation-group">
        <h2>{{ section.label }}</h2>
        <button
          v-for="item in section.items"
          :key="item.key"
          class="navigation-item"
          :class="{ active: isActive(item) }"
          type="button"
          :aria-current="isActive(item) ? 'page' : undefined"
          @click="navigate(item)"
        >
          <span class="navigation-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path :d="iconPaths[item.key]" />
            </svg>
          </span>
          <span class="navigation-label">{{ item.label }}</span>
        </button>
      </section>
    </div>

  </nav>
</template>
