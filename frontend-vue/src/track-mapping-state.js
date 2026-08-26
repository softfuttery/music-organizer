const pathCollator = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: 'base',
})

export function reviewTrackKey(track) {
  return track?.key || track?.track_key || track?.track_id || ''
}

export function sortReviewLocalItems(locals, tracks, mapping = {}) {
  const tracksByKey = new Map(
    (tracks || []).map((track) => [reviewTrackKey(track), track]),
  )
  return [...(locals || [])].sort((left, right) => {
    const leftTrack = tracksByKey.get(mapping[left.local_path] || left.track_key || '')
    const rightTrack = tracksByKey.get(mapping[right.local_path] || right.track_key || '')
    if (Boolean(leftTrack) !== Boolean(rightTrack)) return leftTrack ? -1 : 1
    if (leftTrack && rightTrack) {
      const trackOrder = (
        Number(leftTrack.disc || 0) - Number(rightTrack.disc || 0)
        || Number(leftTrack.track || 0) - Number(rightTrack.track || 0)
      )
      if (trackOrder) return trackOrder
    }
    return pathCollator.compare(
      String(left.local_path || ''),
      String(right.local_path || ''),
    )
  })
}

export function reviewMappingConfidence(local, currentTrackKey) {
  if (!currentTrackKey) return { tone: 'unmatched', label: '未匹配' }
  if (currentTrackKey !== local?.track_key) {
    return { tone: 'manual', label: '手动调整' }
  }
  if (local?.match_score === undefined || local?.match_score === null) {
    return { tone: 'legacy', label: '已匹配' }
  }
  const score = Number(local.match_score)
  if (score >= 0.9) return { tone: 'strong', label: `${Math.round(score * 100)}%` }
  if (score >= 0.75) return { tone: 'good', label: `${Math.round(score * 100)}%` }
  return { tone: 'weak', label: `${Math.round(score * 100)}%` }
}
