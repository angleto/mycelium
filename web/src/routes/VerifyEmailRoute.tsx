import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, establishSession } from '../api/client'

// Consumes ?token= from the verification link, then logs the user in.
// State is set only after the await (async continuation), never
// synchronously in the effect body (react-hooks rule).
export function VerifyEmailRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let active = true
    const token = params.get('token') ?? ''
    void (async () => {
      const { data, error, response } = await api.POST('/auth/verify-email', {
        body: { token },
      })
      if (!active) return
      if (response.ok && data) {
        await establishSession(data.token, data.refresh_token ?? undefined)
        if (active) navigate('/', { replace: true })
        return
      }
      if (error || !response.ok) setFailed(true)
    })()
    return () => {
      active = false
    }
  }, [params, navigate])

  return (
    <section className="card">
      <h1>{t('verify.title')}</h1>
      {failed ? (
        <>
          <p className="err">{t('verify.failed')}</p>
          <p className="hint">
            <Link to="/login">{t('auth.toLogin')}</Link>
          </p>
        </>
      ) : (
        <p>{t('verify.working')}</p>
      )}
    </section>
  )
}
