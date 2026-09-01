<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  CircleStop,
  Clock3,
  Database,
  FolderSync,
  Gauge,
  HardDrive,
  History,
  LayoutDashboard,
  LibraryBig,
  LogOut,
  Music2,
  Play,
  RadioTower,
  RefreshCw,
  Settings2,
  Sparkles,
} from '@lucide/vue'
import BrandMark from './BrandMark.vue'
import ConfigWorkspace from './ConfigWorkspace.vue'
import HistoryWorkspace from './HistoryWorkspace.vue'
import ThemePicker from './ThemePicker.vue'
import ReviewWorkspace from './ReviewWorkspace.vue'
import LibraryWorkspace from './LibraryWorkspace.vue'
import QbAttentionCard from './QbAttentionCard.vue'
import { useAdaptivePolling } from './useAdaptivePolling'
import {
  applyTheme,
  loadThemePreference,
  saveThemePreference,
  watchSystemTheme,
} from './theme-preferences'
import {
  getDashboard,
  getHealth,
  getSession,
  login,
  logout,
  onUnauthorized,
  postAction,
} from './api'

const auth = ref({
  loading: true,
  authenticated: false,
  username: '',
})
const credentials = ref({ username: 'admin', password: '' })
const stats = ref(null)
const job = ref(null)
const health = ref(null)
const loading = ref(false)
const loginPending = ref(false)
const actionPending = ref(false)
const notice = ref('')
const error = ref('')
const loginError = ref('')
const themePreference = ref(loadThemePreference())
const workspaceRefreshKey = ref(0)
let clockTimer
let stopSystemThemeWatch = () => {}
let stopUnauthorizedWatch = () => {}
let refreshRunning = false
const isReview = window.location.pathname === '/review'
const isLibrary = window.location.pathname === '/library'
const isHistory = window.location.pathname === '/history'
const isConfig = window.location.pathname === '/config'

const statusLabel = computed(() => {
  if (!job.value) return '读取中'
  const labels = {
    idle: '空闲',
    queued: '已排队',
    running: '执行中',
    succeeded: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '被中断',
  }
  return labels[job.value.status] || job.value.status
})

const statusTone = computed(() => {
  if (job.value?.status === 'failed') return 'danger'
  if (job.value?.status === 'running' || job.value?.status === 'queued') return 'active'
  if (job.value?.status === 'succeeded') return 'success'
  return 'muted'
})

const webOnline = computed(
  () => health.value?.web === 'ok' || health.value?.status === 'ok',
)
const workerOnline = computed(() => Boolean(stats.value?.worker_running))
const qbConnection = computed(() => stats.value?.qb_connection || {})
const qbConnectionLabel = computed(() => {
  const labels = {
    ok: 'qB 连接正常',
    failed: 'qB 连接失败',
    unknown: 'qB 尚未检查',
  }
  return labels[qbConnection.value.status] || 'qB 状态未知'
})
const qbConnectionTone = computed(() => {
  if (qbConnection.value.status === 'ok') return 'success'
  if (qbConnection.value.status === 'failed') return 'danger'
  return 'muted'
})
const qbConnectionDetail = computed(() => {
  if (qbConnection.value.status === 'failed') {
    return qbConnection.value.last_error || '未返回具体错误'
  }
  if (qbConnection.value.status === 'ok' && qbConnection.value.last_success_at) {
    return `最近成功 ${qbConnection.value.last_success_at}`
  }
  return ''
})
const dashboardPollDelay = () => job.value?.running ? 3000 : 15000

const jobHeadline = computed(() => {
  if (job.value?.status === 'running') return '整理任务正在执行'
  if (job.value?.status === 'queued') return '任务已进入持久队列'
  if (job.value?.status === 'failed') return '上一次任务需要检查'
  return '随时可以开始下一次整理'
})

const revision = computed(() => {
  const value = health.value?.source_revision || ''
  return value ? value.slice(0, 12) : 'unknown'
})

