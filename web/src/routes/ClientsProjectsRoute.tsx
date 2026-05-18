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
  'description',
]

// Manage clients and projects (tags + their satellite profiles).
// Clients carry the invoicing card + the billable default (billing is
// a client relationship); projects carry rate/currency/budget, an
// optional colour and a description (AI context) + the client link.
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
  const [defClient, setDefClient] = useState<string>('')
  const [editC, setEditC] = useState<string | null>(null)
  const [editP, setEditP] = useState<string | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    const h = workspaceHeader()
    const [c, p, ws] = await Promise.all([
      api.GET('/clients', { params: { header: h } }),
      api.GET('/projects', { params: { header: h } }),
      api.GET('/workspaces/me', { params: { header: h } }),
    ])
    if (c.data) setClients(c.data)
    if (p.data) setProjects(p.data)
    const dft = ws.data?.settings?.default_client_tag_id ?? ''
    setDefClient(dft)
    setPClient((v) => v || dft)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [c, p, ws] = await Promise.all([
        api.GET('/clients', { params: { header: h } }),
        api.GET('/projects', { params: { header: h } }),
        api.GET('/workspaces/me', { params: { header: h } }),
      ])
      if (!active) return
      if (c.data) setClients(c.data)
      if (p.data) setProjects(p.data)
      const dft = ws.data?.settings?.default_client_tag_id ?? ''
      setDefClient(dft)
      setPClient((v) => v || dft)
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
      body: { name: cName, ragione_sociale: cRag, default_billable: true },
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

  // Clients and projects are tags: archive = tag status (optimistic).
  async function setArchive(
    id: string,
    version: number,
    archived: boolean,
  ) {
    setErr(null)
    setMsg(null)
    const { error, response } = await api.PATCH('/tags/{tag_id}', {
      params: { header: workspaceHeader(), path: { tag_id: id } },
      body: {
        expected_version: version,
        status: archived ? 'archived' : 'active',
      },
    })
    if (response.status === 409) {
      setErr(t('cp.conflict'))
      await load()
      return
    }
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  const clientName = (id: string | null) =>
    clients.find((c) => c.id === id)?.name ?? '-'
  const visClients = clients.filter(
    (c) => showArchived || c.status !== 'archived',
  )
  const visProjects = projects.filter(
    (p) => showArchived || p.status !== 'archived',
  )

  return (
    <section className="card">
      <h1>{t('cp.title')}</h1>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <label className="row">
        <input
          type="checkbox"
          checked={showArchived}
          onChange={(e) => setShowArchived(e.target.checked)}
        />
        {t('cp.showArchived')}
      </label>

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
        {visClients.map((c) => (
          <li key={c.id}>
            <strong>{c.name}</strong>{' '}
            <span className="muted">
              · {c.ragione_sociale} ·{' '}
              {c.default_billable ? t('cp.billable') : t('cp.nonBillable')}
              {c.status === 'archived' ? ` · ${t('cp.archived')}` : ''}
            </span>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => setEditC(editC === c.id ? null : c.id)}
            >
              {t('cp.edit')}
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() =>
                void setArchive(c.id, c.version, c.status !== 'archived')
              }
            >
              {c.status === 'archived' ? t('cp.unarchive') : t('cp.archive')}
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
                    default_billable: fd.get('default_billable') === 'on',
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
                    defaultValue={(c[f] as string | null) ?? ''}
                    placeholder={t(`cp.f.${f}`)}
                  />
                ))}
                <label>
                  <input
                    type="checkbox"
                    name="default_billable"
                    defaultChecked={c.default_billable}
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

      <h2>{t('cp.projects')}</h2>
      <div className="row">
        <input
          placeholder={t('cp.name')}
          value={pName}
          onChange={(e) => setPName(e.target.value)}
        />
        <select value={pClient} onChange={(e) => setPClient(e.target.value)}>
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
        {visProjects.map((p) => (
          <li key={p.id}>
            <strong>{p.name}</strong>{' '}
            {p.color && (
              <span
                className="swatch"
                style={{ background: p.color }}
                title={p.color}
              />
            )}{' '}
            <span className="muted">
              · {clientName(p.client_tag_id)} ·{' '}
              {p.tariffa ? `${p.tariffa} ${p.valuta}` : t('cp.noRate')}
              {p.status === 'archived' ? ` · ${t('cp.archived')}` : ''}
            </span>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => setEditP(editP === p.id ? null : p.id)}
            >
              {t('cp.edit')}
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() =>
                void setArchive(p.id, p.version, p.status !== 'archived')
              }
            >
              {p.status === 'archived' ? t('cp.unarchive') : t('cp.archive')}
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
                    color: (fd.get('color') as string) || null,
                    description: (fd.get('description') as string) || null,
                  })
                }}
              >
                <input name="name" defaultValue={p.name} placeholder={t('cp.name')} />
                <select
                  name="client_tag_id"
                  defaultValue={p.client_tag_id ?? defClient}
                >
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
                <input
                  name="color"
                  type="color"
                  defaultValue={p.color ?? '#888888'}
                  title={t('cp.color')}
                  style={{ width: '3rem', padding: 0 }}
                />
                <input
                  name="description"
                  defaultValue={p.description ?? ''}
                  placeholder={t('cp.description')}
                />
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
