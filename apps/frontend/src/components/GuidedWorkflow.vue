<script setup>
import { computed } from 'vue'

const props = defineProps({
  currentView: { type: String, required: true },
  currentProjectId: { type: [Number, String], default: null }
})

const steps = [
  { key: 'setup', label: '准备项目' },
  { key: 'modules', label: '确认功能模块' },
  { key: 'git', label: '研发分析' },
  { key: 'review', label: '审阅报告' },
  { key: 'board', label: '安心看板' }
]

const stepMap = {
  home: 0,
  setup: 0,
  modules: 1,
  git: 2,
  task: 2,
  review: 3,
  board: 4
}

const visible = computed(() => props.currentProjectId != null && Object.hasOwn(stepMap, props.currentView))
const currentIndex = computed(() => stepMap[props.currentView] ?? 0)
const currentStep = computed(() => steps[currentIndex.value])
</script>

<template>
  <section v-if="visible" class="guided-flow" aria-label="项目主流程" data-tour="main-workflow">
    <span class="flow-caption">工作流程</span>
    <p class="flow-mobile-position">第 {{ currentIndex + 1 }} / 5 步 · {{ currentStep.label }}</p>
    <ol class="guided-steps" aria-label="五步主流程">
      <li v-for="(step, index) in steps" :key="step.key"
        :class="['guided-step', { current: index === currentIndex }]"
        :aria-current="index === currentIndex ? 'step' : undefined">
        <i aria-hidden="true">{{ index + 1 }}</i><span>{{ step.label }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.guided-flow { display: flex; align-items: center; gap: 24px; margin: 0 0 24px; padding: 0 0 18px; border-bottom: 1px solid var(--border); }
.flow-caption { flex: 0 0 auto; color: var(--muted); font-size: 13px; }
.flow-mobile-position { display: none; margin: 0; font-size: 14px; font-weight: 600; color: var(--title); }
.guided-steps { flex: 1; display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0; padding: 0; list-style: none; }
.guided-step { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; line-height: 1.5; }
.guided-step i { display: grid; place-items: center; width: 24px; height: 24px; flex: 0 0 24px; border: 1px solid var(--border); border-radius: 50%; font-size: 12px; font-style: normal; background: var(--surface); }
.guided-step.current { color: var(--primary-dark); font-weight: 600; }
.guided-step.current i { color: var(--primary-dark); border-color: transparent; background: var(--primary-light); }
@media (max-width: 1100px) { .flow-caption { display: none; } .guided-flow { gap: 12px; } }
@media (max-width: 720px) { .guided-flow { display: block; margin-bottom: 20px; padding-bottom: 14px; } .flow-mobile-position { display: block; margin-bottom: 12px; } .guided-step span { display: none; } .guided-steps { justify-content: flex-start; gap: 16px; } }
</style>
