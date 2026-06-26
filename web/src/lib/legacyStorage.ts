// One-time migration of persisted client state after the Flow -> Mycelium
// rename. localStorage keys moved from the ``flow.``/``flow-`` namespace to
// ``mycelium.``/``mycelium-``. Without this, every browser would silently
// lose its session, view preferences, theme and mindmap layout on first
// load under the new name. Idempotent: once the old keys are gone it is a
// cheap no-op, and it never clobbers a value already written under the new
// namespace.
const PREFIX_MAP: ReadonlyArray<readonly [string, string]> = [
  ['flow.', 'mycelium.'],
  ['flow-', 'mycelium-'],
]

export function migrateLegacyStorageKeys(): void {
  let store: Storage
  try {
    store = window.localStorage
  } catch {
    return // storage disabled (private mode / sandboxed iframe)
  }
  try {
    const keys: string[] = []
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i)
      if (k) keys.push(k)
    }
    for (const oldKey of keys) {
      const rule = PREFIX_MAP.find(([from]) => oldKey.startsWith(from))
      if (!rule) continue
      const [from, to] = rule
      const newKey = to + oldKey.slice(from.length)
      if (store.getItem(newKey) !== null) {
        store.removeItem(oldKey) // already migrated; drop the stale copy
        continue
      }
      const value = store.getItem(oldKey)
      if (value !== null) store.setItem(newKey, value)
      store.removeItem(oldKey)
    }
  } catch {
    // A malformed entry must never block app boot.
  }
}
