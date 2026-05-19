import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Invoice = components['schemas']['InvoiceOut']
type Line = components['schemas']['InvoiceLineOut']
type Profile = components['schemas']['IssuerProfileOut']
type Tag = components['schemas']['TagOut']
type ReportRow = components['schemas']['ReportRowOut']
type Preview = components['schemas']['InvoicePreviewOut']


const EMPTY_LINE = {
  description: '',
  unit_price: 0,
  quantity: 1,
  vat_rate: 22,
  natura: '',
}
type LineForm = typeof EMPTY_LINE

// Forfettario invoices must default lines to 0% + Natura N2.2 (the
// backend resolves the same when vat is unset, but the form must
// SHOW the compliant values, not a misleading 22%).
function blankLine(forfettario: boolean): LineForm {
  return forfettario
    ? { ...EMPTY_LINE, vat_rate: 0, natura: 'N2.2' }
    : EMPTY_LINE
}

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

  // new-invoice form (issuer profiles are managed in Settings)
  const [niClient, setNiClient] = useState('')
  const [niIssuer, setNiIssuer] = useState('')

  // selected invoice + its lines
  const [sel, setSel] = useState<Invoice | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [xml, setXml] = useState<string | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)

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
    const [iv, ln, pv] = await Promise.all([
      api.GET('/invoices/{invoice_id}', {
        params: { header: h, path: { invoice_id: id } },
      }),
      api.GET('/invoices/{invoice_id}/lines', {
        params: { header: h, path: { invoice_id: id } },
      }),
      api.GET('/invoices/{invoice_id}/preview', {
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
    setPreview(pv.data ?? null)
    setDIssuer(inv.issuer_profile_id ?? '')
    setDClient(inv.client_tag_id)
    setDSeries(inv.series)
    setDCausale(inv.causale ?? '')
    setDNotes(inv.notes ?? '')
    setDIban(inv.payment_iban ?? '')
    setDDue(inv.payment_due_date ?? '')
    setDirty(false)
    setLEditId(null)
    setLAdd(blankLine(!!pv.data?.is_forfettario))
    setTriRows([])
    setTriSel(new Set())
    setTriLoaded(false)
  }, [])

  async function reloadSel() {
    if (sel) await openInvoice(sel.id)
    await loadList()
  }

  // Issuer profiles are managed in Settings (read-only here for the
  // issuer picker + the no-issuer guard).

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
        // Series defaults to 'A'; it's editable in the draft editor.
        // (FatturaPA numbering is legally sequential per series+year.)
        series: 'A',
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
        natura: lAdd.natura || null,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setLAdd(blankLine(!!preview?.is_forfettario))
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
        natura: lEdit.natura || null,
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

  async function openPdf(id: string) {
    setErr(null)
    const res = await authFetch(`/invoices/${id}/pdf`)
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    const u = URL.createObjectURL(await res.blob())
    window.open(u, '_blank', 'noopener')
    window.setTimeout(() => URL.revokeObjectURL(u), 60000)
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

      <h2>{t('invoices.create')}</h2>
      {profiles.length === 0 ? (
        <p className="banner">
          {t('invoices.noIssuerGuard')}{' '}
          <Link to="/settings">{t('invoices.goSettings')}</Link>
        </p>
      ) : (
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
              {defaultIssuer
                ? ` · ${profiles.find((p) => p.id === defaultIssuer)?.label}`
                : ''}
            </option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
          <button type="submit" disabled={!niClient}>
            {t('invoices.create')}
          </button>
        </form>
      )}
      <p className="hint">{t('invoices.seriesLegalHint')}</p>

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

          {preview && (
            <div className="docpanel">
              <div className="docpanel__head">
                <strong>{t('invoices.doc.title')}</strong>
                <span className="modal__sp" />
                <button
                  type="button"
                  className="btn--sm"
                  onClick={() => void openPdf(sel.id)}
                >
                  {t('invoices.doc.pdf')}
                </button>
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => void showXml(sel.id)}
                >
                  {t('invoices.doc.xml')}
                </button>
                {preview.is_forfettario && (
                  <span className="tag tag--muted">
                    {t('invoices.doc.forfettario')}
                  </span>
                )}
              </div>
              <div className="docpanel__grid">
                <div>
                  <div className="muted">{t('invoices.doc.cedente')}</div>
                  {preview.issuer ? (
                    <>
                      <div>{preview.issuer.denominazione}</div>
                      <div className="muted">
                        {preview.issuer.piva
                          ? `P.IVA ${preview.issuer.piva}`
                          : ''}
                        {preview.issuer.codice_fiscale
                          ? ` · CF ${preview.issuer.codice_fiscale}`
                          : ''}
                        {preview.issuer.regime_fiscale
                          ? ` · ${preview.issuer.regime_fiscale}`
                          : ''}
                      </div>
                      <div className="muted">
                        {[
                          preview.issuer.indirizzo,
                          preview.issuer.cap,
                          preview.issuer.comune,
                          preview.issuer.provincia,
                        ]
                          .filter(Boolean)
                          .join(' ')}
                      </div>
                    </>
                  ) : (
                    <div className="err">{t('invoices.doc.missing')}</div>
                  )}
                </div>
                <div>
                  <div className="muted">{t('invoices.doc.cessionario')}</div>
                  {preview.client ? (
                    <>
                      <div>{preview.client.denominazione}</div>
                      <div className="muted">
                        {preview.client.piva
                          ? `P.IVA ${preview.client.piva}`
                          : ''}
                        {preview.client.codice_fiscale
                          ? ` · CF ${preview.client.codice_fiscale}`
                          : ''}
                      </div>
                      <div className="muted">
                        {[
                          preview.client.indirizzo,
                          preview.client.cap,
                          preview.client.comune,
                          preview.client.provincia,
                        ]
                          .filter(Boolean)
                          .join(' ')}
                      </div>
                      <div className="muted">
                        {t('invoices.doc.sdi')}:{' '}
                        {preview.client.codice_destinatario ||
                          preview.client.pec ||
                          t('invoices.doc.none')}
                      </div>
                    </>
                  ) : (
                    <div className="err">{t('invoices.doc.missing')}</div>
                  )}
                </div>
                <div>
                  <div className="muted">{t('invoices.doc.totals')}</div>
                  <div>
                    {t('invoices.doc.taxable')}: {preview.totals.taxable} €
                  </div>
                  <div>
                    {t('invoices.doc.vat')}: {preview.totals.vat} €
                  </div>
                  {Number(preview.totals.bollo) > 0 && (
                    <div>
                      {t('invoices.doc.bollo')}: {preview.totals.bollo} €
                    </div>
                  )}
                  <div>
                    <strong>
                      {t('invoices.doc.total')}: {preview.totals.total} €
                    </strong>
                  </div>
                  <div className="muted">
                    {t('invoices.doc.iban')}:{' '}
                    {preview.effective_iban || t('invoices.doc.none')}
                    {preview.iban_source
                      ? ` (${t(`invoices.doc.ibanSrc.${preview.iban_source}`)})`
                      : ''}
                  </div>
                </div>
                <div>
                  <div className="muted">{t('invoices.doc.sdiState')}</div>
                  <div>
                    {t('invoices.state')} {preview.state} · sdi{' '}
                    {preview.sdi_status}
                  </div>
                  <div className="muted">
                    {t('invoices.doc.sdiId')}:{' '}
                    {preview.identificativo_sdi || t('invoices.doc.none')}
                  </div>
                  <div className="muted">
                    {t('invoices.doc.conservation')}:{' '}
                    {preview.conservation_status}
                  </div>
                </div>
              </div>
              {preview.is_forfettario && preview.causale && (
                <p className="hint docpanel__causale">{preview.causale}</p>
              )}
            </div>
          )}

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
                  <th>{t('invoices.natura')}</th>
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
                        <input
                          value={lEdit.natura}
                          placeholder="N2.2"
                          style={{ width: '4.5rem' }}
                          onChange={(e) =>
                            setLEdit({ ...lEdit, natura: e.target.value })
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
                      <td>{ln.natura ?? '—'}</td>
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
                                  natura: ln.natura ?? '',
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
            <form onSubmit={(e) => void addLine(e)} className="row lineform">
              <label>
                {t('invoices.lineDesc')}
                <input
                  required
                  value={lAdd.description}
                  onChange={(e) =>
                    setLAdd({ ...lAdd, description: e.target.value })
                  }
                />
              </label>
              <label>
                {t('invoices.qty')}
                <input
                  type="number"
                  value={lAdd.quantity}
                  style={{ width: '5rem' }}
                  onChange={(e) =>
                    setLAdd({ ...lAdd, quantity: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                {t('invoices.price')}
                <input
                  type="number"
                  value={lAdd.unit_price}
                  style={{ width: '7rem' }}
                  onChange={(e) =>
                    setLAdd({ ...lAdd, unit_price: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                {t('invoices.vat')}
                <input
                  type="number"
                  value={lAdd.vat_rate}
                  style={{ width: '4.5rem' }}
                  onChange={(e) =>
                    setLAdd({ ...lAdd, vat_rate: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                {t('invoices.natura')}
                <input
                  placeholder="N2.2"
                  value={lAdd.natura}
                  style={{ width: '5rem' }}
                  onChange={(e) =>
                    setLAdd({ ...lAdd, natura: e.target.value })
                  }
                />
              </label>
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
