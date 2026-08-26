const STORAGE_KEY = 'music-organizer.audio-preferences.v1'

function storedPreferences() {
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}')
    const volume = Number(value.volume)
    return {
      volume: Number.isFinite(volume) ? Math.min(Math.max(volume, 0), 1) : 1,
      muted: Boolean(value.muted),
    }
  } catch {
    return { volume: 1, muted: false }
  }
}

export function applyAudioPreferences(event) {
  const audio = event?.currentTarget
  if (!audio) return
  const preferences = storedPreferences()
  audio.volume = preferences.volume
  audio.muted = preferences.muted
}

export function saveAudioPreferences(event) {
  const audio = event?.currentTarget
  if (!audio) return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      volume: audio.volume,
      muted: audio.muted,
    }))
  } catch {
    // Playback continues normally when browser storage is unavailable.
  }
}
