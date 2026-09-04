import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  define: {
    // The bundle is compiled against these; the suite has to supply them
    // for the same reason the build does, or config.ts throws at import.
    __MYC_ORIGIN__: JSON.stringify('https://mycelium.test'),
    __MYC_VERSION_NAME__: JSON.stringify('test'),
    __MYC_CAN_CONNECT__: JSON.stringify(true),
  },
  resolve: {
    alias: {
      '@shared': fileURLToPath(new URL('../web/src/shared/index.ts', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts'],
    setupFiles: ['./tests/setup.ts'],
  },
})
