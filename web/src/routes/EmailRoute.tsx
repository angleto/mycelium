import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { TagPicker } from '../components/TagPicker'
import type { components } from '../api/schema'

type Account = components['schemas']['EmailAccountOut']
type Message = components['schemas']['EmailMessageOut']
type Provider = components['schemas']['EmailProvider']
type Tag = components['schemas']['TagOut']
type Draft = components['schemas']['EmailDraftOut']

const PROVIDERS: Provider[] = ['gmail', 'imap_generic', 'proton_bridge']

export function EmailRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [accounts, setAccounts] = useState<Account[]>([])
  const [messages, setMessages] = useState<Message[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [provider, setProvider] = useState<Provider>('imap_generic')
  const [address, setAddress] = useState('')
  const [secret, setSecret] = useState('')
  const [imapHost, setImapHost] = useState('')
  const [filter, setFilter] = useState('')
  const [replyTo, setReplyTo] = useState('')
  const [replyBody, setReplyBody] = useState('')
  const [draftBodies, setDraftBodies] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pendingAcct, setPendingAcct] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [a, m, g, d] = await Promise.all([
      api.GET('/email/accounts', { params: { header: h } }),
      api.GET('/email/messages', {
        params: { header: h, query: filter ? { account_id: filter } : {} },
      }),
      api.GET('/tags', { params: { header: h } }),
      api.GET('/email/drafts', { params: { header: h } }),
    ])
    if (a.data) setAccounts(a.data)
    if (m.data) setMessages(m.data)
    if (g.data) setTags(g.data)
    if (d.data) setDrafts(d.data)
  }, [filter])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [a, m, g, d] = await Promise.all([
        api.GET('/email/accounts', { params: { header: h } }),
        api.GET('/email/messages', {
          params: { header: h, query: filter ? { account_id: filter } : {} },
        }),
        api.GET('/tags', { params: { header: h } }),
        api.GET('/email/drafts', { params: { header: h } }),
      ])
      if (!active) return
      if (a.data) setAccounts(a.data)
      if (m.data) setMessages(m.data)
      if (g.data) setTags(g.data)
      if (d.data) setDrafts(d.data)
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

  // One PATCH helper for the per-account boolean toggles (ingest, auto-draft):
  // disabled while in flight so a rapid re-toggle can't reuse a stale version.
  async function onPatchAccount(a: Account, body: Record<string, unknown>) {
    setErr(null)
    setPendingAcct(a.id)
    try {
      const { error } = await api.PATCH('/email/accounts/{account_id}', {
        params: { header: workspaceHeader(), path: { account_id: a.id } },
        body: { expected_version: a.version, ...body },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
      await reload()
    } finally {
      setPendingAcct(null)
    }
  }

  async function onSetDefaultTags(a: Account, tagIds: string[]) {
    setErr(null)
    setPendingAcct(a.id)
    try {
      const { error } = await api.PUT('/email/accounts/{account_id}/default-tags', {
        params: { header: workspaceHeader(), path: { account_id: a.id } },
        body: { expected_version: a.version, tag_ids: tagIds },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
      await reload()
    } finally {
      setPendingAcct(null)
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

  async function onToNote(id: string) {
    setErr(null)
    const { error } = await api.POST('/email/messages/{message_id}/to-note', {
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

  async function onApproveDraft(d: Draft) {
    setErr(null)
    const edited = draftBodies[d.id]
    const { error } = await api.POST('/email/drafts/{job_id}/approve', {
      params: { header: workspaceHeader(), path: { job_id: d.id } },
      body: { body_text: edited && edited.trim() ? edited : null },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('email.drafts.sent'))
    await reload()
  }

  async function onRejectDraft(d: Draft) {
    setErr(null)
    const { error } = await api.POST('/email/drafts/{job_id}/reject', {
      params: { header: workspaceHeader(), path: { job_id: d.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  const subjectOf = (messageId: string) =>
    messages.find((m) => m.id === messageId)?.subject ?? messageId.slice(0, 8)

  return (
    <section className="card">
      <h1>{t('email.title')}</h1>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <form onSubmit={(e) => void onAddAccount(e)} className="row">
        <select value={provider} onChange={(e) => setProvider(e.target.value as Provider)}>
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
          <li key={a.id} className="email__account">
            <div>
              {a.email_address} <span className="muted">· {a.provider}</span>
              <button type="button" onClick={() => void onSync(a.id)}>
                {t('email.sync')}
              </button>
              <label className="email__ingest" title={t('email.ingestToMemoryHint')}>
                <input
                  type="checkbox"
                  checked={a.ingest_to_memory}
                  disabled={pendingAcct === a.id}
                  onChange={(e) => void onPatchAccount(a, { ingest_to_memory: e.target.checked })}
                />
                {t('email.ingestToMemory')}
              </label>
              <label className="email__ingest" title={t('email.autoDraftHint')}>
                <input
                  type="checkbox"
                  checked={a.auto_draft_replies}
                  disabled={pendingAcct === a.id}
                  onChange={(e) =>
                    void onPatchAccount(a, { auto_draft_replies: e.target.checked })
                  }
                />
                {t('email.autoDraft')}
              </label>
            </div>
            <div className="email__tags">
              <span className="muted">{t('email.defaultTags')}</span>
              <TagPicker
                selected={a.default_tags ?? []}
                all={tags}
                disabled={pendingAcct === a.id}
                onAdd={(tid) =>
                  void onSetDefaultTags(a, [
                    ...(a.default_tags ?? []).map((tg) => tg.id),
                    tid,
                  ])
                }
                onRemove={(tid) =>
                  void onSetDefaultTags(
                    a,
                    (a.default_tags ?? []).map((tg) => tg.id).filter((x) => x !== tid),
                  )
                }
              />
            </div>
          </li>
        ))}
      </ul>

      {drafts.length > 0 && (
        <>
          <h2>{t('email.drafts.title')}</h2>
          <ul className="list">
            {drafts.map((d) => (
              <li key={d.id}>
                <strong>{subjectOf(d.message_id)}</strong>
                {d.origin_model_id && (
                  <span className="muted"> · {d.origin_model_id}</span>
                )}
                <textarea
                  value={draftBodies[d.id] ?? d.draft_reply ?? ''}
                  onChange={(e) =>
                    setDraftBodies((prev) => ({ ...prev, [d.id]: e.target.value }))
                  }
                />
                <button type="button" onClick={() => void onApproveDraft(d)}>
                  {t('email.drafts.approve')}
                </button>
                <button type="button" onClick={() => void onRejectDraft(d)}>
                  {t('email.drafts.reject')}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

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
              <strong>{m.subject}</strong> <span className="muted">· {m.from_addr}</span>
              <div className="muted">{m.snippet}</div>
              {m.linked_task_id ? (
                <span className="ok">{t('email.linked')}</span>
              ) : (
                <button type="button" onClick={() => void onToTask(m.id)}>
                  {t('email.toTask')}
                </button>
              )}
              {m.linked_note_id ? (
                <span className="ok">{t('email.linkedNote')}</span>
              ) : (
                <button type="button" onClick={() => void onToNote(m.id)}>
                  {t('email.toNote')}
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
          <textarea required value={replyBody} onChange={(e) => setReplyBody(e.target.value)} />
          <button type="submit">{t('email.send')}</button>
        </form>
      )}
    </section>
  )
}
