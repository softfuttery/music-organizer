<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Archive,
  Check,
  ChevronRight,
  Folder,
  FolderInput,
  Headphones,
  Inbox,
  Languages,
  LoaderCircle,
  Music2,
  Paperclip,
  RefreshCw,
  Search,
  ShieldCheck,
  SkipForward,
  SlidersHorizontal,
  Trash2,
  X,
} from '@lucide/vue'
import {
  approveManualReviewItem,
  approveReviewItem,
  createReviewBatch,
  deleteReviewArchive,
  fetchReviewLyrics,
  getReviewAudioUrl,
  getReviewBatch,
  getReviewBatches,
  getReviewFiles,
  getManualReviewPreview,
  getReviewRoots,
  reidentifyReviewItem,
  recycleReviewSource,
  saveReviewLyrics,
  searchReviewLyrics,
  skipReviewItem,
  translateLyrics,
} from './api'
import { applyAudioPreferences, saveAudioPreferences } from './audio-preferences'
import {
  hasTextDecodeDamage,
  highConfidenceLyricCandidate,
  lyricCandidateTitle,
  lyricCandidateMatchSummary,
  lyricQualitySummary,
  parseSyncedLyrics,
  plainLyricLines,
  scrollLyricContainer,
} from './lyric-editor-state'
import {
  adjustLyricsOffset,
  compressBlankLines,
  convertLyricsToSimplified,
  standardizeLyrics,
} from './lyric-processing'
import {
  focusModal,
  lockBodyScroll,
  restoreModalFocus,
  trapModalTab,
} from './modal-focus'
import {
  beginPendingItem,
  createLatestRequestGate,
  finishPendingItem,
  reconcilePolledItem,
} from './requestState'
import { useAdaptivePolling } from './useAdaptivePolling'
import {
  reviewMappingConfidence,
  sortReviewLocalItems,
} from './track-mapping-state'

const browser = ref({ enabled: false, roots: [], directories: [], current: null })
const selected = ref(new Set())
const expandedDirectories = ref(new Set())
const directoryPreviews = ref({})
const previewLoading = ref(new Set())
const batches = ref([])
const activeBatch = ref(null)
const selectedBatchId = ref(null)
const scope = ref('active')
const counts = ref({ active: 0, archived: 0 })
const archiveQuery = ref('')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const searches = ref({})
const decisions = ref({})
const manualRules = ref({})
const pendingItems = ref(new Map())
const viewRequests = createLatestRequestGate()
const lyricRequests = createLatestRequestGate()
const lyricPanel = ref(null)
const lyricCandidates = ref([])
const lyricWarnings = ref([])
const lyricAction = ref('')
const lyricLoading = computed(() => Boolean(lyricAction.value))
const lyricFeedback = ref({ type: '', text: '' })
const lyricContent = ref('')
const lyricSelection = ref(null)
const lyricDirty = ref(false)
const lyricTab = ref('lyrics')
const lyricAutoFollow = ref(true)
const preserveWordTiming = ref(true)
const lyricOffset = ref(0)
const playbackTime = ref(0)
const lastActiveLyricIndex = ref(-1)
const lyricList = ref(null)
const lyricDrawer = ref(null)
const lyricAudio = ref(null)
let lyricReturnFocus = null
let releaseLyricBodyLock = null
let archiveSearchTimer = null
const lyricSearch = ref({
  title: '',
  artist: '',
  album: '',
  artistAliases: [],
  sources: { netease: true, qqmusic: true, kugou: true },
})
const lyricTextDamaged = computed(() => hasTextDecodeDamage(
  lyricPanel.value?.local?.local_path,
  lyricSearch.value.title,
  lyricSearch.value.artist,
  lyricContent.value,
))

const selectedCount = computed(() => selected.value.size)
const currentLyricDecision = computed(() => {
  if (!lyricPanel.value) return null
  return lyricPanel.value.item.lyrics?.[lyricPanel.value.local.local_path] || null
})
const allVisibleSelected = computed(() => (
  browser.value.directories.length > 0
  && browser.value.directories.every((dir) => selected.value.has(dir.path))
))

function toggle(path) {
  const next = new Set(selected.value)
  next.has(path) ? next.delete(path) : next.add(path)
  selected.value = next
}

async function browse(path = '') {
  try {
    browser.value = await getReviewRoots(path)
    error.value = ''
  } catch (requestError) {
    error.value = requestError.message
  }
}

async function refresh() {
  const requestToken = viewRequests.begin()
  const requestedScope = scope.value
  const requestedQuery = requestedScope === 'archived' ? archiveQuery.value.trim() : ''
  try {
    const response = await getReviewBatches(requestedScope, requestedQuery)
    if (
      !viewRequests.isCurrent(requestToken)
      || scope.value !== requestedScope
      || (requestedScope === 'archived' && archiveQuery.value.trim() !== requestedQuery)
    ) return
    const currentId = selectedBatchId.value ?? activeBatch.value?.id
    const nextBatch = response.batches.find((batch) => batch.id === currentId)
      || response.batches[0]
    const nextActiveBatch = nextBatch
      ? await getReviewBatch(nextBatch.id, requestedScope, requestedQuery)
      : null
    if (
      !viewRequests.isCurrent(requestToken)
      || scope.value !== requestedScope
      || (requestedScope === 'archived' && archiveQuery.value.trim() !== requestedQuery)
    ) return
    batches.value = response.batches
    counts.value = response.counts || counts.value
    selectedBatchId.value = nextBatch?.id ?? null
    if (nextActiveBatch?.items && lyricPanel.value?.item) {
      nextActiveBatch.items = reconcilePolledItem(
        nextActiveBatch.items,
        lyricPanel.value.item,
        ['lyrics'],
      )
    }
    activeBatch.value = nextActiveBatch
    prepareBatch(nextActiveBatch)
    error.value = ''
  } catch (requestError) {
    if (viewRequests.isCurrent(requestToken)) error.value = requestError.message
  }
}

function toggleAllVisible() {
  const next = new Set(selected.value)
  for (const directory of browser.value.directories) {
    if (allVisibleSelected.value) next.delete(directory.path)
    else next.add(directory.path)
  }
  selected.value = next
}

async function toggleDirectoryPreview(path) {
  const expanded = new Set(expandedDirectories.value)
  if (expanded.has(path)) {
    expanded.delete(path)
    expandedDirectories.value = expanded
    return
  }
  expanded.add(path)
  expandedDirectories.value = expanded
  if (directoryPreviews.value[path]) return
  const loadingPaths = new Set(previewLoading.value)
  loadingPaths.add(path)
  previewLoading.value = loadingPaths
  try {
    const preview = await getReviewFiles(path)
    directoryPreviews.value = { ...directoryPreviews.value, [path]: preview }
  } catch (requestError) {
    error.value = requestError.message
    expanded.delete(path)
    expandedDirectories.value = expanded
  } finally {
    const remaining = new Set(previewLoading.value)
    remaining.delete(path)
    previewLoading.value = remaining
  }
}

const reviewPollDelay = () => {
  const activeStatuses = new Set(['queued', 'identifying', 'approved', 'importing'])
  return activeBatch.value?.items?.some((item) => activeStatuses.has(item.status))
    ? 3000
    : 15000
}
const reviewPolling = useAdaptivePolling(refresh, reviewPollDelay)

