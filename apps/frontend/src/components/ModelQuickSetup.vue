<script setup>
import { ref } from 'vue'
import DeepSeekConnectionSettingsCard from './DeepSeekConnectionSettingsCard.vue'

const disclosure = ref(null)
function closeSettings(event) {
  if (event.key !== 'Escape' || !disclosure.value?.open) return
  disclosure.value.open = false
  disclosure.value.querySelector('summary')?.focus()
  event.stopPropagation()
}
</script>

<template>
  <details ref="disclosure" class="quick-model" @keydown="closeSettings">
    <summary>
      <span>模型设置</span><span class="quick-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="quick-body">
      <h2>分析使用的模型</h2>
      <p>查看连接状态，或更换本次分析使用的模型。</p>
      <DeepSeekConnectionSettingsCard compact />
    </div>
  </details>
</template>

<style scoped>
.quick-model {
  position: relative;
  font-size: var(--ui-text-body);
}

.quick-model summary {
  min-height: 38px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text);
  background: #fff;
  cursor: pointer;
  list-style: none;
  white-space: nowrap;
  font-weight: 600;
}

.quick-model summary::-webkit-details-marker {
  display: none;
}

.quick-model summary:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 3px;
}

.quick-chevron {
  color: var(--muted);
}

.quick-model[open] .quick-chevron {
  transform: rotate(180deg);
}

.quick-body {
  position: absolute;
  z-index: 40;
  right: 0;
  top: calc(100% + 10px);
  width: 480px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 110px);
  overflow: auto;
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 8px 32px #1d1d1f14;
}

.quick-body h2 {
  margin: 0 0 6px;
  font-size: var(--ui-text-section);
  line-height: 1.5;
  font-weight: var(--ui-weight-strong);
  color: var(--title);
}

.quick-body > p {
  margin: 0 0 16px;
  font-size: var(--ui-text-helper);
  line-height: 1.6;
  color: var(--muted);
}

@media (max-width: 720px) {
  .quick-body {
    position: fixed;
    left: 16px;
    right: 16px;
    top: 64px;
    width: auto;
    border-radius: 12px;
    box-shadow: 0 8px 32px #1d1d1f14;
  }
}
</style>
