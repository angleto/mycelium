import { defineConfig, devices } from '@playwright/test'

// Servers (vite :5173, uvicorn :8000) run externally during dev/E2E.
// Both ports are overridable, because neither is ours to assume on a
// developer machine: MYCELIUM_E2E_BASE_URL points the browser at the SPA,
// MYCELIUM_API_URL points Vite's /api proxy at the backend (vite.config.ts)
// and MYCELIUM_E2E_API_URL points the specs' own seeding calls at it
// (e2e/api.ts). Defaults are what CI uses, so CI is unaffected.
export default defineConfig({
  testDir: './e2e',
  // Ensures a neutral admin exists before the suite (idempotent;
  // see e2e/global-setup.ts).
  globalSetup: './e2e/global-setup.ts',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.MYCELIUM_E2E_BASE_URL ?? 'http://localhost:5173',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
