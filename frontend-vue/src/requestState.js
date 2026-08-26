export function createLatestRequestGate() {
  let generation = 0
  return {
    begin() {
      generation += 1
      return generation
    },
    isCurrent(token) {
      return token === generation
    },
  }
}

export function beginPendingItem(current, itemId) {
  const token = Symbol(String(itemId))
  const next = new Map(current)
  const tokens = new Set(current.get(itemId) || [])
  tokens.add(token)
  next.set(itemId, tokens)
  return { next, token }
}

export function finishPendingItem(current, itemId, token) {
  const currentTokens = current.get(itemId)
  if (!currentTokens?.has(token)) return current
  const next = new Map(current)
  const tokens = new Set(currentTokens)
  tokens.delete(token)
  if (tokens.size) next.set(itemId, tokens)
  else next.delete(itemId)
  return next
}

export function reconcilePolledItem(items, currentItem, preservedKeys = []) {
  if (!currentItem) return items
  return (items || []).map((item) => {
    if (item.id !== currentItem.id) return item
    const preserved = Object.fromEntries(
      preservedKeys.map((key) => [key, currentItem[key]]),
    )
    Object.assign(currentItem, item, preserved)
    return currentItem
  })
}