const now = ref(new Date())
const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: 'long',
  day: 'numeric',
  weekday: 'long',
})
const currentDate = computed(() => dateFormatter.format(now.value))
const greeting = computed(() => {
  const hour = now.value.getHours()
  if (hour < 6) return '夜深了'
  if (hour < 9) return '早上好'
  if (hour < 12) return '上午好'
  if (hour < 18) return '下午好'
  if (hour < 23) return '晚上好'
  return '夜深了'
})
const jobTypeLabel = computed(() => {
  const type = job.value?.job_type || ''
  const labels = {
    qb_poll: 'qB 增量检查',
    manual_scan: '全量整理',
    full_scan: '全量整理',
    manual: '手动整理',
  }
  return labels[type] || type
})

function fileStatusText(status) {
  const labels = {
    success: '成功',
    failed: '失败',
    skipped: '已跳过',
  }
  return labels[status] || status
}

function markSignedOut(message = '') {
  auth.value.authenticated = false
  auth.value.username = ''
  dashboardPolling.stop()
  if (message) loginError.value = message
}

async function refresh({ quiet = false } = {}) {
  if (!auth.value.authenticated || refreshRunning) return
  refreshRunning = true
  if (!quiet) loading.value = true
  try {
    const dashboard = await getDashboard()
    stats.value = dashboard
    job.value = dashboard.job_status
    health.value = dashboard.health
    error.value = ''
  } catch (requestError) {
    if (requestError.status === 401) {
      markSignedOut('登录已过期，请重新登录。')
      return
    }
    error.value = requestError.message
  } finally {
    refreshRunning = false
    loading.value = false
  }
}

const dashboardPolling = useAdaptivePolling(
  () => refresh({ quiet: true }),
  dashboardPollDelay,
)

stopUnauthorizedWatch = onUnauthorized(() => {
  if (auth.value.authenticated) markSignedOut('登录已过期，请重新登录。')
})

async function signIn() {
  loginPending.value = true
  loginError.value = ''
  try {
    const sessionData = await login(
      credentials.value.username.trim(),
      credentials.value.password,
    )
    auth.value.authenticated = sessionData.authenticated
    auth.value.username = sessionData.username
    credentials.value.password = ''
    await refresh()
    dashboardPolling.start({ immediate: false })
  } catch (requestError) {
    loginError.value = requestError.message
  } finally {
    loginPending.value = false
  }
}

async function signOut() {
  try {
    await logout()
  } finally {
    credentials.value.username = auth.value.username || 'admin'
    markSignedOut()
  }
}

async function runAction(path, message) {
  actionPending.value = true
  notice.value = ''
  error.value = ''
  try {
    await postAction(path)
    notice.value = message
    await refresh({ quiet: true })
  } catch (requestError) {
    if (requestError.status === 401) {
      markSignedOut('登录已过期，请重新登录。')
    } else if (requestError.status === 409) {
      notice.value = '已有任务处于排队或执行状态。'
      await refresh({ quiet: true })
    } else {
      error.value = requestError.message
    }
  } finally {
    actionPending.value = false
  }
}

function changeTheme(preference = themePreference.value) {
  themePreference.value = saveThemePreference(preference)
  applyTheme(themePreference.value)
}

function refreshWorkspace() {
  workspaceRefreshKey.value += 1
  refresh()
}

onMounted(async () => {
  stopSystemThemeWatch = watchSystemTheme(() => {
    if (themePreference.value === 'system') applyTheme('system')
  })
  clockTimer = window.setInterval(() => {
    now.value = new Date()
  }, 60_000)
  try {
    const [sessionData, healthData] = await Promise.all([
      getSession(),
      getHealth().catch((healthError) => healthError.payload || { status: 'degraded' }),
    ])
    health.value = healthData
    auth.value.authenticated = sessionData.authenticated
    auth.value.username = sessionData.username
    if (sessionData.username) credentials.value.username = sessionData.username
    if (sessionData.authenticated) {
      await refresh()
      dashboardPolling.start({ immediate: false })
    }
  } catch (requestError) {
    loginError.value = requestError.message
  } finally {
    auth.value.loading = false
  }
})

