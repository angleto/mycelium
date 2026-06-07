import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { useMe } from '../auth/useMe'

// Per-user "day start": the local time at which a date-only task's
// reminders fire. A deadline with no time is stored at end-of-day, but
// firing the reminder there reads as a day late; the scanner instead
// anchors it to this time (minutes after midnight, in the user's
// configured timezone). 0 = midnight (default); 06:00 = 360.
function minToHHMM(min: number): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(Math.floor(min / 60))}:${pad(min % 60)}`
}

function hhmmToMin(v: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(v)
  if (!m) return null
  const min = Number(m[1]) * 60 + Number(m[2])
  return min >= 0 && min <= 1439 ? min : null
}

export function DayStartSettings() {
  const { t } = useTranslation()
  const { me } = useMe()
  const stored = me?.day_start_minute ?? 0
  const [draft, setDraft] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const value = draft ?? minToHHMM(stored)

  async function onSave(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    setSaved(false)
    const min = hhmmToMin(value)
    if (min === null) {
      setErr(t('settings.dayStart.invalid'))
      return
    }
    const { error, response } = await api.PATCH('/auth/me', {
      body: { day_start_minute: min },
    })
    if (!response.ok) {
      setErr(errMessage(error))
      return
    }
    setSaved(true)
  }

  return (
    <section className="card">
      <h2>{t('settings.dayStart.title')}</h2>
      <p className="muted">{t('settings.dayStart.help')}</p>
      <form onSubmit={(e) => void onSave(e)}>
        <label>
          {t('settings.dayStart.label')}
          <input
            type="time"
            value={value}
            onChange={(e) => {
              setDraft(e.target.value)
              setSaved(false)
            }}
          />
        </label>
        <button type="submit">{t('settings.dayStart.save')}</button>
        {saved && <p className="ok">{t('settings.dayStart.saved')}</p>}
        {err && <p className="err">{err}</p>}
      </form>
    </section>
  )
}
