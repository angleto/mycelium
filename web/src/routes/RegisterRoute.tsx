import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, establishSession } from '../api/client'

// Personal-first signup (ADR-0024): no workspace name. A personal
// workspace is auto-provisioned. When email verification is required
// the response carries no token: show "check your email".
export function RegisterRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [checkEmail, setCheckEmail] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/auth/signup', {
      body: {
        email,
        password,
        display_name: displayName || null,
      },
    })
    if (error || !data) {
      setBusy(false)
      setErr(errMessage(error))
      return
    }
    if (data.token) {
      await establishSession(data.token)
      navigate('/', { replace: true })
      return
    }
    setBusy(false)
    setCheckEmail(true)
  }

  if (checkEmail) {
    return (
      <section className="card">
        <h1>{t('register.title')}</h1>
        <p className="ok">{t('register.checkEmail')}</p>
        <p className="hint">
          <Link to="/login">{t('auth.toLogin')}</Link>
        </p>
      </section>
    )
  }

  return (
    <form className="card" onSubmit={(e) => void onSubmit(e)}>
      <h1>{t('register.title')}</h1>
      <label>
        {t('auth.email')}
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        {t('auth.password')}
        <input
          type="password"
          required
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <label>
        {t('auth.displayName')}
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
      </label>
      {err && <p className="err">{err}</p>}
      <button type="submit" disabled={busy}>
        {busy ? t('auth.working') : t('auth.signUp')}
      </button>
      <p className="hint">
        {t('register.hint')} <Link to="/login">{t('auth.toLogin')}</Link>
      </p>
    </form>
  )
}
