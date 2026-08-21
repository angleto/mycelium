// A button that drops the accent FILL must also drop the accent INK.
//
// index.css paints the bare element:
//
//   button, .btn { background: var(--accent); color: var(--accent-fg); }
//
// so every control that is not an accent-filled button -- a dropdown row, a
// list option, a chrome-less icon button -- has to undo BOTH halves. Undoing
// only the background leaves `color: var(--accent-fg)`, which is white over
// the light surface (#ffffff on #fdfcf7) and near-black over the dark one
// (#0f1612 on #161e18): about 1.05:1 in either theme, i.e. invisible, and
// invisible in BOTH so no amount of theme-flipping reveals it.
//
// That has now shipped three times -- .assignpick__opt and .deppick__row were
// patched after the fact, .csearch__row was the client-search dropdown of task
// 805a569c ("il dropdown ha dei colori che rendono illeggibile il contenuto sia
// in modalita light che dark"). It is not a thing authors can be asked to
// remember: the fill is the DEFAULT, so getting it right means opting out of
// something invisible in the rule you are writing. This check is the opt-out
// made mechanical.
//
// Scope, deliberately narrow in two ways, so a failure is always a real one:
//
//   - Only class names this script can prove reach a <button>: a string
//     literal in a className on a <button> tag, including the literal parts of
//     a template/conditional expression. A class applied through a variable is
//     unresolvable without evaluating the program; those are counted and
//     reported, never failed on -- same contract as check-i18n.mjs.
//   - Only CSS selectors that are a compound of classes on the element itself
//     (`.a`, `.a.b`, `button.a`), which is how this file styles buttons. A
//     descendant selector is skipped rather than guessed at: `.list li` names
//     an <li>, and deciding whether it can also name a <button> needs the DOM,
//     not the stylesheet.
//
// The fill and the ink may legitimately live in two rules on the same element
// (`.plant__btn` inks, `.plant__btn--ghost` de-fills), so a rule is judged
// against the class SETS actually rendered, not against its own selector.

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SRC = join(WEB, 'src')
const CSS = join(SRC, 'index.css')

// A background that IS the accent fill keeps the accent ink: those rules are
// the ones the global declaration is written for.
const KEEPS_ACCENT_INK = /^var\(--accent[,)]|^var\(--brand[,)]/

function sources(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...sources(p))
    else if (/\.tsx$/.test(name)) out.push(p)
  }
  return out
}

/** One entry per <button> in the source: the set of classes rendered on it. */
function buttonClassSets(files) {
  const sets = []
  let dynamic = 0

  // Collect every string literal reachable inside a className expression;
  // `'chip' + (on ? ' chip--on' : '')` contributes both, which is exactly
  // how this codebase writes conditional classes.
  const literals = (node, into) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      for (const c of node.text.split(/\s+/).filter(Boolean)) into.add(c)
      return true
    }
    let found = false
    ts.forEachChild(node, (child) => {
      if (literals(child, into)) found = true
    })
    return found
  }

  for (const file of files) {
    const sf = ts.createSourceFile(
      file,
      readFileSync(file, 'utf8'),
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    )
    const visit = (node) => {
      const tag =
        ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)
          ? node
          : null
      if (tag && tag.tagName.getText(sf) === 'button') {
        for (const attr of tag.attributes.properties) {
          if (!ts.isJsxAttribute(attr)) continue
          if (attr.name.getText(sf) !== 'className') continue
          const init = attr.initializer
          if (!init) continue
          const expr = ts.isJsxExpression(init) ? init.expression : init
          const set = new Set()
          if (!expr || !literals(expr, set)) dynamic += 1
          if (set.size > 0) sets.push(set)
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)
  }
  return { sets, dynamic }
}

