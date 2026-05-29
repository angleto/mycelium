import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { useMe } from '../auth/useMe'

// Per-user IANA timezone preference. Reminder labels (email/Telegram)
// are rendered in this zone server-side; the date-only "no time set"
// sentinel is also detected in it. Defaults to the browser-detected
// zone so a user who never opens this never sees UTC-skewed times.
export function TimezoneSettings() {
  const { t } = useTranslation()
  const { me } = useMe()
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone
  const [draft, setDraft] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  // local edit > stored preference > browser-detected zone.
  const value = draft ?? me?.timezone ?? detected

  async function onSave(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    setSaved(false)
    const { error, response } = await api.PATCH('/auth/me', {
      body: { timezone: value },
    })
    if (!response.ok) {
      setErr(errMessage(error))
      return
    }
    setSaved(true)
  }

  return (
    <section className="card">
      <h2>{t('settings.timezone.title')}</h2>
      <p className="muted">{t('settings.timezone.help')}</p>
      <form onSubmit={(e) => void onSave(e)}>
        <label>
          {t('settings.timezone.label')}
          <input
            value={value}
            placeholder={detected}
            onChange={(e) => {
              setDraft(e.target.value)
              setSaved(false)
            }}
          />
        </label>
        <button
          type="button"
          className="link"
          onClick={() => {
            setDraft(detected)
            setSaved(false)
          }}
        >
          {t('settings.timezone.useDetected', { tz: detected })}
        </button>
        <button type="submit">{t('settings.timezone.save')}</button>
        {saved && <p className="ok">{t('settings.timezone.saved')}</p>}
        {err && <p className="err">{err}</p>}
      </form>
    </section>
  )
}
