import {
  LINE_TIMESTAMP,
  timestampMilliseconds,
  WORD_TIMESTAMP,
} from './lrc-timestamps.js'

const PREFERENCE_KEY = 'music-organizer.library-lyric-mode.v1'
const MODES = new Set(['embedded', 'sidecar'])
const METADATA_LINE = /^\s*\[(?:ar|ti|al|by|offset|re|ve|length|id):[^\]]*\]\s*$/i

function browserStorage() {
  try {
    return globalThis.window?.localStorage || globalThis.localStorage || null
  } catch {
    return null
  }
}

function readPreferences(storage) {
  try {
    const value = JSON.parse(storage?.getItem(PREFERENCE_KEY) || '{}')
    return value && typeof value === 'object' ? value : {}
  } catch {
    return {}
  }
}

export function lyricBuffersFromState(lyrics = {}) {
  return {
    embedded: String(lyrics.embedded?.content || ''),
    sidecar: String(lyrics.sidecar?.content || ''),
  }
}

export function highestScoredLyricCandidate(candidates = []) {
  return (candidates || []).reduce((best, candidate) => {
    if (!best) return candidate
    return Number(candidate?.score || 0) > Number(best?.score || 0)
      ? candidate
      : best
  }, null)
}

export function highConfidenceLyricCandidate(candidates = [], minimumScore = 0.9) {
  return (candidates || []).find(
    (candidate) => Number(candidate?.score || 0) >= Number(minimumScore),
  ) || null
}

export function lyricCandidateTitle(candidate = {}, fallback = '') {
  return String(candidate?.title || '').trim()
    || String(fallback || '').trim()
    || '未知标题'
}

function percentage(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : ''
}

export function lyricCandidateMatchSummary(candidate = {}) {
  const match = candidate?.match || {}
  const parts = []
  if (match.title != null) parts.push(`标题 ${percentage(match.title)}`)
  if (match.artist != null) parts.push(`艺术家 ${percentage(match.artist)}`)
  if (match.album != null) parts.push(`专辑 ${percentage(match.album)}`)
  if (match.duration != null) parts.push(`时长 ${percentage(match.duration)}`)
  if (match.artist_role_mismatch) parts.push('可能是社团/主唱字段差异')
  return parts.join(' · ')
}

export function lyricQualitySummary(quality = {}) {
  const parts = []
  if (quality.synced) parts.push(quality.word_timed ? '逐字同步' : '行级同步')
  else parts.push('无时间轴')
  if (quality.bilingual) {
    const coverage = percentage(quality.translation_coverage)
    parts.push(`日中双语${coverage ? `（覆盖 ${coverage}）` : ''}`)
  } else if (Number(quality.japanese_line_count || 0) > 0) {
    parts.push('仅日文')
  }
  return parts.join(' · ')
}

export function hasTextDecodeDamage(...values) {
  return values.some((value) => String(value || '').includes('\uFFFD'))
}

export function parseSyncedLyrics(content) {
  const entries = []
  for (const rawLine of String(content || '').replace(/\r\n?/g, '\n').split('\n')) {
    const timestamps = [...rawLine.matchAll(LINE_TIMESTAMP)]
    if (!timestamps.length) continue
    const text = rawLine.replace(LINE_TIMESTAMP, '').replace(WORD_TIMESTAMP, '').trim()
    if (!text) continue
    for (const match of timestamps) {
      entries.push({ time: timestampMilliseconds(...match.slice(1, 6)) / 1000, text })
    }
  }
  entries.sort((left, right) => left.time - right.time)
  const grouped = []
  for (const entry of entries) {
    const current = grouped[grouped.length - 1]
    if (current && Math.abs(current.time - entry.time) < 0.01) {
      if (!current.texts.includes(entry.text)) current.texts.push(entry.text)
    } else {
      grouped.push({ time: entry.time, texts: [entry.text] })
    }
  }
  return grouped
}

export function normalizeSpacedLyricText(value) {
  const text = String(value || '').trim()
  const tokens = text.split(/\s+/u).filter(Boolean)
  const singleCharacterTokens = tokens.filter((token) => [...token].length === 1)
  if (
    tokens.length < 6
    || singleCharacterTokens.length / tokens.length < 0.8
    || !/\s{2,}/u.test(text)
  ) return text
  return text.split(/\s{2,}/u).map((group) => {
    const characters = group.trim().split(/\s+/u)
    return characters.every((token) => [...token].length === 1)
      ? characters.join('')
      : group.trim()
  }).join(' ')
}

export function plainLyricLines(content) {
  const lines = String(content || '')
    .replace(/^\uFEFF/, '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .filter((line) => !METADATA_LINE.test(line))
    .map((line) => normalizeSpacedLyricText(
      line.replace(LINE_TIMESTAMP, '').replace(WORD_TIMESTAMP, ''),
    ))
  while (lines[0] === '') lines.shift()
  while (lines[lines.length - 1] === '') lines.pop()
  return lines.filter((line, index) => line !== '' || lines[index - 1] !== '')
}

export function readPreferredLyricMode(path, lyrics = {}, storage = browserStorage()) {
  const stored = readPreferences(storage)[String(path || '')]
  if (MODES.has(stored)) return stored
  if (lyrics.embedded?.exists || lyrics.embedded?.content) return 'embedded'
  if (lyrics.sidecar?.exists || lyrics.sidecar?.content) return 'sidecar'
  return 'embedded'
}

export function rememberPreferredLyricMode(path, mode, storage = browserStorage()) {
  if (!path || !MODES.has(mode) || !storage) return false
  try {
    storage.setItem(PREFERENCE_KEY, JSON.stringify({
      ...readPreferences(storage),
      [path]: mode,
    }))
    return true
  } catch {
    return false
  }
}

export function scrollLyricContainer(container, line, behavior = 'smooth') {
  if (!container || !line) return false
  const viewport = Number(container.clientHeight || 0)
  let lineHeight = Number(line.offsetHeight || 0)
  let lineTop = Number(line.offsetTop || 0)
  if (
    typeof container.getBoundingClientRect === 'function'
    && typeof line.getBoundingClientRect === 'function'
  ) {
    const containerRect = container.getBoundingClientRect()
    const lineRect = line.getBoundingClientRect()
    lineHeight = Number(lineRect.height || lineHeight)
    lineTop = Number(container.scrollTop || 0) + Number(lineRect.top - containerRect.top)
  }
  const target = Math.max(0, lineTop - (viewport - lineHeight) / 2)
  if (typeof container.scrollTo === 'function') {
    container.scrollTo({ top: target, behavior })
  } else {
    container.scrollTop = target
  }
  return true
}
