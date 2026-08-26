<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { BellRing, CheckCircle2, LoaderCircle, Save } from '@lucide/vue'
import ConfigField from './ConfigField.vue'
import { getConfig, saveConfig, testMagicPush } from './api'

const props = defineProps({ refreshKey: { type: Number, default: 0 } })
const form = ref({})
const saved = ref({})
const baseline = ref('')
const loading = ref(true)
const saving = ref(false)
const testingPush = ref(false)
const feedback = ref({ tone: '', message: '' })

const sections = [
  {
    title: '路径与转移方式', eyebrow: 'TRANSFER',
    fields: [
      { name: 'paths_mapping', label: '路径映射', type: 'textarea', rows: 4, wide: true, help: '每行一组：源路径 => 目标路径' },
      { name: 'mode', label: '转移方式', type: 'select', options: [{ value: 'hardlink', label: 'hardlink' }, { value: 'copy', label: 'copy' }] },
      { name: 'keep_dir_struct', label: '目录结构', type: 'checkbox', checkboxLabel: '保持源目录结构' },
      { name: 'mkdir_if_single', label: '单文件处理', type: 'checkbox', checkboxLabel: '单文件创建同名文件夹' },
    ],
  },
  {
    title: '扫描包含与排除规则', eyebrow: 'FILTERS',
    fields: [
      { name: 'include_globs', label: '包含 glob', type: 'textarea', rows: 5, help: '留空表示全部扫描。' },
      { name: 'exclude_globs', label: '排除 glob', type: 'textarea', rows: 5 },
      { name: 'include_exts', label: '整理后缀', type: 'textarea', rows: 8, help: '每行一个后缀。' },
      { name: 'exclude_exts', label: '排除后缀', type: 'textarea', rows: 8 },
    ],
  },
  {
    title: 'CUE 切分', eyebrow: 'AUDIO PIPELINE',
    fields: [
      { name: 'cue_split_enabled', label: '切分状态', type: 'checkbox', checkboxLabel: '启用 CUE 切分' },
      { name: 'cue_skip_existing', label: '重复文件', type: 'checkbox', checkboxLabel: '已存在则跳过' },
      { name: 'cue_split_multifile_cues', label: '多文件 CUE', type: 'checkbox', checkboxLabel: '切分多文件 CUE' },
      { name: 'cue_skip_source_audio', label: '源音频处理', type: 'checkbox', checkboxLabel: '跳过整轨源音频' },
      { name: 'cue_ffmpeg_path', label: 'FFmpeg 路径' },
      { name: 'cue_flac_compression_level', label: 'FLAC 压缩等级', type: 'number', min: 0, max: 12 },
      { name: 'cue_output_subdir', label: '输出子目录' },
      { name: 'cue_filename_template', label: '文件名模板', wide: true },
    ],
  },
  {
    title: 'qBittorrent 主动联动', eyebrow: 'QBITTORRENT',
    fields: [
      { name: 'qb_enabled', label: '联动状态', type: 'checkbox', checkboxLabel: '启用主动检查' },
      { name: 'qb_base_url', label: '服务地址', placeholder: 'http://127.0.0.1:8080' },
      { name: 'qb_username', label: '用户名' },
      { name: 'qb_password', label: '密码', type: 'password', secret: true },
      { name: 'qb_api_key', label: 'API Key', type: 'password', secret: true },
      { name: 'qb_timeout', label: '超时秒数', type: 'number', min: 3, max: 120 },
      { name: 'qb_min_completion_age_seconds', label: '完成稳定等待秒数', type: 'number', min: 0, max: 3600 },
      { name: 'qb_scan_mode', label: '扫描范围', type: 'select', options: [{ value: 'torrent_paths', label: '仅种子路径' }, { value: 'full', label: '完整扫描' }] },
      { name: 'qb_poll_mode', label: '检查模式', type: 'select', options: [{ value: 'sync', label: '同步快照' }, { value: 'completed_list', label: '完成列表' }] },
      { name: 'qb_category', label: '分类' },
      { name: 'qb_tag', label: '标签' },
    ],
  },
  {
    title: '音乐预审', eyebrow: 'REVIEW WORKSPACE',
    fields: [
      { name: 'review_enabled', label: '预审状态', type: 'checkbox', checkboxLabel: '启用音乐预审' },
      { name: 'review_auto_discover', label: '自动发现', type: 'checkbox', checkboxLabel: '自动扫描 Inbox' },
      { name: 'review_discovery_interval_seconds', label: '发现间隔秒数', type: 'number', min: 5, max: 3600 },
      { name: 'review_discovery_stable_seconds', label: '稳定等待秒数', type: 'number', min: 10, max: 86400 },
      { name: 'review_identify_workers', label: '识别并发数', type: 'number', min: 1, max: 8 },
      { name: 'review_proxy_url', label: 'HTTP 代理' },
      { name: 'review_proxy_username', label: '代理账号' },
      { name: 'review_proxy_password', label: '代理密码', type: 'password', secret: true },
      { name: 'review_source_roots', label: '允许选择的 Inbox 目录', type: 'textarea', rows: 3, wide: true, help: '每行一个容器内绝对路径。' },
      { name: 'review_directory', label: '入库目标目录', wide: true },
      { name: 'review_recycle_directory', label: '群晖回收站目录', wide: true },
      { name: 'review_library', label: 'Library DB' },
      { name: 'review_config_path', label: 'beets Config 路径' },
      { name: 'review_import_mode', label: '入库方式', type: 'select', options: [{ value: 'hardlink', label: 'hardlink' }, { value: 'copy', label: 'copy' }, { value: 'move', label: 'move' }] },
      { name: 'review_write_tags', label: '标签处理', type: 'checkbox', checkboxLabel: '写入文件标签' },
      { name: 'review_move_extra_files', label: '附加文件', type: 'checkbox', checkboxLabel: '移动匹配的附加文件' },
      { name: 'review_cleanup_source_after_import', label: '源目录清理', type: 'checkbox', checkboxLabel: '删除已入库源文件和空目录' },
      { name: 'review_extra_file_patterns', label: '附加文件匹配', placeholder: '*.jpg *.png' },
      { name: 'review_path_format', label: '路径模板（Picard 预设 3）', wide: true },
    ],
  },
  {
    title: 'AI 歌词翻译', eyebrow: 'LYRICS AI',
    fields: [
      { name: 'translation_enabled', label: '翻译状态', type: 'checkbox', checkboxLabel: '启用日文歌词 AI 翻译' },
      { name: 'translation_base_url', label: 'OpenAI 兼容接口地址', wide: true },
      { name: 'translation_model', label: '模型' },
      { name: 'translation_api_key', label: 'API Key', type: 'password', secret: true },
      { name: 'translation_style', label: '翻译风格', type: 'select', options: [{ value: 'literal', label: '忠实直译' }, { value: 'natural', label: '自然中文' }, { value: 'lyrical', label: '歌词化表达' }] },
      { name: 'translation_timeout', label: '超时秒数', type: 'number', min: 10, max: 300 },
    ],
  },
  {
    title: 'MagicPush 整理通知', eyebrow: 'NOTIFICATIONS', action: 'magicpush',
    fields: [
      { name: 'magicpush_enabled', label: '通知状态', type: 'checkbox', checkboxLabel: '启用任务结果推送' },
      { name: 'magicpush_base_url', label: '服务地址', wide: true },
      { name: 'magicpush_timeout', label: '超时秒数', type: 'number', min: 3, max: 30 },
      { name: 'magicpush_title', label: '标题前缀' },
      { name: 'magicpush_token', label: 'Bearer token', type: 'password', secret: true, wide: true },
      { name: 'magicpush_notify_no_changes', label: '空任务通知', type: 'checkbox', checkboxLabel: '无新增任务时也推送' },
    ],
  },
  {
    title: '定时扫描与日志', eyebrow: 'OPERATIONS',
    fields: [
      { name: 'schedule_cron', label: 'Cron' },
      { name: 'schedule_enabled', label: '调度状态', type: 'checkbox', checkboxLabel: '启用定时扫描' },
      { name: 'progress_interval', label: '进度日志间隔', type: 'number', min: 1 },
      { name: 'verbose_file_actions', label: '日志明细', type: 'checkbox', checkboxLabel: '记录每个文件动作' },
    ],
  },
]