async function identify() {
  if (!selectedCount.value) return
  loading.value = true
  try {
    activeBatch.value = await createReviewBatch([...selected.value])
    selectedBatchId.value = activeBatch.value?.id ?? null
    selected.value = new Set()
    notice.value = '识别任务已加入独立持久队列。'
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

async function openBatch(id) {
  selectedBatchId.value = id
  const requestToken = viewRequests.begin()
  const requestedScope = scope.value
  const requestedQuery = requestedScope === 'archived' ? archiveQuery.value.trim() : ''
  try {
    const nextBatch = await getReviewBatch(id, requestedScope, requestedQuery)
    if (
      !viewRequests.isCurrent(requestToken)
      || scope.value !== requestedScope
      || (requestedScope === 'archived' && archiveQuery.value.trim() !== requestedQuery)
    ) return
    activeBatch.value = nextBatch
    prepareBatch(nextBatch)
    error.value = ''
  } catch (requestError) {
    if (viewRequests.isCurrent(requestToken)) error.value = requestError.message
  }
}

function searchArchives() {
  window.clearTimeout(archiveSearchTimer)
  archiveSearchTimer = window.setTimeout(() => {
    selectedBatchId.value = null
    activeBatch.value = null
    refresh()
  }, 250)
}

function clearArchiveSearch() {
  window.clearTimeout(archiveSearchTimer)
  archiveQuery.value = ''
  selectedBatchId.value = null
  activeBatch.value = null
  refresh()
}

async function changeScope(nextScope) {
  if (scope.value === nextScope) return
  scope.value = nextScope
  selectedBatchId.value = null
  activeBatch.value = null
  await refresh()
}

function prepareBatch(batch) {
  for (const item of batch?.items || []) {
    if (!searches.value[item.id]) {
      searches.value[item.id] = {
        artist: item.current_artist || '',
        album: item.current_album || '',
        releaseId: '',
      }
    }
    const existing = decisions.value[item.id]
    if (
      scope.value === 'active'
      && item.candidates?.length
      && (!existing || !item.candidates.some((value) => value.key === existing.candidateKey))
    ) {
      selectCandidate(item, item.candidates[0])
    }
  }
}

function localItems(candidate) {
  if (candidate?.local_items?.length) return candidate.local_items
  return [
    ...(candidate?.tracks || []).map((track) => ({
      local_path: track.local_path,
      local_title: track.local_title,
      extension: track.extension || '',
      track_key: track.track_key || track.track_id,
    })),
    ...(candidate?.extra_items || []).map((path) => ({
      local_path: path,
      local_title: '',
      extension: path.includes('.') ? '.' + path.split('.').pop() : '',
      track_key: '',
    })),
  ]
}

function trackOptions(candidate) {
  const values = candidate?.track_options?.length
    ? candidate.track_options
    : [...(candidate?.tracks || []), ...(candidate?.extra_tracks || [])]
  const seen = new Set()
  return values.filter((track) => {
    const key = track.key || track.track_key || track.track_id
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  }).sort((left, right) => (
    Number(left.disc || 0) - Number(right.disc || 0)
    || Number(left.track || 0) - Number(right.track || 0)
    || String(left.title || '').localeCompare(String(right.title || ''))
  ))
}

function mappingConfidence(item, local) {
  const current = decisions.value[item.id]?.mapping[local.local_path] || ''
  return reviewMappingConfidence(local, current)
}

function sortedLocalItems(item) {
  const candidate = selectedCandidate(item)
  return sortReviewLocalItems(
    localItems(candidate),
    trackOptions(candidate),
    decisions.value[item.id]?.mapping || {},
  )
}

function isCueFile(path) {
  return /\.cue$/i.test(String(path || '').trim())
}

function selectCandidate(item, candidate) {
  const mapping = {}
  const locals = localItems(candidate)
  for (const local of locals) {
    mapping[local.local_path] = local.track_key || ''
  }
  const defaultQuarantine = new Set(
    (candidate.auxiliary_files || []).filter(isCueFile),
  )
  for (const local of locals) {
    if (!mapping[local.local_path] && isCueFile(local.local_path)) {
      defaultQuarantine.add(local.local_path)
    }
  }
  decisions.value[item.id] = {
    candidateKey: candidate.key,
    mapping,
    quarantine: [...defaultQuarantine],
  }
}

function selectedCandidate(item) {
  const key = decisions.value[item.id]?.candidateKey || item.selected_candidate_key
  return item.candidates?.find((candidate) => candidate.key === key) || null
}

function updateMapping(item, localPath, trackKey) {
  const decision = decisions.value[item.id]
  if (!decision) return
  decision.mapping[localPath] = trackKey
  if (trackKey) {
    decision.quarantine = decision.quarantine.filter((path) => path !== localPath)
  }
}

function toggleQuarantine(item, path, checked) {
  const decision = decisions.value[item.id]
  if (!decision) return
  const values = new Set(decision.quarantine)
  checked ? values.add(path) : values.delete(path)
  decision.quarantine = [...values]
}

function mappedCount(item) {
  return Object.values(decisions.value[item.id]?.mapping || {}).filter(Boolean).length
}

function cleanupCount(item) {
  return (item.import_result?.cleanup || []).filter(
    (value) => value.status === 'quarantined',
  ).length
}

function batchStatusText(status) {
  const labels = {
    queued: '等待处理',
    running: '处理中',
    needs_review: '需要确认',
    needs_attention: '需要处理',
    done: '已归档',
  }
  return labels[status] || status
}

function targetPath(candidate, local, trackKey) {
  if (!trackKey) return '不入库 · 保留在源目录'
  const track = trackOptions(candidate).find(
    (value) => (value.key || value.track_key || value.track_id) === trackKey,
  )
  if (!track) return '曲目对应已失效'
  const extension = local.extension || ''
  const albumArtist = candidate.artist || ''
  const directory = albumArtist || track.artist || '未知艺术家'
  const parts = [directory]
  if (albumArtist && candidate.album) parts.push(candidate.album)
  let filename = ''
  if (Number(candidate.mediums || 0) > 1) filename += String(track.disc || 0) + '-'
  if (albumArtist && Number(track.track || 0) > 0) {
    filename += String(track.track).padStart(2, '0') + ' '
  }
  if (candidate.multiartist && track.artist) filename += track.artist + ' - '
  filename += (track.title || '未知标题') + extension
  parts.push(filename)
  return parts.join('/')
}

function decisionPayload(item) {
  const decision = decisions.value[item.id]
  return {
    track_mapping: Object.entries(decision?.mapping || {})
      .filter(([, trackKey]) => trackKey)
      .map(([localPath, trackKey]) => ({
        local_path: localPath,
        track_key: trackKey,
      })),
    quarantine_paths: decision?.quarantine || [],
  }
}

function selectedTrackForLocal(item, local) {
  const candidate = selectedCandidate(item)
  const trackKey = decisions.value[item.id]?.mapping[local.local_path]
    || local.track_key
  return trackOptions(candidate).find(
    (track) => (track.key || track.track_key || track.track_id) === trackKey,
  ) || null
}

function lyricStatus(item, localPath) {
  const status = item.lyrics?.[localPath]?.status
  if (status === 'selected') return '歌词已选'
  if (status === 'instrumental') return '纯音乐'
  if (status === 'skipped') return '已跳过'
  return '试听与歌词'
}

function manualDirectoryName(value, fallback) {
  const cleaned = String(value || '')
    .trim()
    .replaceAll('/', '／')
    .replaceAll('\\', '＼')
    .replace(/^[ .]+|[ .]+$/g, '')
  return cleaned && !['.', '..'].includes(cleaned) ? cleaned : fallback
}

function manualDestinationDirectory(rules) {
  const artist = manualDirectoryName(
    rules?.albumartist || rules?.sourceArtist,
    '未知艺术家',
  )
  const album = manualDirectoryName(rules?.album, '未知专辑')
  return `未分类/${artist}/${album}`
}

function manualDestinationFilename(localPath) {
  return String(localPath || '').split(/[\\/]/).filter(Boolean).pop() || ''
}

async function openLyricPanel(item, local, manualTrack = null) {
  const opening = !lyricPanel.value
  if (opening) {
    lyricReturnFocus = globalThis.document?.activeElement || null
    releaseLyricBodyLock = lockBodyScroll()
  }
  lyricRequests.begin()
  const track = manualTrack || selectedTrackForLocal(item, local)
  const saved = item.lyrics?.[local.local_path]
  lyricPanel.value = {
    item,
    local,
    audioUrl: getReviewAudioUrl(item.id, local.local_path),
  }
  lyricSearch.value = {
    title: track?.title || local.local_title || local.local_path.replace(/\.[^.]+$/, ''),
    artist: track?.artist || selectedCandidate(item)?.artist || item.current_artist || '',
    album: track?.album || selectedCandidate(item)?.album || item.current_album || '',
    artistAliases: [...new Set([
      track?.artist,
      selectedCandidate(item)?.artist,
      item.current_artist,
    ].filter(Boolean))],
    sources: { netease: true, qqmusic: true, kugou: true },
  }
  lyricCandidates.value = []
  lyricWarnings.value = []
  lyricAction.value = ''
  lyricFeedback.value = { type: '', text: '' }
  lyricSelection.value = saved?.status === 'selected' ? saved : null
  lyricContent.value = saved?.content || ''
  lyricDirty.value = false
  lyricTab.value = saved?.status === 'selected'
    ? 'preview'
    : (saved?.status ? 'decision' : 'lyrics')
  lyricAutoFollow.value = true
  playbackTime.value = 0
  lastActiveLyricIndex.value = -1
  await nextTick()
  if (opening) focusModal(lyricDrawer.value)
  lyricAudio.value?.play().catch(() => {})
  if (!saved?.status) await searchLyrics({ autoPreview: true })
}

function closeLyricPanel(force = false) {
  if (
    force !== true
    && lyricDirty.value
    && !window.confirm('歌词还有未保存的修改，仍要关闭吗？')
  ) return
  lyricRequests.begin()
  lyricPanel.value = null
  lyricCandidates.value = []
  lyricWarnings.value = []
  lyricContent.value = ''
  lyricSelection.value = null
  lyricDirty.value = false
  lyricTab.value = 'lyrics'
  lyricAutoFollow.value = true
  lyricAction.value = ''
  lyricFeedback.value = { type: '', text: '' }
  playbackTime.value = 0
  lastActiveLyricIndex.value = -1
  const returnFocus = lyricReturnFocus
  lyricReturnFocus = null
  releaseLyricBodyLock?.()
  releaseLyricBodyLock = null
  nextTick(() => restoreModalFocus(returnFocus))
}

function warnBeforeUnload(event) {
  if (!lyricPanel.value || !lyricDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

async function searchLyrics(options = {}) {
  if (!lyricPanel.value || lyricLoading.value) return
  const panel = lyricPanel.value
  const sources = Object.entries(lyricSearch.value.sources)
    .filter(([, enabled]) => enabled)
    .map(([source]) => source)
  if (!sources.length) {
    lyricFeedback.value = { type: 'error', text: '请至少选择一个歌词来源。' }
    return
  }
  const requestToken = lyricRequests.begin()
  lyricAction.value = 'search'
  lyricWarnings.value = []
  lyricCandidates.value = []
  lyricFeedback.value = { type: 'working', text: '正在搜索歌词…' }
  try {
    const response = await searchReviewLyrics(panel.item.id, {
      local_path: panel.local.local_path,
      title: lyricSearch.value.title,
      artist: lyricSearch.value.artist,
      album: lyricSearch.value.album,
      artist_aliases: lyricSearch.value.artistAliases,
      sources,
    })
    if (!lyricRequests.isCurrent(requestToken) || lyricPanel.value !== panel) return
    lyricCandidates.value = response.candidates || []
    lyricWarnings.value = response.warnings || []
    lyricFeedback.value = lyricCandidates.value.length
      ? { type: 'success', text: `已找到 ${lyricCandidates.value.length} 条候选。` }
      : { type: 'info', text: '搜索完成，没有找到匹配歌词。' }
    const automaticCandidate = options?.autoPreview === true
      ? highConfidenceLyricCandidate(lyricCandidates.value)
      : null
    if (automaticCandidate) {
      lyricAction.value = ''
      await previewLyrics(automaticCandidate, { automatic: true })
    }
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) lyricAction.value = ''
  }
}

async function previewLyrics(candidate, options = {}) {
  if (!lyricPanel.value || lyricLoading.value) return
  const panel = lyricPanel.value
  const requestToken = lyricRequests.begin()
  lyricAction.value = 'fetch'
  lyricFeedback.value = { type: 'working', text: '正在读取所选歌词…' }
  try {
    const response = await fetchReviewLyrics(panel.item.id, {
      local_path: panel.local.local_path,
      candidate,
    })
    if (!lyricRequests.isCurrent(requestToken) || lyricPanel.value !== panel) return
    lyricSelection.value = response
    lyricContent.value = response.content || ''
    lyricDirty.value = true
    lyricFeedback.value = {
      type: 'info',
      text: options?.automatic === true
        ? `已自动选择最高分候选（曲目匹配 ${Math.round(Number(candidate.score || 0) * 100)}%）并载入试听；${lyricQualitySummary(response.quality)}，点击“采用此歌词”后才会保存。`
        : `歌词已载入预览；${lyricQualitySummary(response.quality)}，点击“采用此歌词”后才会保存。`,
    }
    lyricTab.value = 'preview'
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) lyricAction.value = ''
  }
}

async function persistLyrics(decision) {
  if (!lyricPanel.value || lyricLoading.value) return
  const panel = lyricPanel.value
  const requestToken = lyricRequests.begin()
  lyricAction.value = 'save'
  lyricFeedback.value = { type: 'working', text: '正在保存歌词选择…' }
  try {
    const updated = await saveReviewLyrics(panel.item.id, {
      local_path: panel.local.local_path,
      decision,
    })
    if (!lyricRequests.isCurrent(requestToken) || lyricPanel.value !== panel) return
    panel.item.lyrics = updated.lyrics || {}
    if (decision.status === 'selected') {
      lyricSelection.value = updated.lyrics?.[panel.local.local_path] || decision
      lyricContent.value = lyricSelection.value.content || lyricContent.value
      notice.value = '歌词选择已保存，将在入库完成后写入音频标签。'
      lyricFeedback.value = { type: 'success', text: '歌词选择已保存，重新打开仍会保留。' }
    } else {
      lyricSelection.value = null
      lyricContent.value = decision.status === 'instrumental'
        ? (updated.lyrics?.[panel.local.local_path]?.content || '[00:05.00]纯音乐，请欣赏')
        : ''
      notice.value = decision.status === 'instrumental'
        ? '已设置纯音乐提示，入库时将写入内置歌词标签。'
        : '已跳过此曲歌词。'
      lyricFeedback.value = {
        type: 'success',
        text: decision.status === 'instrumental'
          ? '纯音乐提示已保存，入库时会一键内嵌。'
          : '已保存为暂不处理。',
      }
    }
    lyricDirty.value = false
    if (decision.status === 'selected') closeLyricPanel(true)
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) lyricAction.value = ''
  }
}

function processLyrics(action) {
  if (!lyricContent.value.trim()) {
    lyricFeedback.value = { type: 'info', text: '当前没有可处理的歌词。' }
    return
  }
  try {
    const processors = {
      standard: () => standardizeLyrics(lyricContent.value, { preserveWordTiming: preserveWordTiming.value }),
      blanks: () => compressBlankLines(lyricContent.value),
      simplified: () => convertLyricsToSimplified(lyricContent.value),
      offset: () => adjustLyricsOffset(lyricContent.value, lyricOffset.value),
    }
    lyricContent.value = processors[action]()
    lyricDirty.value = true
    const labels = {
      standard: preserveWordTiming.value ? '已转换为标准 LRC，并保留逐字时间。' : '已转换为标准 LRC，并展开为普通行时间。',
      blanks: '已压缩连续空白行。',
      simplified: '已使用 OpenCC 转换为简体中文。',
      offset: `已把全部行与逐字时间调整 ${Number(lyricOffset.value) >= 0 ? '+' : ''}${Math.round(Number(lyricOffset.value))} ms。`,
    }
    lyricFeedback.value = { type: 'info', text: `${labels[action]} 点击“采用此歌词”后才会保存。` }
  } catch (processingError) {
    lyricFeedback.value = { type: 'error', text: processingError.message }
  }
}

async function translateCurrentLyrics() {
  if (!lyricPanel.value || lyricLoading.value || !lyricContent.value.trim()) return
  const panel = lyricPanel.value
  const requestToken = lyricRequests.begin()
  lyricAction.value = 'translate'
  lyricFeedback.value = { type: 'working', text: '正在使用 AI 翻译日文歌词…' }
  try {
    const result = await translateLyrics({
      content: lyricContent.value,
      title: lyricSearch.value.title,
      artist: lyricSearch.value.artist,
    })
    if (!lyricRequests.isCurrent(requestToken) || lyricPanel.value !== panel) return
    lyricContent.value = result.content || lyricContent.value
    lyricDirty.value = true
    lyricTab.value = 'preview'
    lyricAutoFollow.value = false
    lyricFeedback.value = {
      type: 'success',
      text: `AI 已翻译 ${result.translated_lines || 0} 行并保留原时间轴，尚未保存，请试听检查。`,
    }
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) lyricAction.value = ''
  }
}

