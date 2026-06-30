import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'

type Channel = components['schemas']['MemoryChannelOut']

// Platform-admin only: memory channels are a controlled, seeded
// vocabulary so integrations (email, telegram, ...) have a stable
// target. Seeded channels can be renamed and disabled but their
// system key is immutable and they cannot be deleted (the server
// enforces this; the UI only mirrors it). Rendered behind the
// is_admin + admin-mode gate in Settings.
export function MemoryChannelsAdmin() {
  const { t } = useTranslation()
  const [list, setList] = useState<Channel[]>([])
  const [name, setName] = useState('')
  const [key, setKey] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/memory/channels', {
        params: { header: workspaceHeader() },
      })
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        return
      }
      setList(data ?? [])
    })()
    return () => {
      active = false
    }
  }, [tick])

  async function create() {
    if (!name.trim()) return
    setBusy(true)
    setErr(null)
    const { error } = await api.POST('/memory/channels', {
      params: { header: workspaceHeader() },
      body: { name: name.trim(), system_key: key.trim() || null },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    setName('')
    setKey('')
    setTick((n) => n + 1)
  }

  async function patch(
    c: Channel,
    body: { name?: string; enabled?: boolean },
  ) {
    setErr(null)
    const { error } = await api.PATCH('/memory/channels/{channel_id}', {
      params: { header: workspaceHeader(), path: { channel_id: c.id } },
      body,
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTick((n) => n + 1)
  }

  async function rename(c: Channel) {
    const next = window.prompt(t('mch.renamePrompt'), c.name)
    if (next && next.trim() && next.trim() !== c.name)
      await patch(c, { name: next.trim() })
  }

  async function remove(c: Channel) {
    if (!window.confirm(t('mch.confirmDelete'))) return
    setErr(null)
    const { error } = await api.DELETE('/memory/channels/{channel_id}', {
      params: { header: workspaceHeader(), path: { channel_id: c.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setTick((n) => n + 1)
  }

  return (
    <section className="card">
      <h2>{t('mch.title')}</h2>
      <p className="hint">{t('mch.intro')}</p>
      {err && <p className="err">{err}</p>}
      <ul className="list">
        {list.map((c) => (
          <li key={c.id} className="mch__row">
            <span className="grow">
              {c.name}
              {c.system_key && (
                <span className="muted"> · {c.system_key}</span>
              )}
              {c.seeded && (
                <span className="tag tag--muted"> {t('mch.seeded')}</span>
              )}
              {!c.enabled && (
                <span className="tag tag--muted"> {t('mch.disabled')}</span>
              )}
            </span>
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => void rename(c)}
            >
              {t('mch.rename')}
            </button>
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => void patch(c, { enabled: !c.enabled })}
            >
              {c.enabled ? t('mch.disable') : t('mch.enable')}
            </button>
            {!c.seeded && (
              <button
                type="button"
                className="btn--sm btn--danger"
                onClick={() => void remove(c)}
              >
                {t('mch.delete')}
              </button>
            )}
          </li>
        ))}
      </ul>
      <div className="row">
        <input
          placeholder={t('mch.name')}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          placeholder={t('mch.key')}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
        <button
          type="button"
          disabled={busy || !name.trim()}
          onClick={() => void create()}
        >
          {t('mch.add')}
        </button>
      </div>
    </section>
  )
}