/** Flat list of {selector, decls, line} for every top-level CSS rule. */
function cssRules() {
  const css = readFileSync(CSS, 'utf8')
  // Strip comments first so a `/* background: … */` note cannot be read as a
  // declaration, then walk brace depth so @media blocks keep their inner rules.
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
  const rules = []
  let depth = 0
  let start = 0
  for (let i = 0; i < clean.length; i += 1) {
    const ch = clean[i]
    if (ch === '{') {
      if (depth === 0) start = i
      depth += 1
    } else if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        const head = clean.slice(0, start)
        const selector = head.slice(head.lastIndexOf('}') + 1).trim()
        // An at-rule wrapper (@media/@supports) has no declarations of its
        // own; its children were already visited at the inner depth.
        if (selector && !selector.startsWith('@')) {
          rules.push({
            selector,
            body: clean.slice(start + 1, i),
            line: clean.slice(0, start).split('\n').length,
          })
        }
      }
    }
  }
  // Nested rules (inside @media) are skipped by the depth-0 walk above; run
  // the same scan over each at-rule body.
  const nested = []
  for (const m of clean.matchAll(/@(?:media|supports)[^{]*\{/g)) {
    let depth2 = 1
    let i = m.index + m[0].length
    const bodyStart = i
    for (; i < clean.length && depth2 > 0; i += 1) {
      if (clean[i] === '{') depth2 += 1
      else if (clean[i] === '}') depth2 -= 1
    }
    const inner = clean.slice(bodyStart, i - 1)
    const offset = clean.slice(0, bodyStart).split('\n').length - 1
    for (const r of inner.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      nested.push({
        selector: r[1].trim(),
        body: r[2],
        line: offset + inner.slice(0, r.index).split('\n').length,
      })
    }
  }
  return [...rules, ...nested]
}

const decl = (body, prop) => {
  const m = body.match(new RegExp(`(?:^|[;{])\\s*${prop}\\s*:\\s*([^;]+)`, 'i'))
  return m ? m[1].trim() : null
}

/** The classes a compound selector requires, or null if it is not one. */
function compoundClasses(part) {
  const sel = part.trim()
  if (!sel || /[\s>+~[:]/.test(sel)) return null
  const body = sel.startsWith('button') ? sel.slice('button'.length) : sel
  if (!/^(?:\.[A-Za-z0-9_-]+)+$/.test(body)) return null
  return new Set(body.split('.').filter(Boolean))
}

const subset = (need, have) => [...need].every((c) => have.has(c))

const { sets, dynamic } = buttonClassSets(sources(SRC))
const rules = cssRules()

// Flatten every rule into (required classes, declarations), dropping the ones
// whose selector this script will not reason about.
const applicable = []
for (const r of rules) {
  for (const part of r.selector.split(',')) {
    const need = compoundClasses(part)
    if (need) applicable.push({ ...r, part: part.trim(), need })
  }
}

// A button is broken when, over all the rules that reach it, the LAST word on
// `background` de-fills it and no rule sets `color`. Cascade order is source
// order here: every one of these selectors is a class compound, and the
// codebase does not mix specificities on a single element.
const offenders = new Map()
for (const set of sets) {
  const reaching = applicable.filter((r) => subset(r.need, set))
  if (reaching.length === 0) continue
  const inks = reaching.filter((r) => decl(r.body, 'color'))
  if (inks.length > 0) continue
  const fills = reaching.filter(
    (r) =>
      !/:hover|:focus|:active|:disabled/.test(r.part) &&
      (decl(r.body, 'background') ?? decl(r.body, 'background-color')),
  )
  const last = fills[fills.length - 1]
  if (!last) continue
  const bg = decl(last.body, 'background') ?? decl(last.body, 'background-color')
  if (KEEPS_ACCENT_INK.test(bg)) continue
  offenders.set(`${last.line}:${last.part}`, { ...last, bg })
}

console.log(
  `button ink: ${sets.length} <button> class sets, ` +
    `${applicable.length} class-compound rules scanned, ` +
    `${dynamic} dynamic className expression(s) skipped.`,
)

if (offenders.size > 0) {
  console.error(
    `\n${offenders.size} rule(s) repaint a <button> background without ` +
      `setting its color:\n`,
  )
  for (const o of offenders.values()) {
    console.error(`  src/index.css:${o.line}  ${o.part}`)
    console.error(`      background: ${o.bg}   (color: not set)`)
  }
  console.error(
    '\nSuch a button keeps `color: var(--accent-fg)` from the global `button`\n' +
      "rule, which is the fill's ink: unreadable on any non-accent background,\n" +
      'in BOTH themes. Add the matching `color:` (usually var(--text)).\n',
  )
  process.exit(1)
}

console.log('button ink: every de-filled button sets its own colour.')
