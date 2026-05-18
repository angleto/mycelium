import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { setSession } from '../auth/session'

// W0 uses signup (creates org + user, returns token + org_id), the
// end-to-end flow already verified against the live backend. Login for
// an existing account needs an "orgs I belong to" listing; that lands
// with W1.
export function LoginRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [orgName, setOrgName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error } = await api.POST('/auth/signup', {
      body: { email, password, org_name: orgName },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setSession({ token: data.token, orgId: data.org_id })
    navigate('/', { replace: true })
  }

  return (
    <form className="card" onSubmit={(e) => void onSubmit(e)}>
      <h1>{t('login.title')}</h1>
      <label>
        {t('login.email')}
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        {t('login.password')}
        <input
          type="password"
          required
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <label>
        {t('login.orgName')}
        <input
          required
          value={orgName}
          onChange={(e) => setOrgName(e.target.value)}
        />
      </label>
      {err && <p className="err">{err}</p>}
      <button type="submit" disabled={busy}>
        {busy ? t('login.submitting') : t('login.submit')}
      </button>
      <p className="hint">{t('login.hint')}</p>
    </form>
  )
}
