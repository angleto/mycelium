// A length in this stylesheet comes from a scale, never from a literal.
//
// Two scales, one rule, so one script. The spacing steps (--sp-2 … --sp-32)
// are the rhythm BETWEEN things; the radius steps (--radius-2 … --radius-12,
// plus the pill and circle shapes) are the softness of a corner. Different
// tables, identical check -- and identical failure mode, which is the real
// reason they share a file: the boundary rule below was wrong once and made
// the check blind, and a bug like that must be fixable in one place.
//
// Why the rule exists at all, measured on this file before either scale did:
//
//   spacing  790 declarations, 36 distinct values, a near-continuum from
//            0.8px to 32px, eleven of them between 4.8px and 12.8px. Two
//            components side by side had gaps differing by less than a
//            pixel, for reasons nobody could state.
//   radius   143 declarations, 15 distinct values. `button, .btn` and
//            `input, select, textarea` at 7px, the `.card` around them at
//            9px, the kanban card at 8px, a list row at 6px: four radii one
//            pixel apart for one role, while the house token `--radius`
//            was honoured 9 times out of 143.
//
// Neither was decided; both accumulated. Each literal was reasonable on its
// own, which is exactly how UX-01 describes colour drifting, and spacing and
// radius get there faster because a gap has no contrast rule to violate, so
// nothing ever complains. Without this check the scales decay back into a
// continuum one plausible literal at a time, and the decay is invisible in
// review: `gap: 0.45rem` looks like a decision.
//
// Scope, deliberately, so a failure is always a real one:
//
//   - Spacing means padding, margin and gap. Not width, height or inset:
//     those are geometry, not the rhythm between things.
//   - `0` and `auto` are not lengths. A `1px` gap is a hairline, a border
//     drawn as a gap, and the scale has nothing to say about it.
//   - `em` and the viewport units are ALLOWED and counted, never failed on:
//     an em tracks the local font size and a vh tracks the window, which is
//     what the author wanted in the few places they appear. A token in rem
//     or px cannot express either.
//   - Inside clamp() the numbers are the ends of a FLUID range tuned against
//     a viewport width. Snapping those would change the range, not the
//     rhythm, so they are counted and left.
//
// A var(--…) naming a step the stylesheet does not define fails too: an
// undefined custom property resolves to nothing and the browser drops the
// declaration in silence, which is a layout bug with no error anywhere.

import { readFileSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const CSS = join(WEB, 'src', 'index.css')

const SCALES = [
  {
    what: 'spacing',
    props:
      '(?:padding|margin|gap|row-gap|column-gap)' +
      '(?:-(?:top|right|bottom|left|block|inline)(?:-(?:start|end))?)?',
    prefix: '--sp-',
    // A hairline is a border, not a step.
    allowed: /^(?:0|auto|inherit|initial|unset|revert|revert-layer|1px|0px)$/,
    hint: 'a step from the spacing scale in :root (--sp-2 … --sp-32)',
  },
  {
    what: 'radius',
    props: 'border(?:-(?:top|bottom)-(?:left|right))?-radius',
    prefix: '--radius-',
    allowed: /^(?:0|inherit|initial|unset|revert|revert-layer)$/,
    hint:
      'a step from the radius scale in :root (--radius-2 … --radius-12), or ' +
      '--radius-pill / --radius-circle for a shape',
  },
]

/** Font-relative or viewport-relative: reported, never failed on. */
const RELATIVE = /^-?\d*\.?\d+(?:em|ex|ch|%|vh|vw|vmin|vmax|svh|dvh|lvh)$/
const RAW_LENGTH = /(?<![\w-])(-?\d*\.?\d+)(rem|px)(?![\w-])/g

const css = readFileSync(CSS, 'utf8')

/** Character ranges inside a comment, so an example value in prose is never
 *  read as a declaration. */
const comments = []
for (const m of css.matchAll(/\/\*[\s\S]*?\*\//g)) {
  comments.push([m.index, m.index + m[0].length])
}
const inComment = (at) => comments.some(([a, b]) => at >= a && at < b)

/** Every custom property the stylesheet declares. */
const defined = new Set()
for (const m of css.matchAll(/(--[\w-]+)\s*:/g)) defined.add(m[1])

const lineOf = (at) => css.slice(0, at).split('\n').length

const offenders = []
const unknownTokens = []
const counts = []

for (const scale of SCALES) {
  let declarations = 0
  let relative = 0
  let fluid = 0

  // A declaration starts after `{`, after `;`, or at a line start -- the
  // third case is the one after a comment, where neither of the other two
  // holds. Anchoring only at the line start is what the first version of
  // this check did, and it made the check useless in exactly the place
  // nobody looks: `.x { flex: 1; gap: 0.4rem; }` was invisible to it, and
  // sixteen literals lived there.
  const decl = new RegExp(`(?:(?<=[{;])|^)\\s*(${scale.props})\\s*:\\s*([^;}]+)[;}]`, 'gm')

  for (const m of css.matchAll(decl)) {
    if (inComment(m.index)) continue
    declarations += 1
    const prop = m[1]
    const value = m[2].split(/\s+/).join(' ')
    const line = lineOf(m.index)

    // `var(--x, fallback)` is exempt from the existence check: a fallback
    // is the author saying the property may not be set, which is how a
    // component hands a per-element value to the stylesheet (XmlView sets
    // --xmlv-d as an inline style). Without the fallback there is nothing
    // between an undeclared property and a dropped declaration.
    for (const t of m[2].matchAll(/var\((--[\w-]+)\s*(,?)/g)) {
      if (!defined.has(t[1]) && !t[2]) {
        unknownTokens.push({ line, prop, value, token: t[1] })
      } else if (defined.has(t[1]) && !t[1].startsWith(scale.prefix)) {
        offenders.push({ line, prop, value, literal: `var(${t[1]})`, scale })
      }
    }

    const withoutFluid = m[2].replace(/clamp\([^)]*\)/g, 'clamp()')
    if (withoutFluid !== m[2]) fluid += 1

    for (const tok of withoutFluid.split(/\s+/).filter(Boolean)) {
      if (!scale.allowed.test(tok) && RELATIVE.test(tok)) relative += 1
    }
    for (const raw of withoutFluid.matchAll(RAW_LENGTH)) {
      if (scale.allowed.test(raw[0])) continue
      offenders.push({ line, prop, value, literal: raw[0], scale })
    }
  }

  counts.push(
    `${scale.what}: ${declarations} declaration(s), ` +
      `${relative} font/viewport-relative and ${fluid} fluid range(s) left alone`,
  )
}

console.log(`lengths: ${counts.join('; ')}.`)

if (unknownTokens.length > 0) {
  console.error(`\n${unknownTokens.length} reference(s) to a custom property that is not defined:\n`)
  for (const o of unknownTokens) {
    console.error(`  src/index.css:${o.line}  ${o.prop}: ${o.value}`)
    console.error(`      ${o.token} is not declared in :root`)
  }
  console.error(
    '\nAn undefined custom property resolves to nothing and the browser drops\n' +
      'the declaration in silence: the value is simply gone, with no error.\n',
  )
  process.exit(1)
}

if (offenders.length > 0) {
  console.error(`\n${offenders.length} declaration(s) do not come from their scale:\n`)
  for (const o of offenders) {
    console.error(`  src/index.css:${o.line}  ${o.prop}: ${o.value}`)
    console.error(`      ${o.literal} is not a ${o.scale.what} step`)
  }
  const hints = [...new Set(offenders.map((o) => o.scale.hint))]
  console.error(`\nUse ${hints.join(', or ')}.`)
  console.error(
    'If none of them is right, the answer is almost never a new literal: it\n' +
      'is that the design wants a step the scale does not have, and that is a\n' +
      'decision to take once, in :root, rather than in one rule.\n',
  )
  process.exit(1)
}

console.log('lengths: every spacing and every corner comes from its scale.')
