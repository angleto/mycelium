import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Invoice = components['schemas']['InvoiceOut']
type Line = components['schemas']['InvoiceLineOut']
type Profile = components['schemas']['IssuerProfileOut']
type Tag = components['schemas']['TagOut']
type ReportRow = components['schemas']['ReportRowOut']

const EMPTY_PROFILE = {
  label: '',
  denominazione: '',
  piva: '',
  codice_fiscale: '',
  indirizzo: '',
  cap: '',
  comune: '',
  provincia: '',
  is_default: false,
}
type ProfileForm = typeof EMPTY_PROFILE

const EMPTY_LINE = { description: '', unit_price: 0, quantity: 1, vat_rate: 22 }
type LineForm = typeof EMPTY_LINE

function totals(lines: Line[]): { taxable: number; vat: number; total: number } {
  const byRate = new Map<number, number>()
  for (const ln of lines) {
    const rate = Number(ln.vat_rate)
    const lt = Math.round(Number(ln.quantity) * Number(ln.unit_price) * 100) / 100
    byRate.set(rate, (byRate.get(rate) ?? 0) + lt)
  }
  let taxable = 0
  let vat = 0
  for (const [rate, imp] of byRate) {
    const i = Math.round(imp * 100) / 100
    taxable += i
    vat += Math.round((i * rate) / 100 * 100) / 100
  }
  return { taxable, vat, total: taxable + vat }
}

