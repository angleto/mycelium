// The catalogue Chrome reads, and the keys the code asks it for.
//
// chrome.i18n answers a key it does not have with an EMPTY STRING. Not an
// error, not the key name: a blank where a sentence should be. So a typo
// ships a panel with an unlabelled button, and it is invisible to anyone
// running in the language the typo is not in.
//
// Three assertions, and the third is the one a type system cannot make:
//
//   1. every key exists in every locale;
//   2. placeholders match across locales -- a translation may reword a
//      sentence but not drop the value it interpolates, or the sentence
//      renders with a hole in it;
//   3. every key the code asks for is defined, and every key defined is
//      asked for. The second half matters at this size: the catalogue is
//      hand-written, so a key left behind by a deleted feature is a line
//      somebody will later translate for nothing.

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const LOCALES = join(ROOT, '_locales')
const REFERENCE = 'en'

/** @param {string} dir @returns {string[]} */
function sources(dir) {
  /** @type {string[]} */
  const out = []
  /** @param {string} d */
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const full = join(d, name)
      if (statSync(full).isDirectory()) walk(full)
      else if (/\.(tsx?|mjs|html)$/.test(name)) out.push(full)
    }
  }
  walk(dir)
  return out
}

/** @param {string} locale */
function catalogue(locale) {
  return JSON.parse(readFileSync(join(LOCALES, locale, 'messages.json'), 'utf8'))
}

/** @param {Record<string, {message: string, placeholders?: Record<string, unknown>}>} cat */
function placeholderNames(cat) {
  /** @type {Map<string, string[]>} */
  const out = new Map()
  for (const [key, entry] of Object.entries(cat)) {
    out.set(key, Object.keys(entry.placeholders ?? {}).sort())
  }
  return out
}

const locales = readdirSync(LOCALES).filter((name) =>
  statSync(join(LOCALES, name)).isDirectory(),
)
if (!locales.includes(REFERENCE)) {
  console.error(`no ${REFERENCE} catalogue: it is the reference every other locale is read against`)
  process.exit(1)
}

const reference = catalogue(REFERENCE)
const referenceKeys = new Set(Object.keys(reference))
const referencePlaceholders = placeholderNames(reference)

let failed = false

// 1 + 2: parity across locales.
for (const locale of locales) {
  if (locale === REFERENCE) continue
  const other = catalogue(locale)
  const missing = [...referenceKeys].filter((k) => !(k in other)).sort()
  const extra = Object.keys(other).filter((k) => !referenceKeys.has(k)).sort()
  if (missing.length || extra.length) {
    failed = true
    console.error(`\n_locales/${locale}: ${missing.length} missing, ${extra.length} unknown`)
    for (const k of missing) console.error(`  missing  ${k}`)
    for (const k of extra) console.error(`  unknown  ${k}`)
  }
  const otherPlaceholders = placeholderNames(other)
  for (const [key, names] of referencePlaceholders) {
    const theirs = otherPlaceholders.get(key)
    if (!theirs) continue
    if (names.join(',') !== theirs.join(',')) {
      failed = true
      console.error(
        `\n_locales/${locale}: placeholders differ for ${key}\n` +
          `      ${REFERENCE}: ${names.join(', ') || '(none)'}\n` +
          `      ${locale}: ${theirs.join(', ') || '(none)'}`,
      )
    }
  }
}

// 3: the code and the catalogue, both directions.
//
// Two scans with different patterns, and the split is not fussiness.
// `src/` is application code and asks by calling; `scripts/` contains
// only ONE reference, the __MSG_ placeholders the manifest generator
// emits. Scanning scripts/ for the call pattern too made this file match
// its own regex literals and report a key called "key" -- a checker
// failing on itself, which is the least useful kind of red.
const used = new Set()
/** @type {Map<string, string>} */
const firstSeenIn = new Map()

/** @param {string} file @param {RegExp} pattern */
function collect(file, pattern) {
  const text = readFileSync(file, 'utf8')
  for (const match of text.matchAll(pattern)) {
    const key = match[1]
    if (!key) continue
    used.add(key)
    if (!firstSeenIn.has(key)) firstSeenIn.set(key, relative(ROOT, file))
  }
}

for (const file of sources(join(ROOT, 'src'))) {
  collect(file, /\bm\(\s*'([A-Za-z0-9_]+)'/g)
  collect(file, /getMessage\(\s*'([A-Za-z0-9_]+)'/g)
}
for (const file of sources(join(ROOT, 'scripts'))) {
  collect(file, /__MSG_([A-Za-z0-9_]+)__/g)
}

const unknown = [...used]
  .filter((key) => !referenceKeys.has(key))
  .sort()
  .map((key) => ({ key, file: firstSeenIn.get(key) ?? '?' }))

if (unknown.length) {
  failed = true
  console.error(`\n${unknown.length} key(s) asked for and not defined:\n`)
  for (const u of unknown) console.error(`  ${u.file}  ${u.key}`)
  console.error(
    '\nchrome.i18n answers an unknown key with an EMPTY STRING, so this\n' +
      'ships as a blank label rather than as an error.\n',
  )
}

const stale = [...referenceKeys].filter((k) => !used.has(k)).sort()
if (stale.length) {
  failed = true
  console.error(`\n${stale.length} key(s) defined and never asked for:\n`)
  for (const k of stale) console.error(`  ${k}`)
  console.error(
    '\nThe catalogue is hand-written and translated by hand. A key left\n' +
      'behind by a deleted feature is a line somebody translates for nothing.\n',
  )
}

if (failed) process.exit(1)

console.log(
  `messages: ${referenceKeys.size} keys, ${locales.length} locales in parity, ` +
    'every key asked for is defined and every key defined is asked for.',
)
