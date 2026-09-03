#!/usr/bin/env node
// Refuse a bundle that cannot say which release it is.
//
// The SPA publishes its identity twice: inside the bundle (the virtual
// module in vite.config.ts) and beside it at /version.json, which the
// running app polls to notice a deploy. That identity comes from
// MYCELIUM_GIT_SHA / MYCELIUM_VERSION at BUILD time — unlike the backend,
// which reads the same variables at RUN time and therefore cannot miss
// them. A bundle built without them falls back to `dev-<clock>`, which is
// right for a developer's `pnpm build` and wrong for anything shipped:
// 2.3.9 reached production serving `{"buildId":"dev-1788431413626"}`
// because the image's build stage never received the arguments the
// workflow was already passing. Nothing failed; the placeholder was only
// visible by curling production.
//
// So the image build calls this, and the failure lands where the artifact
// is assembled instead of in production. Not covered: this checks that an
// identity was injected, not that it is the RIGHT one — the workflow
// passes `git rev-parse HEAD` of the checked-out source, and that is the
// only place the value can be wrong.
//
// Usage: node scripts/assert-build-identity.mjs dist/version.json

import { readFileSync } from 'node:fs'

const path = process.argv[2]
if (!path) {
  console.error('usage: assert-build-identity.mjs <path/to/version.json>')
  process.exit(2)
}

let identity
try {
  identity = JSON.parse(readFileSync(path, 'utf8'))
} catch (err) {
  console.error(`build identity: cannot read ${path}: ${err.message}`)
  console.error('The build should have emitted it (see vite.config.ts).')
  process.exit(1)
}

const buildId = typeof identity.buildId === 'string' ? identity.buildId : ''

if (!buildId || buildId.startsWith('dev-')) {
  console.error(
    `build identity: ${path} says ${JSON.stringify(identity.buildId)}, ` +
      'which is the local fallback, not a release identity.',
  )
  console.error(
    'Pass the identity into the stage that runs `pnpm build`, e.g.\n' +
      '  docker build -f docker/frontend.Dockerfile \\\n' +
      '    --build-arg MYCELIUM_GIT_SHA="$(git rev-parse HEAD)" \\\n' +
      '    --build-arg MYCELIUM_VERSION="<tag>" .',
  )
  process.exit(1)
}

const extra = [
  identity.version ? `version ${identity.version}` : null,
  identity.builtAt ? `built at ${identity.builtAt}` : null,
]
  .filter(Boolean)
  .join(', ')
console.log(`build identity: ${buildId}${extra ? ` (${extra})` : ''}`)
