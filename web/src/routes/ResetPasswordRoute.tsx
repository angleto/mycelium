import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'

export function ResetPasswordRoute() {
  const { t } = useTranslation()
  const [params] = useSearchParams()
  const [newPassword, setNewPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { error, response } = await api.POST('/auth/reset-password', {
      body: { token: params.get('token') ?? '', new_password: newPassword },
    })
    setBusy(false)
    if (response.ok) {
      setDone(true)
      return
    }
    setErr(errMessage(error))
  }

  return (
    <form className="card" onSubmit={(e) => void onSubmit(e)}>
      <h1>{t('reset.title')}</h1>
      {done ? (
        <p className="ok">{t('reset.done')}</p>
      ) : (
        <>
          <label>
            {t('reset.newPassword')}
            <input
              type="password"
              required
              minLength={8}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </label>
          {err && <p className="err">{err}</p>}
          <button type="submit" disabled={busy}>
            {busy ? t('auth.working') : t('reset.submit')}
          </button>
        </>
      )}
      <p className="hint">
        <Link to="/login">{t('auth.toLogin')}</Link>
      </p>
    </form>
  )
}
