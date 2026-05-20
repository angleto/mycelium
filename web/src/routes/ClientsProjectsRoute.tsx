import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from 'react'
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

// Add-a-project row, scoped to one client (its own input state so
// typing in one client's box does not touch another's).
function AddProjectInline({
  onAdd,
}: {
  onAdd: (name: string) => Promise<void>
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  return (
    <form
      className="cpadd"
      onSubmit={(e) => {
        e.preventDefault()
        const n = name.trim()
        if (!n) return
        void onAdd(n).then(() => setName(''))
      }}
    >
      <input
        placeholder={t('cp.addProjectHere')}
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <button type="submit" className="btn--sm" disabled={!name.trim()}>
        {t('cp.add')}
      </button>
    </form>
  )
}

// Manage clients and projects (tags + their satellite profiles).
// Clients carry the invoicing card + the billable default and the
// hourly rate (billing is a client relationship); projects carry the
// budget, an optional colour and a description (AI context) and link
// to their client. Projects are nested under the client, collapsed.
export function ClientsProjectsRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [clients, setClients] = useState<Client[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [editC, setEditC] = useState<string | null>(null)
  const [editP, setEditP] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [showArchived, setShowArchived] = useState(false)
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

  async function addClient(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = e.currentTarget
    const fd = new FormData(form)
    const name = (fd.get('name') as string).trim()
    const rag = (fd.get('ragione_sociale') as string).trim()
    if (!name || !rag) return
    setErr(null)
    const { error } = await api.POST('/clients', {
      params: { header: workspaceHeader() },
      body: {
        name,
        ragione_sociale: rag,
        tariffa: (fd.get('tariffa') as string) || null,
        valuta: (fd.get('valuta') as string) || 'EUR',
        default_billable: fd.get('default_billable') === 'on',
      },
    })
    if (error) return setErr(errMessage(error))
    form.reset()
    await load()
  }

  async function createProject(name: string, clientId: string) {
    setErr(null)
    const { error } = await api.POST('/projects', {
      params: { header: workspaceHeader() },
      body: { name, client_tag_id: clientId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
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

  async function purgeProject(p: Project): Promise<void> {
    setErr(null)
    setMsg(null)
    if (!window.confirm(t('cp.confirmPurgeProject', { name: p.name }))) return
    const { error } = await api.DELETE('/projects/{tag_id}', {
      params: { header: workspaceHeader(), path: { tag_id: p.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('cp.purged'))
    await load()
  }

  async function purgeClient(c: Client): Promise<void> {
    setErr(null)
    setMsg(null)
    if (!window.confirm(t('cp.confirmPurgeClient', { name: c.name }))) return
    const { error } = await api.DELETE('/clients/{tag_id}', {
      params: { header: workspaceHeader(), path: { tag_id: c.id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('cp.purged'))
    await load()
  }

  async function setArchive(id: string, version: number, archived: boolean) {
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

  const renderProject = (p: Project) => (
    <li key={p.id} className="cpitem">
      <div className="cpitem__head">
        {p.color && (
          <span
            className="swatch"
            style={{ background: p.color }}
            title={p.color}
          />
        )}
        <span className="cpitem__name">{p.name}</span>
        <span className="cpmeta">
          {p.budget != null && (
            <span className="tag">
              {t('cp.budget')}: {p.budget}
            </span>
          )}
          {p.status === 'archived' && (
            <span className="tag tag--muted">{t('cp.archived')}</span>
          )}
        </span>
        <span className="grow" />
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
        {p.status === 'archived' && (
          <button
            type="button"
            className="btn--ghost btn--sm btn--danger"
            onClick={() => void purgeProject(p)}
            title={t('cp.purgeProjectHint')}
          >
            {t('cp.purge')}
          </button>
        )}
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
            <select name="client_tag_id" defaultValue={p.client_tag_id ?? ''}>
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
    <>
      <h1 className="page-title">{t('cp.title')}</h1>
      <section className="card">
        {err && <p className="err">{err}</p>}
        {msg && <p className="ok">{msg}</p>}

        <h2>{t('cp.newClient')}</h2>
        <form className="cpform" onSubmit={(e) => void addClient(e)}>
          <label>
            {t('cp.name')}
            <input name="name" required />
          </label>
          <label>
            {t('cp.ragioneSociale')}
            <input name="ragione_sociale" required />
          </label>
          <label>
            {t('cp.rate')}
            <input name="tariffa" type="number" step="0.01" />
          </label>
          <label>
            {t('cp.currency')}
            <input name="valuta" defaultValue="EUR" />
          </label>
          <label className="cpform__chk">
            <input type="checkbox" name="default_billable" defaultChecked />
            {t('cp.defaultBillable')}
          </label>
          <div className="cpform__actions">
            <button type="submit" className="btn--sm">
              {t('cp.add')}
            </button>
          </div>
        </form>
      </section>

      <section className="card">
        <div className="cptoolbar">
          <h2>{t('cp.clients')}</h2>
          <span className="grow" />
          <label className="cpcheck">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            {t('cp.showArchived')}
          </label>
          <button
            type="button"
            className="btn--ghost btn--sm"
            onClick={expandAll}
          >
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

        {visClients.length === 0 ? (
          <p className="hint">{t('cp.noProjects')}</p>
        ) : (
          <ul className="list">
            {visClients.map((c) => {
              const open = expanded.has(c.id)
              const projs = projectsOf(c.id)
              return (
                <li key={c.id} className="cpitem">
                  <div className="cpitem__head">
                    <button
                      type="button"
                      className="cpcaret"
                      aria-expanded={open}
                      onClick={() => toggleClient(c.id)}
                    >
                      {open ? '▾' : '▸'}
                    </button>
                    <button
                      type="button"
                      className="cpitem__name cpitem__name--btn"
                      onClick={() => toggleClient(c.id)}
                    >
                      {c.name}
                    </button>
                    <span className="cpmeta">
                      <span className="tag">
                        {c.tariffa
                          ? `${c.tariffa} ${c.valuta}/h`
                          : t('cp.noRate')}
                      </span>
                      <span
                        className={
                          c.default_billable ? 'tag' : 'tag tag--muted'
                        }
                      >
                        {c.default_billable
                          ? t('cp.billable')
                          : t('cp.nonBillable')}
                      </span>
                      <span className="tag tag--muted">
                        {t('cp.projectsN', { n: projs.length })}
                      </span>
                      {c.status === 'archived' && (
                        <span className="tag tag--muted">
                          {t('cp.archived')}
                        </span>
                      )}
                    </span>
                    <span className="grow" />
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
                        void setArchive(
                          c.id,
                          c.version,
                          c.status !== 'archived',
                        )
                      }
                    >
                      {c.status === 'archived'
                        ? t('cp.unarchive')
                        : t('cp.archive')}
                    </button>
                    {c.status === 'archived' && (
                      <button
                        type="button"
                        className="btn--ghost btn--sm btn--danger"
                        onClick={() => void purgeClient(c)}
                        title={t('cp.purgeClientHint')}
                      >
                        {t('cp.purge')}
                      </button>
                    )}
                  </div>

                  {editC === c.id && (
                    <form
                      className="cpform"
                      onSubmit={(e) => {
                        e.preventDefault()
                        const fd = new FormData(e.currentTarget)
                        const patch: Record<string, unknown> = {
                          name: fd.get('name'),
                          default_billable:
                            fd.get('default_billable') === 'on',
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
                          className={
                            f === 'description' ? 'cpform__wide' : ''
                          }
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
                    <div className="cpchildren">
                      {projs.length === 0 ? (
                        <p className="hint">{t('cp.noProjects')}</p>
                      ) : (
                        <ul className="list nested">
                          {projs.map(renderProject)}
                        </ul>
                      )}
                      <AddProjectInline
                        onAdd={(name) => createProject(name, c.id)}
                      />
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </>
  )
}
