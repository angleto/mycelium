// Every static t('...') key must exist in the catalogue.
//
// The SPA-side port of core/tests/test_i18n_catalog.py, which has long
// asserted the same thing for backend MessageCodes. Without it the two
// failure modes below are invisible until a human reads the screen:
//
//   t('notes.unsaved')                       -> renders the literal
//                                               "notes.unsaved" to the user
//   t('tagpicker.client', {defaultValue:'Client'})
//                                            -> renders English in every
//                                               locale, forever
//
// Both shipped. The second hid for ten releases behind a defaultValue,
// which is why a missing key is treated as a failure even when a fallback
// makes it look harmless: the fallback IS the bug, it just fails quietly.
//
// en.ts is the reference. it.ts cannot drift from it in SHAPE -- `export
// const it: Catalog` where `type Catalog = typeof en` makes a divergence a
// compile error -- so tsc already covers en<->it keys and this covers
// code<->catalogue.
//
// What tsc canNOT see is the inside of the strings: both sides are just
// `string`, so a translation that drops an interpolation placeholder
// compiles and ships. That is not cosmetic -- the worst case is a
// confirmation dialog that stops naming the thing it is about to destroy
// ("Delete {{name}} and everything in it") and asks the user to approve a
// blank. So placeholder PARITY is checked here too.
//
// Scope, deliberately: only string-literal keys. Keys built from template
// literals or variables are unresolvable without evaluating the program,
// so they are counted and reported, never failed on. That residual gap is
// the price of not migrating to i18next's typed-resources setup, which
// would need casts at ~86 dynamic call sites.

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SRC = join(WEB, 'src')
const CATALOG = join(SRC, 'i18n', 'en.ts')
const IT_CATALOG = join(SRC, 'i18n', 'it.ts')

/** Every .ts/.tsx under src/, excluding generated declaration files. */
function sources(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      out.push(...sources(p))
    } else if (/\.tsx?$/.test(name) && !name.endsWith('.d.ts')) {
      out.push(p)
    }
  }
  return out
}

function parse(file) {
  return ts.createSourceFile(
    file,
    readFileSync(file, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
}

/** The static text of a catalogue value, or null when it is not a plain
 * literal (a concatenation of literals counts; anything computed does
 * not, and is simply not placeholder-checked). */
function literalText(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return node.text
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = literalText(node.left)
    const right = literalText(node.right)
    return left === null || right === null ? null : left + right
  }
  return null
}

/** Flat leaf entries of a catalogue object: path -> literal text (or
 * null when the value is not a plain literal). */
function catalogEntries(file, exportName) {
  const sf = parse(file)
  const entries = new Map()

  const walkObject = (node, prefix) => {
    for (const prop of node.properties) {
      if (!ts.isPropertyAssignment(prop)) continue
      const name = ts.isIdentifier(prop.name)
        ? prop.name.text
        : ts.isStringLiteral(prop.name)
          ? prop.name.text
          : null
      if (name === null) continue
      const path = prefix ? `${prefix}.${name}` : name
      if (ts.isObjectLiteralExpression(prop.initializer)) {
        walkObject(prop.initializer, path)
      } else if (ts.isArrayLiteralExpression(prop.initializer)) {
        // Arrays are addressed by index (t('x.0')); register each slot.
        entries.set(path, null)
        prop.initializer.elements.forEach((el, i) =>
          entries.set(`${path}.${i}`, literalText(el)),
        )
      } else {
        entries.set(path, literalText(prop.initializer))
      }
    }
  }

  const findExport = (node) => {
    if (
      ts.isVariableStatement(node) &&
      node.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)
    ) {
      for (const decl of node.declarationList.declarations) {
        if (
          ts.isIdentifier(decl.name) &&
          decl.name.text === exportName &&
          decl.initializer &&
          ts.isObjectLiteralExpression(decl.initializer)
        ) {
          walkObject(decl.initializer, '')
        }
      }
    }
    ts.forEachChild(node, findExport)
  }
  findExport(sf)
  return entries
}

/** The {{placeholders}} a translated string must keep, as a sorted set.
 * i18next also accepts formatting suffixes ({{count, number}}); only the
 * variable name is compared. */
