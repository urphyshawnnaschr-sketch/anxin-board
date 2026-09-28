<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { markOnboardingComplete } from '../onboarding.js'

const emit = defineEmits(['close'])

const steps = [
  {
    key: 'welcome',
    title: '第一次用？只看这 5 件事',
    body: '不用先学会所有菜单。按当前页面最主要的动作往前走，软件会把下一步放在你眼前。',
    selector: null
  },
  {
    key: 'project-context',
    title: '始终知道自己在哪个项目',
    body: '顶部会持续显示当前项目和当前页面。切换项目后，这里也会跟着更新，避免把不同项目的状态混在一起。',
    selector: '[data-tour="project-context"]'
  },
  {
    key: 'main-workflow',
    title: '顶部 5 步只表示流程位置',
    body: '顶部 1–5 步告诉你现在走到哪里；左侧菜单只是快速跳转，不需要把两套东西都记住。',
    selector: '[data-tour="main-workflow"]'
  },
  {
    key: 'primary-action',
    title: '每页先找最主要的动作',
    body: '页面会优先突出当前建议操作。先完成主要动作，再看诊断或高级信息，通常就不会迷路。',
    selector: 'primary-action'
  },
  {
    key: 'authority',
    title: '技术信息是可选的，关键决定仍由你确认',
    body: 'SHA、诊断和证据仍可展开查看，但不要求先理解。软件不会自动替你调用 AI，也不会替你正式确认报告。',
    selector: null
  }
]

const stepIndex = ref(0)
const cardRef = ref(null)
const cardStyle = ref({})
const highlightStyle = ref({ display: 'none' })
let previousFocus = null
let shell = null
let frameId = null

const currentStep = computed(() => steps[stepIndex.value])
const isLast = computed(() => stepIndex.value === steps.length - 1)
const progressLabel = computed(() => `${stepIndex.value + 1} / ${steps.length}`)

function isRenderable(element) {
  if (!element) return false
  const rect = element.getBoundingClientRect()
  const style = window.getComputedStyle(element)
  return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'
}

function isInViewport(element) {
  if (!isRenderable(element)) return false
  const rect = element.getBoundingClientRect()
  const margin = 8
  return rect.bottom > margin
    && rect.right > margin
    && rect.top < window.innerHeight - margin
    && rect.left < window.innerWidth - margin
}

function bringTargetIntoViewport(element, forceCenter = false) {
  if (!isRenderable(element)) return false
  if (forceCenter || !isInViewport(element)) {
    element.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' })
  }
  return isInViewport(element)
}

function resolvePrimaryTarget() {
  const selectors = [
    '[data-tour="primary-action"]',
    '.content-frame .primary',
    '.content-frame .primary-button',
    '.content-frame .guided-start'
  ]
  for (const selector of selectors) {
    const candidate = [...document.querySelectorAll(selector)].find(isRenderable)
    if (candidate) return candidate
  }
  const fallback = document.querySelector('.content-frame')
  return isRenderable(fallback) ? fallback : null
}

function resolveTarget(step) {
  if (!step?.selector) return null
  if (step.selector === 'primary-action') return resolvePrimaryTarget()
  const target = document.querySelector(step.selector)
  return isRenderable(target) ? target : null
}

function stepAvailable(index) {
  const step = steps[index]
  if (!step?.selector) return true
  return bringTargetIntoViewport(resolveTarget(step))
}

function nextAvailableIndex(start, direction) {
  let candidate = start
  while (candidate >= 0 && candidate < steps.length) {
    if (stepAvailable(candidate)) return candidate
    candidate += direction
  }
  return -1
}

function finishTour() {
  markOnboardingComplete()
  emit('close')
}

function skipTour() {
  finishTour()
}

function move(direction) {
  const next = nextAvailableIndex(stepIndex.value + direction, direction)
  if (next === -1) {
    if (direction > 0) finishTour()
    return
  }
  stepIndex.value = next
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max)
}

