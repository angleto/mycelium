import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Idempotent E2E admin bootstrap. Decouples the suite from any
// external DB seeding and keeps zero personal data in the repo: it
// ensures a neutral admin (E2E_EMAIL/E2E_PASSWORD) exists via the
// same service-path bootstrap production uses
// (mycelium_core.bootstrap_admin sets is_admin=True; an existing user is
// left untouched, so re-runs are safe). It targets the SAME database
// the externally-run test uvicorn serves; the defaults match
// deploy/local + conftest + ci.yml (well-known local test fixtures,
// not secrets). Fails loudly (throws) if the admin cannot be ensured.
export const E2E_EMAIL = 'e2e-admin@example.com'
export const E2E_PASSWORD = 'E2eAdminPw123'

export default function globalSetup(): void {
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
  execFileSync('uv', ['run', 'python', '-m', 'mycelium_core.bootstrap_admin'], {
    cwd: repoRoot,
    env: {
      ...process.env,
      MYCELIUM_ADMIN_EMAIL: E2E_EMAIL,
      MYCELIUM_ADMIN_PASSWORD: E2E_PASSWORD,
      MYCELIUM_DATABASE_URL:
        process.env.MYCELIUM_DATABASE_URL ??
        'postgresql+asyncpg://mycelium_app:mycelium_app@localhost:5432/mycelium',
      MYCELIUM_JWT_SECRET:
        process.env.MYCELIUM_JWT_SECRET ?? 'local-dev-only-secret-min-32-bytes-aaaa',
      MYCELIUM_SECRET_KEY:
        process.env.MYCELIUM_SECRET_KEY ??
        'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    },
    stdio: 'inherit',
  })
}
