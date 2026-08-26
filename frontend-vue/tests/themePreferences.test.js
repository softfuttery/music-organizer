import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyTheme,
  loadThemePreference,
  resolvedTheme,
  saveThemePreference,
  watchSystemTheme,
} from '../src/theme-preferences.js'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
}

test('theme preference defaults safely and persists manual choices', () => {
  const storage = memoryStorage()
  assert.equal(loadThemePreference(storage), 'system')
  assert.equal(saveThemePreference('dark', storage), 'dark')
  assert.equal(loadThemePreference(storage), 'dark')
  assert.equal(saveThemePreference('invalid', storage), 'system')
})

test('system theme resolves from prefers-color-scheme while manual mode wins', () => {
  assert.equal(resolvedTheme('system', { matches: true }), 'dark')
  assert.equal(resolvedTheme('system', { matches: false }), 'light')
  assert.equal(resolvedTheme('light', { matches: true }), 'light')
})

test('applying a theme updates the document and browser chrome color', () => {
  const themeColor = { value: '', setAttribute: (_name, value) => { themeColor.value = value } }
  const documentObject = {
    documentElement: { dataset: {}, style: {} },
    querySelector: () => themeColor,
  }

  assert.equal(applyTheme('system', documentObject, { matches: true }), 'dark')
  assert.equal(documentObject.documentElement.dataset.theme, 'dark')
  assert.equal(documentObject.documentElement.dataset.themePreference, 'system')
  assert.equal(documentObject.documentElement.style.colorScheme, 'dark')
  assert.equal(themeColor.value, '#111315')
})

test('system theme watcher can be detached', () => {
  let listener
  const media = {
    matches: false,
    addEventListener: (_event, callback) => { listener = callback },
    removeEventListener: (_event, callback) => {
      if (listener === callback) listener = null
    },
  }
  const values = []
  const stop = watchSystemTheme((value) => values.push(value), media)
  media.matches = true
  listener()
  assert.deepEqual(values, ['dark'])
  stop()
  assert.equal(listener, null)
})
