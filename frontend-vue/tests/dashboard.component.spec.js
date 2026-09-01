import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, test, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getDashboard: vi.fn(),
  getHealth: vi.fn(),
  getSession: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
  postAction: vi.fn(),
}))

vi.mock('../src/api.js', () => api)

import AppModern from '../src/AppModern.vue'

beforeEach(() => {
  window.history.replaceState({}, '', '/')
  localStorage.clear()
  api.getSession.mockResolvedValue({ authenticated: true, username: 'admin' })
  api.getHealth.mockResolvedValue({ status: 'ok', worker: 'ok', review_worker: 'disabled' })
  api.getDashboard.mockResolvedValue({
    total_files: 12,
    organized_files: 9,
    review_active: 2,
    paths_mapping: {},
    mode: 'hardlink',
    last_run: null,
    recent: [],
    qb_needs_attention: [],
    source_revision: 'test-revision',
    qb_connection: {
      status: 'failed',
      last_attempt_at: '2026-08-27T22:00:28',
      last_success_at: '',
      last_error: 'connection timed out',
    },
    job_status: { status: 'idle', running: false },
    health: {
      status: 'ok',
      worker: 'ok',
      review_worker: 'disabled',
      source_revision: 'test-revision',
    },
  })
})

test('authenticated dashboard refresh uses one consistent snapshot request', async () => {
  const wrapper = mount(AppModern)
  await flushPromises()

  expect(api.getDashboard).toHaveBeenCalledTimes(1)
  expect(wrapper.text()).toContain('12')
  expect(wrapper.text()).toContain('test-revisio')
  expect(wrapper.text()).toContain('空闲')
  expect(wrapper.text()).toContain('qB 连接失败')
  expect(wrapper.text()).toContain('connection timed out')

  wrapper.unmount()
})
