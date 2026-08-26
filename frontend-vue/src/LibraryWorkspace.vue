<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ArchiveRestore,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  FileAudio,
  Folder,
  FolderOpen,
  Headphones,
  Languages,
  LoaderCircle,
  Music2,
  RefreshCw,
  Save,
  Search,
  Trash2,
  X,
} from '@lucide/vue'
import {
  fetchLibraryLyrics,
  getLibraryAudioUrl,
  getLibraryFolders,
  getLibraryTrack,
  getLibraryTrash,
  restoreLibraryTrash,
  saveLibraryLyrics,
  searchLibraryLyrics,
  trashLibraryFolder,
  trashLibraryTrack,
  translateLyrics,
  updateLibraryTrack,
} from './api'
import { applyAudioPreferences, saveAudioPreferences } from './audio-preferences'
import {
  hasTextDecodeDamage,
  highConfidenceLyricCandidate,
  lyricCandidateTitle,
  lyricCandidateMatchSummary,
  lyricQualitySummary,
  lyricBuffersFromState,
  parseSyncedLyrics,
  plainLyricLines,
  readPreferredLyricMode,
  rememberPreferredLyricMode,
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
import { createLatestRequestGate } from './requestState'

const listing = ref({ folders: [], total: 0, track_total: 0, offset: 0, limit: 20, root: '', order: 'desc' })
const openFolders = ref(new Set())
const query = ref('')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const selected = ref(null)
const tagForm = ref({})
const lyricsMode = ref('embedded')
const lyricsByMode = ref({ embedded: '', sidecar: '' })
const savedLyricsByMode = ref({ embedded: '', sidecar: '' })
const lyricSources = ref({ netease: true, qqmusic: true, kugou: true })
const lyricQuery = ref({ title: '', artist: '' })
const lyricCandidates = ref([])
const lyricWarnings = ref([])
const lyricOperation = ref('')
const lyricCandidateKey = ref('')
const lyricFeedback = ref({ type: '', text: '' })
const tagSaving = ref(false)
const tagFeedback = ref({ type: '', text: '' })
const activeEditorPanel = ref('lyrics')
const playbackTime = ref(0)
const lyricList = ref(null)
const lastActiveLyricIndex = ref(-1)
const lyricAutoFollow = ref(true)
const preserveWordTiming = ref(true)
const lyricOffset = ref(0)
const audioPlayer = ref(null)
const libraryHeading = ref(null)
const libraryDrawer = ref(null)
const trashDrawer = ref(null)
const trashOpen = ref(false)
const trashEntries = ref([])
let trackReturnFocus = null
let trashReturnFocus = null
let releaseTrackBodyLock = null
let releaseTrashBodyLock = null
const trackRequests = createLatestRequestGate()
const lyricRequests = createLatestRequestGate()
const INSTRUMENTAL_LYRIC = '[00:05.00]纯音乐，请欣赏'

const lyricsContent = computed({
  get: () => lyricsByMode.value[lyricsMode.value] || '',
  set: (value) => {
    lyricsByMode.value = { ...lyricsByMode.value, [lyricsMode.value]: value }
  },
})
const lyricBusy = computed(() => Boolean(lyricOperation.value))
const lyricsDirty = computed(() => (
  lyricsContent.value !== (savedLyricsByMode.value[lyricsMode.value] || '')
))
const anyLyricsDirty = computed(() => ['embedded', 'sidecar'].some(
  (mode) => (lyricsByMode.value[mode] || '') !== (savedLyricsByMode.value[mode] || ''),
))
const tagsDirty = computed(() => {
  if (!selected.value) return false
  const current = selected.value.tags || {}
  return Object.keys(tagForm.value).some(
    (key) => String(tagForm.value[key] ?? '') !== String(current[key] ?? ''),
  )
})

const page = computed(() => Math.floor(listing.value.offset / listing.value.limit) + 1)
const pages = computed(() => Math.max(1, Math.ceil(listing.value.total / listing.value.limit)))
const lyricTextDamaged = computed(() => hasTextDecodeDamage(
  selected.value?.name,
  selected.value?.tags?.title,
  selected.value?.tags?.artist,
  lyricsContent.value,
))

function formatSize(value) {
  const bytes = Number(value || 0)
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.round(Number(value || 0)))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

async function loadFolders(offset = 0) {
  loading.value = true
  error.value = ''
  try {
    let result = await getLibraryFolders(query.value.trim(), offset, listing.value.limit, 'desc')
    if (!result.folders?.length && offset > 0) {
      result = await getLibraryFolders(
        query.value.trim(),
        Math.max(0, offset - listing.value.limit),
        listing.value.limit,
        'desc',
      )
    }
    listing.value = result
    const visible = new Set((result.folders || []).map((folder) => folder.path))
    const next = new Set([...openFolders.value].filter((path) => visible.has(path)))
    if (query.value.trim() && visible.size <= 10) {
      visible.forEach((path) => next.add(path))
    }
    openFolders.value = next
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

function folderIsOpen(path) {
  return openFolders.value.has(path)
}

function toggleFolder(path) {
  const next = new Set(openFolders.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  openFolders.value = next
}

async function openTrack(track) {
  const opener = globalThis.document?.activeElement || null
  const requestToken = trackRequests.begin()
  loading.value = true
  error.value = ''
  try {
    const detail = await getLibraryTrack(track.path)
    if (!trackRequests.isCurrent(requestToken)) return
    const opening = !selected.value
    selected.value = detail
    if (opening) {
      trackReturnFocus = opener
      releaseTrackBodyLock = lockBodyScroll()
    }
    tagForm.value = { ...detail.tags }
    lyricQuery.value = {
      title: detail.tags.title || detail.name.replace(/\.[^.]+$/, ''),
      artist: detail.tags.artist || detail.tags.albumartist || '',
    }
    lyricsByMode.value = lyricBuffersFromState(detail.lyrics)
    savedLyricsByMode.value = lyricBuffersFromState(detail.lyrics)
    lyricsMode.value = readPreferredLyricMode(detail.path, detail.lyrics)
    lyricCandidates.value = []
    lyricWarnings.value = []
    lyricFeedback.value = { type: '', text: '' }
    tagFeedback.value = { type: '', text: '' }
    lyricOperation.value = ''
    lyricCandidateKey.value = ''
    activeEditorPanel.value = 'lyrics'
    playbackTime.value = 0
    lastActiveLyricIndex.value = -1
    lyricAutoFollow.value = true
    await nextTick()
    if (!trackRequests.isCurrent(requestToken)) return
    if (opening) focusModal(libraryDrawer.value)
    audioPlayer.value?.play().catch(() => {})
    if (lyricsContent.value.trim()) {
      activeEditorPanel.value = 'preview'
    } else {
      await searchLyrics({ autoPreview: true })
    }
  } catch (requestError) {
    if (trackRequests.isCurrent(requestToken)) error.value = requestError.message
  } finally {
    if (trackRequests.isCurrent(requestToken)) loading.value = false
  }
}

function closeTrack(force = false) {
  if (
    force !== true
    && (anyLyricsDirty.value || tagsDirty.value)
    && !window.confirm('歌词或标签还有未保存的修改，仍要关闭吗？')
  ) return
  trackRequests.begin()
  lyricRequests.begin()
  selected.value = null
  lyricQuery.value = { title: '', artist: '' }
  lyricCandidates.value = []
  lyricsByMode.value = { embedded: '', sidecar: '' }
  savedLyricsByMode.value = { embedded: '', sidecar: '' }
  lyricOperation.value = ''
  lyricCandidateKey.value = ''
  lyricFeedback.value = { type: '', text: '' }
  tagFeedback.value = { type: '', text: '' }
  playbackTime.value = 0
  lastActiveLyricIndex.value = -1
  const returnFocus = trackReturnFocus
  trackReturnFocus = null
  releaseTrackBodyLock?.()
  releaseTrackBodyLock = null
  nextTick(() => restoreModalFocus(returnFocus, libraryHeading.value))
}

function warnBeforeUnload(event) {
  if (!selected.value || (!anyLyricsDirty.value && !tagsDirty.value)) return
  event.preventDefault()
  event.returnValue = ''
}

async function saveTags() {
  if (!selected.value) return
  tagSaving.value = true
  tagFeedback.value = { type: 'working', text: '正在保存并回读标签…' }
  try {
    const result = await updateLibraryTrack(selected.value.path, tagForm.value)
    selected.value.tags = result.tags
    tagForm.value = { ...result.tags }
    tagFeedback.value = { type: 'success', text: '标签已保存，并已从音频文件回读校验。' }
    notice.value = '音频标签已原地保存并回读校验。'
    await loadFolders(listing.value.offset)
  } catch (requestError) {
    tagFeedback.value = { type: 'error', text: requestError.message }
  } finally {
    tagSaving.value = false
  }
}

async function searchLyrics(options = {}) {
  if (!selected.value || lyricBusy.value) return
  const title = String(lyricQuery.value.title || '').trim()
  const artist = String(lyricQuery.value.artist || '').trim()
  if (!title) {
    lyricFeedback.value = { type: 'error', text: '请填写歌曲标题后再搜索。' }
    return
  }
  const sources = Object.entries(lyricSources.value)
    .filter(([, enabled]) => enabled)
    .map(([source]) => source)
  if (!sources.length) {
    lyricFeedback.value = { type: 'error', text: '请至少选择一个歌词来源。' }
    return
  }
  const requestToken = lyricRequests.begin()
  const selectedPath = selected.value.path
  lyricOperation.value = 'search'
  lyricCandidates.value = []
  lyricWarnings.value = []
  lyricFeedback.value = { type: 'working', text: '正在搜索歌词…' }
  try {
    const result = await searchLibraryLyrics({
      path: selectedPath,
      title,
      artist,
      artist_aliases: [tagForm.value.artist, tagForm.value.albumartist].filter(Boolean),
      album: tagForm.value.album || '',
      duration: selected.value.duration,
      sources,
    })
    if (!lyricRequests.isCurrent(requestToken) || selected.value?.path !== selectedPath) return
    lyricCandidates.value = result.candidates || []
    lyricWarnings.value = result.warnings || []
    lyricFeedback.value = lyricCandidates.value.length
      ? { type: 'success', text: `已找到 ${lyricCandidates.value.length} 条候选，请选择一条预览。` }
      : { type: 'info', text: '搜索完成，没有找到匹配歌词。可修改上方标题或艺术家后重试，也可直接粘贴 LRC。' }
    const automaticCandidate = options?.autoPreview === true
      ? highConfidenceLyricCandidate(lyricCandidates.value)
      : null
    if (automaticCandidate) {
      lyricOperation.value = ''
      await chooseLyrics(automaticCandidate, { automatic: true })
    }
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) lyricOperation.value = ''
  }
}

async function chooseLyrics(candidate, options = {}) {
  if (!selected.value || lyricBusy.value) return
  const requestToken = lyricRequests.begin()
  const selectedPath = selected.value.path
  lyricCandidateKey.value = `${candidate.source}:${candidate.provider_id}`
  lyricOperation.value = 'fetch'
  lyricFeedback.value = { type: 'working', text: '正在读取所选歌词…' }
  try {
    const result = await fetchLibraryLyrics({
      path: selectedPath,
      candidate,
    })
    if (!lyricRequests.isCurrent(requestToken) || selected.value?.path !== selectedPath) return
    lyricsContent.value = result.content || ''
    lyricFeedback.value = {
      type: 'info',
      text: options?.automatic === true
        ? `已自动选择最高分候选（曲目匹配 ${Math.round(Number(candidate.score || 0) * 100)}%）并载入试听；${lyricQualitySummary(result.quality)}，尚未保存。`
        : `歌词已载入预览；${lyricQualitySummary(result.quality)}，尚未保存。`,
    }
    activeEditorPanel.value = 'preview'
    lyricAutoFollow.value = true
    playbackTime.value = 0
    lastActiveLyricIndex.value = -1
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricOperation.value = ''
      lyricCandidateKey.value = ''
    }
  }
}

async function persistLyrics() {
  if (!selected.value || lyricBusy.value || !lyricsContent.value.trim()) return
  const requestToken = lyricRequests.begin()
  const selectedPath = selected.value.path
  const mode = lyricsMode.value
  const content = lyricsContent.value
  lyricOperation.value = 'save'
  lyricFeedback.value = { type: 'working', text: '正在保存并回读校验歌词…' }
  try {
    const result = await saveLibraryLyrics({
      path: selectedPath,
      content,
      mode,
    })
    if (!lyricRequests.isCurrent(requestToken) || selected.value?.path !== selectedPath) return
    selected.value.lyrics = result.lyrics
    const verifiedBuffers = lyricBuffersFromState(result.lyrics)
    lyricsByMode.value = { ...lyricsByMode.value, [mode]: verifiedBuffers[mode] }
    savedLyricsByMode.value = { ...savedLyricsByMode.value, [mode]: verifiedBuffers[mode] }
    lyricsMode.value = mode
    rememberPreferredLyricMode(selectedPath, mode)
    const destination = mode === 'embedded' ? '音频内置标签' : '同名 UTF-8 .lrc 文件'
    lyricFeedback.value = {
      type: 'saved',
      text: `歌词已保存到${destination}，并已回读校验。`,
    }
    notice.value = mode === 'embedded'
      ? '歌词已写入音频内置标签。'
      : '歌词已保存为同名 UTF-8 .lrc 文件。'
    lyricOperation.value = ''
    closeTrack(true)
    void loadFolders(listing.value.offset)
  } catch (requestError) {
    if (lyricRequests.isCurrent(requestToken)) {
      lyricFeedback.value = { type: 'error', text: requestError.message }
    }
  } finally {
    if (lyricRequests.isCurrent(requestToken)) lyricOperation.value = ''
  }
}

async function embedInstrumentalLyrics() {
  if (!selected.value || lyricBusy.value) return
  const embedded = String(lyricsByMode.value.embedded || '').trim()
  if (
    embedded
    && embedded !== INSTRUMENTAL_LYRIC
    && !window.confirm('当前已有内嵌歌词，确认替换为纯音乐提示？')
  ) return
  lyricsMode.value = 'embedded'
  lyricsContent.value = INSTRUMENTAL_LYRIC
  lyricFeedback.value = { type: 'info', text: '正在写入纯音乐提示…' }
  await persistLyrics()
}

function processLyrics(action) {
  if (!lyricsContent.value.trim()) {
    lyricFeedback.value = { type: 'info', text: '当前没有可处理的歌词。' }
    return
  }
  try {
    const processors = {
      standard: () => standardizeLyrics(lyricsContent.value, { preserveWordTiming: preserveWordTiming.value }),
      blanks: () => compressBlankLines(lyricsContent.value),
      simplified: () => convertLyricsToSimplified(lyricsContent.value),
      offset: () => adjustLyricsOffset(lyricsContent.value, lyricOffset.value),
    }
    lyricsContent.value = processors[action]()
    const labels = {
      standard: preserveWordTiming.value ? '已转换为标准 LRC，并保留逐字时间。' : '已转换为标准 LRC，并展开为普通行时间。',
      blanks: '已压缩连续空白行。',
      simplified: '已使用 OpenCC 转换为简体中文。',
      offset: `已把全部行与逐字时间调整 ${Number(lyricOffset.value) >= 0 ? '+' : ''}${Math.round(Number(lyricOffset.value))} ms。`,
    }
    lyricFeedback.value = { type: 'info', text: `${labels[action]} 尚未保存。` }
  } catch (processingError) {
    lyricFeedback.value = { type: 'error', text: processingError.message }
  }
}

async function translateCurrentLyrics() {
  if (!selected.value || lyricBusy.value || !lyricsContent.value.trim()) return
  const requestToken = lyricRequests.begin()
  const selectedPath = selected.value.path
  lyricOperation.value = 'translate'
  lyricFeedback.value = { type: 'working', text: '正在使用 AI 翻译日文歌词…' }
  try {
    const result = await translateLyrics({
      content: lyricsContent.value,
      title: tagForm.value.title || selected.value.name.replace(/\.[^.]+$/, ''),
      artist: tagForm.value.artist || tagForm.value.albumartist || '',
    })
    if (!lyricRequests.isCurrent(requestToken) || selected.value?.path !== selectedPath) return
    lyricsContent.value = result.content || lyricsContent.value
    activeEditorPanel.value = 'preview'
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
    if (lyricRequests.isCurrent(requestToken)) lyricOperation.value = ''
  }
}

async function removeTrack(track = selected.value) {
  if (!track) return
  if (!window.confirm(`把“${track.name}”移入音乐库回收区？可稍后恢复。`)) return
  loading.value = true
  error.value = ''
  try {
    await trashLibraryTrack(track.path)
    notice.value = '音频及同名 .lrc 已移入隐藏回收区。'
    if (selected.value?.path === track.path) {
      trackReturnFocus = libraryHeading.value
      closeTrack(true)
    }
    await loadFolders(listing.value.offset)
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

async function removeFolder(folder) {
  if (!folder?.deletable) return
  const confirmed = window.confirm(
    `把文件夹“${folder.path}”整体移入回收区？其中 ${folder.track_count} 首音频及封面、歌词等附属文件都会一起移动，可稍后恢复。`,
  )
  if (!confirmed) return
  loading.value = true
  error.value = ''
  try {
    await trashLibraryFolder(folder.path)
    notice.value = `文件夹“${folder.path}”已整体移入隐藏回收区。`
    const next = new Set(openFolders.value)
    next.delete(folder.path)
    openFolders.value = next
    if (selected.value?.directory === folder.path) {
      trackReturnFocus = libraryHeading.value
      closeTrack(true)
    }
    await loadFolders(listing.value.offset)
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

async function openTrash() {
  const opening = !trashOpen.value
  if (opening) {
    trashReturnFocus = globalThis.document?.activeElement || null
    trashOpen.value = true
    releaseTrashBodyLock = lockBodyScroll()
    await nextTick()
    focusModal(trashDrawer.value)
  }
  try {
    trashEntries.value = (await getLibraryTrash()).entries || []
  } catch (requestError) {
    error.value = requestError.message
  }
}

function closeTrash() {
  if (!trashOpen.value) return
  trashOpen.value = false
  const returnFocus = trashReturnFocus
  trashReturnFocus = null
  releaseTrashBodyLock?.()
  releaseTrashBodyLock = null
  nextTick(() => restoreModalFocus(returnFocus))
}

async function restore(entry) {
  loading.value = true
  try {
    await restoreLibraryTrash(entry.token)
    notice.value = '文件已恢复到原位置。'
    await Promise.all([openTrash(), loadFolders(listing.value.offset)])
    await nextTick()
    if (trashOpen.value && !trashDrawer.value?.contains(globalThis.document?.activeElement)) {
      focusModal(trashDrawer.value)
    }
  } catch (requestError) {
    error.value = requestError.message
  } finally {
    loading.value = false
  }
}

const parsedLyrics = computed(() => parseSyncedLyrics(lyricsContent.value))
const unsyncedLyricLines = computed(() => plainLyricLines(lyricsContent.value))

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
  if (activeEditorPanel.value !== 'preview' || !lyricAutoFollow.value) return
  await nextTick()
  const active = lyricList.value?.querySelector('.active')
  scrollLyricContainer(lyricList.value, active)
}

function seekLyrics(time) {
  const audio = audioPlayer.value
  if (!audio) return
  audio.currentTime = time
  audio.play().catch(() => {})
}

async function selectEditorPanel(panel) {
  activeEditorPanel.value = panel
  await nextTick()
  libraryDrawer.value?.scrollTo({ top: 0, behavior: 'auto' })
  if (panel !== 'preview' || !lyricAutoFollow.value) return
  const active = lyricList.value?.querySelector('.active')
  scrollLyricContainer(lyricList.value, active, 'auto')
}

function selectLyricsMode(mode) {
  lyricsMode.value = mode
  lyricFeedback.value = { type: '', text: '' }
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
  if (event.key === 'Tab') {
    if (trashOpen.value) trapModalTab(event, trashDrawer.value)
    else if (selected.value) trapModalTab(event, libraryDrawer.value)
    return
  }
  if (event.key !== 'Escape') return
  if (trashOpen.value) closeTrash()
  else if (selected.value) closeTrack()
}

onMounted(() => {
  window.addEventListener('beforeunload', warnBeforeUnload)
  window.addEventListener('keydown', handleWorkspaceKey)
  loadFolders()
})
onBeforeUnmount(() => {
  trackRequests.begin()
  lyricRequests.begin()
  window.removeEventListener('beforeunload', warnBeforeUnload)
  window.removeEventListener('keydown', handleWorkspaceKey)
  releaseTrackBodyLock?.()
  releaseTrashBodyLock?.()
  releaseTrackBodyLock = null
  releaseTrashBodyLock = null
})
</script>

<template>
  <section class="library-workspace">
    <div v-if="error" class="library-alert danger">{{ error }}</div>
    <div v-if="notice" class="library-alert">{{ notice }}</div>

    <section class="library-toolbar">
      <div>
        <small>DIRECT LIBRARY</small>
        <h2 ref="libraryHeading" tabindex="-1"><Database :size="19" />目标音乐库</h2>
        <code>{{ listing.root || '正在读取目标目录…' }}</code>
      </div>
      <form @submit.prevent="loadFolders(0)">
        <Search :size="16" />
        <input v-model="query" placeholder="搜索路径、艺术家、标题或专辑">
        <button type="submit">搜索</button>
      </form>
      <button class="library-quiet" type="button" @click="openTrash"><Trash2 :size="15" />回收区</button>
      <button class="library-icon" type="button" title="刷新" @click="loadFolders(listing.offset)">
        <RefreshCw :size="16" :class="{ spinning: loading }" />
      </button>
    </section>

    <section class="library-list-panel">
      <div class="library-list-heading">
        <span>共 {{ listing.total }} 个文件夹 / {{ listing.track_total }} 首 · 第 {{ page }} / {{ pages }} 页 · 路径倒序</span>
        <div>
          <button :disabled="listing.offset <= 0" @click="loadFolders(Math.max(0, listing.offset - listing.limit))"><ChevronLeft :size="15" /></button>
          <button :disabled="listing.offset + listing.limit >= listing.total" @click="loadFolders(listing.offset + listing.limit)"><ChevronRight :size="15" /></button>
        </div>
      </div>
      <div v-if="loading && !listing.folders.length" class="library-empty"><LoaderCircle class="spinning" :size="18" />扫描音乐库…</div>
      <div v-else-if="!listing.folders.length" class="library-empty">没有找到音频文件夹。</div>
      <article v-for="folder in listing.folders" :key="folder.path" class="library-folder">
        <header>
          <button class="folder-toggle" type="button" @click="toggleFolder(folder.path)">
            <ChevronDown v-if="folderIsOpen(folder.path)" :size="16" />
            <ChevronRight v-else :size="16" />
            <span class="folder-icon"><FolderOpen v-if="folderIsOpen(folder.path)" :size="19" /><Folder v-else :size="19" /></span>
            <span class="folder-main"><strong>{{ folder.name }}</strong><code>{{ folder.path }}</code></span>
            <span class="folder-summary">
              <i v-if="folder.all_embedded" class="folder-lyrics-complete">内嵌完成</i>
              {{ folder.track_count }} 首 · {{ formatSize(folder.size) }}
            </span>
          </button>
          <button v-if="folder.deletable" class="folder-trash" type="button" title="整个文件夹移入回收区" @click="removeFolder(folder)">
            <Trash2 :size="15" /><span>删除文件夹</span>
          </button>
        </header>
        <div v-if="folderIsOpen(folder.path)" class="folder-tracks">
          <div v-for="track in folder.tracks" :key="track.path" class="library-track">
            <button class="track-open" type="button" @click="openTrack(track)">
              <span class="track-file-icon"><FileAudio :size="18" /></span>
              <span class="track-main">
                <strong>{{ track.tags.title || track.name }}</strong>
                <span>{{ track.tags.artist || track.tags.albumartist || '未知艺术家' }} · {{ track.tags.album || '未知专辑' }}</span>
                <code>{{ track.name }}</code>
              </span>
              <span class="track-flags">
                <span><i v-if="track.lyrics.embedded">内嵌</i><i v-if="track.lyrics.sidecar">LRC</i></span>
                <small>{{ formatDuration(track.duration) }} · {{ formatSize(track.size) }}</small>
              </span>
            </button>
            <button class="track-trash" type="button" title="单曲移入回收区" @click="removeTrack(track)"><Trash2 :size="15" /></button>
          </div>
        </div>
      </article>
    </section>

    <div v-if="selected" class="library-drawer-backdrop" @click.self="closeTrack()">
      <aside ref="libraryDrawer" class="library-drawer" role="dialog" aria-modal="true" aria-label="音乐详情与歌词编辑" tabindex="-1">
        <header>
          <div><small>直接编辑 · 不复制文件</small><h2>{{ selected.tags.title || selected.name }}</h2><code>{{ selected.path }}</code></div>
          <button type="button" aria-label="关闭音乐详情" @click="closeTrack()"><X :size="18" /></button>
        </header>
        <audio ref="audioPlayer" class="library-audio" controls preload="metadata" :src="getLibraryAudioUrl(selected.path)" @timeupdate="syncLyrics" @seeked="syncLyrics" @loadedmetadata="applyAudioPreferences" @volumechange="saveAudioPreferences"></audio>

        <p v-if="lyricTextDamaged" class="library-data-warning" role="alert">
          检测到替换字符“�”，音频标签或歌词文本可能已损坏。请核对后再保存。
        </p>

        <nav class="library-editor-tabs" role="tablist" aria-label="音乐编辑区">
          <button id="library-tab-preview" type="button" role="tab" aria-controls="library-panel-preview" :aria-selected="activeEditorPanel === 'preview'" :class="{ active: activeEditorPanel === 'preview' }" @click="selectEditorPanel('preview')">
            <Headphones :size="16" /><span>试听</span>
          </button>
          <button id="library-tab-lyrics" type="button" role="tab" aria-controls="library-panel-lyrics" :aria-selected="activeEditorPanel === 'lyrics'" :class="{ active: activeEditorPanel === 'lyrics' }" @click="selectEditorPanel('lyrics')">
            <Languages :size="16" /><span>歌词</span><i v-if="anyLyricsDirty">未保存</i>
          </button>
          <button id="library-tab-tags" type="button" role="tab" aria-controls="library-panel-tags" :aria-selected="activeEditorPanel === 'tags'" :class="{ active: activeEditorPanel === 'tags' }" @click="selectEditorPanel('tags')">
            <Music2 :size="16" /><span>标签</span><i v-if="tagsDirty">未保存</i>
          </button>
        </nav>

        <div v-if="activeEditorPanel !== 'tags'" class="library-lyrics-commandbar">
          <p v-if="lyricFeedback.text" class="library-action-feedback" :class="lyricFeedback.type" role="status" aria-live="polite">{{ lyricFeedback.text }}</p>
          <div class="library-lyrics-actions">
            <button class="library-secondary" type="button" :disabled="lyricBusy" @click="embedInstrumentalLyrics">
              <Music2 :size="15" />一键内嵌“纯音乐，请欣赏”
            </button>
            <button class="library-primary" type="button" :disabled="lyricBusy || !lyricsContent.trim()" @click="persistLyrics">
              <LoaderCircle v-if="lyricOperation === 'save'" class="spinning" :size="15" />
              <Check v-else-if="!lyricsDirty && lyricFeedback.type === 'saved'" :size="15" />
              <Save v-else :size="15" />
              {{ lyricOperation === 'save' ? '保存中…' : (!lyricsDirty && lyricFeedback.type === 'saved' ? '已保存' : '保存歌词') }}
            </button>
          </div>
        </div>

        <section id="library-panel-preview" v-show="activeEditorPanel === 'preview'" class="library-editor-section library-preview-section" role="tabpanel" aria-labelledby="library-tab-preview">
          <div class="section-title">
            <Headphones :size="16" /><strong>同步试听</strong>
            <button v-if="parsedLyrics.length" class="lyrics-follow" type="button" :class="{ active: lyricAutoFollow }" @click="lyricAutoFollow ? lyricAutoFollow = false : resumeLyricFollow()">
              {{ lyricAutoFollow ? '自动跟随中' : '恢复跟随' }}
            </button>
            <span v-else-if="lyricsContent" class="lyrics-unsynced-badge">无时间轴</span>
          </div>
          <div
            v-if="parsedLyrics.length"
            ref="lyricList"
            class="library-synced-lyrics"
            tabindex="0"
            @pointerdown.passive="pauseLyricFollow"
            @wheel.passive="pauseLyricFollow"
            @touchstart.passive="pauseLyricFollow"
            @keydown="handleLyricScrollKey"
          >
            <button v-for="(line, index) in parsedLyrics" :key="`${line.time}:${index}`" :class="{ active: index === activeLyricIndex }" @click="seekLyrics(line.time)">
              <span v-for="text in line.texts" :key="text">{{ text }}</span>
            </button>
          </div>
          <div v-else-if="lyricsContent" class="library-plain-lyrics" tabindex="0">
            <p v-for="(line, index) in unsyncedLyricLines" :key="`${index}:${line}`" :class="{ blank: !line }">{{ line || '\u00a0' }}</p>
          </div>
          <p v-else class="library-panel-empty">当前保存位置没有歌词，请切换到“歌词”进行搜索或粘贴。</p>
        </section>

        <section id="library-panel-lyrics" v-show="activeEditorPanel === 'lyrics'" class="library-editor-section" role="tabpanel" aria-labelledby="library-tab-lyrics">
          <div class="section-title"><Languages :size="16" /><strong>歌词</strong><span>搜索后选择写入位置</span></div>
          <div class="library-lyrics-query">
            <label><span>标题</span><input v-model="lyricQuery.title" type="text" placeholder="输入歌曲标题" @keydown.enter="searchLyrics()"></label>
            <label><span>艺术家</span><input v-model="lyricQuery.artist" type="text" placeholder="输入艺术家" @keydown.enter="searchLyrics()"></label>
            <button type="button" :disabled="lyricBusy" @click="searchLyrics">
              <LoaderCircle v-if="lyricOperation === 'search'" class="spinning" :size="14" />
              <Search v-else :size="14" />{{ lyricOperation === 'search' ? '搜索中…' : '搜索歌词' }}
            </button>
          </div>
          <div class="lyrics-source-row">
            <label><input v-model="lyricSources.netease" type="checkbox">网易云音乐</label>
            <label><input v-model="lyricSources.qqmusic" type="checkbox">QQ 音乐</label>
            <label><input v-model="lyricSources.kugou" type="checkbox">酷狗音乐</label>
            <span class="lyrics-source-priority">排序：网易云 → QQ → 酷狗</span>
          </div>
          <p v-for="warning in lyricWarnings" :key="warning" class="library-warning">{{ warning }}</p>
          <div v-if="lyricCandidates.length" class="library-candidates">
            <button v-for="candidate in lyricCandidates" :key="`${candidate.source}:${candidate.provider_id}`" type="button" :disabled="lyricBusy" @click="chooseLyrics(candidate)">
              <strong>{{ lyricCandidateTitle(candidate, lyricQuery.title) }}</strong>
              <span>{{ candidate.artist || '未知艺术家' }} · {{ candidate.album || '未知专辑' }} · {{ candidate.source }}</span>
              <small>{{ lyricCandidateMatchSummary(candidate) }}</small>
              <i>曲目匹配 {{ Math.round(Number(candidate.score || 0) * 100) }}%</i>
              <LoaderCircle v-if="lyricOperation === 'fetch' && lyricCandidateKey === `${candidate.source}:${candidate.provider_id}`" class="spinning candidate-loader" :size="15" />
            </button>
          </div>
          <div class="lyrics-mode-row">
            <label><input type="radio" value="embedded" :checked="lyricsMode === 'embedded'" @change="selectLyricsMode('embedded')">写入内置标签 <i>{{ selected.lyrics?.embedded?.exists ? '已保存' : '空' }}</i></label>
            <label><input type="radio" value="sidecar" :checked="lyricsMode === 'sidecar'" @change="selectLyricsMode('sidecar')">保存同名 .lrc <i>{{ selected.lyrics?.sidecar?.exists ? '已保存' : '空' }}</i></label>
          </div>
          <textarea v-model="lyricsContent" rows="9" placeholder="可搜索候选，也可直接粘贴 LRC"></textarea>
          <div class="lyric-processing-tools">
            <button type="button" :disabled="lyricBusy || !lyricsContent.trim()" @click="processLyrics('standard')">标准格式</button>
            <label><input v-model="preserveWordTiming" type="checkbox">保留逐字时间</label>
            <button type="button" :disabled="lyricBusy || !lyricsContent.trim()" @click="processLyrics('blanks')">压缩空白行</button>
            <button type="button" :disabled="lyricBusy || !lyricsContent.trim()" @click="processLyrics('simplified')">转为简体</button>
            <button type="button" :disabled="lyricBusy || !lyricsContent.trim()" @click="translateCurrentLyrics">
              <LoaderCircle v-if="lyricOperation === 'translate'" class="spinning" :size="13" />{{ lyricOperation === 'translate' ? '翻译中…' : 'AI 翻译日文' }}
            </button>
            <label class="lyric-offset-control"><span>偏移</span><input v-model.number="lyricOffset" type="number" step="100" aria-label="歌词偏移毫秒"><i>ms</i></label>
            <button type="button" :disabled="lyricBusy || !lyricsContent.trim()" @click="processLyrics('offset')">应用偏移</button>
          </div>
        </section>

        <section id="library-panel-tags" v-show="activeEditorPanel === 'tags'" class="library-editor-section" role="tabpanel" aria-labelledby="library-tab-tags">
          <div class="section-title"><Music2 :size="16" /><strong>音频标签</strong><span>原地写入并回读校验</span></div>
          <div class="tag-grid">
            <label><span>标题</span><input v-model="tagForm.title"></label>
            <label><span>艺术家</span><input v-model="tagForm.artist"></label>
            <label><span>专辑</span><input v-model="tagForm.album"></label>
            <label><span>专辑艺术家</span><input v-model="tagForm.albumartist"></label>
            <label><span>音轨号</span><input v-model="tagForm.tracknumber"></label>
            <label><span>碟号</span><input v-model="tagForm.discnumber"></label>
            <label><span>年份</span><input v-model="tagForm.date"></label>
            <label><span>流派</span><input v-model="tagForm.genre"></label>
          </div>
          <p v-if="tagFeedback.text" class="library-action-feedback" :class="tagFeedback.type" role="status" aria-live="polite">{{ tagFeedback.text }}</p>
          <button class="library-primary" type="button" :disabled="tagSaving" @click="saveTags">
            <LoaderCircle v-if="tagSaving" class="spinning" :size="15" /><Save v-else :size="15" />
            {{ tagSaving ? '保存中…' : '保存标签' }}
          </button>
          <div class="library-danger-zone">
            <div><strong>移出音乐库</strong><span>文件先进入隐藏回收区，不会永久删除。</span></div>
            <button type="button" @click="removeTrack()"><Trash2 :size="15" />移入回收区</button>
          </div>
        </section>
      </aside>
    </div>

    <div v-if="trashOpen" class="library-drawer-backdrop" @click.self="closeTrash">
      <aside ref="trashDrawer" class="trash-drawer" role="dialog" aria-modal="true" aria-label="音乐库回收区" tabindex="-1">
        <header><div><small>RECOVERABLE DELETE</small><h2>音乐库回收区</h2></div><button type="button" aria-label="关闭回收区" @click="closeTrash"><X :size="18" /></button></header>
        <p v-if="!trashEntries.length" class="library-empty">回收区为空。</p>
        <article v-for="entry in trashEntries" :key="entry.token">
          <Folder v-if="entry.kind === 'folder'" :size="17" /><FileAudio v-else :size="17" /><div><strong>{{ entry.primary }}</strong><span>{{ entry.kind === 'folder' ? `${entry.track_count || 0} 首 · 文件夹` : '单曲' }} · {{ entry.created_at }}</span></div>
          <button @click="restore(entry)"><ArchiveRestore :size="15" />恢复</button>
        </article>
      </aside>
    </div>
  </section>
</template>

<style scoped src="./library-workspace.css"></style>