const parsedLyrics = computed(() => parseSyncedLyrics(lyricContent.value))
const unsyncedLyricLines = computed(() => plainLyricLines(lyricContent.value))

const activeLyricIndex = computed(() => {
  let active = -1
  parsedLyrics.value.forEach((line, index) => {
    if (line.time <= playbackTime.value + 0.08) active = index
  })
  return active
})

async function syncLyrics(event) {
  playbackTime.value = Number(event.currentTarget?.currentTime || 0)
  if (activeLyricIndex.value === lastActiveLyricIndex.value) return
  lastActiveLyricIndex.value = activeLyricIndex.value
  if (lyricTab.value !== 'preview' || !lyricAutoFollow.value) return
  await nextTick()
  const active = lyricList.value?.querySelector('.active')
  scrollLyricContainer(lyricList.value, active)
}

function seekToLyric(time) {
  const audio = lyricAudio.value
  if (!audio) return
  audio.currentTime = time
  audio.play().catch(() => {})
}

async function selectLyricTab(tab) {
  lyricTab.value = tab
  await nextTick()
  lyricDrawer.value?.scrollTo({ top: 0, behavior: 'auto' })
  if (tab !== 'preview' || !lyricAutoFollow.value) return
  const active = lyricList.value?.querySelector('.active')
  scrollLyricContainer(lyricList.value, active, 'auto')
}

