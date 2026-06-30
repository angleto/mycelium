import { defineConfig, devices } from '@playwright/test'

// Servers (vite :5173, uvicorn :8000) run externally during dev/E2E.
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
    baseURL: 'http://localhost:5173',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