const booleanFields = new Set(sections.flatMap((section) => section.fields).filter((field) => field.type === 'checkbox').map((field) => field.name))
const dirty = computed(() => !loading.value && JSON.stringify(form.value) !== baseline.value)

function applyPayload(payload) {
  form.value = { ...(payload.values || {}) }
  saved.value = { ...(payload.saved || {}) }
  baseline.value = JSON.stringify(form.value)
}

async function load() {
  loading.value = true
  try {
    applyPayload(await getConfig())
    feedback.value = { tone: '', message: '' }
  } catch (error) {
    feedback.value = { tone: 'error', message: error.message }
  } finally {
    loading.value = false
  }
}

async function submit() {
  saving.value = true
  feedback.value = { tone: 'working', message: '正在校验并保存配置…' }
  const payload = new FormData()
  for (const [name, value] of Object.entries(form.value)) {
    if (booleanFields.has(name)) {
      if (value) payload.append(name, 'on')
    } else {
      payload.append(name, value == null ? '' : String(value))
    }
  }
  try {
    const response = await saveConfig(payload)
    applyPayload(response)
    feedback.value = { tone: 'success', message: response.message }
  } catch (error) {
    feedback.value = { tone: 'error', message: error.message }
  } finally {
    saving.value = false
  }
}

