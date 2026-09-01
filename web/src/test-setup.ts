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

// ``window.matchMedia``, which jsdom does not implement at all.
//
// Environment repair for the same reason as the storage above, not a mock.
// The app asks it exactly one question (``useIsDark``, and through it every
// Mermaid diagram, which the editor's block preview renders), and a jsdom
// document has no OS preference to report -- so the honest answer is the CSS
// default: no preference matches. Without it, rendering anything that reads
// the theme throws ``window.matchMedia is not a function`` from inside a
// React commit, which surfaces as an unhandled error attributed to whichever
// test happened to be running when the render flushed.
function installMatchMedia(): void {
  if (typeof window === 'undefined' || typeof window.matchMedia === 'function') return
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia
}

installMatchMedia()

// Relative request URLs under vitest.
//
// The api client is built with a RELATIVE baseUrl (``/api``): in a browser
// the document resolves it against its own origin, and in production nginx
// proxies that path. Under vitest the jsdom environment keeps NODE's
// ``Request``, which has no document to resolve against and throws "Failed to
// parse URL from /api/..." inside the client, before the request ever reaches
// ``fetch``. A test that stubs fetch then sees no call at all, and the failure
// reads as "the code issued no request" instead of as a URL problem.
//
// Environment repair for the same reason as the two above, not a mock: it
// resolves a root-relative URL against the origin the jsdom document already
// has, which is exactly what a browser does with it.
function installRelativeRequestBase(): void {
  if (typeof window === 'undefined' || typeof globalThis.Request !== 'function') return
  const Base = globalThis.Request
  const origin = window.location.origin
  class RelativeRequest extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === 'string' && input.startsWith('/') ? origin + input : input,
        init,
      )
    }
  }
  globalThis.Request = RelativeRequest as unknown as typeof Request
}

installRelativeRequestBase()