function narrowPlacement(target, cardHeight) {
  const margin = 12
  const gap = 12
  let rect = target.getBoundingClientRect()
  const fitsBelow = () => rect.bottom + gap + cardHeight <= window.innerHeight - margin
  const fitsAbove = () => rect.top - gap - cardHeight >= margin

  if (fitsBelow()) return rect.bottom + gap
  if (fitsAbove()) return rect.top - cardHeight - gap

  const targetHeight = Math.min(rect.height, window.innerHeight)
  if (targetHeight + cardHeight + gap + margin * 2 > window.innerHeight) return null

  const desiredTop = margin + 8
  window.scrollBy({ top: rect.top - desiredTop, left: 0, behavior: 'auto' })
  rect = target.getBoundingClientRect()
  if (fitsBelow()) return rect.bottom + gap

  const desiredBottom = window.innerHeight - margin - 8
  window.scrollBy({ top: rect.bottom - desiredBottom, left: 0, behavior: 'auto' })
  rect = target.getBoundingClientRect()
  if (fitsAbove()) return rect.top - cardHeight - gap

  return null
}

function syncPosition() {
  frameId = null
  const step = currentStep.value
  const target = resolveTarget(step)
  const narrow = window.innerWidth <= 640

  if (target && isRenderable(target)) {
    if (!bringTargetIntoViewport(target)) {
      highlightStyle.value = { display: 'none' }
      return
    }

    let rect = target.getBoundingClientRect()
    const pad = 8

    if (narrow) {
      const cardHeight = cardRef.value?.getBoundingClientRect().height || 250
      const top = narrowPlacement(target, cardHeight)
      if (top == null) {
        highlightStyle.value = { display: 'none' }
        return
      }
      rect = target.getBoundingClientRect()
      cardStyle.value = {
        left: '12px',
        right: '12px',
        top: `${top}px`,
        bottom: 'auto',
        width: 'auto',
        transform: 'none'
      }
    } else {
      const cardWidth = 400
      const estimatedHeight = cardRef.value?.getBoundingClientRect().height || 250
      const gap = 18
      let left
      let top = clamp(rect.top, 18, Math.max(18, window.innerHeight - estimatedHeight - 18))

      if (window.innerWidth - rect.right >= cardWidth + gap + 18) {
        left = rect.right + gap
      } else if (rect.left >= cardWidth + gap + 18) {
        left = rect.left - cardWidth - gap
      } else {
        left = clamp(rect.left, 18, window.innerWidth - cardWidth - 18)
        if (window.innerHeight - rect.bottom >= estimatedHeight + gap + 18) top = rect.bottom + gap
        else top = clamp(rect.top - estimatedHeight - gap, 18, window.innerHeight - estimatedHeight - 18)
      }

      cardStyle.value = {
        left: `${left}px`,
        top: `${top}px`,
        width: `${cardWidth}px`,
        right: 'auto',
        bottom: 'auto',
        transform: 'none'
      }
    }

    const highlightLeft = clamp(rect.left - pad, 6, window.innerWidth - 6)
    const highlightTop = clamp(rect.top - pad, 6, window.innerHeight - 6)
    const highlightRight = clamp(rect.right + pad, 6, window.innerWidth - 6)
    const highlightBottom = clamp(rect.bottom + pad, 6, window.innerHeight - 6)
    highlightStyle.value = {
      display: 'block',
      left: `${highlightLeft}px`,
      top: `${highlightTop}px`,
      width: `${Math.max(0, highlightRight - highlightLeft)}px`,
      height: `${Math.max(0, highlightBottom - highlightTop)}px`
    }
    return
  }

  highlightStyle.value = { display: 'none' }
  cardStyle.value = narrow
    ? { left: '12px', right: '12px', bottom: '12px', width: 'auto', top: 'auto', transform: 'none' }
    : { left: '50%', top: '50%', width: '400px', right: 'auto', bottom: 'auto', transform: 'translate(-50%, -50%)' }
}

function scheduleSync() {
  if (frameId != null) cancelAnimationFrame(frameId)
  frameId = requestAnimationFrame(syncPosition)
}

function focusCard() {
  nextTick(() => {
    const focusable = cardRef.value?.querySelector('button:not([disabled])')
    focusable?.focus()
    scheduleSync()
  })
}

function trapFocus(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    finishTour()
    return
  }
  if (event.key !== 'Tab') return

  const focusable = [...(cardRef.value?.querySelectorAll('button:not([disabled])') || [])]
  if (!focusable.length) {
    event.preventDefault()
    cardRef.value?.focus()
    return
  }

  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  } else if (!cardRef.value?.contains(active)) {
    event.preventDefault()
    first.focus()
  }
}

watch(stepIndex, focusCard)

