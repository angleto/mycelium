import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useMe } from '../auth/useMe'
import type { components } from '../api/schema'
import type { Ecl, VCardData } from '../lib/myceliumQr'
import { buildVCard, qrMatrix, randomFingerprint, renderMyceliumQR, verifyDecode } from '../lib/myceliumQr'

type Profile = components['schemas']['IssuerProfileOut']
type Source = 'image' | 'avatar' | 'avatar_qr'
const ECLS: Ecl[] = ['L', 'M', 'Q', 'H']
const FIELDS = ['name', 'org', 'vat', 'cf', 'email', 'pec', 'phone', 'address'] as const
type Field = (typeof FIELDS)[number]
const POSITIONS = ['left', 'right', 'top'] as const

// The issuer letterhead logo: choose an uploaded image, the user's mycelium
// avatar, or a scannable "avatar + QR" (a mycelium-QR of this issuer's fiscal
// vCard), and where it sits relative to the title. A square avatar/QR prints in
// a larger box than a wordmark, so the QR stays scannable on the PDF.
export function IssuerLogoConfig({
  profile,
  onChanged,
}: {
  profile: Profile
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const { me } = useMe()
  const [source, setSource] = useState<Source>((profile.logo_kind as Source) || 'image')
  const [position, setPosition] = useState<string>(profile.logo_position || 'left')
  const [bg, setBg] = useState('#4a6b3e')
  const [net, setNet] = useState('#ffffff')
  const [ecl, setEcl] = useState<Ecl>('H')
  const [fields, setFields] = useState<Record<Field, boolean>>({
    name: true,
    org: true,
    vat: true,
    cf: false,
    email: false,
    pec: false,
    phone: false,
    address: false,
  })
  const [seed, setSeed] = useState(randomFingerprint())
  const [ok, setOk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)

  const vdata = useCallback((): VCardData => {
    const person = [profile.first_name, profile.last_name].filter(Boolean).join(' ')
    const name = fields.name ? profile.legal_name || person || '' : ''
    const org = fields.org ? profile.legal_name || '' : ''
    const addr = fields.address
      ? [profile.address, profile.civic_number, profile.postal_code, profile.city, profile.province]
          .filter(Boolean)
          .join(' ')
      : ''
    return {
      fullName: name || org || profile.label,
      org: org || null,
      vat: fields.vat ? (profile.vat_number ?? null) : null,
      cf: fields.cf ? (profile.tax_code ?? null) : null,
      email: fields.email ? (profile.email ?? null) : null,
      pec: fields.pec ? (profile.pec ?? null) : null,
      phone: fields.phone ? (profile.phone ?? null) : null,
      address: addr || null,
    }
  }, [profile, fields])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || source !== 'avatar_qr') return
    const vcard = buildVCard(vdata())
    renderMyceliumQR(canvas, qrMatrix(vcard, ecl), { seed, bg, net })
    setOk(verifyDecode(canvas, vcard))
  }, [source, vdata, ecl, seed, bg, net])

  useEffect(() => {
    draw()
  }, [draw])

  async function patchPosition(pos: string) {
    setPosition(pos)
    setErr(null)
    const { error } = await api.PATCH('/issuer-profiles/{profile_id}', {
      params: { path: { profile_id: profile.id }, header: workspaceHeader() },
      body: { logo_position: pos },
    })
    if (error) setErr(errMessage(error))
    else onChanged()
  }

  async function upload(blob: Blob, kind: Source, filename: string) {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const body = new FormData()
    body.append('file', blob, filename)
    body.append('kind', kind)
    const res = await authFetch(`/issuer-profiles/${profile.id}/logo`, { method: 'POST', body })
    setBusy(false)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => undefined)))
      return
    }
    setMsg(t('logo.saved'))
    onChanged()
  }

  async function uploadImageFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (file) await upload(file, 'image', file.name)
  }

  async function applyAvatarAsLogo() {
    // Reuse the user's saved avatar PNG verbatim as the logo.
    const res = await authFetch('/auth/me/avatar')
    if (!res.ok) {
      setErr(t('logo.noAvatar'))
      return
    }
    await upload(await res.blob(), 'avatar', 'avatar.png')
  }

  function generateQrLogo() {
    const canvas = canvasRef.current
    if (!canvas) return
    canvas.toBlob((b) => {
      if (b) void upload(b, 'avatar_qr', 'avatar-qr.png')
    }, 'image/png')
  }

  return (
    <div className="field">
      {t('invoices.logo')}
      <p className="hint">{t('logo.hint')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <div className="row" style={{ gap: '0.75rem', flexWrap: 'wrap' }}>
        <strong>{t('logo.position')}:</strong>
        {POSITIONS.map((p) => (
          <label key={p} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem' }}>
            <input
              type="radio"
              name={`pos-${profile.id}`}
              checked={position === p}
              onChange={() => void patchPosition(p)}
            />
            {t(`logo.pos.${p}`)}
          </label>
        ))}
      </div>

      <div className="row" style={{ gap: '0.75rem', flexWrap: 'wrap', marginTop: '0.5rem' }}>
        <strong>{t('logo.source')}:</strong>
        {(['image', 'avatar', 'avatar_qr'] as Source[]).map((s) => (
          <label key={s} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem' }}>
            <input
              type="radio"
              name={`src-${profile.id}`}
              checked={source === s}
              onChange={() => setSource(s)}
            />
            {t(`logo.src.${s}`)}
          </label>
        ))}
      </div>

      {source === 'image' && (
        <label className="btn--sm btn--ghost" style={{ marginTop: '0.5rem' }}>
          {busy ? '…' : t('invoices.logoUpload')}
          <input type="file" accept="image/png,image/jpeg" hidden onChange={(e) => void uploadImageFile(e)} />
        </label>
      )}

      {source === 'avatar' && (
        <div className="row" style={{ marginTop: '0.5rem' }}>
          <button
            type="button"
            className="btn--sm"
            disabled={busy || !me?.has_avatar}
            onClick={() => void applyAvatarAsLogo()}
          >
            {t('logo.useAvatar')}
          </button>
          {!me?.has_avatar && <span className="hint">{t('logo.noAvatar')}</span>}
        </div>
      )}

      {source === 'avatar_qr' && (
        <div className="row" style={{ alignItems: 'flex-start', gap: '1.5rem', marginTop: '0.5rem' }}>
          <canvas
            ref={canvasRef}
            style={{ width: 180, height: 180, border: '1px solid var(--border)' }}
          />
          <div>
            <div className="row" style={{ flexWrap: 'wrap' }}>
              <label>
                {t('avatar.bg')}{' '}
                <input type="color" value={bg} onChange={(e) => setBg(e.target.value)} />
              </label>
              <label>
                {t('avatar.net')}{' '}
                <input type="color" value={net} onChange={(e) => setNet(e.target.value)} />
              </label>
              <button type="button" className="btn--sm" onClick={() => setSeed(randomFingerprint())}>
                {t('avatar.regenerate')}
              </button>
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
            <fieldset style={{ border: '1px solid var(--border)', borderRadius: 6, marginTop: '0.4rem' }}>
              <legend>{t('avatar.fields')}</legend>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.3rem 1rem' }}>
                {FIELDS.map((f) => (
                  <label key={f} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35rem' }}>
                    <input
                      type="checkbox"
                      checked={fields[f]}
                      onChange={(e) => setFields({ ...fields, [f]: e.target.checked })}
                    />
                    {t(`avatar.field.${f}`)}
                  </label>
                ))}
              </div>
            </fieldset>
            <p className={ok ? 'ok' : 'err'}>{ok ? t('avatar.scanOk') : t('avatar.scanNo')}</p>
            <button
              type="button"
              className="btn--sm"
              disabled={busy || !ok}
              onClick={() => generateQrLogo()}
            >
              {t('logo.useQr')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
