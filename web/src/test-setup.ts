// Web Storage under vitest + jsdom on a modern Node.
//
// Node 24 added a built-in global ``localStorage``. Without
// ``--localstorage-file`` it is present but ``undefined``, and vitest's jsdom
// environment only copies a window property onto ``globalThis`` when the
// global is not already defined -- so the built-in undefined wins and jsdom's
// working implementation never lands. Anything reading ``localStorage`` at
// module scope then throws on import, which is not a test failure anyone can
// read: it surfaces as "Cannot read properties of undefined (reading
// 'getItem')" from a file the test never mentions (``src/auth/session.ts``,
// reached through ``api/client`` from almost every module).
//
// This is environment repair, not a mock: it points the global at jsdom's own
// per-document storage when there is one, and falls back to an in-memory
// implementation of the same interface otherwise, so tests see real Storage
// semantics either way.

function memoryStorage(): Storage {
  const map = new Map<string, string>()
  return {
    get length() {
      return map.size
    },
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  } as Storage
}

function install(name: 'localStorage' | 'sessionStorage'): void {
  const current = (globalThis as Record<string, unknown>)[name] as Storage | undefined
  if (current && typeof current.getItem === 'function') return
  const fromWindow =
    typeof window !== 'undefined'
      ? (window as unknown as Record<string, unknown>)[name]
      : undefined
  const value =
    fromWindow && typeof (fromWindow as Storage).getItem === 'function'
      ? (fromWindow as Storage)
      : memoryStorage()
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
}

install('localStorage')
install('sessionStorage')
