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

type ClientFieldDef = {
  name: keyof Client
  kind?: 'text' | 'number' | 'select-condizioni' | 'select-modalita' | 'select-language'
}

// FatturaPA closed enums (kept in sync with
// flow_core.services.payment_methods); only the most common subset
// in the dropdowns (the backend accepts the full table).
const CP_CONDIZIONI: ReadonlyArray<readonly [string, string]> = [
  ['TP01', 'a rate'],
  ['TP02', 'completo'],
  ['TP03', 'anticipo'],
]
const CP_MODALITA: ReadonlyArray<readonly [string, string]> = [
  ['MP01', 'contanti'],
  ['MP02', 'assegno'],
  ['MP03', 'assegno circolare'],
  ['MP05', 'bonifico'],
  ['MP07', 'bollettino bancario'],
  ['MP08', 'carta'],
  ['MP12', 'RIBA'],
  ['MP13', 'MAV'],
  ['MP18', 'bollettino c/c postale'],
  ['MP19', 'SEPA DD'],
  ['MP20', 'SEPA DD CORE'],
  ['MP21', 'SEPA DD B2B'],
  ['MP23', 'PagoPA'],
]
// Supported PDF locales (kept in sync with
// flow_core.services.invoice_pdf._LABELS). The XML stays Italian; this
// only affects the courtesy PDF the customer reads.
const CP_LANGUAGES: ReadonlyArray<readonly [string, string]> = [
  ['it', 'italiano'],
  ['en', 'English'],
  ['de', 'Deutsch'],
  ['fr', 'français'],
  ['es', 'español'],
]

