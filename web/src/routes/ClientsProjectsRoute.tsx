import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Client = components['schemas']['ClientOut']
type Project = components['schemas']['ProjectOut']

const CLIENT_FIELDS: Array<keyof Client> = [
  'ragione_sociale',
  'codice_fiscale',
  'id_paese',
  'id_codice',
  'indirizzo',
  'cap',
  'comune',
  'provincia',
  'nazione',
  'codice_destinatario',
  'pec',
]

// Manage clients and projects (tags + their satellite profiles).
// Clients carry the invoicing card; projects carry the automatic
// properties (rate/currency/budget/default billable/client link)
// that tasks inherit.
export function ClientsProjectsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [clients, setClients] = useState<Client[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [cName, setCName] = useState('')
  const [cRag, setCRag] = useState('')
  const [pName, setPName] = useState('')
  const [pClient, setPClient] = useState('')
  const [editC, setEditC] = useState<string | null>(null)
  const [editP, setEditP] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    const h = workspaceHeader()
    const [c, p] = await Promise.all([
      api.GET('/clients', { params: { header: h } }),
      api.GET('/projects', { params: { header: h } }),
    ])
    if (c.data) setClients(c.data)
    if (p.data) setProjects(p.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [c, p] = await Promise.all([
        api.GET('/clients', { params: { header: h } }),
        api.GET('/projects', { params: { header: h } }),
      ])
      if (!active) return
      if (c.data) setClients(c.data)
      if (p.data) setProjects(p.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function addClient() {
    if (!cName || !cRag) return
    setErr(null)
    const { error } = await api.POST('/clients', {
      params: { header: workspaceHeader() },
      body: { name: cName, ragione_sociale: cRag },
    })
    if (error) return setErr(errMessage(error))
    setCName('')
    setCRag('')
    await load()
  }

  async function addProject() {
    if (!pName) return
    setErr(null)
    const { error } = await api.POST('/projects', {
      params: { header: workspaceHeader() },
      body: {
        name: pName,
        client_tag_id: pClient || null,
        valuta: 'EUR',
        default_billable: true,
      },
    })
    if (error) return setErr(errMessage(error))
    setPName('')
    setPClient('')
    await load()
  }

  async function saveClient(c: Client, patch: Record<string, unknown>) {
    setErr(null)
    setMsg(null)
    const { error } = await api.PATCH('/clients/{tag_id}', {
      params: { header: workspaceHeader(), path: { tag_id: c.id } },
      body: patch,
    })
    if (error) return setErr(errMessage(error))
    setMsg(t('cp.saved'))
    setEditC(null)
    await load()
  }

  async function saveProject(p: Project, patch: Record<string, unknown>) {
    setErr(null)
    setMsg(null)
    const { error } = await api.PATCH('/projects/{tag_id}', {
      params: { header: workspaceHeader(), path: { tag_id: p.id } },
      body: patch,
    })
    if (error) return setErr(errMessage(error))
    setMsg(t('cp.saved'))
    setEditP(null)
    await load()
  }

  const clientName = (id: string | null) =>
    clients.find((c) => c.id === id)?.name ?? '-'

  return (
    <section className="card">
      <h1>{t('cp.title')}</h1>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <h2>{t('cp.clients')}</h2>
      <div className="row">
        <input
          placeholder={t('cp.name')}
          value={cName}
          onChange={(e) => setCName(e.target.value)}
        />
        <input
          placeholder={t('cp.ragioneSociale')}
          value={cRag}
          onChange={(e) => setCRag(e.target.value)}
        />
        <button type="button" className="btn--sm" onClick={() => void addClient()}>
          {t('cp.add')}
        </button>
      </div>
      <ul className="list">
        {clients.map((c) => (
          <li key={c.id}>
            <strong>{c.name}</strong>{' '}
            <span className="muted">· {c.ragione_sociale}</span>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => setEditC(editC === c.id ? null : c.id)}
            >
              {t('cp.edit')}
            </button>
            {editC === c.id && (
              <form
                className="row"
                style={{ flexWrap: 'wrap', marginTop: '0.4rem' }}
                onSubmit={(e) => {
                  e.preventDefault()
                  const fd = new FormData(e.currentTarget)
                  const patch: Record<string, unknown> = {
                    name: fd.get('name'),
                  }
                  for (const f of CLIENT_FIELDS)
                    patch[f] = (fd.get(f) as string) || null
                  void saveClient(c, patch)
                }}
              >
                <input name="name" defaultValue={c.name} placeholder={t('cp.name')} />
                {CLIENT_FIELDS.map((f) => (
                  <input
                    key={f}
                    name={f}
                    defaultValue={c[f] ?? ''}
                    placeholder={t(`cp.f.${f}`)}
                  />
                ))}
                <button type="submit" className="btn--sm">
                  {t('cp.save')}
                </button>
              </form>
            )}
          </li>
        ))}
      </ul>

      <h2>{t('cp.projects')}</h2>
      <div className="row">
        <input
          placeholder={t('cp.name')}
          value={pName}
          onChange={(e) => setPName(e.target.value)}
        />
        <select value={pClient} onChange={(e) => setPClient(e.target.value)}>
          <option value="">{t('cp.noClient')}</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn--sm" onClick={() => void addProject()}>
          {t('cp.add')}
        </button>
      </div>
      <ul className="list">
        {projects.map((p) => (
          <li key={p.id}>
            <strong>{p.name}</strong>{' '}
            <span className="muted">
              · {clientName(p.client_tag_id)} ·{' '}
              {p.tariffa ? `${p.tariffa} ${p.valuta}` : t('cp.noRate')} ·{' '}
              {p.default_billable ? t('cp.billable') : t('cp.nonBillable')}
            </span>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => setEditP(editP === p.id ? null : p.id)}
            >
              {t('cp.edit')}
            </button>
            {editP === p.id && (
              <form
                className="row"
                style={{ flexWrap: 'wrap', marginTop: '0.4rem' }}
                onSubmit={(e) => {
                  e.preventDefault()
                  const fd = new FormData(e.currentTarget)
                  void saveProject(p, {
                    name: fd.get('name'),
                    client_tag_id: (fd.get('client_tag_id') as string) || null,
                    tariffa: (fd.get('tariffa') as string) || null,
                    valuta: (fd.get('valuta') as string) || 'EUR',
                    budget: (fd.get('budget') as string) || null,
                    default_billable: fd.get('default_billable') === 'on',
                  })
                }}
              >
                <input name="name" defaultValue={p.name} placeholder={t('cp.name')} />
                <select name="client_tag_id" defaultValue={p.client_tag_id ?? ''}>
                  <option value="">{t('cp.noClient')}</option>
                  {clients.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <input
                  name="tariffa"
                  type="number"
                  step="0.01"
                  defaultValue={p.tariffa ?? ''}
                  placeholder={t('cp.rate')}
                />
                <input
                  name="valuta"
                  defaultValue={p.valuta}
                  placeholder={t('cp.currency')}
                  style={{ width: '4rem' }}
                />
                <input
                  name="budget"
                  type="number"
                  step="0.01"
                  defaultValue={p.budget ?? ''}
                  placeholder={t('cp.budget')}
                />
                <label>
                  <input
                    type="checkbox"
                    name="default_billable"
                    defaultChecked={p.default_billable}
                  />{' '}
                  {t('cp.defaultBillable')}
                </label>
                <button type="submit" className="btn--sm">
                  {t('cp.save')}
                </button>
              </form>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
