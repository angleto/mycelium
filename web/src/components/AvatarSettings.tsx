import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'
import { randomFingerprint, renderMyceliumOnly } from '../lib/myceliumQr'

// The user's avatar: a pure mycelial network (decorative, not a QR), unique per
// fingerprint. Colours are user-chosen; "regenerate" reseeds -> a different
// organism. The seed + colours are stored so an issuer profile can reuse the
// same avatar as its logo (optionally combined with a scannable QR there).
export function AvatarSettings() {
  const { t } = useTranslation()
  const [bg, setBg] = useState('#4a6b3e')
  const [net, setNet] = useState('#ffffff')
  const [seed, setSeed] = useState(randomFingerprint())
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    renderMyceliumOnly(canvas, { seed, bg, net })
  }, [seed, bg, net])

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
        <div className="row" style={{ flexWrap: 'wrap' }}>
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
      </div>
      <div className="row">
        <button type="button" disabled={busy} onClick={() => void saveAvatar()}>
          {t('avatar.save')}
        </button>
      </div>
    </section>
  )
}