const CLIENT_FIELDS: ClientFieldDef[] = [
  { name: 'ragione_sociale' },
  { name: 'codice_fiscale' },
  { name: 'id_paese' },
  { name: 'id_codice' },
  { name: 'indirizzo' },
  { name: 'cap' },
  { name: 'comune' },
  { name: 'provincia' },
  { name: 'nazione' },
  { name: 'codice_destinatario' },
  { name: 'pec' },
  { name: 'invoice_series' },
  { name: 'description' },
  // Payment defaults: NULL means "inherit from the issuer (then system
  // default)". A blank selection in the UI sends NULL.
  { name: 'default_condizioni_pagamento', kind: 'select-condizioni' },
  { name: 'default_modalita_pagamento', kind: 'select-modalita' },
  { name: 'default_payment_terms_days', kind: 'number' },
  // Locale for the PDF only; XML is always Italian.
  { name: 'invoice_language', kind: 'select-language' },
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
                        for (const f of CLIENT_FIELDS) {
                          const raw = (fd.get(f.name) as string) || null
                          // Number fields parse to int; blank stays null
                          // (which means "inherit from the issuer").
                          if (f.kind === 'number') {
                            patch[f.name] = raw ? Number(raw) : null
                          } else {
                            patch[f.name] = raw
                          }
                        }
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
                      {CLIENT_FIELDS.map((f) => {
                        const v = c[f.name]
                        const dv =
                          v == null
                            ? ''
                            : typeof v === 'number'
                              ? String(v)
                              : (v as string)
                        return (
                          <label
                            key={f.name}
                            className={
                              f.name === 'description' ? 'cpform__wide' : ''
                            }
                          >
                            {t(`cp.f.${f.name}`)}
                            {f.kind === 'select-condizioni' ? (
                              <select name={f.name} defaultValue={dv}>
                                <option value="">{t('cp.inherit')}</option>
                                {CP_CONDIZIONI.map(([code, lbl]) => (
                                  <option key={code} value={code}>
                                    {code} - {lbl}
                                  </option>
                                ))}
                              </select>
                            ) : f.kind === 'select-modalita' ? (
                              <select name={f.name} defaultValue={dv}>
                                <option value="">{t('cp.inherit')}</option>
                                {CP_MODALITA.map(([code, lbl]) => (
                                  <option key={code} value={code}>
                                    {code} - {lbl}
                                  </option>
                                ))}
                              </select>
                            ) : f.kind === 'select-language' ? (
                              <select name={f.name} defaultValue={dv}>
                                <option value="">{t('cp.languageDefault')}</option>
                                {CP_LANGUAGES.map(([code, lbl]) => (
                                  <option key={code} value={code}>
                                    {code} - {lbl}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input
                                name={f.name}
                                type={f.kind === 'number' ? 'number' : 'text'}
                                min={f.kind === 'number' ? 0 : undefined}
                                max={f.kind === 'number' ? 365 : undefined}
                                defaultValue={dv}
                              />
                            )}
                          </label>
                        )
                      })}
                      <div className="cpform__actions">
                        <button type="submit" className="btn--sm">
                          {t('cp.save')}
                        </button>
                      </div>
                    </form>
                  )}

                  {editC === c.id && (
                    <ClientStartingNumber client={c} />
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

// Per-client starting-number widget on the client form. Sets the
// (default_issuer, client.invoice_series, current_year) counter so the
// next invoice for this client gets the chosen N (sets last_number =
// N - 1). Used when migrating from another system: e.g. you already
// emitted #1 elsewhere, you want Flow to start from #2.
//
// Constraints surfaced to the user:
// - The client must have an invoice_series (sezionale) and a default
//   issuer profile must exist. Otherwise the widget tells the user to
//   set those first instead of silently failing.
// - The backend rejects any N below max(invoices.number) already
//   emitted under the same key; the resulting 409 is surfaced.
type CounterRow = components['schemas']['InvoiceCounterOut']

function ClientStartingNumber({ client }: { client: Client }) {
  const { t } = useTranslation()
  const [issuerId, setIssuerId] = useState<string | null>(null)
  const [counter, setCounter] = useState<CounterRow | null>(null)
  const [next, setNext] = useState<string>('')
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const year = new Date().getFullYear()
  const series = client.invoice_series ?? null

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const { data } = await api.GET('/issuer-profiles', {
        params: { header: h },
      })
      if (!active) return
      const def = data?.find((p) => p.is_default) ?? data?.[0]
      const iid = def?.id ?? null
      setIssuerId(iid)
      if (iid && series) {
        const cnt = await api.GET('/issuer-profiles/{profile_id}/counters', {
          params: { header: h, path: { profile_id: iid } },
        })
        if (!active) return
        const row =
          cnt.data?.find((r) => r.series === series && r.year === year) ??
          null
        setCounter(row)
        // Pre-fill with "next allocation": last_number + 1, defaulting
        // to 1 when no counter exists yet.
        setNext(String((row?.last_number ?? 0) + 1))
      }
    })()
    return () => {
      active = false
    }
  }, [client.id, series, year])

  if (!series) {
    return (
      <p className="hint">
        {t('cp.startingNumberNoSeries')}
      </p>
    )
  }
  if (!issuerId) {
    return (
      <p className="hint">
        {t('cp.startingNumberNoIssuer')}
      </p>
    )
  }

  async function save() {
    setErr(null)
    setMsg(null)
    const n = Number(next)
    if (!Number.isFinite(n) || n < 1) {
      setErr(t('cp.startingNumberInvalid'))
      return
    }
    const { error, data } = await api.PUT(
      '/issuer-profiles/{profile_id}/counters/{series}/{year}',
      {
        params: {
          header: workspaceHeader(),
          // issuerId and series guaranteed non-null by the guards above.
          path: { profile_id: issuerId!, series: series!, year },
        },
        // last_number = N - 1 so the next allocated number is N.
        body: { last_number: n - 1 },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (data) setCounter(data)
    setMsg(t('cp.saved'))
  }

  return (
    <div className="cpform" style={{ marginTop: '0.5rem' }}>
      <p className="hint">
        {t('cp.startingNumberHint', { series, year })}
      </p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <label>
        {t('cp.startingNumber')}
        <input
          type="number"
          min={Math.max(1, (counter?.max_emitted ?? 0) + 1)}
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
      </label>
      {counter && counter.max_emitted > 0 && (
        <p className="hint">
          {t('cp.startingNumberFloor', { n: counter.max_emitted + 1 })}
        </p>
      )}
      <button type="button" className="btn--sm" onClick={() => void save()}>
        {t('cp.save')}
      </button>
    </div>
  )
}
