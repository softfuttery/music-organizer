import { computed, nextTick, ref } from 'vue'

import {
  parseSyncedLyrics,
  plainLyricLines,
  scrollLyricContainer,
} from './lyric-editor-state.js'
import {
  adjustLyricsOffset,
  compressBlankLines,
  convertLyricsToSimplified,
  standardizeLyrics,
} from './lyric-processing.js'

const SCROLL_KEYS = new Set([
  'ArrowUp',
  'ArrowDown',
  'PageUp',
  'PageDown',
  'Home',
  'End',
  ' ',
])

const TAB_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'Home', 'End'])

export function useLyricEditor({
  content,
  activePanel,
  list,
  audio,
  drawer,
  feedback,
  processedMessage = '尚未保存。',
  onContentChanged = () => {},
}) {
  const preserveWordTiming = ref(true)
  const lyricOffset = ref(0)
  const playbackTime = ref(0)
  const lastActiveLyricIndex = ref(-1)
  const lyricAutoFollow = ref(true)

  const parsedLyrics = computed(() => parseSyncedLyrics(content.value))
  const unsyncedLyricLines = computed(() => plainLyricLines(content.value))
  const activeLyricIndex = computed(() => {
    let active = -1
    parsedLyrics.value.forEach((line, index) => {
      if (line.time <= playbackTime.value + 0.08) active = index
    })
    return active
  })

  function activeLine() {
    return list.value?.querySelector('.active')
  }

  async function syncLyrics(event) {
    playbackTime.value = Number(event.currentTarget?.currentTime || 0)
    if (activeLyricIndex.value === lastActiveLyricIndex.value) return
    lastActiveLyricIndex.value = activeLyricIndex.value
    if (activePanel.value !== 'preview' || !lyricAutoFollow.value) return
    await nextTick()
    scrollLyricContainer(list.value, activeLine())
  }

  function seekLyrics(time) {
    if (!audio.value) return
    audio.value.currentTime = time
    audio.value.play().catch(() => {})
  }

  async function selectPanel(panel) {
    activePanel.value = panel
    await nextTick()
    drawer.value?.scrollTo({ top: 0, behavior: 'auto' })
    if (panel !== 'preview' || !lyricAutoFollow.value) return
    scrollLyricContainer(list.value, activeLine(), 'auto')
  }

  async function resumeLyricFollow() {
    lyricAutoFollow.value = true
    await nextTick()
    scrollLyricContainer(list.value, activeLine())
  }

  function pauseLyricFollow() {
    lyricAutoFollow.value = false
  }

  function resetLyricPlayback({ autoFollow } = {}) {
    playbackTime.value = 0
    lastActiveLyricIndex.value = -1
    if (typeof autoFollow === 'boolean') lyricAutoFollow.value = autoFollow
  }

  function handleLyricScrollKey(event) {
    if (SCROLL_KEYS.has(event.key)) pauseLyricFollow()
  }

  async function handlePanelTabKey(event, panels) {
    if (!TAB_KEYS.has(event.key) || !panels.length) return
    event.preventDefault()
    const current = Math.max(0, panels.indexOf(activePanel.value))
    const next = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? panels.length - 1
        : (current + (event.key === 'ArrowRight' ? 1 : -1) + panels.length) % panels.length
    await selectPanel(panels[next])
    event.currentTarget
      ?.closest('[role="tablist"]')
      ?.querySelectorAll('[role="tab"]')
      ?.[next]
      ?.focus()
  }

  function processLyrics(action) {
    if (!content.value.trim()) {
      feedback.value = { type: 'info', text: '当前没有可处理的歌词。' }
      return
    }
    try {
      const processors = {
        standard: () => standardizeLyrics(content.value, {
          preserveWordTiming: preserveWordTiming.value,
        }),
        blanks: () => compressBlankLines(content.value),
        simplified: () => convertLyricsToSimplified(content.value),
        offset: () => adjustLyricsOffset(content.value, lyricOffset.value),
      }
      content.value = processors[action]()
      onContentChanged()
      const labels = {
        standard: preserveWordTiming.value
          ? '已转换为标准 LRC，并保留逐字时间。'
          : '已转换为标准 LRC，并展开为普通行时间。',
        blanks: '已压缩连续空白行。',
        simplified: '已使用 OpenCC 转换为简体中文。',
        offset: `已把全部行与逐字时间调整 ${Number(lyricOffset.value) >= 0 ? '+' : ''}${Math.round(Number(lyricOffset.value))} ms。`,
      }
      feedback.value = {
        type: 'info',
        text: `${labels[action]} ${processedMessage}`,
      }
    } catch (processingError) {
      feedback.value = { type: 'error', text: processingError.message }
    }
  }

  return {
    activeLyricIndex,
    handleLyricScrollKey,
    handlePanelTabKey,
    lyricAutoFollow,
    lyricOffset,
    parsedLyrics,
    pauseLyricFollow,
    preserveWordTiming,
    processLyrics,
    resetLyricPlayback,
    resumeLyricFollow,
    seekLyrics,
    selectPanel,
    syncLyrics,
    unsyncedLyricLines,
  }
}
