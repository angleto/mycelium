import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The browser talks to the backend through `/api`, which Vite proxies
// to the FastAPI service (and a reverse proxy does the same in the
// Docker Compose deploy, docs/architecture.md). `ws: true` keeps the
// path usable for the realtime timer WebSocket (W4).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
