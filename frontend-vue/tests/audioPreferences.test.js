import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyAudioPreferences,
  saveAudioPreferences,
} from '../src/audio-preferences.js'

test('audio volume and mute state survive player recreation', () => {
  const values = new Map()
  globalThis.window = {
    localStorage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
    },
  }

  try {
    saveAudioPreferences({ currentTarget: { volume: 0.37, muted: true } })
    const recreated = { volume: 1, muted: false }
    applyAudioPreferences({ currentTarget: recreated })
    assert.equal(recreated.volume, 0.37)
    assert.equal(recreated.muted, true)
  } finally {
    delete globalThis.window
  }
})
