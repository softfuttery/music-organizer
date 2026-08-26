<script setup>
defineProps({
  items: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  jobRunning: { type: Boolean, default: false },
})

defineEmits(['retry'])
</script>

<template>
  <article v-if="items.length" class="card activity-card">
    <div class="card-heading">
      <div>
        <span class="eyebrow">ACTION REQUIRED</span>
        <h2>qBittorrent 待人工处理</h2>
      </div>
      <span class="mode-pill">{{ items.length }} 项</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>种子</th><th>失败次数</th><th>最近错误</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in items" :key="item.torrent_hash">
            <td data-label="种子">{{ item.name || item.torrent_hash }}</td>
            <td data-label="失败次数">{{ item.attempt_count }}</td>
            <td data-label="最近错误">{{ item.message || '整理失败' }}</td>
            <td data-label="操作">
              <button
                class="button secondary"
                type="button"
                :disabled="busy || jobRunning"
                @click="$emit('retry', item.torrent_hash)"
              >重新尝试</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </article>
</template>
