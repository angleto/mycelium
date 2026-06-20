import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Account = components['schemas']['EmailAccountOut']
type Message = components['schemas']['EmailMessageOut']
type Provider = components['schemas']['EmailProvider']

const PROVIDERS: Provider[] = ['gmail', 'imap_generic', 'proton_bridge']

export function EmailRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [accounts, setAccounts] = useState<Account[]>([])
  const [messages, setMessages] = useState<Message[]>([])
  const [provider, setProvider] = useState<Provider>('imap_generic')
  const [address, setAddress] = useState('')
  const [secret, setSecret] = useState('')
  const [imapHost, setImapHost] = useState('')
  const [filter, setFilter] = useState('')
  const [replyTo, setReplyTo] = useState('')
  const [replyBody, setReplyBody] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pendingIngest, setPendingIngest] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [a, m] = await Promise.all([
      api.GET('/email/accounts', { params: { header: h } }),
      api.GET('/email/messages', {
        params: { header: h, query: filter ? { account_id: filter } : {} },
      }),
    ])
    if (a.data) setAccounts(a.data)
    if (m.data) setMessages(m.data)
  }, [filter])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [a, m] = await Promise.all([
        api.GET('/email/accounts', { params: { header: h } }),
        api.GET('/email/messages', {
          params: { header: h, query: filter ? { account_id: filter } : {} },
        }),
      ])
      if (!active) return
      if (a.data) setAccounts(a.data)
      if (m.data) setMessages(m.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, filter])

  async function onAddAccount(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.POST('/email/accounts', {
      params: { header: workspaceHeader() },
      body: {
        provider,
        email_address: address,
        secret,
        imap_host: imapHost || null,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setAddress('')
    setSecret('')
    await reload()
  }

  async function onSync(id: string) {
    setErr(null)
    setMsg(null)
    const { data, error } = await api.POST('/email/accounts/{account_id}/sync', {
      params: { header: workspaceHeader(), path: { account_id: id } },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('email.synced', { fetched: data.fetched, created: data.created }))
    await reload()
  }

  async function onToggleIngest(a: Account, enabled: boolean) {
    setErr(null)
    // Guard against a rapid re-toggle reusing the now-stale a.version (which
    // would 409 on optimistic concurrency): the checkbox is disabled while
    // its PATCH is in flight, cleared in finally.
    setPendingIngest(a.id)
    try {
      const { error } = await api.PATCH('/email/accounts/{account_id}', {
        params: { header: workspaceHeader(), path: { account_id: a.id } },
        body: { expected_version: a.version, ingest_to_memory: enabled },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
      await reload()
    } finally {
      setPendingIngest(null)
    }
  }

  async function onToTask(id: string) {
    setErr(null)
    const { error } = await api.POST('/email/messages/{message_id}/to-task', {
      params: { header: workspaceHeader(), path: { message_id: id } },
      body: {},
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onReply(e: FormEvent) {
    e.preventDefault()
    if (!replyTo) return
    setErr(null)
    const { error } = await api.POST('/email/messages/{message_id}/reply', {
      params: { header: workspaceHeader(), path: { message_id: replyTo } },
      body: { body_text: replyBody },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setReplyTo('')
    setReplyBody('')
    setMsg(t('email.send'))
  }

  return (
    <section className="card">
      <h1>{t('email.title')}</h1>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <form onSubmit={(e) => void onAddAccount(e)} className="row">
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value as Provider)}
        >
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <input
          type="email"
          required
          placeholder={t('email.address')}
          value={address}
          onChange={(e) => setAddress(e.target.value)}
        />
        <input
          required
          placeholder={t('email.secret')}
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
        />
        <input
          placeholder={t('email.imapHost')}
          value={imapHost}
          onChange={(e) => setImapHost(e.target.value)}
        />
        <button type="submit">{t('email.addAccount')}</button>
      </form>

      <h2>{t('email.accounts')}</h2>
      <ul className="list">
        {accounts.map((a) => (
          <li key={a.id}>
            {a.email_address} <span className="muted">· {a.provider}</span>
            <button type="button" onClick={() => void onSync(a.id)}>
              {t('email.sync')}
            </button>
            <label className="email__ingest" title={t('email.ingestToMemoryHint')}>
              <input
                type="checkbox"
                checked={a.ingest_to_memory}
                disabled={pendingIngest === a.id}
                onChange={(e) => void onToggleIngest(a, e.target.checked)}
              />
              {t('email.ingestToMemory')}
            </label>
          </li>
        ))}
      </ul>

      <h2>{t('email.messages')}</h2>
      <label>
        {t('email.filterAcct')}{' '}
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">{t('email.all')}</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.email_address}
            </option>
          ))}
        </select>
      </label>
      {messages.length === 0 ? (
        <p className="hint">{t('email.none')}</p>
      ) : (
        <ul className="list">
          {messages.map((m) => (
            <li key={m.id}>
              <strong>{m.subject}</strong>{' '}
              <span className="muted">· {m.from_addr}</span>
              <div className="muted">{m.snippet}</div>
              {m.linked_task_id ? (
                <span className="ok">{t('email.linked')}</span>
              ) : (
                <button type="button" onClick={() => void onToTask(m.id)}>
                  {t('email.toTask')}
                </button>
              )}
              <button type="button" onClick={() => setReplyTo(m.id)}>
                {t('email.reply')}
              </button>
            </li>
          ))}
        </ul>
      )}

      {replyTo && (
        <form onSubmit={(e) => void onReply(e)}>
          <h2>{t('email.reply')}</h2>
          <textarea
            required
            value={replyBody}
            onChange={(e) => setReplyBody(e.target.value)}
          />
          <button type="submit">{t('email.send')}</button>
        </form>
      )}
    </section>
  )
}
