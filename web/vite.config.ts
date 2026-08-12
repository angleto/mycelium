import { defineConfig } from 'vite'
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

export default defineConfig({
  plugins: [react()],
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
