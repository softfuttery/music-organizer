let csrfToken = ''
let unauthorizedHandler = null

export function onUnauthorized(handler) {
  unauthorizedHandler = typeof handler === 'function' ? handler : null
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.headers || {}),
    },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(payload.error || payload.message || `HTTP ${response.status}`)
    error.status = response.status
    error.payload = payload
    if (response.status === 401) {
      csrfToken = ''
      if (path !== '/api/login') {
        try {
          unauthorizedHandler?.(error)
        } catch {
          // Authentication recovery must not replace the original API error.
        }
      }
    }
    throw error
  }
  return payload
}

export async function getCsrfToken(force = false) {
  if (force || !csrfToken) {
    csrfToken = (await request('/api/csrf')).token
  }
  return csrfToken
}

export function getSession() {
  return request('/api/session')
}

export async function login(username, password) {
  const token = await getCsrfToken(true)
  const payload = await request('/api/login', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': token,
    },
    body: JSON.stringify({ username, password }),
  })
  csrfToken = payload.csrf_token
  return payload
}

export async function logout() {
  const token = await getCsrfToken()
  const payload = await request('/api/logout', {
    method: 'POST',
    headers: { 'X-CSRF-Token': token },
  })
  csrfToken = payload.csrf_token
  return payload
}

export function getStats() {
  return request('/api/stats')
}

export function getJob() {
  return request('/api/job')
}

export function getHealth() {
  return request('/api/health')
}

export function getHistory(page = 1, query = '') {
  const params = new URLSearchParams({ page: String(page) })
  if (query) params.set('q', query)
  return request(`/api/history?${params}`)
}

export function getConfig() {
  return request('/api/config', { cache: 'no-store' })
}

export async function saveConfig(formData) {
  const token = await getCsrfToken()
  return request('/api/config', {
    method: 'POST',
    headers: { 'X-CSRF-Token': token },
    body: formData,
  })
}

export function testMagicPush() {
  return postAction('/api/notifications/magicpush/test')
}

export async function postAction(path) {
  const token = await getCsrfToken()
  return request(path, {
    method: 'POST',
    headers: { 'X-CSRF-Token': token },
  })
}

export function getReviewRoots(path = '') {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  return request(`/api/review/roots${query}`)
}

export function getReviewFiles(path) {
  return request(`/api/review/files?path=${encodeURIComponent(path)}`)
}

export function getReviewAudioUrl(itemId, localPath) {
  return `/api/review/items/${itemId}/audio?path=${encodeURIComponent(localPath)}`
}

export function getReviewBatches(scope = 'active', query = '') {
  const params = new URLSearchParams({ scope })
  if (query) params.set('q', query)
  return request('/api/review/batches?' + params)
}

export function getReviewBatch(id, scope = 'active', query = '') {
  const params = new URLSearchParams({ scope })
  if (query) params.set('q', query)
  return request('/api/review/batches/' + id + '?' + params)
}

async function postJson(path, body) {
  const token = await getCsrfToken()
  return request(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': token,
    },
    body: JSON.stringify(body),
  })
}

export function createReviewBatch(paths, label = '') {
  return postJson('/api/review/batches', { paths, label })
}

export function approveReviewItem(itemId, candidateKey, decision = {}) {
  return postJson(`/api/review/items/${itemId}/approve`, {
    candidate_key: candidateKey,
    track_mapping: decision.track_mapping,
    quarantine_paths: decision.quarantine_paths,
  })
}

export function getManualReviewPreview(itemId) {
  return request(`/api/review/items/${itemId}/manual-preview`)
}

export function approveManualReviewItem(itemId, payload) {
  return postJson(`/api/review/items/${itemId}/approve-manual`, payload)
}

export function reidentifyReviewItem(itemId, payload) {
  return postJson(`/api/review/items/${itemId}/identify`, payload)
}

export function searchReviewLyrics(itemId, payload) {
  return postJson(`/api/review/items/${itemId}/lyrics/search`, payload)
}

export function fetchReviewLyrics(itemId, payload) {
  return postJson(`/api/review/items/${itemId}/lyrics/fetch`, payload)
}

export function saveReviewLyrics(itemId, payload) {
  return postJson(`/api/review/items/${itemId}/lyrics/save`, payload)
}

export function translateLyrics(payload) {
  return postJson('/api/lyrics/translate', payload)
}

export function skipReviewItem(itemId) {
  return postJson(`/api/review/items/${itemId}/skip`, {})
}

export function recycleReviewSource(itemId, confirmPath) {
  return postJson(`/api/review/items/${itemId}/recycle-source`, {
    confirm_path: confirmPath,
  })
}

export async function deleteReviewArchive(itemId) {
  const token = await getCsrfToken()
  return request(`/api/review/items/${itemId}/archive`, {
    method: 'DELETE',
    headers: { 'X-CSRF-Token': token },
  })
}

export function getLibraryFolders(query = '', offset = 0, limit = 20, order = 'desc') {
  const params = new URLSearchParams({ q: query, offset, limit, order })
  return request(`/api/library/folders?${params}`)
}

export function getLibraryTrack(path) {
  return request(`/api/library/track?path=${encodeURIComponent(path)}`, { cache: 'no-store' })
}

export function getLibraryAudioUrl(path) {
  return `/api/library/audio?path=${encodeURIComponent(path)}`
}

export function updateLibraryTrack(path, tags) {
  return postJson('/api/library/track/update', { path, tags })
}

export function searchLibraryLyrics(payload) {
  return postJson('/api/library/lyrics/search', payload)
}

export function fetchLibraryLyrics(payload) {
  return postJson('/api/library/lyrics/fetch', payload)
}

export function saveLibraryLyrics(payload) {
  return postJson('/api/library/lyrics/save', payload)
}

export function getLibraryTrash() {
  return request('/api/library/trash')
}

export function trashLibraryTrack(path) {
  return postJson('/api/library/trash', { path })
}

export function trashLibraryFolder(path) {
  return postJson('/api/library/trash/folder', { path })
}

export function restoreLibraryTrash(token) {
  return postJson('/api/library/trash/restore', { token })
}
