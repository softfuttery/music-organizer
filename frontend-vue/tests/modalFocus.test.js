import assert from 'node:assert/strict'
import test from 'node:test'

import {
  lockBodyScroll,
  modalFocusableElements,
  restoreModalFocus,
  trapModalTab,
} from '../src/modal-focus.js'

function focusable(name, options = {}) {
  return {
    name,
    disabled: options.disabled || false,
    hidden: options.hidden || false,
    focused: false,
    focus() { this.focused = true },
    getAttribute(attribute) {
      if (attribute === 'aria-hidden') return options.ariaHidden ? 'true' : null
      return null
    },
  }
}

function modal(elements) {
  return {
    focused: false,
    contains(element) { return elements.includes(element) },
    focus() { this.focused = true },
    querySelectorAll() { return elements },
  }
}

function tabEvent(shiftKey = false) {
  return {
    key: 'Tab',
    shiftKey,
    prevented: false,
    preventDefault() { this.prevented = true },
  }
}

test('modal focus filtering excludes unavailable controls', () => {
  const visible = focusable('visible')
  const controls = [visible, focusable('disabled', { disabled: true }), focusable('hidden', { ariaHidden: true })]
  assert.deepEqual(modalFocusableElements(modal(controls)), [visible])
})

test('modal tab handling wraps in both directions and contains outside focus', () => {
  const first = focusable('first')
  const middle = focusable('middle')
  const last = focusable('last')
  const container = modal([first, middle, last])

  const forward = tabEvent()
  assert.equal(trapModalTab(forward, container, last), true)
  assert.equal(forward.prevented, true)
  assert.equal(first.focused, true)

  const backward = tabEvent(true)
  assert.equal(trapModalTab(backward, container, first), true)
  assert.equal(last.focused, true)

  const outside = tabEvent()
  assert.equal(trapModalTab(outside, container, focusable('outside')), true)
  assert.equal(first.focused, true)

  const internal = tabEvent()
  assert.equal(trapModalTab(internal, container, middle), false)
  assert.equal(internal.prevented, false)
})

test('nested modal scroll locks restore the original body overflow once', () => {
  const originalDocument = globalThis.document
  globalThis.document = { body: { style: { overflow: 'auto' } } }
  try {
    const releaseFirst = lockBodyScroll()
    const releaseSecond = lockBodyScroll()
    assert.equal(globalThis.document.body.style.overflow, 'hidden')
    releaseFirst()
    assert.equal(globalThis.document.body.style.overflow, 'hidden')
    releaseSecond()
    assert.equal(globalThis.document.body.style.overflow, 'auto')
    releaseSecond()
    assert.equal(globalThis.document.body.style.overflow, 'auto')
  } finally {
    globalThis.document = originalDocument
  }
})

test('modal focus restoration falls back when the opener was removed', () => {
  const removedOpener = { ...focusable('removed'), isConnected: false }
  const fallback = { ...focusable('fallback'), isConnected: true }
  restoreModalFocus(removedOpener, fallback)
  assert.equal(removedOpener.focused, false)
  assert.equal(fallback.focused, true)
})
