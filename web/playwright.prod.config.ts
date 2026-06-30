import { defineConfig, devices } from '@playwright/test'

// Prod E2E config. No globalSetup (bootstrap-admin is local-only).
// Storage state authored offline from /auth/login-mfa response; placed
// at e2e-prod/.auth/storage.json (gitignored). Token expires ~12h.
export default defineConfig({
  testDir: './e2e-prod',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'https://mycelium.xeno.garden',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    storageState: './e2e-prod/.auth/storage.json',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