onMounted(() => {
  previousFocus = document.activeElement
  shell = document.querySelector('.app-shell')
  shell?.setAttribute('inert', '')
  window.addEventListener('keydown', trapFocus, true)
  window.addEventListener('resize', scheduleSync)
  window.addEventListener('scroll', scheduleSync, true)
  focusCard()
})

onBeforeUnmount(() => {
  if (frameId != null) cancelAnimationFrame(frameId)
  window.removeEventListener('keydown', trapFocus, true)
  window.removeEventListener('resize', scheduleSync)
  window.removeEventListener('scroll', scheduleSync, true)
  shell?.removeAttribute('inert')
  if (previousFocus instanceof HTMLElement && document.contains(previousFocus)) previousFocus.focus()
})
</script>

<template>
  <Teleport to="body">
    <div class="onboarding-root" aria-live="polite">
      <div class="onboarding-backdrop" aria-hidden="true"></div>
      <div class="onboarding-highlight" :style="highlightStyle" aria-hidden="true"></div>
      <section
        ref="cardRef"
        class="onboarding-card"
        :style="cardStyle"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        tabindex="-1"
      >
        <div class="onboarding-meta">
          <span class="onboarding-progress">{{ progressLabel }}</span>
          <button class="onboarding-skip" type="button" @click="skipTour">跳过引导</button>
        </div>
        <div class="onboarding-copy">
          <span class="onboarding-kicker">首次使用引导</span>
          <h2 id="onboarding-title">{{ currentStep.title }}</h2>
          <p>{{ currentStep.body }}</p>
        </div>
        <div class="onboarding-actions">
          <button v-if="stepIndex > 0" class="tour-secondary" type="button" @click="move(-1)">上一步</button>
          <span v-else></span>
          <button v-if="!isLast" class="tour-primary" type="button" @click="move(1)">下一步</button>
          <button v-else class="tour-primary" type="button" @click="finishTour">完成</button>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.onboarding-root{position:fixed;inset:0;z-index:1200;pointer-events:none}.onboarding-backdrop{position:absolute;inset:0;background:rgba(18,25,39,.5);pointer-events:auto}.onboarding-highlight{position:fixed;border:2px solid rgba(156,177,255,.95);border-radius:16px;box-shadow:0 0 0 4px rgba(112,137,255,.18),0 12px 36px rgba(18,25,39,.18);transition:left .18s ease,top .18s ease,width .18s ease,height .18s ease;pointer-events:none}.onboarding-card{position:fixed;max-width:420px;padding:18px;border:1px solid rgba(225,230,240,.95);border-radius:16px;background:rgba(255,255,255,.98);box-shadow:0 24px 70px rgba(12,20,35,.3);color:#172033;pointer-events:auto;transition:left .18s ease,top .18s ease,transform .18s ease}.onboarding-meta{display:flex;justify-content:space-between;align-items:center;gap:12px}.onboarding-progress{font-size:11px;font-weight:900;letter-spacing:.08em;color:#6072c7}.onboarding-skip{border:0;background:transparent;color:#738096;font:inherit;font-size:11px;font-weight:800;cursor:pointer}.onboarding-copy{margin-top:14px}.onboarding-kicker{font-size:10px;font-weight:900;letter-spacing:.11em;color:#7887d0}.onboarding-copy h2{margin:5px 0 8px;font-size:21px;line-height:1.3;letter-spacing:-.015em}.onboarding-copy p{margin:0;color:#657186;font-size:13px;line-height:1.7}.onboarding-actions{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:18px;padding-top:14px;border-top:1px solid #edf0f5}.tour-primary,.tour-secondary{min-height:38px;padding:8px 14px;border-radius:10px;font:inherit;font-size:12px;font-weight:850;cursor:pointer}.tour-primary{border:1px solid #5268e8;background:#5268e8;color:#fff}.tour-secondary{border:1px solid #d8deea;background:#fff;color:#526079}.tour-primary:focus-visible,.tour-secondary:focus-visible,.onboarding-skip:focus-visible{outline:3px solid rgba(82,104,232,.28);outline-offset:2px}@media(max-width:640px){.onboarding-card{max-width:none;border-radius:16px;padding:16px}.onboarding-copy h2{font-size:19px}.onboarding-copy p{font-size:12px}.onboarding-highlight{border-radius:13px}.tour-primary,.tour-secondary{min-height:42px}}
</style>
