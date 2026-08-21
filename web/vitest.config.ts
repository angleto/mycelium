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
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // Repairs the Web Storage globals before any module is imported; see
    // the file for why a modern Node needs it.
    setupFiles: ['./src/test-setup.ts'],
  },
})
