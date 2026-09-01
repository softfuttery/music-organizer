import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, test, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  testMagicPush: vi.fn(),
}))

vi.mock('../src/api.js', () => api)

import ConfigWorkspace from '../src/ConfigWorkspace.vue'

const initialValues = {
  paths_mapping: '/incoming => /library',
  review_enabled: true,
  review_source_profiles: [
    {
      id: 'qb',
      name: 'qB 新下载',
      path: '/media/library/music',
      discovery_mode: 'direct',
      auto_discover: true,
      import_mode: 'hardlink',
      move_extra_files: true,
      cleanup_source_after_import: true,
    },
  ],
}

beforeEach(() => {
  api.getConfig.mockResolvedValue({ values: initialValues, saved: {} })
  api.saveConfig.mockImplementation(async () => ({
    values: initialValues,
    saved: {},
    message: 'saved',
  }))
})

test('directory profiles are edited as cards and submitted as structured JSON', async () => {
  const wrapper = mount(ConfigWorkspace)
  await flushPromises()

  expect(wrapper.text()).toContain('来源目录方案')
  expect(wrapper.get('.source-profile-name').element.value).toBe('qB 新下载')
  expect(wrapper.text()).not.toContain('qBittorrent 主动联动')

  await wrapper.get('.source-profiles-heading button').trigger('click')
  const cards = wrapper.findAll('.source-profile-card')
  expect(cards).toHaveLength(2)
  await cards[1].get('.source-profile-name').setValue('旧音乐库')
  await cards[1].find('input[placeholder="/media/incoming/music"]').setValue('/media/music/unsorted')
  await cards[1].find('select').setValue('artist_album')
  await wrapper.get('form').trigger('submit')
  await flushPromises()

  const payload = api.saveConfig.mock.calls[0][0]
  const profiles = JSON.parse(payload.get('review_source_profiles'))
  expect(profiles[1]).toMatchObject({
    name: '旧音乐库',
    path: '/media/music/unsorted',
    discovery_mode: 'artist_album',
    import_mode: 'copy',
    cleanup_source_after_import: false,
  })
})
