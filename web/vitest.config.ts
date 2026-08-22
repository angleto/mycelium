import { defineConfig } from 'vitest/config'

// Unit tests for the SPA. Separate from the Playwright e2e suite
// (playwright.config.ts), which drives the real stack: these need no
// backend, no browser and no server, and run in seconds inside the cheap
// ``web`` CI job.
//
// jsdom rather than 'node': CodeMirror's view layer and anything that
// renders touch ``document`` at import time. The identity assertions
// themselves are pure (``EditorState`` needs no DOM), but one environment
// for the whole suite is one thing to reason about.
// `virtual:mycelium-build-id` is supplied by the build-identity plugin in
// vite.config.ts, which this config deliberately does not load (the unit
// suite wants no React plugin, no proxy, no dist emit). Resolve it to a
// fixed id instead: the tests assert the reload POLICY, which takes the
// ids as inputs, so any stable value does — what must not happen is the
// import failing and taking the whole module down with it.
const TEST_BUILD_ID = 'test-build-id'

export default defineConfig({
  plugins: [
    {
      name: 'mycelium-build-identity-stub',
      resolveId: (id) =>
        id === 'virtual:mycelium-build-id' ? '\0' + id : null,
      load: (id) =>
        id === '\0virtual:mycelium-build-id'
          ? `export const BUILD_ID = ${JSON.stringify(TEST_BUILD_ID)}`
          : null,
    },
  ],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // Repairs the Web Storage globals before any module is imported; see
    // the file for why a modern Node needs it.
    setupFiles: ['./src/test-setup.ts'],
  },
})
