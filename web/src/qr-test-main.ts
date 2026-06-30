// Standalone preview for the mycelium-QR generator (served by Vite at
// /qr-test.html). NOT wired into the app; a throwaway harness to eyeball the
// figure, choose which vCard fields to encode + the ECC, and confirm the jsQR
// self-check passes before the real UI is built.
import type { Ecl, VCardData } from './lib/myceliumQr'
import {
  buildVCard,
  qrMatrix,
  randomFingerprint,
  renderMyceliumQR,
  verifyDecode,
} from './lib/myceliumQr'

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T

const canvas = $<HTMLCanvasElement>('qr')
const bgInput = $<HTMLInputElement>('bg')
const netInput = $<HTMLInputElement>('net')
const eclSel = $<HTMLSelectElement>('ecl')
const regenBtn = $<HTMLButtonElement>('regen')
const statusEl = $<HTMLElement>('status')
const fpEl = $<HTMLElement>('fp')
const infoEl = $<HTMLElement>('info')

// Synthetic issuer data per field (no real PII).
const SAMPLE = {
  name: 'Mario Rossi',
  org: 'Acme Srl',
  vat: '01234567890',
  cf: 'RSSMRA80A01H501U',
  email: 'info@acme.example',
  phone: '0612345678',
  address: 'Via Roma 1, 00100 Roma (RM)',
}
const FIELDS = ['name', 'org', 'vat', 'cf', 'email', 'phone', 'address'] as const
const checks = Object.fromEntries(
  FIELDS.map((f) => [f, $<HTMLInputElement>(`f_${f}`)]),
) as Record<(typeof FIELDS)[number], HTMLInputElement>

let seed = randomFingerprint()

function vcardData(): VCardData {
  const name = checks.name.checked ? SAMPLE.name : ''
  const org = checks.org.checked ? SAMPLE.org : ''
  return {
    fullName: name || org || 'Emittente', // FN is mandatory in a vCard
    org: org || null,
    vat: checks.vat.checked ? SAMPLE.vat : null,
    cf: checks.cf.checked ? SAMPLE.cf : null,
    email: checks.email.checked ? SAMPLE.email : null,
    phone: checks.phone.checked ? SAMPLE.phone : null,
    address: checks.address.checked ? SAMPLE.address : null,
  }
}

function draw(): void {
  const vcard = buildVCard(vcardData())
  const matrix = qrMatrix(vcard, eclSel.value as Ecl)
  renderMyceliumQR(canvas, matrix, { seed, bg: bgInput.value, net: netInput.value })
  const ok = verifyDecode(canvas, vcard)
  statusEl.textContent = ok ? 'scansionabile (jsQR): OK ✓' : 'scansionabile (jsQR): NO ✗'
  statusEl.className = ok ? 'ok' : 'no'
  fpEl.textContent = seed
  infoEl.textContent = `${matrix.length}×${matrix.length} moduli · ECC ${eclSel.value} · ${vcard.length} char`
}

regenBtn.addEventListener('click', () => {
  seed = randomFingerprint()
  draw()
})
for (const el of [bgInput, netInput, eclSel, ...Object.values(checks)]) {
  el.addEventListener('input', draw)
}
draw()
