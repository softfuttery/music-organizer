import assert from 'node:assert/strict'
import test from 'node:test'

import {
  beginPendingItem,
  createLatestRequestGate,
  finishPendingItem,
  reconcilePolledItem,
} from '../src/requestState.js'

test('only the latest view request may update state', () => {
  const gate = createLatestRequestGate()
  const older = gate.begin()
  const newer = gate.begin()

  assert.equal(gate.isCurrent(older), false)
  assert.equal(gate.isCurrent(newer), true)
})

test('one item finishing does not clear another pending item', () => {
  const first = beginPendingItem(new Map(), 10)
  const second = beginPendingItem(first.next, 20)
  const afterFirst = finishPendingItem(second.next, 10, first.token)

  assert.equal(afterFirst.has(10), false)
  assert.equal(afterFirst.has(20), true)
})

test('an older request cannot clear a newer request for the same item', () => {
  const older = beginPendingItem(new Map(), 10)
  const newer = beginPendingItem(older.next, 10)
  const afterOlder = finishPendingItem(newer.next, 10, older.token)

  assert.equal(afterOlder.get(10).has(newer.token), true)
  assert.equal(finishPendingItem(afterOlder, 10, newer.token).has(10), false)
})

test('a newer request finishing first keeps an older request busy', () => {
  const older = beginPendingItem(new Map(), 10)
  const newer = beginPendingItem(older.next, 10)
  const afterNewer = finishPendingItem(newer.next, 10, newer.token)

  assert.equal(afterNewer.has(10), true)
  assert.equal(afterNewer.get(10).has(older.token), true)
  assert.equal(finishPendingItem(afterNewer, 10, older.token).has(10), false)
})

test('poll refresh reuses the open item and preserves its lyric state', () => {
  const openItem = { id: 7, status: 'needs_review', lyrics: { 'song.flac': { status: 'selected' } } }
  const refreshed = reconcilePolledItem(
    [{ id: 7, status: 'ready', lyrics: {} }, { id: 8, status: 'ready', lyrics: {} }],
    openItem,
    ['lyrics'],
  )

  assert.equal(refreshed[0], openItem)
  assert.equal(openItem.status, 'ready')
  assert.equal(openItem.lyrics['song.flac'].status, 'selected')
})
