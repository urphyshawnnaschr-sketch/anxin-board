export const ONBOARDING_STORAGE_KEY = 'rd-agent:uiux-v2-1:onboarding-complete'
export const ONBOARDING_REPLAY_EVENT = 'rd-agent:uiux-v2-1:onboarding-replay'

export function isOnboardingComplete() {
  try {
    return window.localStorage.getItem(ONBOARDING_STORAGE_KEY) === '1'
  } catch {
    // Onboarding must never block the product when browser storage is unavailable.
    return true
  }
}

export function markOnboardingComplete() {
  try {
    window.localStorage.setItem(ONBOARDING_STORAGE_KEY, '1')
  } catch {
    // Local persistence is optional; the business product remains usable without it.
  }
}

export function requestOnboardingReplay() {
  window.dispatchEvent(new CustomEvent(ONBOARDING_REPLAY_EVENT))
}
