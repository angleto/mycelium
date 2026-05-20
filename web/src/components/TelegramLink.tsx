import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch } from '../api/client'

// /telegram/link* endpoints are not yet in schema.d.ts; we use
// authFetch with the contract shapes. schema regen pending.

type LinkStatus = {
  linked: boolean
  chat_username: string | null
  linked_at: string | null
}

type LinkRequest = {
  code: string
  expires_at: string
  bot_username: string
  deep_link: string
}

export function TelegramLink() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<LinkStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [pending, setPending] = useState<LinkRequest | null>(null)
  const [copied, setCopied] = useState(false)
  const [tick, setTick] = useState(0)
  const pollRef = useRef<number | null>(null)

  const fetchStatus = useCallback(async (): Promise<LinkStatus | null> => {
    const res = await authFetch('/telegram/link/status')
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return null
    }
    return (await res.json()) as LinkStatus
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const s = await fetchStatus()
      if (!active) return
      if (s) setStatus(s)
      setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [fetchStatus, tick])

  // While a link code is outstanding, poll the status every 3s. The
  // poll stops once linked or when pending is cleared.
  useEffect(() => {
    if (!pending) return
    pollRef.current = window.setInterval(() => {
      void (async () => {
        const s = await fetchStatus()
        if (s?.linked) {
          setStatus(s)
          setPending(null)
          if (pollRef.current !== null) {
            window.clearInterval(pollRef.current)
            pollRef.current = null
          }
        }
      })()
    }, 3000)
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [pending, fetchStatus])

  function reload() {
    setLoading(true)
    setErr(null)
    setTick((n) => n + 1)
  }

  async function onLink() {
    setErr(null)
    const res = await authFetch('/telegram/link/request', { method: 'POST' })
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return
    }
    setPending((await res.json()) as LinkRequest)
  }

  async function onUnlink() {
    setErr(null)
    const res = await authFetch('/telegram/link', { method: 'DELETE' })
    if (!res.ok) {
      setErr(`HTTP ${res.status}`)
      return
    }
    setPending(null)
    reload()
  }

  async function onCopyCode() {
    if (!pending) return
    try {
      await navigator.clipboard.writeText(pending.code)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard may be blocked */
    }
  }

  return (
    <section className="card">
      <h2>{t('connectors.telegram.title')}</h2>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}

      {!loading && status?.linked && (
        <>
          <dl className="kv">
            {status.chat_username && (
              <>
                <dt>@</dt>
                <dd>
                  <code>{status.chat_username}</code>
                </dd>
              </>
            )}
            {status.linked_at && (
              <>
                <dt>{t('connectors.telegram.linkedAt')}</dt>
                <dd>{new Date(status.linked_at).toLocaleString()}</dd>
              </>
            )}
          </dl>
          <button type="button" onClick={() => void onUnlink()}>
            {t('connectors.telegram.unlink')}
          </button>
        </>
      )}

      {!loading && !status?.linked && !pending && (
        <button type="button" onClick={() => void onLink()}>
          {t('connectors.telegram.link')}
        </button>
      )}

      {!loading && !status?.linked && pending && (
        <div>
          <p>
            <a
              href={pending.deep_link}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t('connectors.telegram.deepLinkLabel')}
            </a>{' '}
            (@{pending.bot_username})
          </p>
          <dl className="kv">
            <dt>{t('connectors.telegram.codeLabel')}</dt>
            <dd>
              <code>{pending.code}</code>{' '}
              <button
                type="button"
                onClick={() => void onCopyCode()}
                style={{ fontSize: '0.85rem' }}
              >
                {copied ? 'OK' : t('connectors.mcp.copyToken')}
              </button>
            </dd>
            <dt>{t('connectors.telegram.expiresAt')}</dt>
            <dd>{new Date(pending.expires_at).toLocaleString()}</dd>
          </dl>
          <p>{t('connectors.telegram.waiting')}</p>
        </div>
      )}
    </section>
  )
}
