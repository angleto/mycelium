// Every `pnpm <script>` the SPA's README names must exist.
//
// A document is the one artifact nothing else validates: a script gets
// renamed, the seven call sites in CI and the Makefile get updated
// because they fail loudly, and the README keeps telling the next person
// to run a command that exits with "Command not found". They then either
// guess, or conclude the checks do not work. Both are worse than the
// rename would have been.
//
// This is the same shape as the running-timer reader guard next door in
// useRunningTimer.test.tsx: read the sources through Vite's own glob
// rather than node:fs, because src is typechecked without node types on
// purpose (tsconfig.app.json) and one assertion is not worth loosening
// that for. That guard scans for its endpoint by TEXT, so naming the
// path here -- even in a comment -- would have counted this file as a
// second reader of it. It did, which is the guard working.
//
// Scope, deliberately: `pnpm <name>` in web/README.md against the
// scripts in web/package.json. It does not reach the repo root's
// Makefile or CONTRIBUTING.md -- those name `make` targets, and a
// missing target fails loudly the first time anyone runs it, which is
// the failure mode this test exists to replace, not to duplicate.

import { describe, expect, it } from 'vitest'

// pnpm's own verbs. `pnpm install` is not a script and never will be.
const PNPM_BUILTINS = new Set([
  'add',
  'dlx',
  'exec',
  'install',
  'link',
  'remove',
  'run',
  'update',
  'why',
])

const RE_PNPM = /\bpnpm\s+([a-z][a-z0-9:._-]*)/g

function read(glob: Record<string, string>, suffix: string): string {
  const entry = Object.entries(glob).find(([path]) => path.endsWith(suffix))
  if (!entry) throw new Error(`${suffix} not found through import.meta.glob`)
  return entry[1]
}

describe('web/README.md names real scripts', () => {
  const raws = import.meta.glob('../../{README.md,package.json}', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  const readme = read(raws, 'README.md')
  const pkg = JSON.parse(read(raws, 'package.json')) as { scripts?: Record<string, string> }
  const scripts = new Set(Object.keys(pkg.scripts ?? {}))

  const named = [...readme.matchAll(RE_PNPM)]
    .map((m) => m[1])
    .filter((name) => !PNPM_BUILTINS.has(name))

  it('finds the commands at all, so a passing run means something', () => {
    expect(named.length).toBeGreaterThan(3)
  })

  it('resolves every one of them', () => {
    const missing = [...new Set(named)].filter((name) => !scripts.has(name)).sort()
    expect(missing).toEqual([])
  })
})