function placeholders(text) {
  const out = new Set()
  for (const m of text.matchAll(/\{\{\s*([^},\s]+)/g)) out.add(m[1])
  return [...out].sort()
}

/** Static t('...') keys, plus a count of the dynamic ones we skip. */
function usedKeys(files) {
  const used = new Map() // key -> [file:line]
  let dynamic = 0

  for (const file of files) {
    const sf = parse(file)
    const visit = (node) => {
      if (ts.isCallExpression(node)) {
        const callee = node.expression
        const isT =
          (ts.isIdentifier(callee) && callee.text === 't') ||
          (ts.isPropertyAccessExpression(callee) && callee.name.text === 't')
        if (isT && node.arguments.length > 0) {
          const arg = node.arguments[0]
          if (ts.isStringLiteral(arg) || ts.isNoSubstitutionTemplateLiteral(arg)) {
            const { line } = sf.getLineAndCharacterOfPosition(arg.getStart(sf))
            const where = `${file.slice(WEB.length + 1)}:${line + 1}`
            if (!used.has(arg.text)) used.set(arg.text, [])
            used.get(arg.text).push(where)
          } else {
            dynamic += 1
          }
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)
  }
  return { used, dynamic }
}

const enEntries = catalogEntries(CATALOG, 'en')
const itEntries = catalogEntries(IT_CATALOG, 'it')
const defined = new Set(enEntries.keys())
const { used, dynamic } = usedKeys(sources(SRC))

// i18next resolves a counted key to a SUFFIXED entry: t('a.b', {count})
// reads a.b_one / a.b_other, and the bare a.b need not exist. Checking
// only for the literal key reports every plural in the catalogue as
// missing -- which it did on the first run here, for two keys that were
// perfectly fine.
const PLURAL_CATEGORIES = ['zero', 'one', 'two', 'few', 'many', 'other']
const resolves = (key) =>
  defined.has(key) || PLURAL_CATEGORIES.some((c) => defined.has(`${key}_${c}`))

const missing = [...used.entries()]
  .filter(([key]) => !resolves(key))
  .sort(([a], [b]) => a.localeCompare(b))

// Dead keys are reported, never fatal: the dynamic call sites above make
// reachability an over-approximation, and the pre-existing backlog would
// force either a large cleanup or an allowlist that rots.
const usedSet = new Set(used.keys())
const deadCount = [...defined].filter((k) => !usedSet.has(k)).length

console.log(
  `i18n: ${defined.size} keys defined, ${used.size} static keys used, ` +
    `${dynamic} dynamic call sites skipped, ${deadCount} defined-but-unused.`,
)

if (missing.length > 0) {
  console.error(`\n${missing.length} key(s) used in code but MISSING from src/i18n/en.ts:\n`)
  for (const [key, sites] of missing) {
    console.error(`  ${key}`)
    for (const site of sites) console.error(`      ${site}`)
  }
  console.error(
    '\nAdd them to en.ts (and it.ts -- tsc enforces the pair). A defaultValue\n' +
      'at the call site is not a fix: it renders English in every locale.\n',
  )
  process.exit(1)
}

// Placeholder parity: a translation may reword freely, but it may not
// drop (or invent) an interpolation. tsc cannot see this -- both sides
// are `string` -- and the failure is silent at runtime: i18next simply
// renders the sentence without the value.
const drift = []
for (const [key, enText] of enEntries) {
  if (typeof enText !== 'string') continue
  const itText = itEntries.get(key)
  if (typeof itText !== 'string') continue
  const a = placeholders(enText)
  const b = placeholders(itText)
  if (a.join('|') !== b.join('|')) drift.push({ key, en: a, it: b })
}

if (drift.length > 0) {
  console.error(
    `\n${drift.length} key(s) whose {{placeholders}} differ between en.ts and it.ts:\n`,
  )
  for (const d of drift) {
    console.error(`  ${d.key}`)
    console.error(`      en: ${d.en.join(', ') || '(none)'}`)
    console.error(`      it: ${d.it.join(', ') || '(none)'}`)
  }
  console.error(
    '\nA translation may reword the sentence but not drop the value it\n' +
      'interpolates: the string renders without it, silently.\n',
  )
  process.exit(1)
}

console.log(
  `i18n: every static key resolves; placeholders match across en/it.`,
)
