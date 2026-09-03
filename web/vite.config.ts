import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// The browser talks to the backend through `/api`, which Vite proxies
// to the FastAPI service (and a reverse proxy does the same in the
// Docker Compose deploy, docs/architecture.md). `ws: true` keeps the
// path usable for the realtime timer WebSocket (W4).
//
// The target is overridable because 8000 is not ours to assume: on a
// machine where another project already listens there, the proxy silently
// forwards the whole app to the wrong backend and every call 404s. Point
// MYCELIUM_API_URL at the API and dev + E2E follow it.
const API_TARGET = process.env.MYCELIUM_API_URL ?? 'http://localhost:8000'

// Identity of THIS bundle, decided before Rollup hashes anything (so it
// can be baked into the code AND written to a sidecar file without the
// circularity of hashing a value derived from the hash).
//
// The git SHA is preferred because it is the same identity the backend
// reports at /api/buildinfo and the images carry as a label, so a stale
// tab can be traced to a commit. A local `pnpm build` has no SHA, so
// fall back to the build clock: distinct per build, which is enough for
// the question the client is asking ("is what the server serves still
// what I am running?") and useless for the question an operator asks
// ("which release is this?").
//
// That fallback is only acceptable locally. These variables reach the
// bundle exclusively through the environment of the process running the
// build, and a shipped image that misses them serves a placeholder that
// nothing complains about — 2.3.9 did. docker/frontend.Dockerfile now
// injects them into the build stage and asserts the result
// (web/scripts/assert-build-identity.mjs), so a release that cannot name
// itself fails there rather than in production.
const BUILD_ID =
  process.env.MYCELIUM_GIT_SHA ||
  process.env.MYCELIUM_VERSION ||
  `dev-${Date.now()}`

// The release name and build time, when the build was given them. They
// are not what the app COMPARES (BUILD_ID is), they are what makes
// /version.json answerable by a person: "2.3.9, built at ...". Omitted
// rather than emitted empty when absent, so the document never asserts
// an identity the build did not have.
const RELEASE = process.env.MYCELIUM_VERSION || null
const BUILT_AT = process.env.MYCELIUM_BUILD_AT || null

// The app imports its own identity from here (see src/lib/buildId.ts).
const VIRTUAL_ID = 'virtual:mycelium-build-id'
const RESOLVED_VIRTUAL_ID = '\0' + VIRTUAL_ID

/**
 * Publish the bundle identity twice: once INSIDE the bundle, once beside
 * it at a fixed URL. The running app compares the two and knows whether
 * it is still the frontend the server serves.
 *
 * `/version.json` is the outside half. It is the only stable-named file
 * the SPA can poll — index.html would work too, but that means parsing
 * HTML for a script tag; a 40-byte JSON document states the fact
 * directly and is cheap enough to fetch on a timer. Deliberately NOT a
 * file in `public/`: its content is generated, and a checked-in copy
 * would go stale the moment someone forgot to regenerate it.
 *
 * The inside half is a virtual module rather than a `define`, because
 * Vite's `define` substitution does not run in dev — the served module
 * still carries the raw identifier, so the whole mechanism would be dead
 * in `pnpm dev` and in the Playwright suite (which runs against the dev
 * server) and alive only in production, i.e. exactly the configuration
 * nobody exercises before shipping. A virtual module resolves the same
 * way in both modes.
 */
function buildIdentity(): Plugin {
  const body =
    JSON.stringify({
      buildId: BUILD_ID,
      ...(RELEASE ? { version: RELEASE } : {}),
      ...(BUILT_AT ? { builtAt: BUILT_AT } : {}),
    }) + '\n'
  return {
    name: 'mycelium-build-identity',
    resolveId(id) {
      return id === VIRTUAL_ID ? RESOLVED_VIRTUAL_ID : null
    },
    load(id) {
      if (id !== RESOLVED_VIRTUAL_ID) return null
      return `export const BUILD_ID = ${JSON.stringify(BUILD_ID)}\n`
    },
    // `generateBundle` (not writeBundle) so `vite build --watch` and any
    // consumer reading the bundle map both see the file as an output.
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'version.json', source: body })
    },
    configureServer(server) {
      // Same URL in dev, so the polling path is exercised by `pnpm dev`
      // and E2E. The id never changes within a dev session, so it never
      // fires there — HMR already owns the dev refresh loop.
      server.middlewares.use('/version.json', (_req, res) => {
        res.setHeader('Content-Type', 'application/json')
        res.setHeader('Cache-Control', 'no-store')
        res.end(body)
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), buildIdentity()],
  server: {
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        ws: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
