// Mycelium-QR: a real, scannable QR code rendered as a mycelial network.
//
// The QR *matrix* is computed by qrcode-generator at ECC level H (max
// redundancy -> tolerant of the organic styling). The styling (node sizes,
// hypha jitter) is driven by a seeded PRNG, so a given fingerprint always
// produces the same figure; "regenerate" = new fingerprint. Finder + timing
// patterns are kept solid so scanners lock on; data modules become organic
// nodes linked by hyphae that stay within dark territory (orthogonal
// neighbours only), so light modules are never corrupted. verifyDecode() runs
// jsQR (trying the inverted image too, since the default white-on-green is an
// inverted code) to PROVE the rendered image still scans.

import jsQR from 'jsqr'
import qrcode from 'qrcode-generator'

function mulberry32(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) | 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function hashStr(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

const _B32 = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789' // Crockford-ish, no I/O/0/1

/** A crypto-random, practically-unique fingerprint string (the regeneration
 * seed). Same fingerprint -> same topology; new one -> a different figure. */
export function randomFingerprint(): string {
  const bytes = new Uint8Array(12)
  crypto.getRandomValues(bytes)
  let out = ''
  for (const b of bytes) out += _B32[b & 31]
  return `MYC-${out}`
}

export interface VCardData {
  fullName: string
  org?: string | null
  vat?: string | null
  cf?: string | null
  email?: string | null
  pec?: string | null
  phone?: string | null
  address?: string | null
}

/** A vCard 3.0 string a phone camera scans into a contact. Built from the
 * issuer's fiscal identity (so the QR identifies the issuer). */
export function buildVCard(d: VCardData): string {
  const esc = (v: string) => v.replace(/([,;\\])/g, '\\$1').replace(/\n/g, '\\n')
  const lines = ['BEGIN:VCARD', 'VERSION:3.0', `FN:${esc(d.fullName)}`]
  if (d.org) lines.push(`ORG:${esc(d.org)}`)
  if (d.email) lines.push(`EMAIL;TYPE=INTERNET:${esc(d.email)}`)
  if (d.pec) lines.push(`EMAIL;TYPE=PEC:${esc(d.pec)}`)
  if (d.phone) lines.push(`TEL:${esc(d.phone)}`)
  if (d.address) lines.push(`ADR:;;${esc(d.address)};;;;`)
  const note = [d.vat ? `P.IVA ${d.vat}` : '', d.cf ? `C.F. ${d.cf}` : ''].filter(Boolean).join(' ')
  if (note) lines.push(`NOTE:${esc(note)}`)
  lines.push('END:VCARD')
  return lines.join('\r\n')
}

export type Ecl = 'L' | 'M' | 'Q' | 'H'

/** The QR module matrix (boolean[r][c], true = dark). Default ECC ``H`` (max
 * redundancy ~30%, most robust to damage + styling). Density is then tuned by
 * the payload: fewer vCard fields -> fewer modules -> the filamentous look.
 * Lower ECC trades robustness for fewer modules. */
export function qrMatrix(text: string, ecl: Ecl = 'H'): boolean[][] {
  const qr = qrcode(0, ecl)
  qr.addData(text)
  qr.make()
  const n = qr.getModuleCount()
  const m: boolean[][] = []
  for (let r = 0; r < n; r++) {
    const row: boolean[] = []
    for (let c = 0; c < n; c++) row.push(qr.isDark(r, c))
    m.push(row)
  }
  return m
}

/** Whether (r,c) is inside one of the three 7x7 finder patterns. */
function isFinder(r: number, c: number, n: number): boolean {
  const inBox = (br: number, bc: number) => r >= br && r < br + 7 && c >= bc && c < bc + 7
  return inBox(0, 0) || inBox(0, n - 7) || inBox(n - 7, 0)
}

export interface RenderOpts {
  seed: string
  bg: string
  net: string
  size?: number
}

/** Draw the mycelium-styled QR onto a canvas. Quiet zone preserved; finders
 * solid; data modules -> nodes; orthogonally-adjacent dark modules linked by
 * hyphae. Deterministic given ``seed``. */
export function renderMyceliumQR(
  canvas: HTMLCanvasElement,
  matrix: boolean[][],
  opts: RenderOpts,
): void {
  const n = matrix.length
  const quiet = 4
  const total = n + quiet * 2
  // Adaptive resolution: ~14 px per module so a dense (ECC H) grid still
  // resolves the thin filaments and a scanner samples each module centre.
  const size = opts.size ?? Math.max(512, total * 14)
  const cell = size / total
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const rnd = mulberry32(hashStr(opts.seed))
  const off = quiet * cell
  const cx = (c: number) => off + (c + 0.5) * cell
  const cy = (r: number) => off + (r + 0.5) * cell

  ctx.fillStyle = opts.bg
  ctx.fillRect(0, 0, size, size)

  // Finder patterns: concentric, lightly rounded, drawn solid so scanners
  // reliably locate the symbol even with the organic data styling.
  const finder = (br: number, bc: number) => {
    ctx.fillStyle = opts.net
    ctx.beginPath()
    ctx.roundRect(off + bc * cell, off + br * cell, 7 * cell, 7 * cell, cell * 1.4)
    ctx.fill()
    ctx.fillStyle = opts.bg
    ctx.beginPath()
    ctx.roundRect(off + (bc + 1) * cell, off + (br + 1) * cell, 5 * cell, 5 * cell, cell)
    ctx.fill()
    ctx.fillStyle = opts.net
    ctx.beginPath()
    ctx.roundRect(off + (bc + 2) * cell, off + (br + 2) * cell, 3 * cell, 3 * cell, cell * 0.7)
    ctx.fill()
  }
  finder(0, 0)
  finder(0, n - 7)
  finder(n - 7, 0)

  // A data module = dark, not in a finder. Hyphae link orthogonal data
  // neighbours; a stroke lies between two dark centres (dark on both ends) so
  // a light module is never overdrawn.
  const isData = (r: number, c: number) =>
    r >= 0 && r < n && c >= 0 && c < n && matrix[r][c] && !isFinder(r, c, n)
  const degree = (r: number, c: number) =>
    (isData(r, c - 1) ? 1 : 0) +
    (isData(r, c + 1) ? 1 : 0) +
    (isData(r - 1, c) ? 1 : 0) +
    (isData(r + 1, c) ? 1 : 0)

  // Density-adaptive coverage: a sparse (low-ECC / short payload) figure stays
  // thin and thread-like; a dense (ECC H) one fills the modules more so a
  // scanner still resolves the grid. The thin filamentous look lives at low
  // density; high density trades some of it for scannability.
  const dense = Math.min(1, Math.max(0, (n - 29) / 24))
  const boost = dense * 0.18

  // Filaments: thin continuous threads, so a straight run of dark modules
  // reads as one hypha rather than a row of dots. Kept straight (a curve could
  // bow into an adjacent light module and break the scan).
  ctx.strokeStyle = opts.net
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (!isData(r, c)) continue
      ctx.lineWidth = cell * (0.3 + boost + rnd() * 0.1)
      if (isData(r, c + 1)) {
        ctx.beginPath()
        ctx.moveTo(cx(c), cy(r))
        ctx.lineTo(cx(c + 1), cy(r))
        ctx.stroke()
      }
      if (isData(r + 1, c)) {
        ctx.beginPath()
        ctx.moveTo(cx(c), cy(r))
        ctx.lineTo(cx(c), cy(r + 1))
        ctx.stroke()
      }
    }
  }

  // Nodes sized by degree: a spore dot on an isolated module, a small swelling
  // on a pass-through, a fuller node at a branch point. Every dark module's
  // centre stays covered (filament and/or node) so the scanner samples dark.
  ctx.fillStyle = opts.net
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (!isData(r, c)) continue
      const deg = degree(r, c)
      const base = deg === 0 ? 0.4 : deg === 1 ? 0.32 : deg >= 3 ? 0.36 : 0.24
      const rad = cell * (base + boost + rnd() * 0.06)
      if (rad <= 0) continue
      ctx.beginPath()
      ctx.arc(cx(c), cy(r), rad, 0, Math.PI * 2)
      ctx.fill()
    }
  }

  drawCreature(ctx, { cx: off + (n * cell) / 2, cy: off + (n * cell) / 2, r: n * cell * 0.18, cell, bg: opts.bg, net: opts.net, rnd })
}

