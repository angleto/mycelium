import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useMe } from '../auth/useMe'
import type { components } from '../api/schema'
import type { Ecl, VCardData } from '../lib/myceliumQr'
import {
  buildVCard,
  qrMatrix,
  randomFingerprint,
  renderMyceliumQR,
  verifyDecode,
} from '../lib/myceliumQr'

type Profile = components['schemas']['IssuerProfileOut']

const ECLS: Ecl[] = ['L', 'M', 'Q', 'H']
const FIELDS = ['name', 'org', 'vat', 'cf', 'email', 'phone', 'address'] as const
type Field = (typeof FIELDS)[number]

// Generate the user's mycelium-QR avatar from the default issuer profile's
// fiscal identity (a scannable vCard), styled as a mycelial network. The
// random fingerprint seeds the styling (regenerate -> a new figure); colours
// and which fields to encode are user-chosen. A jsQR self-check (full size +
// the 22 mm invoice-logo size) confirms the result actually scans before it
// can be saved. The same PNG can optionally be applied as the issuer logo.
export function AvatarSettings() {
  const { t } = useTranslation()
  const { me } = useMe()
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [issuer, setIssuer] = useState<Profile | null>(null)
  const [bg, setBg] = useState('#4a6b3e')
  const [net, setNet] = useState('#ffffff')
  const [ecl, setEcl] = useState<Ecl>('H')
  const [fields, setFields] = useState<Record<Field, boolean>>({
    name: true,
    org: true,
    vat: false,
    cf: false,
    email: false,
    phone: true,
    address: false,
  })
  const [seed, setSeed] = useState(randomFingerprint())
  const [ok, setOk] = useState(false)
  const [okPrint, setOkPrint] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const printRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/issuer-profiles', {
        params: { header: workspaceHeader() },
      })
      if (!active || !data) return
      setProfiles(data)
      setIssuer(data.find((p) => p.is_default) ?? data[0] ?? null)
    })()
    return () => {
      active = false
    }
  }, [])

  const vdata = useCallback((): VCardData => {
    const p = issuer
    const person = [p?.first_name, p?.last_name].filter(Boolean).join(' ')
    const name = fields.name ? p?.legal_name || person || me?.display_name || '' : ''
    const org = fields.org ? p?.legal_name || '' : ''
    const addr = fields.address
      ? [p?.address, p?.civic_number, p?.postal_code, p?.city, p?.province]
          .filter(Boolean)
          .join(' ')
      : ''
    return {
      fullName: name || org || me?.email || 'Emittente',
      org: org || null,
      vat: fields.vat ? (p?.vat_number ?? null) : null,
      cf: fields.cf ? (p?.tax_code ?? null) : null,
      email: fields.email ? (p?.email ?? me?.email ?? null) : null,
      phone: fields.phone ? (p?.phone ?? null) : null,
      address: addr || null,
    }
  }, [issuer, fields, me])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    if (!printRef.current) printRef.current = document.createElement('canvas')
    const vcard = buildVCard(vdata())
    const matrix = qrMatrix(vcard, ecl)
    renderMyceliumQR(canvas, matrix, { seed, bg, net })
    setOk(verifyDecode(canvas, vcard))
    renderMyceliumQR(printRef.current, matrix, { seed, bg, net, size: 130 })
    setOkPrint(verifyDecode(printRef.current, vcard))
  }, [vdata, ecl, seed, bg, net])

  useEffect(() => {
    draw()
  }, [draw])

  function blobFromCanvas(): Promise<Blob | null> {
    return new Promise((resolve) => {
      const canvas = canvasRef.current
      if (!canvas) return resolve(null)
      canvas.toBlob((b) => resolve(b), 'image/png')
    })
  }

  async function saveAvatar() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const blob = await blobFromCanvas()
    if (!blob) {
      setBusy(false)
      return
    }
    const body = new FormData()
    body.append('file', blob, 'avatar.png')
    body.append('seed', seed)
    body.append('bg', bg)
    body.append('net', net)
    const res = await authFetch('/auth/me/avatar', { method: 'POST', body })
    setBusy(false)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => undefined)))
      return
    }
    setMsg(t('avatar.saved'))
    window.dispatchEvent(new Event('avatar-updated'))
  }

  async function applyAsLogo() {
    if (!issuer) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    const blob = await blobFromCanvas()
    if (!blob) {
      setBusy(false)
      return
    }
    const body = new FormData()
    body.append('file', blob, 'avatar.png')
    const res = await authFetch(`/issuer-profiles/${issuer.id}/logo`, {
      method: 'POST',
      body,
    })
    setBusy(false)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => undefined)))
      return
    }
    setMsg(t('avatar.usedAsLogo', { label: issuer.label }))
  }

  return (
    <section className="card">
      <h2>{t('avatar.title')}</h2>
      <p className="hint">{t('avatar.hint')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <div className="row" style={{ alignItems: 'flex-start', gap: '1.5rem' }}>
        <canvas
          ref={canvasRef}
          style={{ width: 220, height: 220, border: '1px solid var(--border)' }}
        />
        <div>
          <div className="row">
            <label>
              {t('avatar.bg')}{' '}
              <input type="color" value={bg} onChange={(e) => setBg(e.target.value)} />
            </label>
            <label>
              {t('avatar.net')}{' '}
              <input type="color" value={net} onChange={(e) => setNet(e.target.value)} />
            </label>
            <button type="button" onClick={() => setSeed(randomFingerprint())}>
              {t('avatar.regenerate')}
            </button>
          </div>
          <div className="row">
            <label>
              {t('avatar.ecc')}{' '}
              <select value={ecl} onChange={(e) => setEcl(e.target.value as Ecl)}>
                {ECLS.map((x) => (
                  <option key={x} value={x}>
                    {x}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <fieldset style={{ border: '1px solid var(--border)', borderRadius: 6 }}>
            <legend>{t('avatar.fields')}</legend>
            {FIELDS.map((f) => (
              <label key={f}>
                <input
                  type="checkbox"
                  checked={fields[f]}
                  onChange={(e) => setFields({ ...fields, [f]: e.target.checked })}
                />{' '}
                {t(`avatar.field.${f}`)}
              </label>
            ))}
          </fieldset>
          <p className={ok ? 'ok' : 'err'}>
            {ok ? t('avatar.scanOk') : t('avatar.scanNo')}
          </p>
          <p className={okPrint ? 'ok' : 'err'}>
            {okPrint ? t('avatar.scanPrintOk') : t('avatar.scanPrintNo')}
          </p>
        </div>
      </div>
      <div className="row">
        <button type="button" disabled={busy || !ok} onClick={() => void saveAvatar()}>
          {t('avatar.save')}
        </button>
        {issuer && (
          <button
            type="button"
            className="btn--ghost"
            disabled={busy || !ok}
            onClick={() => void applyAsLogo()}
          >
            {t('avatar.useAsLogo', { label: issuer.label })}
          </button>
        )}
      </div>
      {profiles.length > 1 && (
        <label className="row">
          {t('avatar.issuer')}{' '}
          <select
            value={issuer?.id ?? ''}
            onChange={(e) => setIssuer(profiles.find((p) => p.id === e.target.value) ?? null)}
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
      )}
    </section>
  )
}