onBeforeUnmount(() => {
  window.clearInterval(clockTimer)
  stopSystemThemeWatch()
  stopUnauthorizedWatch()
})
</script>

<template>
  <main v-if="auth.loading" class="loading-view" aria-live="polite">
    <div class="loading-logo"><BrandMark :size="48" /></div>
    <div class="loading-copy">
      <strong>Music Organizer</strong>
      <span>正在连接控制平面</span>
    </div>
    <span class="loading-bar"></span>
  </main>

  <main v-else-if="!auth.authenticated" class="auth-view">
    <section class="auth-story">
      <a class="auth-brand" href="/">
        <BrandMark :size="34" />
        Music Organizer
      </a>
      <div class="auth-message">
        <span class="eyebrow"><Sparkles :size="14" /> YOUR LIBRARY, IN ORDER</span>
        <h1>让整理安静地<br>发生在后台。</h1>
        <p>从 qBittorrent 完成检测、文件转移到音乐预审与确认入库，所有状态都在一个清晰的工作台里。</p>
      </div>
      <div class="auth-proof">
        <div><RadioTower :size="17" /><span>增量轮询</span></div>
        <div><Database :size="17" /><span>持久队列</span></div>
        <div><FolderSync :size="17" /><span>自动整理</span></div>
      </div>
    </section>

    <section class="auth-form-side">
      <ThemePicker v-model="themePreference" class="auth-theme-control" @change="changeTheme" />
      <div class="auth-status">
        <span :class="{ online: health?.status === 'ok' }"></span>
        {{ health?.status === 'ok' ? '服务运行正常' : '正在确认服务状态' }}
      </div>
      <div class="auth-card">
        <span class="eyebrow">ADMIN CONSOLE</span>
        <h2>欢迎回来</h2>
        <p>登录以查看整理任务和媒体库状态。</p>

        <form autocomplete="on" @submit.prevent="signIn">
          <label for="username">用户名</label>
          <div class="field">
            <input
              id="username"
              v-model="credentials.username"
              name="username"
              type="text"
              autocomplete="username"
              autocapitalize="none"
              spellcheck="false"
              required
            >
          </div>

          <label for="password">密码</label>
          <div class="field">
            <input
              id="password"
              v-model="credentials.password"
              name="password"
              type="password"
              autocomplete="current-password"
              required
            >
          </div>

          <p v-if="loginError" class="form-alert" role="alert">{{ loginError }}</p>
          <button class="auth-submit" type="submit" :disabled="loginPending">
            <span>{{ loginPending ? '正在登录…' : '进入工作台' }}</span>
            <ArrowRight :size="17" />
          </button>
        </form>

        <p class="auth-hint">支持浏览器密码管理器自动填充。密码在服务器上仅保存 scrypt 哈希。</p>
      </div>
      <span class="auth-version">CONTROL PLANE / VUE 3</span>
    </section>
  </main>

  <main v-else class="app-layout">
    <aside class="sidebar">
      <a class="brand" href="/">
        <BrandMark />
        <span>Music Organizer</span>
      </a>

      <div class="side-section">
        <span class="side-label">工作区</span>
        <nav aria-label="主导航">
          <a :class="{ active: !isReview && !isLibrary && !isHistory && !isConfig }" href="/"><LayoutDashboard :size="17" />概览</a>
          <a :class="{ active: isReview }" href="/review"><Music2 :size="17" />音乐预审</a>
          <a :class="{ active: isLibrary }" href="/library"><LibraryBig :size="17" />音乐库</a>
          <a :class="{ active: isHistory }" href="/history"><History :size="17" />历史记录</a>
          <a :class="{ active: isConfig }" href="/config"><Settings2 :size="17" />配置</a>
        </nav>
      </div>

      <div class="side-section side-runtime">
        <span class="side-label">运行状态</span>
        <div class="runtime-row">
          <span class="runtime-icon"><RadioTower :size="16" /></span>
          <div>
            <strong>Web 服务</strong>
            <span :class="webOnline ? 'status-online' : 'status-offline'">{{ webOnline ? '在线' : '离线' }}</span>
          </div>
          <i :class="webOnline ? 'online' : 'offline'"></i>
        </div>
        <div class="runtime-row">
          <span class="runtime-icon"><HardDrive :size="16" /></span>
          <div>
            <strong>Worker</strong>
            <span :class="workerOnline ? 'status-online' : 'status-offline'">{{ workerOnline ? '在线' : '离线' }}</span>
          </div>
          <i :class="workerOnline ? 'online' : 'offline'"></i>
        </div>
      </div>

      <div class="side-account">
        <span class="avatar">{{ auth.username.slice(0, 1).toUpperCase() }}</span>
        <div>
          <strong>{{ auth.username }}</strong>
          <span>Administrator</span>
        </div>
        <button type="button" title="退出登录" @click="signOut"><LogOut :size="17" /></button>
      </div>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div>
          <span class="breadcrumb">工作区 / {{ isReview ? '音乐预审' : isLibrary ? '音乐库' : isHistory ? '历史记录' : isConfig ? '配置' : '概览' }}</span>
          <h1>{{ isReview ? '确认后再入库' : isLibrary ? '直接管理目标音乐库' : isHistory ? '历史记录' : isConfig ? '运行配置' : `${greeting}，${auth.username}` }}</h1>
          <p>{{ isReview ? '批量识别、比较候选，保留你对最终元数据的决定权。' : isLibrary ? '不复制音乐文件，直接查询、编辑标签与歌词，并提供可恢复删除。' : isHistory ? '集中检索整理动作、任务结果与失败原因。' : isConfig ? '安全维护转移、预审、通知与调度参数。' : `${currentDate} · 这是媒体库当前的运行概况。` }}</p>
        </div>
        <div class="header-actions">
          <span class="revision">rev {{ revision }}</span>
          <ThemePicker v-model="themePreference" @change="changeTheme" />
          <button class="icon-button" type="button" title="刷新" :disabled="loading" @click="refreshWorkspace">
            <RefreshCw :size="17" :class="{ spinning: loading }" />
          </button>
        </div>
      </header>

      <div v-if="error" class="notice error" role="alert">{{ error }}</div>
      <div v-if="notice" class="notice">{{ notice }}</div>

      <ReviewWorkspace v-if="isReview" />
      <LibraryWorkspace v-else-if="isLibrary" />
      <HistoryWorkspace v-else-if="isHistory" :refresh-key="workspaceRefreshKey" />
      <ConfigWorkspace v-else-if="isConfig" :refresh-key="workspaceRefreshKey" />
      <section v-else class="dashboard-grid">
        <article class="card command-card">
          <div class="card-topline">
            <div class="card-icon dark"><Gauge :size="18" /></div>
            <span class="job-state" :class="statusTone"><i></i>{{ statusLabel }}</span>
          </div>
          <div class="command-copy">
            <span class="eyebrow">CURRENT OPERATION</span>
            <h2>{{ jobHeadline }}</h2>
            <p v-if="job?.id">任务 #{{ job.id }} · {{ jobTypeLabel }}</p>
            <p v-else>选择增量检查或全量整理，任务会安全写入持久队列。</p>
            <div class="qb-connection" role="status" aria-live="polite">
              <span class="job-state" :class="qbConnectionTone">
                <i></i>{{ qbConnectionLabel }}
              </span>
              <span
                v-if="qbConnectionDetail"
                class="qb-connection-detail"
                :title="qbConnectionDetail"
              >{{ qbConnectionDetail }}</span>
            </div>
          </div>
          <div class="command-actions">
            <button
              class="button secondary"
              :disabled="actionPending || job?.running"
              @click="runAction('/api/qb/trigger', 'qBittorrent 检查已加入持久队列。')"
            ><RadioTower :size="16" />检查 qBittorrent</button>
            <button
              class="button primary"
              :disabled="actionPending || job?.running"
              @click="runAction('/api/trigger', '全量整理已加入持久队列。')"
            ><Play :size="16" />开始整理</button>
            <button
              class="button quiet-danger"
              :disabled="actionPending || !job?.running"
              @click="runAction('/api/stop', '停止请求已写入任务状态。')"
            ><CircleStop :size="16" />停止</button>
          </div>
        </article>

        <article class="card metric-card">
          <div class="metric-head"><Database :size="17" /><span>全部记录</span></div>
          <strong>{{ stats?.total_files ?? '—' }}</strong>
          <div class="metric-foot"><span>数据库累计</span><i class="sparkline bars"></i></div>
        </article>

        <article class="card metric-card">
          <div class="metric-head"><CheckCircle2 :size="17" /><span>已整理</span></div>
          <strong>{{ stats?.organized_files ?? '—' }}</strong>
          <div class="metric-foot"><span>成功归档</span><i class="sparkline rise"></i></div>
        </article>

        <article class="card metric-card">
          <div class="metric-head"><Clock3 :size="17" /><span>待预审</span></div>
          <strong>{{ stats?.review_active ?? '—' }}</strong>
          <div class="metric-foot"><span>等待确认或入库</span><i class="sparkline flat"></i></div>
        </article>

        <article class="card mapping-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">LIBRARY ROUTES</span>
              <h2>路径映射</h2>
            </div>
            <span class="mode-pill">{{ stats?.mode || '—' }}</span>
          </div>
          <div v-if="stats && Object.keys(stats.paths_mapping || {}).length" class="mapping-list">
            <div v-for="(target, source) in stats.paths_mapping" :key="source" class="mapping-row">
              <span class="folder-icon"><FolderSync :size="16" /></span>
              <div><small>来源</small><code>{{ source }}</code></div>
              <ArrowRight class="mapping-arrow" :size="17" />
              <div><small>目标</small><code>{{ target }}</code></div>
            </div>
          </div>
          <p v-else class="empty-state">暂未配置路径映射。</p>
        </article>

        <article class="card last-run-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">LAST RUN</span>
              <h2>最近运行</h2>
            </div>
            <Activity :size="18" />
          </div>
          <dl v-if="stats?.last_run">
            <div><dt>开始时间</dt><dd>{{ stats.last_run.started_at }}</dd></div>
            <div><dt>扫描文件</dt><dd>{{ stats.last_run.scanned }}</dd></div>
            <div><dt>成功整理</dt><dd>{{ stats.last_run.organized }}</dd></div>
            <div><dt>失败项目</dt><dd>{{ stats.last_run.failed }}</dd></div>
          </dl>
          <p v-else class="empty-state">尚无运行记录。</p>
        </article>

        <QbAttentionCard
          :items="stats?.qb_needs_attention || []"
          :busy="actionPending"
          :job-running="Boolean(job?.running)"
          @retry="hash => runAction(`/api/qb/retry/${encodeURIComponent(hash)}`, '已重置失败次数并提交 qBittorrent 重试。')"
        />

        <article class="card activity-card">
          <div class="card-heading">
            <div>
              <span class="eyebrow">RECENT ACTIVITY</span>
              <h2>最近文件记录</h2>
            </div>
            <a href="/history">查看全部 <ArrowRight :size="15" /></a>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>时间</th><th>状态</th><th>源文件</th><th>目标文件</th></tr></thead>
              <tbody>
                <tr v-for="item in stats?.recent || []" :key="item.id">
                  <td data-label="时间">{{ item.created_at }}</td>
                  <td data-label="状态">
                    <span
                      class="table-status"
                      :class="item.status === 'success' ? 'success' : item.status === 'failed' ? 'danger' : ''"
                    ><i></i>{{ fileStatusText(item.status) }}</span>
                  </td>
                  <td data-label="源文件"><code>{{ item.source_path }}</code></td>
                  <td data-label="目标文件"><code>{{ item.target_path }}</code></td>
                </tr>
                <tr v-if="!stats?.recent?.length"><td colspan="4" class="empty-cell">暂无记录</td></tr>
              </tbody>
            </table>
          </div>
        </article>
      </section>

      <footer class="workspace-footer">
        <span>Music Organizer Control Plane</span>
        <span>Vue 3 · Persistent SQLite Queue</span>
      </footer>
    </section>
  </main>
</template>
