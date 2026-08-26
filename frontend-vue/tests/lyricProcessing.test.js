import test from 'node:test'
import assert from 'node:assert/strict'

import {
  adjustLyricsOffset,
  compressBlankLines,
  convertLyricsToSimplified,
  standardizeLyrics,
} from '../src/lyric-processing.js'

test('standardizeLyrics normalizes timestamps and preserves enhanced timing by default', () => {
  const source = '\uFEFF[ar:歌手]\r\n[00:01.234]<00:01.234>歌<00:02.000>词   \r\n\r\n\r\n[1:02.9]下一行'
  assert.equal(
    standardizeLyrics(source),
    '[ar:歌手]\n[00:01.23]<00:01.23>歌<00:02.00>词\n\n[01:02.90]下一行',
  )
  assert.equal(
    standardizeLyrics(source, { preserveWordTiming: false }),
    '[ar:歌手]\n[00:01.23]歌词\n\n[01:02.90]下一行',
  )
})

test('compressBlankLines leaves at most one blank line', () => {
  assert.equal(compressBlankLines('\n\nA\n \n\n\nB\n\n'), 'A\n\nB')
})

test('convertLyricsToSimplified uses phrase-aware OpenCC conversion', () => {
  assert.equal(convertLyricsToSimplified('[00:01.00]滑鼠與軟體'), '[00:01.00]鼠标与软件')
})

test('adjustLyricsOffset bakes the offset tag into line and word timestamps', () => {
  assert.equal(
    adjustLyricsOffset('[offset:100]\n[00:01.00]<00:01.20>词<00:02.00>', 400),
    '[00:01.50]<00:01.70>词<00:02.50>',
  )
  assert.equal(adjustLyricsOffset('[00:00.20]开头', -500), '[00:00.00]开头')
})

test('legacy colon fractions are standardized before processing', () => {
  assert.equal(
    standardizeLyrics('[00:16:40]Start\n[05:03:00]<05:03:25>End'),
    '[00:16.40]Start\n[05:03.00]<05:03.25>End',
  )
  assert.equal(
    adjustLyricsOffset('[00:16:40]Start', 100),
    '[00:16.50]Start',
  )
})
