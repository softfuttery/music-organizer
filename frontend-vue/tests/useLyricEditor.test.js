import assert from 'node:assert/strict'
import test from 'node:test'

import { ref } from 'vue'

import { useLyricEditor } from '../src/useLyricEditor.js'

function editor(overrides = {}) {
  const content = ref('[00:01.00]第一行\n[00:02.00]第二行')
  const activePanel = ref('preview')
  const activeLine = {
    offsetHeight: 20,
    offsetTop: 80,
  }
  const container = {
    clientHeight: 100,
    scrollTo(value) {
      this.lastScroll = value
    },
    querySelector() {
      return activeLine
    },
  }
  const audio = {
    currentTime: 0,
    play: () => Promise.resolve(),
  }
  const feedback = ref({ type: '', text: '' })
  const result = useLyricEditor({
    content,
    activePanel,
    list: ref(container),
    audio: ref(audio),
    drawer: ref(container),
    feedback,
    ...overrides,
  })
  return { activePanel, audio, container, content, feedback, ...result }
}

test('shared lyric editor processes content and reports unsaved state', () => {
  const changed = []
  const state = editor({
    onContentChanged: () => changed.push(true),
    processedMessage: '保存后生效。',
  })

  state.content.value = '[00:01:25]繁體\n\n\n第二行'
  state.processLyrics('standard')

  assert.match(state.content.value, /^\[00:01\.25\]繁體/)
  assert.deepEqual(changed, [true])
  assert.match(state.feedback.value.text, /保存后生效/)
})

test('shared lyric editor follows playback and seeks audio', async () => {
  const state = editor()

  await state.syncLyrics({ currentTarget: { currentTime: 1.5 } })
  assert.equal(state.activeLyricIndex.value, 0)
  assert.equal(state.container.lastScroll.behavior, 'smooth')

  state.seekLyrics(2)
  assert.equal(state.audio.currentTime, 2)
  state.pauseLyricFollow()
  assert.equal(state.lyricAutoFollow.value, false)
  state.resetLyricPlayback({ autoFollow: true })
  assert.equal(state.activeLyricIndex.value, -1)
  assert.equal(state.lyricAutoFollow.value, true)
})

test('shared lyric tabs support arrow-key navigation and focus', async () => {
  const state = editor()
  const focused = []
  const tabs = [0, 1, 2].map((index) => ({
    focus: () => focused.push(index),
  }))
  const event = {
    key: 'ArrowRight',
    preventDefault() {
      this.prevented = true
    },
    currentTarget: {
      closest: () => ({ querySelectorAll: () => tabs }),
    },
  }

  await state.handlePanelTabKey(event, ['preview', 'lyrics', 'decision'])

  assert.equal(event.prevented, true)
  assert.equal(state.activePanel.value, 'lyrics')
  assert.deepEqual(focused, [1])
})
