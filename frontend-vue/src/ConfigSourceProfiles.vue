<script setup>
import { FolderTree, Plus, Trash2 } from '@lucide/vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue'])

function updateProfile(index, key, value) {
  const profiles = props.modelValue.map((profile, current) => (
    current === index ? { ...profile, [key]: value } : profile
  ))
  emit('update:modelValue', profiles)
}

function addProfile() {
  emit('update:modelValue', [
    ...props.modelValue,
    {
      id: `source-${Date.now()}`,
      name: '新目录方案',
      path: '',
      discovery_mode: 'direct',
      auto_discover: false,
      import_mode: 'copy',
      move_extra_files: false,
      cleanup_source_after_import: false,
    },
  ])
}

function removeProfile(index) {
  emit('update:modelValue', props.modelValue.filter((_, current) => current !== index))
}
</script>

<template>
  <div class="source-profiles">
    <div class="source-profiles-heading">
      <div>
        <strong>来源目录方案</strong>
        <span>每个目录可以使用不同的发现层级和入库安全策略。</span>
      </div>
      <button type="button" class="config-secondary" @click="addProfile"><Plus :size="15" />新增方案</button>
    </div>

    <div v-if="!modelValue.length" class="source-profiles-empty">
      <FolderTree :size="22" />
      <span>还没有来源目录。新增方案后才能在音乐预审中选择文件夹。</span>
    </div>

    <article v-for="(profile, index) in modelValue" :key="profile.id || index" class="source-profile-card">
      <header>
        <span class="source-profile-number">{{ index + 1 }}</span>
        <input
          class="source-profile-name"
          :value="profile.name"
          aria-label="目录方案名称"
          placeholder="例如：qB 新下载"
          @input="updateProfile(index, 'name', $event.target.value)"
        >
        <button type="button" class="source-profile-remove" title="删除目录方案" @click="removeProfile(index)"><Trash2 :size="15" /></button>
      </header>

      <div class="source-profile-grid">
        <label class="wide"><span>源目录</span>
          <input :value="profile.path" placeholder="/media/incoming/music" @input="updateProfile(index, 'path', $event.target.value)">
        </label>
        <label><span>目录结构</span>
          <select :value="profile.discovery_mode" @change="updateProfile(index, 'discovery_mode', $event.target.value)">
            <option value="direct">直属目录就是专辑</option>
            <option value="artist_album">艺术家 / 专辑（两级）</option>
          </select>
        </label>
        <label><span>入库方式</span>
          <select :value="profile.import_mode" @change="updateProfile(index, 'import_mode', $event.target.value)">
            <option value="hardlink">硬链接</option>
            <option value="copy">复制</option>
            <option value="move">移动</option>
          </select>
        </label>
      </div>

      <div class="source-profile-switches">
        <label class="config-switch"><input type="checkbox" :checked="profile.auto_discover" @change="updateProfile(index, 'auto_discover', $event.target.checked)"><i></i><span>自动发现</span></label>
        <label class="config-switch"><input type="checkbox" :checked="profile.move_extra_files" @change="updateProfile(index, 'move_extra_files', $event.target.checked)"><i></i><span>转移封面等附属文件</span></label>
        <label class="config-switch danger"><input type="checkbox" :checked="profile.cleanup_source_after_import" @change="updateProfile(index, 'cleanup_source_after_import', $event.target.checked)"><i></i><span>入库后清理源文件</span></label>
      </div>
      <p v-if="profile.discovery_mode === 'artist_album'" class="source-profile-note">系统会跳过艺术家层，把第二级目录作为专辑；专辑内的 CD1/CD2 仍会合并处理。</p>
    </article>
  </div>
</template>
