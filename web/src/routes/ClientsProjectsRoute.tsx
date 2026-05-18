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
  // Projects are nested under their client, collapsed by default.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
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
      body: {
        name: cName,
        ragione_sociale: cRag,
        default_billable: true,
        valuta: 'EUR',
      },
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
      body: { name: pName, client_tag_id: pClient || null },
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

  const visClients = clients.filter(
    (c) => showArchived || c.status !== 'archived',
  )
  const visProjects = projects.filter(
    (p) => showArchived || p.status !== 'archived',
  )
  const projectsOf = (clientId: string) =>
    visProjects.filter((p) => p.client_tag_id === clientId)
  function toggleClient(id: string) {
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  }
  const expandAll = () => setExpanded(new Set(visClients.map((c) => c.id)))
  const collapseAll = () => setExpanded(new Set())

  // One project row, reused inside each client's nested list (no dup).
  const renderProject = (p: Project) => (
    <li key={p.id}>
      <div className="cprow">
        {p.color && (
          <span
            className="swatch"
            style={{ background: p.color }}
            title={p.color}
          />
        )}
        <strong>{p.name}</strong>
        <span className="muted">
          {p.budget ? `· ${t('cp.budget')} ${p.budget}` : ''}
          {p.status === 'archived' ? ` · ${t('cp.archived')}` : ''}
        </span>
        <span className="cprow__sp" />
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
      </div>
      {editP === p.id && (
        <form
          className="cpform"
          onSubmit={(e) => {
            e.preventDefault()
            const fd = new FormData(e.currentTarget)
            void saveProject(p, {
              name: fd.get('name'),
              client_tag_id: (fd.get('client_tag_id') as string) || null,
              budget: (fd.get('budget') as string) || null,
              color: (fd.get('color') as string) || null,
              description: (fd.get('description') as string) || null,
            })
          }}
        >
          <label>
            {t('cp.name')}
            <input name="name" defaultValue={p.name} />
          </label>
          <label>
            {t('cp.clientLabel')}
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
          </label>
          <label>
            {t('cp.budget')}
            <input
              name="budget"
              type="number"
              step="0.01"
              defaultValue={p.budget ?? ''}
            />
          </label>
          <label>
            {t('cp.color')}
            <input
              name="color"
              type="color"
              defaultValue={p.color ?? '#888888'}
            />
          </label>
          <label className="cpform__wide">
            {t('cp.description')}
            <input name="description" defaultValue={p.description ?? ''} />
          </label>
          <div className="cpform__actions">
            <button type="submit" className="btn--sm">
              {t('cp.save')}
            </button>
            <span className="muted">{t('cp.rateOnClient')}</span>
          </div>
        </form>
      )}
    </li>
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
      <div className="row">
        <button type="button" className="btn--ghost btn--sm" onClick={expandAll}>
          {t('cp.expandAll')}
        </button>
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={collapseAll}
        >
          {t('cp.collapseAll')}
        </button>
      </div>
      <ul className="list">
        {visClients.map((c) => {
          const open = expanded.has(c.id)
          const projs = projectsOf(c.id)
          return (
          <li key={c.id}>
            <div className="cprow">
              <button
                type="button"
                className="btn--ghost btn--sm cprow__toggle"
                aria-expanded={open}
                onClick={() => toggleClient(c.id)}
              >
                {open ? '▾' : '▸'}
              </button>
              <strong>{c.name}</strong>
              <span className="muted">
                · {c.ragione_sociale} ·{' '}
                {c.tariffa ? `${c.tariffa} ${c.valuta}/h` : t('cp.noRate')} ·{' '}
                {c.default_billable ? t('cp.billable') : t('cp.nonBillable')} ·{' '}
                {t('cp.projectsN', { n: projs.length })}
                {c.status === 'archived' ? ` · ${t('cp.archived')}` : ''}
              </span>
              <span className="cprow__sp" />
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
            </div>
            {editC === c.id && (
              <form
                className="cpform"
                onSubmit={(e) => {
                  e.preventDefault()
                  const fd = new FormData(e.currentTarget)
                  const patch: Record<string, unknown> = {
                    name: fd.get('name'),
                    default_billable: fd.get('default_billable') === 'on',
                    tariffa: (fd.get('tariffa') as string) || null,
                    valuta: (fd.get('valuta') as string) || 'EUR',
                  }
                  for (const f of CLIENT_FIELDS)
                    patch[f] = (fd.get(f) as string) || null
                  void saveClient(c, patch)
                }}
              >
                <label>
                  {t('cp.name')}
                  <input name="name" defaultValue={c.name} />
                </label>
                <label>
                  {t('cp.rate')}
                  <input
                    name="tariffa"
                    type="number"
                    step="0.01"
                    defaultValue={c.tariffa ?? ''}
                  />
                </label>
                <label>
                  {t('cp.currency')}
                  <input name="valuta" defaultValue={c.valuta} />
                </label>
                <label className="cpform__chk">
                  <input
                    type="checkbox"
                    name="default_billable"
                    defaultChecked={c.default_billable}
                  />
                  {t('cp.defaultBillable')}
                </label>
                {CLIENT_FIELDS.map((f) => (
                  <label
                    key={f}
                    className={f === 'description' ? 'cpform__wide' : ''}
                  >
                    {t(`cp.f.${f}`)}
                    <input
                      name={f}
                      defaultValue={(c[f] as string | null) ?? ''}
                    />
                  </label>
                ))}
                <div className="cpform__actions">
                  <button type="submit" className="btn--sm">
                    {t('cp.save')}
                  </button>
                </div>
              </form>
            )}
            {open && (
              <ul className="list nested">
                {projs.length === 0 ? (
                  <li className="hint">{t('cp.noProjects')}</li>
                ) : (
                  projs.map(renderProject)
                )}
              </ul>
            )}
          </li>
          )
        })}
      </ul>

      <h2>{t('cp.addProject')}</h2>
      <div className="cprow">
        <input
          placeholder={t('cp.name')}
          value={pName}
          onChange={(e) => setPName(e.target.value)}
          style={{ minWidth: '14rem' }}
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
    </section>
  )
}
