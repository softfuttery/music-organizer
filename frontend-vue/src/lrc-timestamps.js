// Some providers emit the non-standard [mm:ss:cc] form. Match it before
// hour-aware [h:mm:ss.xx] timestamps so the third colon is treated as a
// fractional separator, not as an hours field.
export const LINE_TIMESTAMP = /\[(?:(\d{1,3}):([0-5]?\d):(\d{1,3})|((?:\d{1,3}:)?\d{1,3}:\d{1,2})(?:[.:](\d{1,3}))?)\]/g
export const WORD_TIMESTAMP = /<(?:(\d{1,3}):([0-5]?\d):(\d{1,3})|((?:\d{1,3}:)?\d{1,3}:\d{1,2})(?:[.:](\d{1,3}))?)>/g

function fractionToMilliseconds(value = '') {
  return Number(String(value || '').padEnd(3, '0').slice(0, 3)) || 0
}

export function timestampMilliseconds(
  legacyMinutes,
  legacySeconds,
  legacyFraction,
  time,
  fraction,
) {
  if (legacyMinutes !== undefined) {
    return (
      (Number(legacyMinutes) * 60 + Number(legacySeconds)) * 1000
      + fractionToMilliseconds(legacyFraction)
    )
  }
  const parts = String(time || '').split(':').map(Number)
  const seconds = parts.pop() || 0
  const minutes = parts.pop() || 0
  const hours = parts.pop() || 0
  return (
    ((hours * 60 + minutes) * 60 + seconds) * 1000
    + fractionToMilliseconds(fraction)
  )
}