// The central "creature": a unique random mycelial network seeded by the
// fingerprint, occupying the middle of the code. ECC H recovers the modules it
// occludes (kept well under the error budget), so the QR still scans; the
// creature is what makes each avatar recognisably distinct. Regenerate -> new
// seed -> a different creature.
function drawCreature(
  ctx: CanvasRenderingContext2D,
  o: { cx: number; cy: number; r: number; cell: number; bg: string; net: string; rnd: () => number },
): void {
  const { cx, cy, r, cell, bg, net, rnd } = o
  // Clear a background disc (with a small gap) so the data modules under the
  // creature are gone and it reads separately from the surrounding hyphae.
  ctx.fillStyle = bg
  ctx.beginPath()
  ctx.arc(cx, cy, r + cell * 1.1, 0, Math.PI * 2)
  ctx.fill()

  ctx.strokeStyle = net
  ctx.fillStyle = net
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'

  const grow = (x: number, y: number, ang: number, len: number, width: number, depth: number) => {
    if (len < cell * 0.7 || depth > 4) return
    const steps = 2 + Math.floor(rnd() * 3)
    let px = x
    let py = y
    let a = ang
    for (let i = 0; i < steps; i++) {
      a += (rnd() - 0.5) * 0.9
      const seg = len / steps
      const nx = px + Math.cos(a) * seg
      const ny = py + Math.sin(a) * seg
      ctx.lineWidth = width
      ctx.beginPath()
      ctx.moveTo(px, py)
      ctx.lineTo(nx, ny)
      ctx.stroke()
      px = nx
      py = ny
      if (depth < 3 && rnd() < 0.45) {
        grow(px, py, a + (rnd() - 0.5) * 1.6, len * 0.55, width * 0.7, depth + 1)
      }
    }
    // A spore swelling at the filament tip.
    ctx.beginPath()
    ctx.arc(px, py, Math.max(width * 1.1, cell * 0.35), 0, Math.PI * 2)
    ctx.fill()
  }

  const arms = 5 + Math.floor(rnd() * 4)
  for (let k = 0; k < arms; k++) {
    const ang = (k / arms) * Math.PI * 2 + (rnd() - 0.5) * 0.7
    grow(cx, cy, ang, r * (0.75 + rnd() * 0.3), cell * 0.55, 0)
  }
  // Central hypha knot.
  ctx.beginPath()
  ctx.arc(cx, cy, cell * 0.95, 0, Math.PI * 2)
  ctx.fill()
}