async function sendTestPush() {
  testingPush.value = true
  feedback.value = { tone: 'working', message: '正在使用已保存配置发送测试消息…' }
  try {
    const response = await testMagicPush()
    feedback.value = { tone: 'success', message: response.message || '测试消息已发送' }
  } catch (error) {
    feedback.value = { tone: 'error', message: error.message }
  } finally {
    testingPush.value = false
  }
}

watch(() => props.refreshKey, load)
onMounted(load)
</script>

<template>
  <section class="config-workspace">
    <div class="config-summary card">
      <div><CheckCircle2 :size="18" /><span><strong>运行配置</strong><small>保存后由 Worker 自动重新加载</small></span></div>
      <span :class="dirty ? 'dirty' : 'saved'">{{ dirty ? '有未保存修改' : '已同步' }}</span>
    </div>
    <p v-if="feedback.message" class="config-feedback" :class="feedback.tone" role="status">{{ feedback.message }}</p>
    <div v-if="loading" class="config-loading"><LoaderCircle :size="20" class="spinning" />正在读取配置…</div>
    <form v-else @submit.prevent="submit">
      <article v-for="section in sections" :key="section.title" class="config-section card">
        <header><div><small>{{ section.eyebrow }}</small><h2>{{ section.title }}</h2></div>
          <button v-if="section.action === 'magicpush'" type="button" class="config-secondary" :disabled="testingPush" @click="sendTestPush"><BellRing :size="15" />发送测试推送</button>
        </header>
        <div class="config-grid">
          <ConfigField
            v-for="field in section.fields"
            :key="field.name"
            :field="field"
            :model-value="form[field.name]"
            :saved="Boolean(saved[field.name])"
            @update:model-value="form[field.name] = $event"
          />
        </div>
      </article>
      <div class="config-savebar">
        <span>{{ dirty ? '请检查修改后保存' : '当前配置已保存' }}</span>
        <button type="submit" :disabled="saving || !dirty"><Save :size="16" />{{ saving ? '保存中…' : '保存配置' }}</button>
      </div>
    </form>
  </section>
</template>
