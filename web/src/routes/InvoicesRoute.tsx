import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import { periodRange, type Period } from '../lib/period'
import { PeriodPicker } from '../components/PeriodPicker'
import type { components } from '../api/schema'

type Invoice = components['schemas']['InvoiceOut']
type Line = components['schemas']['InvoiceLineOut']
type Profile = components['schemas']['IssuerProfileOut']
// /clients returns full ClientOut (with tariffa + default payment fields);
// we use those for (a) the addSelectedLines rate (NEVER recompute it as
// amount/hours, that picks up the server-side .quantize() rounding) and
// (b) showing the *effective inherited* payment defaults in the editor
// dropdowns instead of a generic "inherit" placeholder.
type Client = components['schemas']['ClientOut']
type ReportRow = components['schemas']['ReportRowOut']
type Preview = components['schemas']['InvoicePreviewOut']
// Granularity of the "bill from tracked time" report. 'task' bills one
// line per task (full detail); 'project' aggregates to one line per
// project, so clients who shouldn't see the task breakdown don't.
type BillGroup = Extract<components['schemas']['ReportGroup'], 'task' | 'project'>
type InvoiceState = components['schemas']['InvoiceState']
type PaymentStatus = components['schemas']['PaymentStatus']
type SdiNotif = components['schemas']['InvoiceNotificationOut']

// Lifecycle states offered in the list filter (default = work in
// progress). sdi_status (the SdI receipt) and payment_status are
// orthogonal axes, filtered/shown separately.
const FILTER_STATES: readonly InvoiceState[] = [
  'draft',
  'transmitted',
  'delivered',
  'accepted',
  'rejected',
]
// Payment axis as its own toggle group. Both selected (or none) = no
// payment constraint; exactly one selected filters to it.
const FILTER_PAYMENTS: readonly PaymentStatus[] = ['unpaid', 'paid']

// The filter selection is remembered across sessions (localStorage, same
// pattern as the Tasks/Recent widgets). A persisted empty array is a
// valid user choice (deselected all); only an absent/garbled key falls
// back to the default.
const FILTER_STATES_KEY = 'mycelium.invoices.filterStates'
const FILTER_PAYMENTS_KEY = 'mycelium.invoices.filterPayments'
const VIEW_KEY = 'mycelium.invoices.view'
const DEFAULT_FILTER_STATES: InvoiceState[] = ['draft', 'transmitted']

// Visibility band (orthogonal to the state/payment filters). The
// state/payment toggles apply only within 'active'; archived/trashed show
// everything in their band.
type InvView = 'active' | 'archived' | 'trashed'
const INV_VIEWS: readonly InvView[] = ['active', 'archived', 'trashed']

// The view persists, but never restores INTO the recycle bin: a reload
// always lands on the active list (or archived, if that was chosen), so
// you don't reopen the app stuck in the bin.
function readView(): InvView {
  try {
    if (localStorage.getItem(VIEW_KEY) === 'archived') return 'archived'
  } catch {
    /* ignore */
  }
  return 'active'
}

function readFilter<T extends string>(key: string, allowed: readonly T[], fallback: T[]): T[] {
  try {
    const raw = localStorage.getItem(key)
    if (raw == null) return fallback
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed)) return parsed.filter((x): x is T => allowed.includes(x as T))
  } catch {
    /* private mode / malformed: use default */
  }
  return fallback
}


const EMPTY_LINE = {
  description: '',
  unit_price: 0,
  quantity: 1,
  vat_rate: 22,
  vat_nature: '',
}

