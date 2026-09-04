// The two query grammars must stay one grammar.
//
// The /tasks search box and this panel parse the same short language, and
// they do very different things with a token: one compiles a predicate
// over a list already in the page, the other builds request parameters
// for a server that truncates. What they must never do is disagree about
// what a token IS. A `@name` meaning "tag" on one surface and "assignee"
// on the other is the kind of divergence nobody notices until a saved
// query returns the wrong rows.
//
// So: every `key:` this surface handles must either be a key the /tasks
// grammar already knows, or one of the scope sigils declared here. A new
// key on one side alone fails this check rather than shipping.
//
// It reads both tables out of the SOURCE rather than trusting a list
// written by hand: the /tasks key set from web/src/shared/query.ts, the
// panel's from its own parser. A table nobody can forget to update.

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SHARED = join(ROOT, '..', 'web', 'src', 'shared', 'query.ts')
const PANEL = join(ROOT, 'src', 'shared', 'query.ts')

/** The closed key set the /tasks grammar declares. */
function sharedKeys() {
  const text = readFileSync(SHARED, 'utf8')
  const block = /export const FILTER_KEYS = \[([\s\S]*?)\] as const/.exec(text)?.[1]
  if (!block) throw new Error('FILTER_KEYS not found in web/src/shared/query.ts')
  return new Set([...block.matchAll(/'([a-z_]+)'/g)].map((m) => m[1]))
}

/** The sigils this surface adds on purpose. */
function declaredSigils() {
  const text = readFileSync(PANEL, 'utf8')
  const block = /export const SCOPE_SIGILS = \[([\s\S]*?)\] as const/.exec(text)?.[1]
  if (!block) throw new Error('SCOPE_SIGILS not found in extension/src/shared/query.ts')
  return new Set([...block.matchAll(/'([a-z_]+)'/g)].map((m) => m[1]))
}

/** Every key the panel's parser actually branches on. */
function panelKeys() {
  const text = readFileSync(PANEL, 'utf8')
  return new Set([...text.matchAll(/key === '([a-z_]+)'/g)].map((m) => m[1]))
}

const shared = sharedKeys()
const sigils = declaredSigils()
const panel = panelKeys()

const undeclared = [...panel].filter((key) => !shared.has(key) && !sigils.has(key)).sort()
const unused = [...sigils].filter((key) => !panel.has(key)).sort()

let failed = false

if (undeclared.length > 0) {
  failed = true
  console.error(`\n${undeclared.length} key(s) the panel handles and nothing declares:\n`)
  for (const key of undeclared) console.error(`  ${key}:`)
  console.error(
    '\nEither it is a key the /tasks grammar should know too -- add it to\n' +
      'web/src/shared/query.ts FILTER_KEYS and implement it there -- or it is\n' +
      "a scope sigil belonging to this surface only, in which case declare it\n" +
      'in SCOPE_SIGILS so the divergence is deliberate and visible.\n',
  )
}

if (unused.length > 0) {
  failed = true
  console.error(`\n${unused.length} sigil(s) declared and never handled:\n`)
  for (const key of unused) console.error(`  ${key}:`)
  console.error('\nA declared divergence nothing implements is a claim, not a feature.\n')
}

if (failed) process.exit(1)

console.log(
  `query grammar: ${panel.size} key(s) handled, all of them either shared with ` +
    `/tasks (${shared.size} keys) or declared as a sigil (${[...sigils].join(', ')}).`,
)
