import assert from 'node:assert/strict'
import test from 'node:test'

import {
  hasTextDecodeDamage,
  highConfidenceLyricCandidate,
  highestScoredLyricCandidate,
  lyricCandidateTitle,
  lyricCandidateMatchSummary,
  lyricBuffersFromState,
  lyricQualitySummary,
  normalizeSpacedLyricText,
  parseSyncedLyrics,
  plainLyricLines,
  readPreferredLyricMode,
  rememberPreferredLyricMode,
  scrollLyricContainer,
} from '../src/lyric-editor-state.js'

test('highest scored lyric candidate is selected for automatic preview', () => {
  const candidates = [
    { provider_id: 'first', score: 0.81 },
    { provider_id: 'best', score: 1 },
    { provider_id: 'last', score: 0.92 },
  ]

  assert.equal(highestScoredLyricCandidate(candidates).provider_id, 'best')
  assert.equal(highestScoredLyricCandidate([]), null)
  assert.equal(highConfidenceLyricCandidate(candidates).provider_id, 'best')
  assert.equal(highConfidenceLyricCandidate([{ score: 0.8 }]), null)
})

test('automatic preview respects provider-priority candidate order', () => {
  const candidates = [
    { source: 'netease', provider_id: 'preferred', score: 0.92 },
    { source: 'qqmusic', provider_id: 'higher-score', score: 1 },
  ]

  assert.equal(highConfidenceLyricCandidate(candidates).provider_id, 'preferred')
})

test('lyric candidates always expose a useful display title', () => {
  assert.equal(lyricCandidateTitle({ title: '  On Your Side  ' }, 'query'), 'On Your Side')
  assert.equal(lyricCandidateTitle({ title: '' }, '  On your side  '), 'On your side')
  assert.equal(lyricCandidateTitle({}, ''), '未知标题')
})

test('lyric candidate metadata match and fetched lyric quality stay separate', () => {
  assert.equal(
    lyricCandidateMatchSummary({
      match: {
        title: 1,
        artist: 0,
        album: 1,
        duration: 0.96,
        artist_role_mismatch: true,
      },
    }),
    '标题 100% · 艺术家 0% · 专辑 100% · 时长 96% · 可能是社团/主唱字段差异',
  )
  assert.equal(
    lyricQualitySummary({
      synced: true,
      word_timed: false,
      bilingual: true,
      translation_coverage: 1,
      japanese_line_count: 20,
    }),
    '行级同步 · 日中双语（覆盖 100%）',
  )
  assert.equal(
    lyricQualitySummary({ synced: true, word_timed: true, japanese_line_count: 20 }),
    '逐字同步 · 仅日文',
  )
})

test('lyric preview handles enhanced, translated, and hour-long timestamps', () => {
  assert.deepEqual(
    parseSyncedLyrics('[01:02.30]<01:02.30>原<01:02.60>文\n[01:02.30]译文\n[1:02:03.45]长音频'),
    [
      { time: 62.3, texts: ['原文', '译文'] },
      { time: 3723.45, texts: ['长音频'] },
    ],
  )
})

test('lyric preview treats legacy colon fractions as minutes and centiseconds', () => {
  assert.deepEqual(
    parseSyncedLyrics('[00:16:40]Original\n[00:16:40]翻译\n[05:03:00]Ending'),
    [
      { time: 16.4, texts: ['Original', '翻译'] },
      { time: 303, texts: ['Ending'] },
    ],
  )
})

test('plain lyric preview removes metadata and repairs artificial character spacing', () => {
  assert.equal(
    normalizeSpacedLyricText('К у д а  ж е  т ы  п р о п а л'),
    'Куда же ты пропал',
  )
  assert.deepEqual(
    plainLyricLines('[ar:Artist]\nК у д а  ж е\n\n\nNormal words stay intact'),
    ['Куда же', '', 'Normal words stay intact'],
  )
  assert.equal(hasTextDecodeDamage('Maurits "禅" Cornelis'), false)
  assert.equal(hasTextDecodeDamage('Maurits "�" Cornelis'), true)
})

function memoryStorage() {
  const values = new Map()
  return {
    getItem(key) {
      return values.get(key) ?? null
    },
    setItem(key, value) {
      values.set(key, String(value))
    },
  }
}

test('embedded and sidecar lyric buffers remain independently accessible', () => {
  const lyrics = {
    embedded: { exists: true, content: '[00:01.00]embedded' },
    sidecar: { exists: true, content: '[00:01.00]sidecar' },
  }
  const storage = memoryStorage()

  assert.deepEqual(lyricBuffersFromState(lyrics), {
    embedded: '[00:01.00]embedded',
    sidecar: '[00:01.00]sidecar',
  })
  assert.equal(readPreferredLyricMode('Artist/Song.flac', lyrics, storage), 'embedded')
  assert.equal(rememberPreferredLyricMode('Artist/Song.flac', 'sidecar', storage), true)
  assert.equal(readPreferredLyricMode('Artist/Song.flac', lyrics, storage), 'sidecar')
})

test('lyric following scrolls only the lyric container', () => {
  let requested = null
  const container = {
    clientHeight: 200,
    scrollTop: 100,
    getBoundingClientRect() {
      return { top: 300, height: 200 }
    },
    scrollTo(options) {
      requested = options
    },
  }
  const line = {
    offsetTop: 430,
    offsetHeight: 20,
    getBoundingClientRect() {
      return { top: 430, height: 20 }
    },
  }

  assert.equal(scrollLyricContainer(container, line), true)
  assert.deepEqual(requested, { top: 140, behavior: 'smooth' })
})
