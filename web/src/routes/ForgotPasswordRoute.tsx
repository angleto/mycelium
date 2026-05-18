import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'

// Enumeration-safe: the response is the same whether or not the
// address exists, so the UI always shows the neutral "done" message.
export function ForgotPasswordRoute() {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    await api.POST('/auth/forgot-password', { body: { email } })
    setBusy(false)
    setDone(true)
  }

  return (
    <form className="card" onSubmit={(e) => void onSubmit(e)}>
      <h1>{t('forgot.title')}</h1>
      {done ? (
        <p className="ok">{t('forgot.done')}</p>
      ) : (
        <>
          <label>
            {t('auth.email')}
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? t('auth.working') : t('forgot.submit')}
          </button>
        </>
      )}
      <p className="hint">
        <Link to="/login">{t('auth.toLogin')}</Link>
      </p>
    </form>
  )
}
