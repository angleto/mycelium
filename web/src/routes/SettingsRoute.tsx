import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { EstimatePresets } from '../components/EstimatePresets'
import { IssuerProfiles } from '../components/IssuerProfiles'
import { WorkspaceManager } from '../components/WorkspaceManager'
import type { components } from '../api/schema'

type Status = components['schemas']['MfaStatusOut']
type Setup = components['schemas']['MfaSetupOut']

export function SettingsRoute() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<Status | null>(null)
  const [setup, setSetup] = useState<Setup | null>(null)
  const [totp, setTotp] = useState('')
  const [disableCode, setDisableCode] = useState('')
  const [backup, setBackup] = useState<string[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const loadStatus = useCallback(async () => {
    const { data, error } = await api.GET('/mfa/status')
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setStatus(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/mfa/status')
      if (active && data) setStatus(data)
    })()
    return () => {
      active = false
    }
  }, [])

  async function onSetup() {
    setErr(null)
    const { data, error } = await api.POST('/mfa/setup')
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setSetup(data)
  }

  async function onActivate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { data, error } = await api.POST('/mfa/activate', {
      body: { totp_code: totp },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setBackup(data.backup_codes)
    setSetup(null)
    setTotp('')
    await loadStatus()
  }

  async function onDisable(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error, response } = await api.POST('/mfa/disable', {
      body: { code: disableCode },
    })
    if (!response.ok) {
      setErr(errMessage(error))
      return
    }
    setDisableCode('')
    setBackup(null)
    await loadStatus()
  }

  return (
    <>
    <section className="card">
      <h1>{t('mfa.title')}</h1>
      {status === null ? (
        <p>{t('home.loading')}</p>
      ) : (
        <p>
          {t('app.title')}: <strong>{status.enabled ? t('mfa.enabled') : t('mfa.disabled')}</strong>
        </p>
      )}
      {err && <p className="err">{err}</p>}

      {backup && (
        <div>
          <h2>{t('mfa.backupTitle')}</h2>
          <ul className="codes">
            {backup.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {status && !status.enabled && !setup && (
        <button type="button" onClick={() => void onSetup()}>
          {t('mfa.setup')}
        </button>
      )}

      {setup && (
        <form onSubmit={(e) => void onActivate(e)}>
          <p>{t('mfa.scan')}</p>
          <img
            alt="MFA QR"
            src={`data:image/png;base64,${setup.qr_png_base64}`}
            width={180}
            height={180}
          />
          <p className="kv">
            <code>{setup.secret}</code>
          </p>
          <label>
            {t('mfa.enterCode')}
            <input
              required
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
              autoComplete="one-time-code"
            />
          </label>
          <button type="submit">{t('mfa.activate')}</button>
        </form>
      )}

      {status?.enabled && (
        <form onSubmit={(e) => void onDisable(e)}>
          <h2>{t('mfa.disableTitle')}</h2>
          <label>
            {t('mfa.disableCode')}
            <input
              required
              value={disableCode}
              onChange={(e) => setDisableCode(e.target.value)}
            />
          </label>
          <button type="submit">{t('mfa.disable')}</button>
        </form>
      )}
    </section>
    <WorkspaceManager />
    <IssuerProfiles />
    <EstimatePresets />
    </>
  )
}