export function InvoicesRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId

  const [profiles, setProfiles] = useState<Profile[]>([])
  const [clients, setClients] = useState<Tag[]>([])
  const [list, setList] = useState<Invoice[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  // issuer-profile editor
  const [pForm, setPForm] = useState<ProfileForm>(EMPTY_PROFILE)
  const [pEdit, setPEdit] = useState<string | 'new' | null>(null)

  // new-invoice form
  const [niClient, setNiClient] = useState('')
  const [niIssuer, setNiIssuer] = useState('')
  const [niSeries, setNiSeries] = useState('A')

  // selected invoice + its lines
  const [sel, setSel] = useState<Invoice | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [xml, setXml] = useState<string | null>(null)

  // draft invoice fields (dirty-gated Save)
  const [dIssuer, setDIssuer] = useState('')
  const [dClient, setDClient] = useState('')
  const [dSeries, setDSeries] = useState('A')
  const [dCausale, setDCausale] = useState('')
  const [dNotes, setDNotes] = useState('')
  const [dIban, setDIban] = useState('')
  const [dDue, setDDue] = useState('')
  const [dirty, setDirty] = useState(false)

  // line add / edit
  const [lAdd, setLAdd] = useState<LineForm>(EMPTY_LINE)
  const [lEditId, setLEditId] = useState<string | null>(null)
  const [lEdit, setLEdit] = useState<LineForm>(EMPTY_LINE)

  // time-report -> lines
  const [triFrom, setTriFrom] = useState('')
  const [triTo, setTriTo] = useState('')
  const [triRows, setTriRows] = useState<ReportRow[]>([])
  const [triSel, setTriSel] = useState<Set<string>>(new Set())
  const [triLoaded, setTriLoaded] = useState(false)

  const isDraft = sel?.state === 'draft'
  const defaultIssuer = useMemo(
    () => profiles.find((p) => p.is_default)?.id ?? profiles[0]?.id ?? '',
    [profiles],
  )

  const loadList = useCallback(async () => {
    const h = workspaceHeader()
    const [pr, cl, iv] = await Promise.all([
      api.GET('/issuer-profiles', { params: { header: h } }),
      api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
      api.GET('/invoices', { params: { header: h } }),
    ])
    if (pr.data) setProfiles(pr.data)
    if (cl.data) setClients(cl.data)
    if (iv.data) setList(iv.data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [pr, cl, iv] = await Promise.all([
        api.GET('/issuer-profiles', { params: { header: h } }),
        api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
        api.GET('/invoices', { params: { header: h } }),
      ])
      if (!active) return
      if (pr.data) setProfiles(pr.data)
      if (cl.data) setClients(cl.data)
      if (iv.data) setList(iv.data)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  const openInvoice = useCallback(async (id: string) => {
    setErr(null)
    setXml(null)
    const h = workspaceHeader()
    const [iv, ln] = await Promise.all([
      api.GET('/invoices/{invoice_id}', {
        params: { header: h, path: { invoice_id: id } },
      }),
      api.GET('/invoices/{invoice_id}/lines', {
        params: { header: h, path: { invoice_id: id } },
      }),
    ])
    if (!iv.data) {
      setErr(errMessage(iv.error))
      return
    }
    const inv = iv.data
    setSel(inv)
    setLines(ln.data ?? [])
    setDIssuer(inv.issuer_profile_id ?? '')
    setDClient(inv.client_tag_id)
    setDSeries(inv.series)
    setDCausale(inv.causale ?? '')
    setDNotes(inv.notes ?? '')
    setDIban(inv.payment_iban ?? '')
    setDDue(inv.payment_due_date ?? '')
    setDirty(false)
    setLEditId(null)
    setLAdd(EMPTY_LINE)
    setTriRows([])
    setTriSel(new Set())
    setTriLoaded(false)
  }, [])

  async function reloadSel() {
    if (sel) await openInvoice(sel.id)
    await loadList()
  }

  // --- issuer profiles ---

  function startProfile(p: Profile | null) {
    setErr(null)
    if (p) {
      setPEdit(p.id)
      setPForm({
        label: p.label,
        denominazione: p.denominazione,
        piva: p.piva ?? '',
        codice_fiscale: p.codice_fiscale ?? '',
        indirizzo: p.indirizzo,
        cap: p.cap,
        comune: p.comune,
        provincia: p.provincia ?? '',
        is_default: p.is_default,
      })
    } else {
      setPEdit('new')
      setPForm(EMPTY_PROFILE)
    }
  }

  async function saveProfile(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const h = workspaceHeader()
    const common = {
      label: pForm.label,
      denominazione: pForm.denominazione,
      piva: pForm.piva || null,
      codice_fiscale: pForm.codice_fiscale || null,
      indirizzo: pForm.indirizzo,
      cap: pForm.cap,
      comune: pForm.comune,
      provincia: pForm.provincia || null,
      is_default: pForm.is_default,
    }
    const res =
      pEdit === 'new'
        ? await api.POST('/issuer-profiles', {
            params: { header: h },
            body: { regime_fiscale: 'RF01', paese: 'IT', nazione: 'IT', ...common },
          })
        : await api.PATCH('/issuer-profiles/{profile_id}', {
            params: { header: h, path: { profile_id: pEdit as string } },
            body: common,
          })
    if (res.error) {
      setErr(errMessage(res.error))
      return
    }
    setPEdit(null)
    setMsg(t('invoices.saved'))
    await loadList()
  }

  async function setDefaultProfile(id: string) {
    setErr(null)
    const { error } = await api.POST('/issuer-profiles/{profile_id}/default', {
      params: { header: workspaceHeader(), path: { profile_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadList()
  }

  async function deleteProfile(id: string) {
    if (!window.confirm(t('invoices.confirmDeleteProfile'))) return
    setErr(null)
    const { error } = await api.DELETE('/issuer-profiles/{profile_id}', {
      params: { header: workspaceHeader(), path: { profile_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadList()
  }

  // --- invoices ---

  async function createDraft(e: FormEvent) {
    e.preventDefault()
    if (!niClient) return
    setErr(null)
    const { data, error } = await api.POST('/invoices', {
      params: { header: workspaceHeader() },
      body: {
        client_tag_id: niClient,
        issuer_profile_id: niIssuer || defaultIssuer || null,
        series: niSeries,
      },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await loadList()
    await openInvoice(data.id)
  }

  async function saveInvoice() {
    if (!sel) return
    setErr(null)
    const { error } = await api.PATCH('/invoices/{invoice_id}', {
      params: { header: workspaceHeader(), path: { invoice_id: sel.id } },
      body: {
        client_tag_id: dClient,
        issuer_profile_id: dIssuer || null,
        series: dSeries,
        causale: dCausale || null,
        notes: dNotes || null,
        payment_iban: dIban || null,
        payment_due_date: dDue || null,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('invoices.saved'))
    setDirty(false)
    await reloadSel()
  }

  async function addLine(e: FormEvent) {
    e.preventDefault()
    if (!sel || !lAdd.description) return
    setErr(null)
    const { error } = await api.POST('/invoices/{invoice_id}/lines', {
      params: { header: workspaceHeader(), path: { invoice_id: sel.id } },
      body: {
        description: lAdd.description,
        unit_price: lAdd.unit_price,
        quantity: lAdd.quantity,
        vat_rate: lAdd.vat_rate,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setLAdd(EMPTY_LINE)
    await reloadSel()
  }

  async function saveLine(id: string) {
    if (!sel) return
    setErr(null)
    const { error } = await api.PUT('/invoices/{invoice_id}/lines/{line_id}', {
      params: { header: workspaceHeader(), path: { invoice_id: sel.id, line_id: id } },
      body: {
        description: lEdit.description,
        unit_price: lEdit.unit_price,
        quantity: lEdit.quantity,
        vat_rate: lEdit.vat_rate,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setLEditId(null)
    await reloadSel()
  }

  async function deleteLine(id: string) {
    if (!sel) return
    setErr(null)
    const { error } = await api.DELETE('/invoices/{invoice_id}/lines/{line_id}', {
      params: { header: workspaceHeader(), path: { invoice_id: sel.id, line_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reloadSel()
  }

  async function loadReport() {
    if (!sel) return
    setErr(null)
    const { data, error } = await api.GET('/time/report', {
      params: {
        header: workspaceHeader(),
        query: {
          group_by: 'task',
          billable: true,
          client_tag_id: dClient,
          ...(triFrom ? { start_from: `${triFrom}T00:00:00Z` } : {}),
          ...(triTo ? { start_to: `${triTo}T23:59:59Z` } : {}),
        },
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    // Only rows with billable time are invoiceable.
    setTriRows((data ?? []).filter((r) => r.billable_seconds > 0))
    setTriSel(new Set())
    setTriLoaded(true)
  }

  async function addSelectedLines() {
    if (!sel) return
    setErr(null)
    const picked = triRows.filter((r) => r.key && triSel.has(r.key))
    for (const r of picked) {
      const hours = Math.round((r.billable_seconds / 3600) * 100) / 100
      if (hours <= 0) continue
      const rate = Math.round((Number(r.amount) / hours) * 100) / 100
      const { error } = await api.POST('/invoices/{invoice_id}/lines', {
        params: { header: workspaceHeader(), path: { invoice_id: sel.id } },
        body: {
          description: r.label ?? 'Time',
          quantity: hours,
          unit_price: rate,
          vat_rate: 22,
        },
      })
      if (error) {
        setErr(errMessage(error))
        return
      }
    }
    setTriRows([])
    setTriSel(new Set())
    setTriLoaded(false)
    await reloadSel()
  }

  async function act(p: Promise<{ error?: unknown }>, confirmMsg?: string) {
    if (confirmMsg && !window.confirm(confirmMsg)) return
    setErr(null)
    setMsg(null)
    const { error } = await p
    if (error) {
      setErr(errMessage(error))
      return
    }
    await loadList()
  }

  async function showXml(id: string) {
    setErr(null)
    const { data, error } = await api.GET('/invoices/{invoice_id}/xml', {
      params: { header: workspaceHeader(), path: { invoice_id: id } },
      parseAs: 'text',
    })
    if (error || data == null) {
      setErr(errMessage(error))
      return
    }
    setXml(String(data))
  }

  const tv = totals(lines)
  const clientName = (id: string) => clients.find((c) => c.id === id)?.name ?? id
  const dField = <T,>(setter: (v: T) => void) => (v: T) => {
    setter(v)
    setDirty(true)
  }

  return (
    <section className="card">
      <h1>{t('invoices.title')}</h1>
      <p className="hint">{t('invoices.intro')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <h2>{t('invoices.profiles')}</h2>
      <p className="hint">{t('invoices.profilesHint')}</p>
      {profiles.length === 0 ? (
        <p className="hint">{t('invoices.noProfiles')}</p>
      ) : (
        <ul className="list">
          {profiles.map((p) => (
            <li key={p.id}>
              <strong>{p.label}</strong> · {p.denominazione}
              {p.piva ? ` · ${p.piva}` : ''}{' '}
              {p.is_default && <span className="muted">[{t('invoices.isDefault')}]</span>}
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => startProfile(p)}
              >
                {t('invoices.edit')}
              </button>
              {!p.is_default && (
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => void setDefaultProfile(p.id)}
                >
                  {t('invoices.setDefault')}
                </button>
              )}
              {!p.is_default && (
                <button
                  type="button"
                  className="btn--sm btn--danger"
                  onClick={() => void deleteProfile(p.id)}
                >
                  {t('invoices.delete')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {pEdit === null ? (
        <button type="button" className="btn--sm" onClick={() => startProfile(null)}>
          {t('invoices.newProfile')}
        </button>
      ) : (
        <form onSubmit={(e) => void saveProfile(e)} className="card card--running">
          <h3>{pEdit === 'new' ? t('invoices.newProfile') : t('invoices.editProfile')}</h3>
          <div className="row">
            <input
              required
              placeholder={t('invoices.label')}
              value={pForm.label}
              onChange={(e) => setPForm({ ...pForm, label: e.target.value })}
            />
            <input
              required
              placeholder={t('invoices.denom')}
              value={pForm.denominazione}
              onChange={(e) => setPForm({ ...pForm, denominazione: e.target.value })}
            />
            <input
              placeholder={t('invoices.piva')}
              value={pForm.piva}
              onChange={(e) => setPForm({ ...pForm, piva: e.target.value })}
            />
            <input
              placeholder={t('invoices.cf')}
              value={pForm.codice_fiscale}
              onChange={(e) => setPForm({ ...pForm, codice_fiscale: e.target.value })}
            />
          </div>
          <div className="row">
            <input
              placeholder={t('invoices.address')}
              value={pForm.indirizzo}
              onChange={(e) => setPForm({ ...pForm, indirizzo: e.target.value })}
            />
            <input
              placeholder={t('invoices.cap')}
              value={pForm.cap}
              onChange={(e) => setPForm({ ...pForm, cap: e.target.value })}
            />
            <input
              placeholder={t('invoices.comune')}
              value={pForm.comune}
              onChange={(e) => setPForm({ ...pForm, comune: e.target.value })}
            />
            <input
              placeholder={t('invoices.provincia')}
              value={pForm.provincia}
              onChange={(e) => setPForm({ ...pForm, provincia: e.target.value })}
            />
          </div>
          <label className="row">
            <input
              type="checkbox"
              checked={pForm.is_default}
              onChange={(e) => setPForm({ ...pForm, is_default: e.target.checked })}
            />
            {t('invoices.isDefault')}
          </label>
          <div className="row">
            <button type="submit">{t('invoices.saveProfile')}</button>
            <button type="button" className="btn--ghost" onClick={() => setPEdit(null)}>
              {t('invoices.cancel')}
            </button>
          </div>
        </form>
      )}

      <h2>{t('invoices.create')}</h2>
      <form onSubmit={(e) => void createDraft(e)} className="row">
        <select value={niClient} onChange={(e) => setNiClient(e.target.value)}>
          <option value="">{t('invoices.client')}</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <select value={niIssuer} onChange={(e) => setNiIssuer(e.target.value)}>
          <option value="">
            {t('invoices.issuer')}
            {defaultIssuer ? ` · ${profiles.find((p) => p.id === defaultIssuer)?.label}` : ''}
          </option>
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        <input
          value={niSeries}
          onChange={(e) => setNiSeries(e.target.value)}
          style={{ width: '3rem' }}
          aria-label={t('invoices.series')}
        />
        <button type="submit" disabled={!niClient || profiles.length === 0}>
          {t('invoices.create')}
        </button>
      </form>

      <h2>{t('invoices.list')}</h2>
      {list.length === 0 ? (
        <p className="hint">{t('invoices.none')}</p>
      ) : (
        <ul className="list">
          {list.map((i) => (
            <li key={i.id}>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void openInvoice(i.id)}
              >
                {t('invoices.open')}
              </button>{' '}
              {i.series}/{i.year}/{i.number ?? '–'}{' '}
              <span className="muted">
                · {clientName(i.client_tag_id)} · {t('invoices.state')} {i.state} ·{' '}
                {i.total} · sdi {i.sdi_status}
              </span>
            </li>
          ))}
        </ul>
      )}

      {sel && (
        <div className="card card--running">
          <h2>
            {t('invoices.title')} {sel.series}/{sel.year}/{sel.number ?? '–'}
          </h2>
          <p className="hint">
            {isDraft ? t('invoices.draftEditable') : t('invoices.emitted')}
          </p>

          <div className="row">
            <label>
              {t('invoices.issuer')}
              <select
                value={dIssuer}
                disabled={!isDraft}
                onChange={(e) => dField(setDIssuer)(e.target.value)}
              >
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('invoices.client')}
              <select
                value={dClient}
                disabled={!isDraft}
                onChange={(e) => dField(setDClient)(e.target.value)}
              >
                {clients.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('invoices.series')}
              <input
                value={dSeries}
                disabled={!isDraft}
                style={{ width: '3rem' }}
                onChange={(e) => dField(setDSeries)(e.target.value)}
              />
            </label>
          </div>

          <h3>{t('invoices.lines')}</h3>
          {lines.length === 0 ? (
            <p className="hint">{t('invoices.noLines')}</p>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>#</th>
                  <th>{t('invoices.lineDesc')}</th>
                  <th>{t('invoices.qty')}</th>
                  <th>{t('invoices.price')}</th>
                  <th>{t('invoices.vat')}</th>
                  <th>{t('invoices.lineTotal')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {lines.map((ln) =>
                  lEditId === ln.id ? (
                    <tr key={ln.id}>
                      <td>{ln.line_no}</td>
                      <td>
                        <input
                          value={lEdit.description}
                          onChange={(e) =>
                            setLEdit({ ...lEdit, description: e.target.value })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={lEdit.quantity}
                          style={{ width: '4rem' }}
                          onChange={(e) =>
                            setLEdit({ ...lEdit, quantity: Number(e.target.value) })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={lEdit.unit_price}
                          style={{ width: '6rem' }}
                          onChange={(e) =>
                            setLEdit({ ...lEdit, unit_price: Number(e.target.value) })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={lEdit.vat_rate}
                          style={{ width: '4rem' }}
                          onChange={(e) =>
                            setLEdit({ ...lEdit, vat_rate: Number(e.target.value) })
                          }
                        />
                      </td>
                      <td>
                        {(Number(lEdit.quantity) * Number(lEdit.unit_price)).toFixed(2)}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn--sm"
                          onClick={() => void saveLine(ln.id)}
                        >
                          {t('invoices.save')}
                        </button>
                        <button
                          type="button"
                          className="btn--sm btn--ghost"
                          onClick={() => setLEditId(null)}
                        >
                          {t('invoices.cancel')}
                        </button>
                      </td>
                    </tr>
                  ) : (
                    <tr key={ln.id}>
                      <td>{ln.line_no}</td>
                      <td>{ln.description}</td>
                      <td>{Number(ln.quantity)}</td>
                      <td>{Number(ln.unit_price).toFixed(2)}</td>
                      <td>{Number(ln.vat_rate)}%</td>
                      <td>
                        {(Number(ln.quantity) * Number(ln.unit_price)).toFixed(2)}
                      </td>
                      <td>
                        {isDraft && (
                          <>
                            <button
                              type="button"
                              className="btn--sm btn--ghost"
                              onClick={() => {
                                setLEditId(ln.id)
                                setLEdit({
                                  description: ln.description,
                                  unit_price: Number(ln.unit_price),
                                  quantity: Number(ln.quantity),
                                  vat_rate: Number(ln.vat_rate),
                                })
                              }}
                            >
                              {t('invoices.edit')}
                            </button>
                            <button
                              type="button"
                              className="btn--sm btn--danger"
                              onClick={() => void deleteLine(ln.id)}
                            >
                              {t('invoices.delete')}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          )}

          {isDraft && (
            <form onSubmit={(e) => void addLine(e)} className="row">
              <input
                required
                placeholder={t('invoices.lineDesc')}
                value={lAdd.description}
                onChange={(e) => setLAdd({ ...lAdd, description: e.target.value })}
              />
              <input
                type="number"
                placeholder={t('invoices.qty')}
                value={lAdd.quantity}
                style={{ width: '4rem' }}
                onChange={(e) => setLAdd({ ...lAdd, quantity: Number(e.target.value) })}
              />
              <input
                type="number"
                placeholder={t('invoices.price')}
                value={lAdd.unit_price}
                style={{ width: '6rem' }}
                onChange={(e) => setLAdd({ ...lAdd, unit_price: Number(e.target.value) })}
              />
              <input
                type="number"
                placeholder={t('invoices.vat')}
                value={lAdd.vat_rate}
                style={{ width: '4rem' }}
                onChange={(e) => setLAdd({ ...lAdd, vat_rate: Number(e.target.value) })}
              />
              <button type="submit">{t('invoices.addLine')}</button>
            </form>
          )}

          {isDraft && (
            <div className="card">
              <h3>{t('invoices.fromTime')}</h3>
              <p className="hint">{t('invoices.fromTimeHint')}</p>
              <div className="row">
                <label>
                  {t('invoices.periodFrom')}
                  <input
                    type="date"
                    value={triFrom}
                    onChange={(e) => setTriFrom(e.target.value)}
                  />
                </label>
                <label>
                  {t('invoices.periodTo')}
                  <input
                    type="date"
                    value={triTo}
                    onChange={(e) => setTriTo(e.target.value)}
                  />
                </label>
                <button type="button" className="btn--sm" onClick={() => void loadReport()}>
                  {t('invoices.loadReport')}
                </button>
              </div>
              {triLoaded && triRows.length === 0 && (
                <p className="hint">{t('invoices.noReport')}</p>
              )}
              {triRows.length > 0 && (
                <>
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th />
                        <th>{t('invoices.lineDesc')}</th>
                        <th>{t('invoices.hours')}</th>
                        <th>{t('invoices.amount')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {triRows.map((r) => (
                        <tr key={r.key ?? r.label}>
                          <td>
                            <input
                              type="checkbox"
                              checked={!!r.key && triSel.has(r.key)}
                              disabled={!r.key}
                              onChange={(e) => {
                                if (!r.key) return
                                const n = new Set(triSel)
                                if (e.target.checked) n.add(r.key)
                                else n.delete(r.key)
                                setTriSel(n)
                              }}
                            />
                          </td>
                          <td>{r.label ?? '–'}</td>
                          <td>{(r.billable_seconds / 3600).toFixed(2)}</td>
                          <td>
                            {Number(r.amount).toFixed(2)} {r.currency}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <button
                    type="button"
                    disabled={triSel.size === 0}
                    onClick={() => void addSelectedLines()}
                  >
                    {t('invoices.addSelected')}
                  </button>
                </>
              )}
            </div>
          )}

          <p>
            <strong>{t('invoices.taxable')}:</strong> {tv.taxable.toFixed(2)} ·{' '}
            <strong>{t('invoices.vatTotal')}:</strong> {tv.vat.toFixed(2)} ·{' '}
            <strong>{t('invoices.total')}:</strong> {tv.total.toFixed(2)}
          </p>

          <div className="row">
            <label>
              {t('invoices.causale')}
              <input
                value={dCausale}
                disabled={!isDraft}
                onChange={(e) => dField(setDCausale)(e.target.value)}
              />
            </label>
            <label>
              {t('invoices.iban')}
              <input
                value={dIban}
                disabled={!isDraft}
                onChange={(e) => dField(setDIban)(e.target.value)}
              />
            </label>
            <label>
              {t('invoices.dueDate')}
              <input
                type="date"
                value={dDue}
                disabled={!isDraft}
                onChange={(e) => dField(setDDue)(e.target.value)}
              />
            </label>
          </div>
          <label>
            {t('invoices.notes')}
            <textarea
              rows={3}
              value={dNotes}
              disabled={!isDraft}
              onChange={(e) => dField(setDNotes)(e.target.value)}
            />
          </label>

          <div className="row">
            {isDraft && (
              <>
                <button type="button" disabled={!dirty} onClick={() => void saveInvoice()}>
                  {t('invoices.save')}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    void act(
                      api.POST('/invoices/{invoice_id}/transmit', {
                        params: {
                          header: workspaceHeader(),
                          path: { invoice_id: sel.id },
                        },
                        body: {},
                      }),
                    ).then(() => void openInvoice(sel.id))
                  }
                >
                  {t('invoices.transmit')}
                </button>
                <button
                  type="button"
                  className="btn--danger"
                  onClick={() =>
                    void act(
                      api.DELETE('/invoices/{invoice_id}', {
                        params: {
                          header: workspaceHeader(),
                          path: { invoice_id: sel.id },
                        },
                      }),
                      t('invoices.confirmDeleteDraft'),
                    ).then(() => setSel(null))
                  }
                >
                  {t('invoices.deleteDraft')}
                </button>
              </>
            )}
            {!isDraft && (
              <>
                <button type="button" onClick={() => void showXml(sel.id)}>
                  {t('invoices.xml')}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    void act(
                      api.POST('/invoices/{invoice_id}/paid', {
                        params: {
                          header: workspaceHeader(),
                          path: { invoice_id: sel.id },
                        },
                      }),
                    ).then(() => void openInvoice(sel.id))
                  }
                >
                  {t('invoices.paid')}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    void act(
                      api.POST('/invoices/credit-note', {
                        params: { header: workspaceHeader() },
                        body: { parent_invoice_id: sel.id },
                      }),
                    )
                  }
                >
                  {t('invoices.creditNote')}
                </button>
              </>
            )}
          </div>

          {xml && (
            <pre className="xml" aria-label="FatturaPA XML">
              {xml}
            </pre>
          )}
        </div>
      )}
    </section>
  )
}