// FatturaPA 1.2 closed enums for the per-invoice payment overrides.
// Kept in sync with mycelium_core.services.payment_methods.
const INV_CONDIZIONI: ReadonlyArray<readonly [string, string]> = [
  ['TP01', 'a rate'],
  ['TP02', 'completo'],
  ['TP03', 'anticipo'],
]
const INV_MODALITA: ReadonlyArray<readonly [string, string]> = [
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
type LineForm = typeof EMPTY_LINE

// Forfettario invoices must default lines to 0% + Natura N2.2 (the
// backend resolves the same when vat is unset, but the form must
// SHOW the compliant values, not a misleading 22%).
function blankLine(forfettario: boolean): LineForm {
  return forfettario
    ? { ...EMPTY_LINE, vat_rate: 0, vat_nature: 'N2.2' }
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
  const [clients, setClients] = useState<Client[]>([])
  const [list, setList] = useState<Invoice[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  // List filters: lifecycle multi-select (default work-in-progress) + an
  // orthogonal paid/unpaid toggle group. Both restored from localStorage
  // so the selection survives reloads / sessions.
  const [filterStates, setFilterStates] = useState<InvoiceState[]>(() =>
    readFilter(FILTER_STATES_KEY, FILTER_STATES, DEFAULT_FILTER_STATES),
  )
  const [filterPayments, setFilterPayments] = useState<PaymentStatus[]>(() =>
    readFilter(FILTER_PAYMENTS_KEY, FILTER_PAYMENTS, [...FILTER_PAYMENTS]),
  )
  const [view, setView] = useState<InvView>(() => readView())
  // SdI transmission timeline (RC/MC/NS/AT/NE/DT) of the open invoice.
  const [notifs, setNotifs] = useState<SdiNotif[]>([])
  // ``?id=<invoice>`` deep-links a specific invoice: it opens on load and the
  // param tracks the open modal, so a row can be referenced by URL.
  const [searchParams, setSearchParams] = useSearchParams()

  // Persist the filter selection + view on every change.
  useEffect(() => {
    try {
      localStorage.setItem(FILTER_STATES_KEY, JSON.stringify(filterStates))
    } catch {
      /* ignore */
    }
  }, [filterStates])
  useEffect(() => {
    try {
      localStorage.setItem(FILTER_PAYMENTS_KEY, JSON.stringify(filterPayments))
    } catch {
      /* ignore */
    }
  }, [filterPayments])
  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, view)
    } catch {
      /* ignore */
    }
  }, [view])
  // The state/payment filters refine only the active list; the archive and
  // recycle bin show everything in their band. payment_status is a single
  // backend param: sent only when exactly one toggle is active.
  const paymentFilter = filterPayments.length === 1 ? filterPayments[0] : undefined
  const stateQuery = view === 'active' && filterStates.length ? filterStates : undefined
  const paymentQuery = view === 'active' ? paymentFilter : undefined

  // new-invoice form (issuer profiles are managed in Settings)
  const [niClient, setNiClient] = useState('')
  const [niIssuer, setNiIssuer] = useState('')

  // selected invoice + its lines
  const [sel, setSel] = useState<Invoice | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  const [xml, setXml] = useState<string | null>(null)
  const xmlRef = useRef<HTMLDivElement>(null)
  const [preview, setPreview] = useState<Preview | null>(null)

  // draft invoice fields (dirty-gated Save)
  const [dIssuer, setDIssuer] = useState('')
  const [dClient, setDClient] = useState('')
  const [dSeries, setDSeries] = useState('A')
  const [dCausale, setDCausale] = useState('')
  const [dNotes, setDNotes] = useState('')
  const [dIban, setDIban] = useState('')
  const [dDue, setDDue] = useState('')
  // Per-document payment overrides (NULL = inherit from client/issuer).
  const [dCondizioni, setDCondizioni] = useState('')
  const [dModalita, setDModalita] = useState('')
  const [dTermsDays, setDTermsDays] = useState('')
  const [dirty, setDirty] = useState(false)
  // Inline "change starting number" widget on a draft. Lets the user
  // raise the counter for (issuer, sezionale, year) without leaving
  // /invoices when they realise the prev system already used the
  // current number. last_number = N - 1 so the next allocation = N.
  const [editingNum, setEditingNum] = useState(false)
  const [newNextN, setNewNextN] = useState('')

  // line add / edit
  const [lAdd, setLAdd] = useState<LineForm>(EMPTY_LINE)
  const [lEditId, setLEditId] = useState<string | null>(null)
  const [lEdit, setLEdit] = useState<LineForm>(EMPTY_LINE)

  // time-report -> lines. Period defaults to the current month with
  // prev/next navigation (same widget as the Time report); custom mode
  // exposes free from/to date inputs. Granularity toggles between one
  // line per task and one aggregated line per project. from/to are
  // derived (not state) to avoid a setState-in-effect sync loop.
  const [triPeriod, setTriPeriod] = useState<Period>('month')
  const [triAnchor, setTriAnchor] = useState<Date>(() => new Date())
  const [triCustomFrom, setTriCustomFrom] = useState('')
  const [triCustomTo, setTriCustomTo] = useState('')
  const [triGroup, setTriGroup] = useState<BillGroup>('task')
  const [triRows, setTriRows] = useState<ReportRow[]>([])
  const [triSel, setTriSel] = useState<Set<string>>(new Set())
  const [triLoaded, setTriLoaded] = useState(false)
  const { from: triFrom, to: triTo } =
    triPeriod === 'custom'
      ? { from: triCustomFrom, to: triCustomTo }
      : periodRange(triPeriod, triAnchor)

  const isDraft = sel?.state === 'draft'
  const defaultIssuer = useMemo(
    () => profiles.find((p) => p.is_default)?.id ?? profiles[0]?.id ?? '',
    [profiles],
  )

  const loadList = useCallback(async () => {
    const h = workspaceHeader()
    const [pr, cl, iv] = await Promise.all([
      api.GET('/issuer-profiles', { params: { header: h } }),
      api.GET('/clients', { params: { header: h } }),
      api.GET('/invoices', {
        params: {
          header: h,
          query: {
            view,
            ...(stateQuery ? { state: stateQuery } : {}),
            ...(paymentQuery ? { payment_status: paymentQuery } : {}),
          },
        },
      }),
    ])
    if (pr.data) setProfiles(pr.data)
    if (cl.data) setClients(cl.data)
    if (iv.data) setList(iv.data)
  }, [view, stateQuery, paymentQuery])

  // Reload on workspace switch and whenever a filter changes. Inlined
  // (rather than calling loadList) so the setState lands after an await
  // — a synchronous setState in an effect body cascades renders — and so
  // an unmount mid-fetch is cancellable via the active guard.
  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [pr, cl, iv] = await Promise.all([
        api.GET('/issuer-profiles', { params: { header: h } }),
        api.GET('/clients', { params: { header: h } }),
        api.GET('/invoices', {
          params: {
            header: h,
            query: {
              view,
              ...(stateQuery ? { state: stateQuery } : {}),
              ...(paymentQuery ? { payment_status: paymentQuery } : {}),
            },
          },
        }),
      ])
      if (!active) return
      if (pr.data) setProfiles(pr.data)
      if (cl.data) setClients(cl.data)
      if (iv.data) setList(iv.data)
    })()
    return () => {
      active = false
    }
  }, [activeId, view, stateQuery, paymentQuery])

  const openInvoice = useCallback(async (id: string) => {
    setErr(null)
    setXml(null)
    const h = workspaceHeader()
    const [iv, ln, pv, nt] = await Promise.all([
      api.GET('/invoices/{invoice_id}', {
        params: { header: h, path: { invoice_id: id } },
      }),
      api.GET('/invoices/{invoice_id}/lines', {
        params: { header: h, path: { invoice_id: id } },
      }),
      api.GET('/invoices/{invoice_id}/preview', {
        params: { header: h, path: { invoice_id: id } },
      }),
      api.GET('/invoices/{invoice_id}/notifications', {
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
    // SdI notification timeline (empty for a draft / not-yet-transmitted),
    // set in the same batch so the panel never shows the prior invoice's.
    setNotifs(nt.data ?? [])
    setDIssuer(inv.issuer_profile_id ?? '')
    setDClient(inv.client_tag_id)
    setDSeries(inv.series)
    setDCausale(inv.purpose ?? '')
    setDNotes(inv.notes ?? '')
    setDIban(inv.payment_iban ?? '')
    setDDue(inv.payment_due_date ?? '')
    setDCondizioni(inv.payment_conditions_code ?? '')
    setDModalita(inv.payment_method_code ?? '')
    setDTermsDays(
      inv.payment_terms_days != null ? String(inv.payment_terms_days) : '',
    )
    setDirty(false)
    setLEditId(null)
    setLAdd(blankLine(!!pv.data?.is_forfettario))
    setTriRows([])
    setTriSel(new Set())
    setTriLoaded(false)
    // Reflect the open invoice in the URL (?id=): shareable + deep-linkable.
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.set('id', id)
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  // Close the modal and drop ``?id=`` — one place so every dismissal path
  // (Escape, backdrop, Close, Cancel) keeps the URL in sync.
  const closeInvoice = useCallback(() => {
    setSel(null)
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('id')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  // The open invoice shows in a modal (no scrolling to the bottom of the
  // page): Escape dismisses it, alongside the backdrop click and the Close
  // button in the header.
  useEffect(() => {
    if (!sel) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeInvoice()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel, closeInvoice])

  // Deep-link: ``?id=<invoice>`` opens that invoice once on load (a shareable
  // URL that lands straight on the document, no searching).
  const deepLinked = useRef(false)
  useEffect(() => {
    if (deepLinked.current) return
    const id = searchParams.get('id')
    if (id) {
      deepLinked.current = true
      // Deferred so the effect body itself never triggers openInvoice's
      // synchronous setState (react-hooks/set-state-in-effect).
      queueMicrotask(() => void openInvoice(id))
    }
  }, [searchParams, openInvoice])

  // Bring the document panel into view when its XML content changes (invoice
  // or signed notification). Kept in an effect so the click handlers never read
  // the ref during render (react-hooks/refs).
  useEffect(() => {
    if (xml == null) return
    requestAnimationFrame(() =>
      xmlRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }),
    )
  }, [xml])

  async function reloadSel() {
    if (sel) await openInvoice(sel.id)
    await loadList()
  }

  // Override the per-(issuer, series, year) counter so the next number
  // allocated for this client/year is N. Used when the user has
  // already emitted invoice #N elsewhere and wants Mycelium to continue
  // from #(N+1). The backend rejects any value below the max already
  // emitted in Mycelium under the same key — that error is surfaced.
  async function saveStartingNumber() {
    if (!sel || !dIssuer) return
    setErr(null)
    setMsg(null)
    const n = Number(newNextN)
    if (!Number.isFinite(n) || n < 1) {
      setErr(t('invoices.startingNumberInvalid'))
      return
    }
    const { error } = await api.PUT(
      '/issuer-profiles/{profile_id}/counters/{series}/{year}',
      {
        params: {
          header: workspaceHeader(),
          path: { profile_id: dIssuer, series: dSeries, year: sel.year },
        },
        // last_number = N - 1 so the next allocated number is N.
        body: { last_number: n - 1 },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setEditingNum(false)
    setMsg(t('invoices.saved'))
    await reloadSel()
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
        // Series omitted on purpose: the backend defaults to the client's own
        // sezionale (per-client numbering). Editable later in the draft editor.
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
    // client_tag_id / issuer_profile_id are intentionally omitted:
    // they are frozen at create_draft on the backend and the SPA shows
    // them as read-only. Sending them would 400 against the tightened
    // _DRAFT_UPDATABLE whitelist.
    const { error } = await api.PATCH('/invoices/{invoice_id}', {
      params: { header: workspaceHeader(), path: { invoice_id: sel.id } },
      body: {
        series: dSeries,
        purpose: dCausale || null,
        notes: dNotes || null,
        payment_iban: dIban || null,
        payment_due_date: dDue || null,
        payment_conditions_code: dCondizioni || null,
        payment_method_code: dModalita || null,
        payment_terms_days:
          dTermsDays.trim() === '' ? null : Number(dTermsDays),
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
        vat_nature: lAdd.vat_nature || null,
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
        vat_nature: lEdit.vat_nature || null,
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

  const loadReport = useCallback(async () => {
    if (!sel || !dClient) return
    setErr(null)
    const { data, error } = await api.GET('/time/report', {
      params: {
        header: workspaceHeader(),
        query: {
          group_by: triGroup,
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
  }, [sel, dClient, triGroup, triFrom, triTo])

  // Auto-refresh the billable report whenever the draft, its client,
  // the range, or the granularity changes (loadReport's identity tracks
  // range + group). No manual "load" step: the month is preselected.
  useEffect(() => {
    if (!isDraft || !dClient) return
    // loadReport is a fetch (external-system sync); its synchronous
    // setErr(null) is what trips the rule, not a cascading render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadReport()
  }, [isDraft, dClient, loadReport])

  async function addSelectedLines() {
    if (!sel) return
    setErr(null)
    // Forfettario invoices bill at 0% + Natura N2.2 (same default as
    // manual lines via blankLine); ordinary regimes default to 22%.
    const forfettario = !!preview?.is_forfettario
    // The client's hourly rate is the authoritative source for the
    // invoice line unit_price. The previous code recomputed it as
    // ``amount / hours`` — that rounds twice (the server already
    // quantizes ``amount`` to 2 decimals, and hours rounded to 2
    // decimals lose up to ~36 seconds): with a 62.50 €/h rate and a
    // 3599-second entry you got 62.48 instead of 62.50. We now take
    // the rate verbatim from the client and use hours at minute
    // precision (4 decimals = 0.36 s).
    const client = clients.find((c) => c.id === dClient)
    const clientRate =
      client?.hourly_rate != null ? Number(client.hourly_rate) : null
    const picked = triRows.filter((r) => r.key && triSel.has(r.key))
    for (const r of picked) {
      // Time is rounded to the nearest minute before billing: the timer
      // has second-level resolution, so a "1 hour" entry can come out
      // as 3599s and produce an amount of 62.4826 → 62.48 €, which
      // surfaces as ugly cents on the invoice. Rounding up/down to the
      // nearest minute gives clean amounts (3599s → 3600s → 1.0000h)
      // and is the standard convention in professional time trackers.
      const seconds = Math.round(r.billable_seconds / 60) * 60
      const hours = Math.round((seconds / 3600) * 10000) / 10000
      if (hours <= 0) continue
      // Rate is the client's hourly rate, verbatim. It is NEVER
      // recomputed as amount/hours (that propagates the quantize
      // rounding of the server-side amount, and contradicts the user
      // rule "rate is fixed, what I type is what I bill"). When the
      // client has no tariffa we leave unit_price at 0 so the user
      // notices and sets it on the line manually — a derived rate from
      // a misconfigured client would silently invent a wrong value.
      const rate = clientRate ?? 0
      const { error } = await api.POST('/invoices/{invoice_id}/lines', {
        params: { header: workspaceHeader(), path: { invoice_id: sel.id } },
        body: {
          description: r.label ?? 'Time',
          quantity: hours,
          unit_price: rate,
          vat_rate: forfettario ? 0 : 22,
          ...(forfettario ? { vat_nature: 'N2.2' } : {}),
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

  // Recycle-bin / archive actions. openapi-fetch needs a literal path, so
  // each verb is dispatched explicitly rather than via a templated string.
  function rowAction(id: string, kind: 'trash' | 'restore' | 'archive' | 'unarchive') {
    const p = { params: { header: workspaceHeader(), path: { invoice_id: id } } }
    const call =
      kind === 'trash'
        ? api.POST('/invoices/{invoice_id}/trash', p)
        : kind === 'restore'
          ? api.POST('/invoices/{invoice_id}/restore', p)
          : kind === 'archive'
            ? api.POST('/invoices/{invoice_id}/archive', p)
            : api.POST('/invoices/{invoice_id}/unarchive', p)
    return act(call)
  }

  function deletePermanent(id: string) {
    return act(
      api.DELETE('/invoices/{invoice_id}', {
        params: { header: workspaceHeader(), path: { invoice_id: id } },
      }),
      t('invoices.confirmDeletePermanent'),
    )
  }

  async function showXml(id: string) {
    setErr(null)
    const { data, error } = await api.GET('/invoices/{invoice_id}/xml', {
      params: { header: workspaceHeader(), path: { invoice_id: id } },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    // The viewer renders in the document panel; the effect on ``xml`` brings
    // it into view (both the panel-head and action-row buttons trigger it from
    // far up/down the long modal).
    setXml(data.xml)
  }

  // Filename for the preview .xml. We do not try to mirror the SdI
  // file-name convention (IT{piva}_{progressivo}.xml): the preview
  // progressivo is the ANTEPRIMA placeholder, and the cedente/trasmittente
  // IDs are not exposed to the SPA. A human-readable name is enough for
  // a verification download.
  function xmlFilename(): string {
    if (!preview) return 'fattura.xml'
    const { series, year, number } = preview
    if (number != null) return `fattura-${series}-${year}-${number}.xml`
    return `fattura-bozza-${series}-${year}.xml`
  }

  async function downloadXml(id: string) {
    setErr(null)
    const { data, error } = await api.GET('/invoices/{invoice_id}/xml', {
      params: { header: workspaceHeader(), path: { invoice_id: id } },
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    const blob = new Blob([data.xml], { type: 'application/xml;charset=utf-8' })
    const u = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = u
    a.download = xmlFilename()
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(u), 60000)
  }

  // The signed SdI notification XML (RC/MC/NS/...): the XAdES-signed proof of
  // the outcome. View reuses the same document-panel viewer as the invoice XML.
  async function showNotifXml(notificationId: string) {
    if (!sel) return
    setErr(null)
    const { data, error } = await api.GET(
      '/invoices/{invoice_id}/notifications/{notification_id}/xml',
      {
        params: {
          header: workspaceHeader(),
          path: { invoice_id: sel.id, notification_id: notificationId },
        },
      },
    )
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setXml(data.xml)
  }

  async function downloadNotifXml(notificationId: string, fileName: string | null) {
    if (!sel) return
    setErr(null)
    const { data, error } = await api.GET(
      '/invoices/{invoice_id}/notifications/{notification_id}/xml',
      {
        params: {
          header: workspaceHeader(),
          path: { invoice_id: sel.id, notification_id: notificationId },
        },
      },
    )
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    const blob = new Blob([data.xml], { type: 'application/xml;charset=utf-8' })
    const u = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = u
    a.download = fileName ?? `sdi-${notificationId}.xml`
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(u), 60000)
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

  // Reopen a scartato (SdI NS / rejected) invoice back to draft so it can be
  // corrected and re-sent under the same number + date. Untyped authFetch so
  // this does not depend on a schema.d.ts regen.
  async function reopen(id: string) {
    setErr(null)
    const res = await authFetch(`/invoices/${id}/reopen`, { method: 'POST' })
    if (!res.ok) {
      setErr(errMessage(await res.json().catch(() => null)))
      return
    }
    await openInvoice(id)
  }

  const tv = totals(lines)
  // Select-all over the billable report rows. Only rows with a usable
  // ``key`` are selectable (a null key has its per-row checkbox
  // disabled too), so the master checkbox operates on exactly that
  // set: all-selected → clear, otherwise → select every selectable
  // row. ``triSomeSelected`` drives the indeterminate (dash) state.
  const triSelectableKeys = triRows
    .map((r) => r.key)
    .filter((k): k is string => Boolean(k))
  const triAllSelected =
    triSelectableKeys.length > 0 &&
    triSelectableKeys.every((k) => triSel.has(k))
  const triSomeSelected = triSelectableKeys.some((k) => triSel.has(k))
  const toggleSelectAllRows = () =>
    setTriSel(triAllSelected ? new Set() : new Set(triSelectableKeys))
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
      {/* Visibility band selector (Active | Archived | Trash). */}
      <div className="row">
        {INV_VIEWS.map((v) => (
          <button
            key={v}
            type="button"
            className={`btn--sm ${view === v ? '' : 'btn--ghost'}`}
            aria-pressed={view === v}
            onClick={() => setView(v)}
          >
            {t(`invoices.view.${v}`)}
          </button>
        ))}
      </div>
      {/* Lifecycle + paid/unpaid toggle filters (persisted), refining only
          the active list; archive/bin show everything in their band. */}
      {view === 'active' && (
        <div className="row">
          {FILTER_STATES.map((s) => {
          const on = filterStates.includes(s)
          return (
            <button
              key={s}
              type="button"
              className={`btn--sm ${on ? '' : 'btn--ghost'}`}
              aria-pressed={on}
              onClick={() =>
                setFilterStates((prev) =>
                  on ? prev.filter((x) => x !== s) : [...prev, s],
                )
              }
            >
              {t(`invoices.stateLabel.${s}`)}
            </button>
          )
        })}
        <span className="filter-divider" aria-hidden="true" />
        {FILTER_PAYMENTS.map((p) => {
          const on = filterPayments.includes(p)
          return (
            <button
              key={p}
              type="button"
              className={`btn--sm ${on ? '' : 'btn--ghost'}`}
              aria-pressed={on}
              onClick={() =>
                setFilterPayments((prev) =>
                  on ? prev.filter((x) => x !== p) : [...prev, p],
                )
              }
            >
              {t(`invoices.paymentStatus.${p}`)}
            </button>
          )
        })}
        </div>
      )}
      {list.length === 0 ? (
        <p className="hint">
          {t(
            view === 'trashed'
              ? 'invoices.noneTrash'
              : view === 'archived'
                ? 'invoices.noneArchived'
                : 'invoices.none',
          )}
        </p>
      ) : (
        <ul className="list">
          {list.map((i) => (
            <li key={i.id} className="invrow">
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => void openInvoice(i.id)}
              >
                {t('invoices.open')}
              </button>{' '}
              {i.series}/{i.year}/{i.number ?? '–'}{' '}
              <span className="muted">
                · {clientName(i.client_tag_id)} ·{' '}
                {t(`invoices.stateLabel.${i.state}`)} · {i.total} · sdi{' '}
                {t(`invoices.sdiStatus.${i.sdi_status}`)} ·{' '}
                {t(`invoices.paymentStatus.${i.payment_status}`)}
                {i.identificativo_sdi ? ` · ${i.identificativo_sdi}` : ''}
              </span>{' '}
              {view === 'trashed' ? (
                <>
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void rowAction(i.id, 'restore')}
                  >
                    {t('invoices.restore')}
                  </button>
                  {i.state === 'draft' && (
                    <button
                      type="button"
                      className="btn--sm btn--danger"
                      onClick={() => void deletePermanent(i.id)}
                    >
                      {t('invoices.deletePermanent')}
                    </button>
                  )}
                </>
              ) : view === 'archived' ? (
                <>
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void rowAction(i.id, 'unarchive')}
                  >
                    {t('invoices.unarchive')}
                  </button>
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void rowAction(i.id, 'trash')}
                  >
                    {t('invoices.trash')}
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void rowAction(i.id, 'archive')}
                  >
                    {t('invoices.archive')}
                  </button>
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void rowAction(i.id, 'trash')}
                  >
                    {t('invoices.trash')}
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {sel && (() => {
        // On a draft the persisted ``sel.number`` is null until SdI
        // transmit; the resolved progressive lives on preview.number
        // formatted as ``<sezionale>-<counter>`` (e.g. ``EXAMPLE-2``).
        // Strip the prefix to get the integer counter so the H2 reads
        // ``EXAMPLE/2026/2`` (the user-facing slash format) rather than
        // ``EXAMPLE/2026/–`` — the user specifically asked that the
        // would-be number show prominently in the heading.
        const counter =
          sel.number ?? preview?.number?.match(/(\d+)$/)?.[1] ?? '–'
        return (
        <div
          className="modal__backdrop"
          role="dialog"
          aria-modal="true"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeInvoice()
          }}
        >
        <div className="modal__panel">
          <div className="modal__head">
            <strong>
              {t('invoices.title')} {sel.series}/{sel.year}/{counter}
            </strong>
            <span className="modal__sp" />
            <button
              type="button"
              className="btn--ghost btn--sm"
              onClick={() => closeInvoice()}
            >
              {t('notes.close')}
            </button>
          </div>
          <div className="modal__body">
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
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => void downloadXml(sel.id)}
                >
                  {t('invoices.doc.xmlDownload')}
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
                      <div>{preview.issuer.legal_name}</div>
                      <div className="muted">
                        {preview.issuer.vat_number
                          ? `P.IVA ${preview.issuer.vat_number}`
                          : ''}
                        {preview.issuer.tax_code
                          ? ` · CF ${preview.issuer.tax_code}`
                          : ''}
                        {preview.issuer.tax_regime
                          ? ` · ${preview.issuer.tax_regime}`
                          : ''}
                      </div>
                      <div className="muted">
                        {[
                          preview.issuer.address,
                          preview.issuer.civic_number,
                          preview.issuer.postal_code,
                          preview.issuer.city,
                          preview.issuer.province,
                        ]
                          .filter(Boolean)
                          .join(' ')}
                      </div>
                      {/* Cedente contacts mirror what the document actually
                          carries: phone/email ride in FatturaPA <Contatti>
                          and PEC on the courtesy PDF, each gated by its
                          per-contact visibility toggle (hidden = not emitted,
                          so not shown here either). */}
                      {(() => {
                        const c = [
                          preview.issuer.show_phone && preview.issuer.phone
                            ? `${t('invoices.doc.tel')} ${preview.issuer.phone}`
                            : '',
                          preview.issuer.show_email && preview.issuer.email
                            ? preview.issuer.email
                            : '',
                          preview.issuer.show_pec && preview.issuer.pec
                            ? `${t('invoices.doc.pec')} ${preview.issuer.pec}`
                            : '',
                        ].filter(Boolean)
                        return c.length > 0 ? (
                          <div className="muted">{c.join(' · ')}</div>
                        ) : null
                      })()}
                    </>
                  ) : (
                    <div className="err">{t('invoices.doc.missing')}</div>
                  )}
                </div>
                <div>
                  <div className="muted">{t('invoices.doc.cessionario')}</div>
                  {preview.client ? (
                    <>
                      <div>{preview.client.legal_name}</div>
                      <div className="muted">
                        {preview.client.vat_number
                          ? `P.IVA ${preview.client.vat_number}`
                          : ''}
                        {preview.client.tax_code
                          ? ` · CF ${preview.client.tax_code}`
                          : ''}
                      </div>
                      <div className="muted">
                        {[
                          preview.client.address,
                          preview.client.civic_number,
                          preview.client.postal_code,
                          preview.client.city,
                          preview.client.province,
                        ]
                          .filter(Boolean)
                          .join(' ')}
                      </div>
                      <div className="muted">
                        {t('invoices.doc.sdi')}:{' '}
                        {preview.client.sdi_code ||
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
                  {Number(preview.totals.stamp_duty) > 0 && (
                    <div>
                      {t('invoices.doc.bollo')}: {preview.totals.stamp_duty} €
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
                    {t(`invoices.stateLabel.${preview.state}`)} · sdi{' '}
                    {t(`invoices.sdiStatus.${preview.sdi_status}`)}
                  </div>
                  <div className="muted">
                    {t('invoices.doc.sdiId')}:{' '}
                    {preview.identificativo_sdi || t('invoices.doc.none')}
                  </div>
                  <div className="muted">
                    {t('invoices.doc.conservation')}:{' '}
                    {t(`invoices.conservationStatus.${preview.conservation_status}`)}
                  </div>
                </div>
              </div>
              {preview.is_forfettario && preview.purpose && (
                <p className="hint docpanel__causale">{preview.purpose}</p>
              )}
              {notifs.length > 0 && (
                <div className="sdi-timeline">
                  <div className="muted">{t('invoices.sdi.timeline')}</div>
                  <ul className="list">
                    {notifs.map((n, idx) => (
                      <li key={`${n.kind}-${n.message_id ?? idx}`}>
                        <strong>{t(`invoices.sdiStatus.${n.kind}`)}</strong>
                        {' · '}
                        {n.received_at.slice(0, 19).replace('T', ' ')}
                        {n.esito ? ` · ${n.esito}` : ''}
                        {' · '}
                        <button
                          type="button"
                          className="sdi-timeline__act"
                          onClick={() => void showNotifXml(n.id)}
                        >
                          {t('invoices.sdi.viewXml')}
                        </button>
                        {' '}
                        <button
                          type="button"
                          className="sdi-timeline__act"
                          onClick={() => void downloadNotifXml(n.id, n.file_name)}
                        >
                          {t('invoices.sdi.downloadXml')}
                        </button>
                        {n.errors.length > 0 && (
                          <ul className="sdi-timeline__errors">
                            {n.errors.map((e, j) => (
                              <li key={j} className="err">
                                {e.codice ? `[${e.codice}] ` : ''}
                                {e.descrizione}
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {xml && (
                <div className="docpanel__xml" ref={xmlRef}>
                  <div className="docpanel__head">
                    <strong>{t('invoices.doc.xml')}</strong>
                    <span className="modal__sp" />
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      onClick={() => setXml(null)}
                    >
                      {t('invoices.doc.xmlClose')}
                    </button>
                  </div>
                  <pre className="xml" aria-label="FatturaPA XML">
                    {xml}
                  </pre>
                </div>
              )}
            </div>
          )}

          <div className="row">
            {/* Issuer and client are frozen at create_draft: changing
                either on an existing draft would silently rewire the
                per-client sezionale + the (issuer, series, year)
                counter underneath. To change them, delete this draft
                and create a fresh one (the choice lives on the "new
                invoice" form above the list). We render them as plain
                read-only text — a disabled dropdown is just visual
                clutter for a single immutable value. */}
            <label>
              {t('invoices.issuer')}
              <span className="ro-value">
                {profiles.find((p) => p.id === dIssuer)?.label ?? '—'}
              </span>
            </label>
            <label>
              {t('invoices.client')}
              <span className="ro-value">
                {clients.find((c) => c.id === dClient)?.name ?? '—'}
              </span>
            </label>
            {/* Document number (resolved). preview.number formats as
                ``<sezionale>-<counter>`` (e.g. ``EXAMPLE-2``); the
                sezionale itself is a client property, never an input.
                On a draft, ``Change number`` raises the counter
                directly (e.g. start from #2 when #1 was emitted on
                another system). The value + button sit on the same
                row as Issuer / Client and the button is inline next
                to the value — stacking it underneath split the
                identifier from its control and read as a layout bug. */}
            <label>
              {t('invoices.numberLabel')}
              <span
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                }}
              >
                <span
                  style={{
                    fontWeight: 700,
                    fontSize: '1.05rem',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {preview?.number ?? '—'}
                </span>
                {isDraft && !editingNum && (
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => {
                      setNewNextN(
                        preview?.number?.match(/(\d+)$/)?.[1] || '1',
                      )
                      setEditingNum(true)
                    }}
                  >
                    {t('invoices.changeNumber')}
                  </button>
                )}
              </span>
            </label>
          </div>
          {editingNum && (
            <div className="row">
              <label>
                {t('invoices.newNextNumber')}
                <input
                  type="number"
                  min={1}
                  value={newNextN}
                  onChange={(e) => setNewNextN(e.target.value)}
                />
              </label>
              <button
                type="button"
                className="btn--sm"
                onClick={() => void saveStartingNumber()}
              >
                {t('invoices.save')}
              </button>
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => setEditingNum(false)}
              >
                {t('invoices.cancel')}
              </button>
              <span className="hint">{t('invoices.changeNumberHint')}</span>
            </div>
          )}

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
                          value={lEdit.vat_nature}
                          placeholder="N2.2"
                          style={{ width: '4.5rem' }}
                          onChange={(e) =>
                            setLEdit({ ...lEdit, vat_nature: e.target.value })
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
                      <td>{ln.vat_nature ?? '—'}</td>
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
                                  vat_nature: ln.vat_nature ?? '',
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
                  value={lAdd.vat_nature}
                  style={{ width: '5rem' }}
                  onChange={(e) =>
                    setLAdd({ ...lAdd, vat_nature: e.target.value })
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
              <div className="periodbar">
                <span className="periodbar__label" style={{ minWidth: 'auto' }}>
                  {t('invoices.billGranularity')}
                </span>
                {(['task', 'project'] as BillGroup[]).map((g) => (
                  <button
                    key={g}
                    type="button"
                    className={'btn--sm' + (triGroup === g ? '' : ' btn--ghost')}
                    onClick={() => setTriGroup(g)}
                  >
                    {t(`invoices.billBy_${g}`)}
                  </button>
                ))}
              </div>
              <PeriodPicker
                period={triPeriod}
                anchor={triAnchor}
                onChange={(p, a) => {
                  setTriPeriod(p)
                  setTriAnchor(a)
                }}
              />
              {triPeriod === 'custom' && (
                <div className="row">
                  <label>
                    {t('invoices.periodFrom')}
                    <input
                      type="date"
                      value={triCustomFrom}
                      onChange={(e) => setTriCustomFrom(e.target.value)}
                    />
                  </label>
                  <label>
                    {t('invoices.periodTo')}
                    <input
                      type="date"
                      value={triCustomTo}
                      onChange={(e) => setTriCustomTo(e.target.value)}
                    />
                  </label>
                </div>
              )}
              {triLoaded && triRows.length === 0 && (
                <p className="hint">{t('invoices.noReport')}</p>
              )}
              {triRows.length > 0 && (
                <>
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th>
                          {/* Master select-all: tri-state (checked /
                              indeterminate / empty). Saves picking
                              every task or project row one by one. */}
                          <input
                            type="checkbox"
                            checked={triAllSelected}
                            disabled={triSelectableKeys.length === 0}
                            ref={(el) => {
                              if (el)
                                el.indeterminate =
                                  triSomeSelected && !triAllSelected
                            }}
                            onChange={toggleSelectAllRows}
                            // Three-state label so the indeterminate
                            // (dash) state, invisible to screen readers,
                            // is spelled out: all → deselect, some →
                            // select-all-(some), none → select all.
                            aria-label={
                              triAllSelected
                                ? t('invoices.deselectAll')
                                : triSomeSelected
                                  ? t('invoices.selectAllSome')
                                  : t('invoices.selectAll')
                            }
                            title={
                              triAllSelected
                                ? t('invoices.deselectAll')
                                : triSomeSelected
                                  ? t('invoices.selectAllSome')
                                  : t('invoices.selectAll')
                            }
                          />
                        </th>
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
          {/* Per-document overrides of payment metadata. The "empty"
              option shows the effective resolved value (invoice >
              client > issuer > TP02/MP05) so the user always sees what
              will actually be transmitted instead of a generic
              "inherit" label. Terms-days auto-materializes due-date
              when the latter is empty (computed server-side at save). */}
          {(() => {
            // Resolved defaults if the user leaves the override blank.
            const c = clients.find((x) => x.id === dClient)
            const isr = profiles.find((p) => p.id === dIssuer)
            const effCondizioni =
              c?.default_payment_conditions_code ||
              isr?.default_payment_conditions_code ||
              'TP02'
            const effModalita =
              c?.default_payment_method_code ||
              isr?.default_payment_method_code ||
              'MP05'
            const effTerms =
              c?.default_payment_terms_days ??
              isr?.default_payment_terms_days ??
              null
            const labelCond = (code: string) => {
              const row = INV_CONDIZIONI.find(([k]) => k === code)
              return row ? `${row[0]} - ${row[1]}` : code
            }
            const labelMod = (code: string) => {
              const row = INV_MODALITA.find(([k]) => k === code)
              return row ? `${row[0]} - ${row[1]}` : code
            }
            return (
          <div className="row">
            <label>
              {t('invoices.condizioni')}
              <select
                value={dCondizioni}
                disabled={!isDraft}
                onChange={(e) => dField(setDCondizioni)(e.target.value)}
              >
                <option value="">{labelCond(effCondizioni)}</option>
                {INV_CONDIZIONI.map(([code, lbl]) => (
                  <option key={code} value={code}>
                    {code} - {lbl}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('invoices.modalita')}
              <select
                value={dModalita}
                disabled={!isDraft}
                onChange={(e) => dField(setDModalita)(e.target.value)}
              >
                <option value="">{labelMod(effModalita)}</option>
                {INV_MODALITA.map(([code, lbl]) => (
                  <option key={code} value={code}>
                    {code} - {lbl}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('invoices.termsDays')}
              <input
                type="number"
                min={0}
                max={365}
                value={dTermsDays}
                placeholder={
                  effTerms != null ? String(effTerms) : t('invoices.inherit')
                }
                disabled={!isDraft}
                onChange={(e) => dField(setDTermsDays)(e.target.value)}
              />
            </label>
          </div>
            )
          })()}
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
                  className="btn--ghost"
                  onClick={() =>
                    void act(
                      api.POST('/invoices/{invoice_id}/trash', {
                        params: {
                          header: workspaceHeader(),
                          path: { invoice_id: sel.id },
                        },
                      }),
                    ).then(() => closeInvoice())
                  }
                >
                  {t('invoices.trash')}
                </button>
              </>
            )}
            {!isDraft && (
              <>
                {sel.state === 'rejected' && (
                  <button type="button" onClick={() => void reopen(sel.id)}>
                    {t('invoices.reopen')}
                  </button>
                )}
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
          </div>
        </div>
        </div>
        )
      })()}
    </section>
  )
}
