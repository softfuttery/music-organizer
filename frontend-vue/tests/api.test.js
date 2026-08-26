import assert from 'node:assert/strict'
import test from 'node:test'

import { getStats, login, onUnauthorized } from '../src/api.js'

test('401 responses notify the global unauthorized handler', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => {
    globalThis.fetch = originalFetch
  })
  globalThis.fetch = async () => new Response(
    JSON.stringify({ error: 'authentication required' }),
    { status: 401, headers: { 'Content-Type': 'application/json' } },
  )

  const messages = []
  const unsubscribe = onUnauthorized((error) => messages.push(error.message))
  context.after(unsubscribe)

  await assert.rejects(getStats(), { status: 401 })
  assert.deepEqual(messages, ['authentication required'])
})

test('invalid login remains a form error instead of a session-expiry event', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => {
    globalThis.fetch = originalFetch
  })
  let requestCount = 0
  globalThis.fetch = async () => {
    requestCount += 1
    if (requestCount === 1) {
      return new Response(JSON.stringify({ token: 'test-csrf' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return new Response(JSON.stringify({ error: '用户名或密码错误' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  let unauthorizedCount = 0
  const unsubscribe = onUnauthorized(() => { unauthorizedCount += 1 })
  context.after(unsubscribe)

  await assert.rejects(login('admin', 'wrong'), { status: 401 })
  assert.equal(unauthorizedCount, 0)
})