async function resumeLyricFollow() {
  lyricAutoFollow.value = true
  await nextTick()
  const active = lyricList.value?.querySelector('.active')
  scrollLyricContainer(lyricList.value, active)
}

function pauseLyricFollow() {
  lyricAutoFollow.value = false
}

function handleLyricScrollKey(event) {
  if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) {
    pauseLyricFollow()
  }
}

function handleWorkspaceKey(event) {
  if (!lyricPanel.value) return
  if (event.key === 'Tab') {
    trapModalTab(event, lyricDrawer.value)
    return
  }
  if (event.key === 'Escape') closeLyricPanel()
}

function beginItemRequest(item) {
  const started = beginPendingItem(pendingItems.value, item.id)
  pendingItems.value = started.next
  return started.token
}

function finishItemRequest(item, token) {
  pendingItems.value = finishPendingItem(pendingItems.value, item.id, token)
}

async function approve(item) {
  const candidate = selectedCandidate(item)
  if (!candidate) {
    error.value = '请先选择一个发行候选。'
    return
  }
  const payload = decisionPayload(item)
  const trackKeys = payload.track_mapping.map((value) => value.track_key)
  if (!trackKeys.length) {
    error.value = '至少需要保留一首已对应的曲目。'
    return
  }
  if (new Set(trackKeys).size !== trackKeys.length) {
    error.value = '同一 MusicBrainz 曲目不能对应多个本地文件。'
    return
  }
  const requestToken = beginItemRequest(item)
  try {
    await approveReviewItem(item.id, candidate.key, payload)
    notice.value = '已确认：' + candidate.artist + ' — ' + candidate.album
      + '。入库成功后会自动移入归档。'
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

function canDecide(item) {
  return ['ready', 'needs_review', 'failed'].includes(item.status)
}

function statusText(status) {
  const labels = {
    queued: '等待识别',
    identifying: '识别中',
    ready: '推荐候选',
    needs_review: '需要确认',
    approved: '已确认',
    importing: '正在入库',
    done: '入库完成',
    failed: '处理失败',
    skipped: '已跳过',
  }
  return labels[status] || status
}

async function searchMetadata(item) {
  const values = searches.value[item.id] || {}
  const artist = (values.artist || '').trim()
  const album = (values.album || '').trim()
  if (!artist && !album) {
    error.value = '请至少填写艺术家或专辑名称。'
    return
  }
  const requestToken = beginItemRequest(item)
  try {
    await reidentifyReviewItem(item.id, { artist, album })
    notice.value = '已按艺术家与专辑名称重新搜索。'
    decisions.value[item.id] = null
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

async function exactSearch(item) {
  const releaseId = (searches.value[item.id]?.releaseId || '').trim()
  if (!releaseId) {
    error.value = '请输入 MusicBrainz Release ID。'
    return
  }
  const requestToken = beginItemRequest(item)
  try {
    await reidentifyReviewItem(item.id, { release_id: releaseId })
    notice.value = '已按 Release ID 重新排队：' + releaseId
    decisions.value[item.id] = null
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

async function skipItem(item) {
  const requestToken = beginItemRequest(item)
  try {
    await skipReviewItem(item.id)
    notice.value = '此目录已跳过，源文件未发生变化。'
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

function itemBusy(item) {
  return pendingItems.value.has(item.id)
}

async function recycleSource(item) {
  const confirmed = window.confirm(
    `把预审专辑文件夹“${item.source_path}”整体移入回收目录？\n\n只移动当前 Inbox 中的这个目录，不会处理其他路径中的 qBittorrent 做种文件。`,
  )
  if (!confirmed) return
  const requestToken = beginItemRequest(item)
  try {
    await recycleReviewSource(item.id, item.source_path)
    notice.value = `已将专辑文件夹移入回收目录：${item.source_path}`
    await Promise.all([refresh(), browse()])
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

async function deleteArchive(item) {
  const confirmed = window.confirm(
    `删除这条归档记录？\n\n${item.current_artist || '未知艺术家'} · ${item.current_album || '未知专辑'}\n\n只删除预审历史，不会删除已入库音乐或源文件。`,
  )
  if (!confirmed) return
  const requestToken = beginItemRequest(item)
  try {
    await deleteReviewArchive(item.id)
    notice.value = '已删除归档记录，音乐文件未受影响。'
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

async function loadManualRules(item) {
  if (manualRules.value[item.id]) {
    manualRules.value[item.id] = null
    return
  }
  const requestToken = beginItemRequest(item)
  try {
    const candidate = await getManualReviewPreview(item.id)
    manualRules.value[item.id] = {
      albumartist: candidate.artist || '',
      album: candidate.album || '',
      year: candidate.year || '',
      sourceArtist: candidate.artist || '',
      auxiliaryFiles: candidate.auxiliary_files || [],
      tracks: (candidate.tracks || []).map((track) => ({ ...track, include: true })),
    }
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

async function approveManual(item) {
  const rules = manualRules.value[item.id]
  const tracks = (rules?.tracks || []).filter((track) => track.include).map((track) => ({
    local_path: track.local_path,
    artist: track.artist,
    title: track.title,
    disc: track.disc,
    track: track.track,
  }))
  if (!tracks.length) {
    error.value = '规则入库至少需要保留一首音频。'
    return
  }
  if (tracks.some((track) => !track.artist?.trim() || !track.title?.trim())) {
    error.value = '规则入库的每首音频都必须填写艺术家和标题。'
    return
  }
  const requestToken = beginItemRequest(item)
  try {
    await approveManualReviewItem(item.id, {
      albumartist: rules.albumartist,
      album: rules.album,
      year: rules.year,
      tracks,
      quarantine_paths: [],
    })
    notice.value = `已按文件名规则加入队列，将进入 ${manualDestinationDirectory(rules)}。`
    await refresh()
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    finishItemRequest(item, requestToken)
  }
}

onMounted(async () => {
  window.addEventListener('beforeunload', warnBeforeUnload)
  window.addEventListener('keydown', handleWorkspaceKey)
  await Promise.all([browse(), refresh()])
  reviewPolling.start({ immediate: false })
})
onBeforeUnmount(() => {
  window.clearTimeout(archiveSearchTimer)
  window.removeEventListener('beforeunload', warnBeforeUnload)
  window.removeEventListener('keydown', handleWorkspaceKey)
  releaseLyricBodyLock?.()
  releaseLyricBodyLock = null
  reviewPolling.stop()
})
</script>

<template>
  <section class="review-layout">
    <div v-if="error" class="review-alert danger">{{ error }}</div>
    <div v-if="notice" class="review-alert">{{ notice }}</div>

    <aside class="review-panel browser-panel">
      <div class="review-heading">
        <div><small>01 / 选择目录</small><h2>音乐 Inbox</h2></div>
        <Folder :size="19" />
      </div>
      <p v-if="!browser.enabled" class="review-empty">请先在配置中启用音乐预审并设置允许目录。</p>
      <p v-else-if="!browser.roots.length" class="review-empty">尚未配置可浏览的 Inbox 目录；请先在配置中添加允许目录。</p>
      <div v-else class="root-list">
        <button v-for="root in browser.roots" :key="root.path" @click="browse(root.path)">
          <Folder :size="15" />{{ root.name }}
        </button>
      </div>
      <div v-if="browser.current" class="current-path" :title="browser.current">{{ browser.current }}</div>
      <div v-if="browser.directories.length" class="folder-toolbar">
        <span>当前 {{ browser.directories.length }} 个目录 · 已选 {{ selectedCount }}</span>
        <button @click="toggleAllVisible">
          <Check :size="13" />{{ allVisibleSelected ? '取消全选' : '全选' }}
        </button>
      </div>
      <p v-else-if="browser.roots.length && browser.current" class="review-empty">当前目录没有可选择的子目录。</p>
      <div class="folder-list">
        <div v-for="dir in browser.directories" :key="dir.path" class="folder-row">
          <div class="folder-main">
            <button
              class="folder-expand"
              :class="{ expanded: expandedDirectories.has(dir.path) }"
              :aria-expanded="expandedDirectories.has(dir.path)"
              :aria-label="`查看目录中的音乐文件 ${dir.name}`"
              @click="toggleDirectoryPreview(dir.path)"
            ><ChevronRight :size="15" /></button>
            <button class="folder-open" :title="dir.path" :aria-label="`进入目录 ${dir.name}`" @click="browse(dir.path)">
              <span>{{ dir.name }}</span>
            </button>
          </div>
          <button class="folder-select" :class="{ selected: selected.has(dir.path) }" :aria-pressed="selected.has(dir.path)" @click="toggle(dir.path)">
            <Check :size="14" />{{ selected.has(dir.path) ? '已选' : '选择' }}
          </button>
          <div v-if="expandedDirectories.has(dir.path)" class="folder-files">
            <span v-if="previewLoading.has(dir.path)" class="folder-files-state">
              <LoaderCircle class="spinning" :size="14" />正在读取音乐文件…
            </span>
            <template v-else-if="directoryPreviews[dir.path]?.files?.length">
              <div class="folder-files-summary">
                {{ directoryPreviews[dir.path].total }} 个音乐文件
                <span v-if="directoryPreviews[dir.path].truncated">（仅显示前 500 个）</span>
              </div>
              <div v-for="file in directoryPreviews[dir.path].files" :key="file.relative_path" class="folder-file">
                <Music2 :size="13" /><span :title="file.relative_path">{{ file.relative_path }}</span>
              </div>
            </template>
            <span v-else class="folder-files-state">该目录没有可识别的音乐文件</span>
          </div>
        </div>
      </div>
      <button class="review-primary" :disabled="!selectedCount || loading" @click="identify">
        <LoaderCircle v-if="loading" class="spinning" :size="16" />
        <Search v-else :size="16" />识别 {{ selectedCount }} 个目录
      </button>
    </aside>

    <div class="review-main">
      <section class="review-panel batches-panel">
        <div class="batch-toolbar">
          <div>
            <small>02 / 识别批次</small>
            <h2>{{ scope === 'active' ? '待处理任务' : '归档记录' }}</h2>
          </div>
          <div class="scope-actions">
            <div class="scope-switch">
              <button :class="{ active: scope === 'active' }" @click="changeScope('active')">
                <Inbox :size="14" />待处理 <span>{{ counts.active }}</span>
              </button>
              <button :class="{ active: scope === 'archived' }" @click="changeScope('archived')">
                <Archive :size="14" />归档 <span>{{ counts.archived }}</span>
              </button>
            </div>
            <button class="review-icon" title="刷新" @click="refresh"><RefreshCw :size="16" /></button>
          </div>
        </div>
        <label v-if="scope === 'archived'" class="archive-search">
          <Search :size="15" />
          <input
            v-model="archiveQuery"
            type="search"
            placeholder="搜索艺术家、专辑或源路径"
            @input="searchArchives"
          >
          <button v-if="archiveQuery" type="button" title="清除搜索" @click="clearArchiveSearch"><X :size="14" /></button>
        </label>
        <div class="batch-tabs">
          <button v-for="batch in batches" :key="batch.id" :class="{ active: selectedBatchId === batch.id }" @click="openBatch(batch.id)">
            <strong>#{{ batch.id }}</strong>
            <span>{{ batch.item_count }} 张专辑 · {{ batchStatusText(batch.status) }}</span>
          </button>
          <p v-if="!batches.length && scope === 'active'" class="review-empty">待处理已清空；可从左侧选择新的专辑目录。</p>
          <p v-if="!batches.length && scope === 'archived'" class="review-empty">
            {{ archiveQuery ? '没有找到匹配的归档记录。' : '尚无已入库或已跳过的归档记录。' }}
          </p>
        </div>
      </section>

      <section v-if="activeBatch" class="candidate-stack">
        <article v-for="item in activeBatch.items" :key="item.id" class="review-panel album-review">
          <header class="album-header">
            <div>
              <small>{{ item.source_path }}</small>
              <h3>{{ item.current_artist || '未知艺术家' }} · {{ item.current_album || '未知专辑' }}</h3>
            </div>
            <div class="item-header-actions">
              <button
                v-if="scope === 'active' && canDecide(item)"
                class="recycle-source-button"
                :disabled="itemBusy(item)"
                title="把整个源专辑文件夹移入回收目录"
                @click="recycleSource(item)"
              ><Trash2 :size="14" />移入回收站</button>
              <button
                v-if="scope === 'active' && canDecide(item)"
                class="skip-button"
                :disabled="itemBusy(item)"
                @click="skipItem(item)"
              ><SkipForward :size="14" />跳过并归档</button>
              <button
                v-if="scope === 'archived'"
                class="delete-archive-button"
                :disabled="itemBusy(item)"
                title="只删除这条归档记录"
                @click="deleteArchive(item)"
              ><Trash2 :size="14" />删除记录</button>
              <span class="review-status" :class="item.status">{{ statusText(item.status) }}</span>
            </div>
          </header>

          <p v-if="item.error" class="inline-error">{{ item.error }}</p>

          <template v-if="scope === 'active'">
            <section v-if="canDecide(item)" class="search-panel">
              <div class="search-panel-title"><Search :size="15" /><strong>重新搜索候选</strong><span>名称搜索与发行 ID 分开，避免误操作</span></div>
              <div class="search-row metadata-row">
                <label><span>艺术家</span><input v-model="searches[item.id].artist" placeholder="例如：Lune"></label>
                <label><span>专辑</span><input v-model="searches[item.id].album" placeholder="例如：折笠富美子"></label>
                <button :disabled="itemBusy(item)" @click="searchMetadata(item)">
                  <Search :size="14" />搜索更多候选
                </button>
              </div>
              <div class="search-row release-row">
                <label><span>MusicBrainz Release ID</span><input v-model="searches[item.id].releaseId" placeholder="粘贴完整 Release UUID"></label>
                <button :disabled="itemBusy(item)" @click="exactSearch(item)">
                  <Search :size="14" />按 ID 精确识别
                </button>
              </div>
              <div class="manual-import-entry">
                <div>
                  <strong>MusicBrainz 没有对应曲目？</strong>
                  <span>按文件名解析艺术家与标题，固定进入目标库的“未分类”目录。</span>
                </div>
                <button :disabled="itemBusy(item)" @click="loadManualRules(item)">
                  <FolderInput :size="14" />{{ manualRules[item.id] ? '收起规则入库' : '按文件名规则入库' }}
                </button>
              </div>
            </section>

            <section v-if="canDecide(item) && manualRules[item.id]" class="manual-import-editor">
              <div class="decision-heading">
                <div><FolderInput :size="16" /><strong>文件名规则入库</strong></div>
                <span>目标：{{ manualDestinationDirectory(manualRules[item.id]) }}</span>
              </div>
              <p class="decision-hint">不会伪造 MusicBrainz 对应；按专辑艺术家与专辑标签组织目录，保留原文件名，只把下方确认的元数据写入标签。取消勾选的音频留在源目录。</p>
              <div class="manual-album-fields">
                <label><span>专辑艺术家</span><input v-model="manualRules[item.id].albumartist"></label>
                <label><span>专辑</span><input v-model="manualRules[item.id].album"></label>
                <label><span>年份</span><input v-model="manualRules[item.id].year" inputmode="numeric"></label>
              </div>
              <div class="manual-track-list">
                <div v-for="track in manualRules[item.id].tracks" :key="track.local_path" class="manual-track-row">
                  <label class="manual-include"><input v-model="track.include" type="checkbox"><code>{{ track.local_path }}</code></label>
                  <label><span>艺术家</span><input v-model="track.artist" :disabled="!track.include"></label>
                  <label><span>标题</span><input v-model="track.title" :disabled="!track.include"></label>
                  <label class="manual-number"><span>碟</span><input v-model="track.disc" :disabled="!track.include" inputmode="numeric"></label>
                  <label class="manual-number"><span>轨</span><input v-model="track.track" :disabled="!track.include" inputmode="numeric"></label>
                  <button class="lyrics-open" :class="{ saved: item.lyrics?.[track.local_path] }" type="button" @click="openLyricPanel(item, track, track)"><Languages :size="13" />{{ lyricStatus(item, track.local_path) }}</button>
                  <small>{{ manualDestinationDirectory(manualRules[item.id]) }}/{{ manualDestinationFilename(track.local_path) }}</small>
                </div>
              </div>
              <div v-if="manualRules[item.id].auxiliaryFiles.length" class="manual-auxiliary-files">
                <Paperclip :size="16" />
                <div>
                  <strong>附属文件将随专辑入库 · {{ manualRules[item.id].auxiliaryFiles.length }} 个</strong>
                  <span>{{ manualRules[item.id].auxiliaryFiles.join('、') }}</span>
                </div>
              </div>
              <div class="decision-bar manual-decision-bar">
                <div><strong>{{ manualRules[item.id].albumartist || '未填写艺术家' }} · {{ manualRules[item.id].album || '未填写专辑' }}</strong><span>{{ manualRules[item.id].tracks.filter((track) => track.include).length }} 首将按规则入库</span></div>
                <button :disabled="itemBusy(item)" @click="approveManual(item)"><ShieldCheck :size="15" />确认并进入未分类</button>
              </div>
            </section>

            <div v-if="canDecide(item) && item.candidates.length" class="candidate-grid">
              <button
                v-for="candidate in item.candidates"
                :key="candidate.key"
                class="candidate-card"
                :class="{ selected: selectedCandidate(item)?.key === candidate.key }"
                @click="selectCandidate(item, candidate)"
              >
                <span class="candidate-check"><Check v-if="selectedCandidate(item)?.key === candidate.key" :size="13" /></span>
                <span class="candidate-copy">
                  <strong>{{ candidate.artist || '未知艺术家' }}</strong>
                  <span>{{ candidate.album || '未知专辑' }}</span>
                  <small>{{ candidate.year || '年份未知' }} · {{ candidate.country || '地区未知' }} · {{ candidate.media || '媒介未知' }} · {{ candidate.tracks?.length || 0 }} 首</small>
                  <small v-if="candidate.artist_credit_match" class="credit-match">艺人署名精确匹配</small>
                  <small :title="candidate.album_id">Release: {{ candidate.album_id?.slice(0, 8) }}</small>
                </span>
                <span class="score">{{ Math.round(Number(candidate.score || 0) * 1000) / 10 }}%</span>
                <span v-if="candidate.extra_items?.length || candidate.extra_tracks?.length" class="candidate-warning">
                  {{ candidate.extra_items?.length || 0 }} 多余 / {{ candidate.extra_tracks?.length || 0 }} 缺失
                </span>
              </button>
            </div>

            <section v-if="canDecide(item) && selectedCandidate(item)" class="decision-editor">
              <div class="decision-heading">
                <div><SlidersHorizontal :size="16" /><strong>曲目对应与文件处理</strong></div>
                <span>{{ mappedCount(item) }} / {{ localItems(selectedCandidate(item)).length }} 个文件将入库</span>
              </div>
              <p class="decision-hint">可修正每个本地文件对应的 MusicBrainz 曲目。未对应文件默认保留；只有明确勾选后才会移入隐藏隔离区，不会永久删除。</p>

              <div class="mapping-table">
                <div
                  v-for="local in sortedLocalItems(item)"
                  :key="local.local_path"
                  class="mapping-edit-row"
                  :class="`match-${mappingConfidence(item, local).tone}`"
                >
                  <div class="local-file">
                    <small>本地文件 <b class="match-confidence">{{ mappingConfidence(item, local).label }}</b></small><code>{{ local.local_path }}</code>
                    <button
                      class="lyrics-open"
                      :class="{ saved: item.lyrics?.[local.local_path] }"
                      type="button"
                      @click="openLyricPanel(item, local)"
                    ><Languages :size="13" />{{ lyricStatus(item, local.local_path) }}</button>
                  </div>
                  <label>
                    <span>对应曲目</span>
                    <select
                      :value="decisions[item.id]?.mapping[local.local_path] || ''"
                      @change="updateMapping(item, local.local_path, $event.target.value)"
                    >
                      <option value="">不入库，保留源文件</option>
                      <option
                        v-for="track in trackOptions(selectedCandidate(item))"
                        :key="track.key || track.track_key || track.track_id"
                        :value="track.key || track.track_key || track.track_id"
                      >
                        {{ track.disc || 1 }}-{{ track.track || 0 }} {{ track.artist }} — {{ track.title }}
                      </option>
                    </select>
                  </label>
                  <div class="target-file">
                    <small>最终路径</small>
                    <code>{{ targetPath(selectedCandidate(item), local, decisions[item.id]?.mapping[local.local_path]) }}</code>
                  </div>
                  <label class="quarantine-choice" :class="{ disabled: decisions[item.id]?.mapping[local.local_path] }">
                    <input
                      type="checkbox"
                      :disabled="Boolean(decisions[item.id]?.mapping[local.local_path])"
                      :checked="decisions[item.id]?.quarantine.includes(local.local_path)"
                      @change="toggleQuarantine(item, local.local_path, $event.target.checked)"
                    >
                    入库后移入隔离区
                  </label>
                </div>
              </div>

              <details v-if="selectedCandidate(item).auxiliary_files?.length" class="auxiliary-files">
                <summary>附属文件处理 · {{ selectedCandidate(item).auxiliary_files.length }} 个</summary>
                <label v-for="path in selectedCandidate(item).auxiliary_files" :key="path">
                  <input
                    type="checkbox"
                    :checked="decisions[item.id]?.quarantine.includes(path)"
                    @change="toggleQuarantine(item, path, $event.target.checked)"
                  >
                  <code>{{ path }}</code><span>移入隔离区</span>
                </label>
              </details>

              <div v-if="selectedCandidate(item).extra_tracks?.length" class="missing-tracks">
                <strong>发行中仍缺少 {{ selectedCandidate(item).extra_tracks.length }} 首曲目</strong>
                <span v-for="track in selectedCandidate(item).extra_tracks || []" :key="track.key || track.track_id">
                  {{ track.disc }}-{{ track.track }} {{ track.artist }} — {{ track.title }}
                </span>
              </div>

              <div class="decision-bar">
                <div>
                  <strong>{{ selectedCandidate(item).artist }} · {{ selectedCandidate(item).album }}</strong>
                  <span>{{ mappedCount(item) }} 首入库 · {{ decisions[item.id]?.quarantine.length || 0 }} 个文件隔离</span>
                </div>
                <button :disabled="itemBusy(item)" @click="approve(item)">
                  <LoaderCircle v-if="itemBusy(item)" class="spinning" :size="15" />
                  <ShieldCheck v-else :size="15" />确认所选并加入入库队列
                </button>
              </div>
            </section>

            <div v-else-if="['approved', 'importing'].includes(item.status)" class="import-queue-state">
              <LoaderCircle :class="{ spinning: item.status === 'importing' }" :size="17" />
              <div>
                <strong>{{ item.status === 'importing' ? '正在执行已确认的入库方案' : '已加入持久入库队列' }}</strong>
                <span>成功后会自动从待处理移入归档，无需再次点击。</span>
              </div>
            </div>
            <p v-else-if="!['queued','identifying'].includes(item.status)" class="review-empty">没有足够可靠的候选，可使用上方名称搜索或 Release ID 精确识别。</p>
            <p v-else class="review-empty">正在读取标签并查询 MusicBrainz 候选，请稍候。</p>
          </template>

          <section v-else class="archive-summary">
            <div class="archive-result">
              <Archive :size="18" />
              <div>
                <small>{{ item.status === 'done' ? '已完成入库并自动归档' : item.import_result?.outcome === 'source_recycled' ? '源文件夹已移入回收站并归档' : '已跳过并归档' }}</small>
                <strong>{{ item.import_result?.candidate?.artist || item.current_artist || '未知艺术家' }} · {{ item.import_result?.candidate?.album || item.current_album || '未知专辑' }}</strong>
                <span>归档时间：{{ item.archived_at }}</span>
              </div>
            </div>
            <div v-if="item.import_result?.imported_track_count" class="archive-metrics">
              <span><strong>{{ item.import_result.imported_track_count }}</strong> 首已入库</span>
              <span v-if="item.import_result?.lyrics?.length"><strong>{{ item.import_result.lyrics.filter((entry) => entry.status === 'embedded').length }}</strong> 首歌词已内嵌</span>
              <span><strong>{{ cleanupCount(item) }}</strong> 个文件已隔离</span>
            </div>
            <p v-if="item.import_result?.message" class="archive-message">{{ item.import_result.message }}</p>
            <p v-for="warning in item.import_result?.warnings || []" :key="warning" class="inline-error">{{ warning }}</p>
            <div v-for="entry in item.import_result?.additional_files || []" :key="entry.destination" class="archive-cleanup">
              <code>{{ entry.source }}</code>
              <span>附加文件已移动</span>
            </div>
            <p v-if="item.import_result?.source_cleanup?.source_removed" class="archive-message">原目录已清理。</p>
            <p v-else-if="item.import_result?.source_cleanup?.remaining_files?.length" class="archive-message warning">
              原目录保留：{{ item.import_result.source_cleanup.remaining_files.join('、') }}
            </p>
            <details v-if="item.import_result?.cleanup?.length" class="auxiliary-files">
              <summary>查看隔离文件结果</summary>
              <div v-for="entry in item.import_result.cleanup" :key="entry.source" class="archive-cleanup">
                <code>{{ entry.source }}</code>
                <span :class="{ warning: entry.status === 'failed' }">{{ entry.status === 'quarantined' ? '已隔离' : entry.error }}</span>
              </div>
            </details>
          </section>
        </article>
      </section>
    </div>
  </section>

  <div v-if="lyricPanel" class="lyrics-overlay" @click.self="closeLyricPanel">
    <aside ref="lyricDrawer" class="lyrics-drawer" role="dialog" aria-modal="true" aria-label="试听与歌词匹配" tabindex="-1">
      <header class="lyrics-header">
        <div>
          <small>{{ lyricPanel.local.local_path }}</small>
          <h2>{{ lyricSearch.title || '未知标题' }}</h2>
          <span>{{ lyricSearch.artist || '未知艺术家' }}</span>
        </div>
        <button class="lyrics-close" type="button" title="关闭" aria-label="关闭歌词面板" @click="closeLyricPanel"><X :size="18" /></button>
      </header>

      <audio
        ref="lyricAudio"
        class="lyrics-audio"
        :src="lyricPanel.audioUrl"
        controls
        preload="metadata"
        @timeupdate="syncLyrics"
        @seeked="syncLyrics"
        @loadedmetadata="applyAudioPreferences"
        @volumechange="saveAudioPreferences"
      ></audio>

      <p v-if="lyricTextDamaged" class="lyrics-data-warning" role="alert">
        检测到替换字符“�”，音频标签或歌词源文本可能已在上游解码时损坏。请核对标题、艺术家和歌词后再保存。
      </p>

      <nav class="lyrics-tabs" role="tablist" aria-label="歌词预审区">
        <button id="review-lyrics-tab-preview" type="button" role="tab" aria-controls="review-lyrics-panel-preview" :aria-selected="lyricTab === 'preview'" :class="{ active: lyricTab === 'preview' }" @click="selectLyricTab('preview')">
          <Headphones :size="16" /><span>试听</span>
        </button>
        <button id="review-lyrics-tab-editor" type="button" role="tab" aria-controls="review-lyrics-panel-editor" :aria-selected="lyricTab === 'lyrics'" :class="{ active: lyricTab === 'lyrics' }" @click="selectLyricTab('lyrics')">
          <Languages :size="16" /><span>歌词</span><i v-if="lyricDirty">未保存</i>
        </button>
        <button id="review-lyrics-tab-decision" type="button" role="tab" aria-controls="review-lyrics-panel-decision" :aria-selected="lyricTab === 'decision'" :class="{ active: lyricTab === 'decision' }" @click="selectLyricTab('decision')">
          <Check :size="16" /><span>处理</span><i v-if="currentLyricDecision">已保存</i>
        </button>
      </nav>

      <div class="lyrics-commandbar">
        <p v-if="lyricFeedback.text" class="lyrics-action-feedback" :class="lyricFeedback.type" role="status" aria-live="polite">{{ lyricFeedback.text }}</p>
        <div class="lyrics-actions">
          <button class="lyrics-secondary" type="button" :disabled="lyricLoading" @click="persistLyrics({ status: 'instrumental' })">一键内嵌纯音乐提示</button>
          <button class="lyrics-secondary" type="button" :disabled="lyricLoading" @click="persistLyrics({ status: 'skipped' })">暂不处理</button>
          <button
            class="lyrics-save"
            type="button"
            :disabled="lyricLoading || !lyricSelection || !lyricContent.trim()"
            @click="persistLyrics({ ...(lyricSelection || {}), status: 'selected', content: lyricContent })"
          >
            <LoaderCircle v-if="lyricAction === 'save'" class="spinning" :size="14" />
            <Languages v-else :size="14" />{{ lyricAction === 'save' ? '保存中…' : currentLyricDecision?.status === 'selected' && !lyricDirty ? '歌词已选' : '采用此歌词' }}
          </button>
        </div>
      </div>

      <section id="review-lyrics-panel-preview" v-show="lyricTab === 'preview'" class="lyrics-preview lyrics-tab-panel" role="tabpanel" aria-labelledby="review-lyrics-tab-preview">
        <div class="lyrics-preview-title">
          <strong>同步歌词预览</strong>
          <button v-if="parsedLyrics.length" class="lyrics-follow" type="button" :class="{ active: lyricAutoFollow }" @click="lyricAutoFollow ? lyricAutoFollow = false : resumeLyricFollow()">
            {{ lyricAutoFollow ? '自动跟随中' : '恢复跟随' }}
          </button>
          <span v-else-if="lyricContent" class="lyrics-unsynced-badge">无时间轴</span>
        </div>
        <div
          v-if="parsedLyrics.length"
          ref="lyricList"
          class="lyrics-lines"
          tabindex="0"
          @pointerdown.passive="pauseLyricFollow"
          @wheel.passive="pauseLyricFollow"
          @touchstart.passive="pauseLyricFollow"
          @keydown="handleLyricScrollKey"
        >
          <button
            v-for="(line, index) in parsedLyrics"
            :key="line.time + ':' + index"
            :class="{ active: index === activeLyricIndex }"
            @click="seekToLyric(line.time)"
          >
            <span v-for="text in line.texts" :key="text">{{ text }}</span>
          </button>
        </div>
        <div v-else-if="lyricContent" class="lyrics-plain" tabindex="0">
          <p v-for="(line, index) in unsyncedLyricLines" :key="`${index}:${line}`" :class="{ blank: !line }">{{ line || '\u00a0' }}</p>
        </div>
        <p v-else class="review-empty">当前没有可试听的歌词，请切换到“歌词”搜索并选择候选。</p>
      </section>

      <section id="review-lyrics-panel-editor" v-show="lyricTab === 'lyrics'" class="lyrics-tab-panel lyrics-editor-panel" role="tabpanel" aria-labelledby="review-lyrics-tab-editor">
        <section class="lyrics-search-box">
          <div class="lyrics-query">
            <label><span>标题</span><input v-model="lyricSearch.title"></label>
            <label><span>艺术家</span><input v-model="lyricSearch.artist"></label>
            <button type="button" :disabled="lyricLoading" @click="searchLyrics">
              <LoaderCircle v-if="lyricAction === 'search'" class="spinning" :size="14" />
              <Search v-else :size="14" />{{ lyricAction === 'search' ? '搜索中…' : '搜索歌词' }}
            </button>
          </div>
          <div class="lyrics-sources">
            <label><input v-model="lyricSearch.sources.netease" type="checkbox">网易云音乐</label>
            <label><input v-model="lyricSearch.sources.qqmusic" type="checkbox">QQ 音乐</label>
            <label><input v-model="lyricSearch.sources.kugou" type="checkbox">酷狗音乐</label>
            <span>排序：网易云 → QQ → 酷狗</span>
            <span v-if="lyricWarnings.length" class="lyrics-warning">{{ lyricWarnings.join('；') }}</span>
          </div>
        </section>

        <section v-if="lyricCandidates.length" class="lyrics-candidates">
          <button
            v-for="candidate in lyricCandidates"
            :key="candidate.source + ':' + candidate.provider_id"
            :class="{ selected: lyricSelection?.source === candidate.source && lyricSelection?.provider_id === candidate.provider_id }"
            type="button"
            :disabled="lyricLoading"
            @click="previewLyrics(candidate)"
          >
            <span class="lyrics-source">{{ candidate.source }}</span>
            <span class="lyrics-candidate-copy">
              <strong>{{ lyricCandidateTitle(candidate, lyricSearch.title) }}</strong>
              <small>{{ candidate.artist || '未知艺术家' }} · {{ candidate.album || '未知专辑' }}</small>
              <small class="lyrics-match-details">{{ lyricCandidateMatchSummary(candidate) }}</small>
            </span>
            <span class="lyrics-score">匹配 {{ Math.round(Number(candidate.score || 0) * 100) }}%</span>
          </button>
        </section>
        <p v-else class="review-empty">搜索后在这里选择候选，载入后可以继续修改 LRC。</p>

        <label class="lyrics-editor">
          <span>歌词内容</span>
          <textarea v-model="lyricContent" rows="11" placeholder="选择候选后可在保存前修改 LRC" @input="lyricDirty = true"></textarea>
        </label>
        <div class="lyric-processing-tools">
          <button type="button" :disabled="lyricLoading || !lyricContent.trim()" @click="processLyrics('standard')">标准格式</button>
          <label><input v-model="preserveWordTiming" type="checkbox">保留逐字时间</label>
          <button type="button" :disabled="lyricLoading || !lyricContent.trim()" @click="processLyrics('blanks')">压缩空白行</button>
          <button type="button" :disabled="lyricLoading || !lyricContent.trim()" @click="processLyrics('simplified')">转为简体</button>
          <button type="button" :disabled="lyricLoading || !lyricContent.trim()" @click="translateCurrentLyrics">
            <LoaderCircle v-if="lyricAction === 'translate'" class="spinning" :size="13" />{{ lyricAction === 'translate' ? '翻译中…' : 'AI 翻译日文' }}
          </button>
          <label class="lyric-offset-control"><span>偏移</span><input v-model.number="lyricOffset" type="number" step="100" aria-label="歌词偏移毫秒"><i>ms</i></label>
          <button type="button" :disabled="lyricLoading || !lyricContent.trim()" @click="processLyrics('offset')">应用偏移</button>
        </div>
      </section>

      <section id="review-lyrics-panel-decision" v-show="lyricTab === 'decision'" class="lyrics-tab-panel lyrics-decision-panel" role="tabpanel" aria-labelledby="review-lyrics-tab-decision">
        <div class="lyrics-decision-summary">
          <Languages :size="20" />
          <div>
            <small>当前歌词处理</small>
            <strong v-if="currentLyricDecision?.status === 'selected'">已选择歌词 · {{ currentLyricDecision.source || '手工内容' }}</strong>
            <strong v-else-if="currentLyricDecision?.status === 'instrumental'">已设置纯音乐内嵌提示</strong>
            <strong v-else-if="currentLyricDecision?.status === 'skipped'">已保存为暂不处理</strong>
            <strong v-else>尚未保存处理结果</strong>
            <span v-if="lyricSelection">{{ lyricSelection.synced ? '含时间轴' : '无时间轴' }} · {{ lyricSelection.source || '手工内容' }}</span>
          </div>
        </div>
        <p>歌词选择和纯音乐提示会先写入预审任务；确认专辑入库时再写进目标音频标签。</p>
      </section>
    </aside>
  </div>
</template>

<style scoped src="./review-workspace.css"></style>
