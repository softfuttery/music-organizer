<script setup>
import { onMounted, ref, watch } from 'vue'
import { ArrowLeft, ArrowRight, Search } from '@lucide/vue'
import { getHistory } from './api'

const props = defineProps({ refreshKey: { type: Number, default: 0 } })
const data = ref({ items: [], total: 0, page: 1, query: '', has_prev: false, has_next: false })
const query = ref('')
const loading = ref(false)
const error = ref('')

const modeLabels = { manual_scan: '手动全量整理', qb_poll: 'qB 增量整理' }
const statusLabels = { success: '成功', failed: '失败', skipped: '已跳过', cancelled: '已取消' }

function normalizeTime(value) {
  return String(value || '').replace('T', ' ')
}

async function load(page = data.value.page || 1) {
  loading.value = true
  try {
    data.value = await getHistory(page, query.value.trim())
    query.value = data.value.query || ''
    const params = new URLSearchParams()
    if (data.value.page > 1) params.set('page', String(data.value.page))
    if (query.value) params.set('q', query.value)
    window.history.replaceState({}, '', `/history${params.size ? `?${params}` : ''}`)
    error.value = ''
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

function submitSearch() {
  load(1)
}

watch(() => props.refreshKey, () => load())
onMounted(() => {
  const params = new URLSearchParams(window.location.search)
  query.value = params.get('q') || ''
  load(Math.max(Number(params.get('page')) || 1, 1))
})
</script>

<template>
  <section class="history-workspace">
    <div class="history-commandbar card">
      <div><strong>{{ data.total.toLocaleString() }}</strong><span>条整理与任务记录</span></div>
      <form role="search" @submit.prevent="submitSearch">
        <Search :size="16" />
        <input v-model="query" type="search" placeholder="搜索路径、任务或消息" aria-label="搜索历史记录">
        <button type="submit" :disabled="loading">搜索</button>
      </form>
    </div>

    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <div class="history-list card" :class="{ loading }">
      <div class="history-table-wrap">
        <table class="history-table">
          <thead><tr><th>ID</th><th>时间</th><th>模式</th><th>状态</th><th>源文件</th><th>目标文件</th><th>消息</th></tr></thead>
          <tbody>
            <tr v-for="item in data.items" :key="`${item.record_type}:${item.id}`">
              <td data-label="ID"><span class="history-id">#{{ item.record_type === 'job' ? 'J' : '' }}{{ item.id }}</span></td>
              <td data-label="时间"><time>{{ normalizeTime(item.created_at) }}</time></td>
              <td data-label="模式"><strong>{{ modeLabels[item.mode] || item.mode }}</strong></td>
              <td data-label="状态"><span class="history-status" :class="item.status">{{ statusLabels[item.status] || item.status }}</span></td>
              <td data-label="源文件"><code>{{ item.record_type === 'job' ? '任务级记录' : item.source_path }}</code></td>
              <td data-label="目标文件"><code>{{ item.record_type === 'job' ? '—' : item.target_path }}</code></td>
              <td data-label="消息"><span>{{ item.message }}</span></td>
            </tr>
            <tr v-if="!data.items.length && !loading"><td colspan="7" class="empty-cell">暂无记录</td></tr>
          </tbody>
        </table>
      </div>
      <footer class="history-pagination">
        <button type="button" :disabled="!data.has_prev || loading" @click="load(data.page - 1)"><ArrowLeft :size="15" />上一页</button>
        <span>第 {{ data.page }} 页</span>
        <button type="button" :disabled="!data.has_next || loading" @click="load(data.page + 1)">下一页<ArrowRight :size="15" /></button>
      </footer>
    </div>
  </section>
</template>
