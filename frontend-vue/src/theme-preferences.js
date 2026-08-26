export const THEME_STORAGE_KEY = 'music-organizer-theme'
export const THEME_MODES = new Set(['system', 'light', 'dark'])

export function normalizeThemePreference(value) {
  return THEME_MODES.has(value) ? value : 'system'
}

export function loadThemePreference(storage = globalThis.localStorage) {
  try {
    return normalizeThemePreference(storage?.getItem(THEME_STORAGE_KEY))
  } catch {
    return 'system'
  }
}

export function resolvedTheme(
  preference,
  media = globalThis.matchMedia?.('(prefers-color-scheme: dark)'),
) {
  const normalized = normalizeThemePreference(preference)
  if (normalized !== 'system') return normalized
  return media?.matches ? 'dark' : 'light'
}

export function applyTheme(
  preference,
  documentObject = globalThis.document,
  media = globalThis.matchMedia?.('(prefers-color-scheme: dark)'),
) {
  const normalized = normalizeThemePreference(preference)
  const theme = resolvedTheme(normalized, media)
  const root = documentObject?.documentElement
  if (root) {
    root.dataset.theme = theme
    root.dataset.themePreference = normalized
    root.style.colorScheme = theme
  }
  const themeColor = documentObject?.querySelector('meta[name="theme-color"]')
  themeColor?.setAttribute('content', theme === 'dark' ? '#111315' : '#f5f5f4')
  return theme
}

export function saveThemePreference(preference, storage = globalThis.localStorage) {
  const normalized = normalizeThemePreference(preference)
  try {
    storage?.setItem(THEME_STORAGE_KEY, normalized)
  } catch {
    // Theme changes still apply when storage is unavailable.
  }
  return normalized
}

export function watchSystemTheme(callback, media = globalThis.matchMedia?.(
  '(prefers-color-scheme: dark)',
)) {
  if (!media) return () => {}
  const listener = () => callback(media.matches ? 'dark' : 'light')
  media.addEventListener?.('change', listener)
  return () => media.removeEventListener?.('change', listener)
}