/** A pure mycelial network filling the frame — the decorative avatar (NOT a
 * QR, not scannable). A random branching hypha web grown from the centre,
 * seeded by the fingerprint so each is unique + reproducible; regenerate ->
 * a new organism. Same colours as the QR variant. */
export function renderMyceliumOnly(
  canvas: HTMLCanvasElement,
  opts: { seed: string; bg: string; net: string; size?: number },
): void {
  const size = opts.size ?? 512
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const rnd = mulberry32(hashStr(opts.seed))
  const unit = size / 32
  ctx.fillStyle = opts.bg
  ctx.fillRect(0, 0, size, size)
  ctx.strokeStyle = opts.net
  ctx.fillStyle = opts.net
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  const mx = size / 2
  const my = size / 2
  // Keep the organism centred and INSIDE the frame: hyphae never grow past
  // this radius (a margin short of the edge), so nothing is clipped by the box.
  const bound = size * 0.44

  const grow = (x: number, y: number, ang: number, len: number, width: number, depth: number) => {
    if (len < unit * 0.8 || depth > 6) return
    const steps = 2 + Math.floor(rnd() * 3)
    let px = x
    let py = y
    let a = ang
    for (let i = 0; i < steps; i++) {
      a += (rnd() - 0.5) * 0.7
      const seg = len / steps
      const nx = px + Math.cos(a) * seg
      const ny = py + Math.sin(a) * seg
      // Stop this filament at the boundary rather than letting it exit the box.
      if ((nx - mx) ** 2 + (ny - my) ** 2 > bound * bound) break
      ctx.lineWidth = width
      ctx.beginPath()
      ctx.moveTo(px, py)
      ctx.lineTo(nx, ny)
      ctx.stroke()
      px = nx
      py = ny
      if (depth < 5 && rnd() < 0.5) {
        grow(px, py, a + (rnd() - 0.5) * 1.5, len * 0.62, width * 0.72, depth + 1)
      }
    }
    // Spore swelling at the tip.
    ctx.beginPath()
    ctx.arc(px, py, Math.max(width * 1.1, unit * 0.32), 0, Math.PI * 2)
    ctx.fill()
  }

  const arms = 6 + Math.floor(rnd() * 5)
  for (let k = 0; k < arms; k++) {
    const ang = (k / arms) * Math.PI * 2 + (rnd() - 0.5) * 0.5
    grow(mx, my, ang, size * 0.42 * (0.8 + rnd() * 0.4), unit * 0.7, 0)
  }
  // Central hypha knot.
  ctx.beginPath()
  ctx.arc(mx, my, unit * 1.2, 0, Math.PI * 2)
  ctx.fill()
}

/** Decode the rendered canvas with jsQR (trying the inverted image too, since
 * a white-network-on-coloured-bg is an inverted code) and check it equals the
 * expected payload. The in-app scannability self-check. */
export function verifyDecode(canvas: HTMLCanvasElement, expected: string): boolean {
  const ctx = canvas.getContext('2d')
  if (!ctx) return false
  const img = ctx.getImageData(0, 0, canvas.width, canvas.height)
  const res = jsQR(img.data, img.width, img.height, { inversionAttempts: 'attemptBoth' })
  return res?.data === expected
}
