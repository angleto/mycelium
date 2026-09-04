// web/src/shared/ must import nothing outside itself.
//
// That directory is compiled into two packages: the SPA, and the browser
// extension. They are both thin adapters over the same REST API, and a
// handful of rules belong to that API rather than to either client --
// how its error envelope reads, what an entity code looks like, what a
// recents row is, what the query grammar's tokens mean. Written twice,
// those drift, and each drift is invisible until someone's query returns
// the wrong rows or an error renders as [object Object].
//
// The rule that keeps them shareable is narrow and absolute: no import
// may resolve outside web/src/shared. Not React, not i18next, not
// ../api/client, not localStorage, not chrome.*. Everything is taken as
// an argument and a value is returned; storage, transport, catalogues
// and clocks belong to the caller.
//
// Why this is a script and not a review habit. The failure is silent on
// the side where the mistake is made: adding `import i18n from '../i18n'`
// to shared/errors.ts compiles, passes the SPA's tests, and ships. It
// breaks in the OTHER package, later, for somebody else -- and the
// obvious repair at that point is to copy the module, which is the exact
// duplication the directory exists to prevent.
//
// Two rules, both textual on the import graph rather than on behaviour:
//
//   1. no import specifier inside src/shared may leave src/shared;
//   2. nothing outside src/shared may deep-import into it -- the barrel
//      (src/shared/index.ts) is the only entry point, so the surface can
//      be changed without auditing two packages.
//
// Type-only imports are NOT exempt from rule 1. `import type` erases at
// build time, so it would not break the extension's bundle -- but it
// does break its typecheck, and a shared module whose types come from
// React is not shared, it is SPA code in a shared folder.
//
// Tests inside src/shared get ONE exemption, and it is deliberately not
// a blanket one: they may import the test runner, and nothing else. A
// test is never compiled into either package, so the strict rule would
// buy nothing -- but "the runner and its own siblings" is a stronger
// statement than "anything goes", because it means the suite proves the
// module works with no collaborator at all. A shared test that needs a
// DOM, a store or a React renderer is telling you the module under it is
// not shared code.

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SRC = join(WEB, 'src')
const SHARED = join(SRC, 'shared')
const BARREL = join(SHARED, 'index.ts')

/** Every .ts/.tsx under a root, excluding generated declarations: a
 *  generated .d.ts is an artifact, not authored code, and openapi-typescript
 *  emits no imports anyway. */
function sources(root) {
  const out = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name)
      if (statSync(full).isDirectory()) walk(full)
      else if (/\.tsx?$/.test(name) && !name.endsWith('.d.ts')) out.push(full)
    }
  }
  walk(root)
  return out
}

/** Every module specifier a file imports from or re-exports, including
 *  `import type`, `export ... from`, and `import(...)` expressions. */
function specifiers(file) {
  const text = readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true)
  const found = []
  const visit = (node) => {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      found.push({ text: node.moduleSpecifier.text, node: node.moduleSpecifier })
    } else if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      node.arguments.length > 0 &&
      ts.isStringLiteral(node.arguments[0])
    ) {
      found.push({ text: node.arguments[0].text, node: node.arguments[0] })
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return found.map((f) => ({
    text: f.text,
    line: sf.getLineAndCharacterOfPosition(f.node.getStart(sf)).line + 1,
  }))
}

const escapes = []
const deepImports = []

/** The only package a module under src/shared may name, and only from a
 *  test file. Everything else -- including node: builtins -- is out. */
const TEST_ONLY_PACKAGES = new Set(['vitest'])

const isTest = (file) => /\.test\.tsx?$/.test(file)

// Rule 1: nothing inside src/shared may import outside src/shared.
for (const file of sources(SHARED)) {
  for (const spec of specifiers(file)) {
    // A bare specifier is a package (react, i18next, node:fs).
    if (!spec.text.startsWith('.')) {
      if (isTest(file) && TEST_ONLY_PACKAGES.has(spec.text)) continue
      escapes.push({ file: relative(WEB, file), line: spec.line, spec: spec.text })
      continue
    }
    const target = resolve(dirname(file), spec.text)
    if (target !== SHARED && !target.startsWith(SHARED + '/')) {
      escapes.push({ file: relative(WEB, file), line: spec.line, spec: spec.text })
    }
  }
}

// Rule 2: outside src/shared, only the barrel may be imported.
for (const file of sources(SRC)) {
  if (file === SHARED || file.startsWith(SHARED + '/')) continue
  for (const spec of specifiers(file)) {
    if (!spec.text.startsWith('.')) continue
    const target = resolve(dirname(file), spec.text)
    if (target === SHARED || target === BARREL) continue
    if (target.startsWith(SHARED + '/')) {
      deepImports.push({ file: relative(WEB, file), line: spec.line, spec: spec.text })
    }
  }
}

let failed = false

if (escapes.length > 0) {
  failed = true
  console.error(`\n${escapes.length} import(s) leaving src/shared:\n`)
  for (const e of escapes) console.error(`  ${e.file}:${e.line}  ${e.spec}`)
  console.error(
    '\nsrc/shared is compiled into the browser extension as well as the SPA,\n' +
      'so it cannot reach a React hook, a catalogue, a store or a transport.\n' +
      'Take what you need as an argument and return a value; the caller owns\n' +
      'the collaborator. If the module genuinely needs one, it is not shared\n' +
      'code -- move it back to src/lib or src/api.\n',
  )
}

if (deepImports.length > 0) {
  failed = true
  console.error(`\n${deepImports.length} deep import(s) into src/shared:\n`)
  for (const d of deepImports) console.error(`  ${d.file}:${d.line}  ${d.spec}`)
  console.error(
    "\nsrc/shared/index.ts is the entry point. Importing past it makes every\n" +
      'file in there public surface, so it can no longer be reorganised\n' +
      'without auditing two packages. Export it from the barrel instead.\n',
  )
}

if (failed) process.exit(1)

const all = sources(SHARED)
const modules = all.filter((f) => !isTest(f)).length
const tests = all.length - modules
console.log(
  `shared: ${modules} module(s) import nothing outside src/shared ` +
    `(+${tests} test file(s), runner only); no deep imports.`,
)
