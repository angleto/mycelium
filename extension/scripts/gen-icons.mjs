// The four icons Chrome asks for, rasterised from the brand assets.
//
// Which source at which size is not a guess: the brand guidelines set a
// minimum of 24px for the full mark and record that the monogram stays
// legible down to 16px, because it draws fewer hyphae. So the two small
// sizes come from the monogram and the two large ones from the full mark.
// Rendering the full mark at 16px produces a smudge that reads as a
// rendering fault rather than as a logo.
//
// Run by hand, and the output is COMMITTED. Rasterising during the build
// would put librsvg in the release gate to redraw four files that change
// about never, and a gate that can fail for a reason unrelated to the
// change is a gate people learn to rerun rather than read.

import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const ASSETS = join(ROOT, '..', 'assets')
const OUT = join(ROOT, 'icons')

/** @type {{size: number, source: string}[]} */
const ICONS = [
  { size: 16, source: 'mycelium-monogram.svg' },
  { size: 32, source: 'mycelium-monogram.svg' },
  { size: 48, source: 'mycelium-logo.svg' },
  { size: 128, source: 'mycelium-logo.svg' },
]

mkdirSync(OUT, { recursive: true })

for (const { size, source } of ICONS) {
  const target = join(OUT, `icon-${size}.png`)
  execFileSync(
    'rsvg-convert',
    ['--width', String(size), '--height', String(size), '--output', target, join(ASSETS, source)],
    { stdio: 'inherit' },
  )
  console.log(`icons: ${size}px from ${source}`)
}
