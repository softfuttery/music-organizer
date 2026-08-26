import { onBeforeUnmount } from 'vue'

export function useAdaptivePolling(task, getDelay) {
  let timer
  let started = false
  let running = false

  function clearTimer() {
    window.clearTimeout(timer)
    timer = undefined
  }

  function schedule(delay = getDelay()) {
    clearTimer()
    if (started && !document.hidden) {
      timer = window.setTimeout(tick, delay)
    }
  }

  async function tick() {
    if (!started || document.hidden) return
    if (running) {
      schedule()
      return
    }
    running = true
    try {
      await task()
    } finally {
      running = false
      schedule()
    }
  }

  function handleVisibilityChange() {
    if (document.hidden) clearTimer()
    else schedule(0)
  }

  function start({ immediate = true } = {}) {
    if (started) return
    started = true
    document.addEventListener('visibilitychange', handleVisibilityChange)
    schedule(immediate ? 0 : getDelay())
  }

  function stop() {
    started = false
    clearTimer()
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  }

  onBeforeUnmount(stop)
  return { start, stop, schedule }
}
