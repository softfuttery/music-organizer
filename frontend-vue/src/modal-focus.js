const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let bodyLockCount = 0
let previousBodyOverflow = ''

function isFocusable(element) {
  if (!element || typeof element.focus !== 'function' || element.disabled || element.hidden) return false
  if (element.getAttribute?.('aria-hidden') === 'true') return false
  if (typeof element.getClientRects === 'function' && element.getClientRects().length === 0) return false
  return true
}

function focusWithoutScroll(element) {
  if (!element || typeof element.focus !== 'function') return
  try {
    element.focus({ preventScroll: true })
  } catch {
    element.focus()
  }
}

export function modalFocusableElements(container) {
  if (!container?.querySelectorAll) return []
  return [...container.querySelectorAll(FOCUSABLE_SELECTOR)].filter(isFocusable)
}

export function focusModal(container) {
  const target = modalFocusableElements(container)[0] || container
  focusWithoutScroll(target)
  return target
}

export function trapModalTab(event, container, activeElement = globalThis.document?.activeElement) {
  if (event?.key !== 'Tab' || !container) return false
  const focusable = modalFocusableElements(container)
  if (!focusable.length) {
    event.preventDefault()
    focusWithoutScroll(container)
    return true
  }

  const first = focusable[0]
  const last = focusable.at(-1)
  const activeInside = container.contains?.(activeElement) === true
  const target = event.shiftKey
    ? (!activeInside || activeElement === first ? last : null)
    : (!activeInside || activeElement === last ? first : null)
  if (!target) return false
  event.preventDefault()
  focusWithoutScroll(target)
  return true
}

export function lockBodyScroll() {
  const body = globalThis.document?.body
  if (!body) return () => {}
  if (bodyLockCount === 0) {
    previousBodyOverflow = body.style.overflow
    body.style.overflow = 'hidden'
  }
  bodyLockCount += 1
  let released = false
  return () => {
    if (released) return
    released = true
    bodyLockCount = Math.max(0, bodyLockCount - 1)
    if (bodyLockCount === 0) body.style.overflow = previousBodyOverflow
  }
}

export function restoreModalFocus(element, fallback = null) {
  const target = element && element.isConnected !== false ? element : fallback
  if (!target || target.isConnected === false) return
  focusWithoutScroll(target)
}
