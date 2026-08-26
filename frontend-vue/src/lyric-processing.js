import { ConverterFactory } from 'opencc-js/core'
import { from, to } from 'opencc-js/preset'
import {
  LINE_TIMESTAMP as BRACKET_TIME,
  timestampMilliseconds,
  WORD_TIMESTAMP as WORD_TIME,
} from './lrc-timestamps.js'

const OFFSET_TAG = /^\s*\[offset:([+-]?\d+)\]\s*$/i

const toSimplifiedChinese = ConverterFactory(from.twp, to.cn)

function formatTimestamp(milliseconds) {
  const safe = Math.max(0, Math.round(milliseconds / 10) * 10)
  const minutes = Math.floor(safe / 60000)
  const seconds = Math.floor((safe % 60000) / 1000)
  const centiseconds = Math.floor((safe % 1000) / 10)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(centiseconds).padStart(2, '0')}`
}

function normalizeInput(content) {
  return String(content || '')
    .replace(/^\uFEFF/, '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.replace(/[\t ]+$/g, ''))
    .join('\n')
}

function normalizeTimestamps(content, preserveWordTiming) {
  const lineTimed = content.replace(
    BRACKET_TIME,
    (_match, ...groups) => `[${formatTimestamp(timestampMilliseconds(...groups.slice(0, 5)))}]`,
  )
  if (!preserveWordTiming) return lineTimed.replace(WORD_TIME, '')
  return lineTimed.replace(
    WORD_TIME,
    (_match, ...groups) => `<${formatTimestamp(timestampMilliseconds(...groups.slice(0, 5)))}>`,
  )
}

export function compressBlankLines(content) {
  return normalizeInput(content)
    .replace(/\n[\t ]*\n(?:[\t ]*\n)+/g, '\n\n')
    .replace(/^\s*\n|\n\s*$/g, '')
}

export function standardizeLyrics(content, { preserveWordTiming = true } = {}) {
  return compressBlankLines(normalizeTimestamps(normalizeInput(content), preserveWordTiming)).trim()
}

export function convertLyricsToSimplified(content) {
  return toSimplifiedChinese(normalizeInput(content)).trim()
}

export function adjustLyricsOffset(content, offsetMilliseconds) {
  const requestedOffset = Number(offsetMilliseconds)
  if (!Number.isFinite(requestedOffset)) throw new TypeError('歌词偏移必须是有效毫秒数')
  let embeddedOffset = 0
  const withoutOffsetTag = normalizeInput(content)
    .split('\n')
    .filter((line) => {
      const match = line.match(OFFSET_TAG)
      if (!match) return true
      embeddedOffset += Number(match[1])
      return false
    })
    .join('\n')
  const totalOffset = Math.round(requestedOffset) + embeddedOffset
  const shiftedLines = withoutOffsetTag.replace(
    BRACKET_TIME,
    (_match, ...groups) => `[${formatTimestamp(
      timestampMilliseconds(...groups.slice(0, 5)) + totalOffset,
    )}]`,
  )
  return shiftedLines.replace(
    WORD_TIME,
    (_match, ...groups) => `<${formatTimestamp(
      timestampMilliseconds(...groups.slice(0, 5)) + totalOffset,
    )}>`,
  ).trim()
}
