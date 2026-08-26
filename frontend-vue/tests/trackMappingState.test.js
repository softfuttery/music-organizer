import assert from 'node:assert/strict'
import test from 'node:test'

import {
  reviewMappingConfidence,
  sortReviewLocalItems,
} from '../src/track-mapping-state.js'

test('review files sort by mapped disc and numeric track instead of path text', () => {
  const tracks = [1, 10, 11, 12, 13, 2].map((track) => ({
    key: `track-${track}`,
    disc: 1,
    track,
  }))
  const locals = tracks.map((track) => ({
    local_path: `PARADE/${track.track}.flac`,
    track_key: track.key,
  }))

  const sorted = sortReviewLocalItems(locals, tracks)

  assert.deepEqual(
    sorted.map((local) => local.local_path),
    ['PARADE/1.flac', 'PARADE/2.flac', 'PARADE/10.flac', 'PARADE/11.flac', 'PARADE/12.flac', 'PARADE/13.flac'],
  )
})

test('review sorting follows a manually changed mapping', () => {
  const locals = [
    { local_path: 'first.flac', track_key: 'track-1' },
    { local_path: 'second.flac', track_key: 'track-2' },
  ]
  const tracks = [
    { key: 'track-1', disc: 1, track: 1 },
    { key: 'track-2', disc: 1, track: 2 },
  ]

  const sorted = sortReviewLocalItems(locals, tracks, {
    'first.flac': 'track-2',
    'second.flac': 'track-1',
  })

  assert.deepEqual(sorted.map((local) => local.local_path), ['second.flac', 'first.flac'])
})

test('legacy mappings without a score are not presented as zero percent', () => {
  assert.deepEqual(
    reviewMappingConfidence({ track_key: 'track-1' }, 'track-1'),
    { tone: 'legacy', label: '已匹配' },
  )
})
