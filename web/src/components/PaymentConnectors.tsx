import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { authFetch, errMessage } from '../api/client'

// Inbound payment connectors (ADR-0051), org-facing surface. A connector turns
// a payment provider's webhooks into FatturaPA documents for ONE issuer
// profile, so it lives here as a third sub-card next to API keys and webhook
// endpoints rather than as a page of its own: the issuer is what gives the
// emitted document its cedente, and a connector detached from one would be a
// credential pointing at nothing.
//
// The management routes are brand new and src/api/schema.d.ts is GENERATED, so
// the typed client does not know them yet. Same escape hatch SdiSettings uses:
// untyped authFetch plus the hand-declared shapes below, narrowed to the fields
// this card actually renders. When the schema is regenerated these types are
// the diff to delete.

type Connector = {
  id: string
  issuer_profile_id: string
  provider: string
  label: string
  enabled: boolean
  invoice_mode: string
  credit_note_mode: string
  emission_event: string
  payment_sync_enabled: boolean
  series: string | null
  default_purpose: string | null
  // Pydantic serialises Numeric(5,2) as a JSON string, but a hand-declared type
  // must not bet the render on that: both shapes go through String().
  default_vat_rate: string | number | null
  default_payment_method_code: string | null
  amounts_include_vat: boolean
  revoked_at: string | null
  last_event_at: string | null
  // Never the key itself -- only whether the optional second factor is armed.
  has_api_key: boolean
  webhook_url: string
}

/** The only shape that ever carries plaintext credentials. ``signing_secret``
 * comes back empty from rotate-api-key (rotating one credential must not
 * re-expose the other) and ``api_key`` is null unless one was just minted. */
type ConnectorCreated = Connector & {
  signing_secret: string
  api_key: string | null
}

/** Closed vocabularies served by the backend instead of duplicated here, so
 * widening one (a new provider, a new emission event) is a backend change and
 * this card picks it up without a release. */
type Vocabulary = {
  providers: string[]
  automation_modes: string[]
  emission_events: string[]
  delivery_outcomes: string[]
}

type EventRow = {
  id: string
  provider_event_id: string
  event_type: string
  status: string
  attempt_count: number
  max_attempts: number
  created_at: string
  last_error: string | null
  error_detail: string | null
  invoice_id: string | null
  dry_run: boolean
  has_dry_run_xml: boolean
}

type DeliveryRow = {
  id: string
  outcome: string
  http_status: number
  provider_event_id: string | null
  signature_present: boolean
  api_key_present: boolean
  received_at: string
}

// Fallbacks used only if the vocabulary call fails: the card must stay usable
// (and must not silently offer an empty select) when one request out of two
// does not land. They mirror models/payment_connector.py.
const FALLBACK_PROVIDERS = ['stripe', 'mycelium']
const FALLBACK_MODES = ['transmit', 'draft', 'dry_run', 'off']
const FALLBACK_EMISSION_EVENTS = ['invoice.paid']

// The automation modes are a CLOSED fiscal vocabulary, so each one gets a real
// sentence rather than its slug. Unknown values (a backend that widened the set
// before this file did) fall back to the raw slug, which is honest, instead of
// rendering a missing-key placeholder.
const MODE_KEYS: Record<string, string> = {
  transmit: 'paymentConnectors.modeTransmit',
  draft: 'paymentConnectors.modeDraft',
  dry_run: 'paymentConnectors.modeDryRun',
  off: 'paymentConnectors.modeOff',
}

// FatturaPA 1.2 ModalitaPagamento, the closed SdI table. Mirrored from
// IssuerProfiles rather than shared: every card in this repo that offers a
// fiscal enum carries its own copy (see PERMISSIONS in IssuerApiKeys and
// EVENT_TYPES in WebhookEndpoints), and a shared constant module would be the
// only one of its kind. MP08 (carta di pagamento) is the honest value for a
// card processor, which is why it is worth offering per connector.
const MODALITA: ReadonlyArray<readonly [string, string]> = [
  ['MP01', 'contanti'],
  ['MP02', 'assegno'],
  ['MP03', 'assegno circolare'],
  ['MP05', 'bonifico'],
  ['MP07', 'bollettino bancario'],
  ['MP08', 'carta di pagamento'],
  ['MP12', 'RIBA'],
  ['MP13', 'MAV'],
  ['MP18', 'bollettino c/c postale'],
  ['MP19', 'SEPA Direct Debit'],
  ['MP20', 'SEPA DD CORE'],
  ['MP21', 'SEPA DD B2B'],
  ['MP23', 'PagoPA'],
]

/** authFetch plus the backend's {code, detail} envelope. The typed client
 * returns {data, error}; this raises instead, because every caller below is
 * already inside one try/catch that funnels into the card's single error line.
 * 204 (revoke, purge) carries no body and must not be parsed. */
async function send<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(path, init)
  if (!res.ok) throw new Error(errMessage(await res.json().catch(() => null)))
  if (res.status === 204) return undefined as unknown as T
  return (await res.json()) as T
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function stamp(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : '—'
}

/** The subset of connector settings this card edits, held as form strings.
 * The same fields appear at create and at edit, so they live in one shape and
 * one fragment instead of two drifting copies. */
type Defaults = {
  invoice_mode: string
  credit_note_mode: string
  emission_event: string
  default_vat_rate: string
  amounts_include_vat: boolean
  series: string
  default_purpose: string
  default_payment_method_code: string
}

const EMPTY_DEFAULTS: Defaults = {
  invoice_mode: 'transmit',
  credit_note_mode: 'transmit',
  emission_event: 'invoice.paid',
  default_vat_rate: '',
  amounts_include_vat: false,
  series: '',
  default_purpose: '',
  default_payment_method_code: '',
}

function defaultsOf(c: Connector): Defaults {
  return {
    invoice_mode: c.invoice_mode,
    credit_note_mode: c.credit_note_mode,
    emission_event: c.emission_event,
    default_vat_rate: c.default_vat_rate == null ? '' : String(c.default_vat_rate),
    amounts_include_vat: c.amounts_include_vat,
    series: c.series ?? '',
    default_purpose: c.default_purpose ?? '',
    default_payment_method_code: c.default_payment_method_code ?? '',
  }
}

/** Empty string -> null. The columns are nullable and "" is not "unset": a
 * zero-length codice destinatario or sezionale would be written as a real
 * value and ride into the XML. The VAT rate is sent as a STRING because a JSON
 * number is a float in almost every parser, and 22.00 must not make a round
 * trip through binary floating point on its way to Numeric(5,2). */
function defaultsBody(d: Defaults): Record<string, unknown> {
  return {
    invoice_mode: d.invoice_mode,
    credit_note_mode: d.credit_note_mode,
    emission_event: d.emission_event,
    default_vat_rate: d.default_vat_rate.trim() || null,
    amounts_include_vat: d.amounts_include_vat,
    series: d.series.trim() || null,
    default_purpose: d.default_purpose.trim() || null,
    default_payment_method_code: d.default_payment_method_code || null,
  }
}

/** The fiscal settings, shared by the create form and the per-connector edit
 * form. There is deliberately no codice-destinatario default: 0000000 cannot be
 * used to deliver, so a connector-wide value would only make invoices look
 * emittable. A recipient is addressable when the customer supplied a real code
 * or a PEC (a non-Italian one is addressed by the standard's XXXXXXX). */
function DefaultsFields({
  value,
  vocab,
  onChange,
}: {
  value: Defaults
  vocab: Vocabulary | null
  onChange: (next: Defaults) => void
}) {
  const { t } = useTranslation()
  const modes = vocab?.automation_modes ?? FALLBACK_MODES
  const emissionEvents = vocab?.emission_events ?? FALLBACK_EMISSION_EVENTS
  const modeLabel = (m: string) => (MODE_KEYS[m] ? t(MODE_KEYS[m]) : m)

  return (
    <>
      <div className="row">
        <label>
          {t('paymentConnectors.invoiceMode')}
          <select
            value={value.invoice_mode}
            onChange={(e) => onChange({ ...value, invoice_mode: e.target.value })}
          >
            {modes.map((m) => (
              <option key={m} value={m}>
                {modeLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('paymentConnectors.creditNoteMode')}
          <select
            value={value.credit_note_mode}
            onChange={(e) => onChange({ ...value, credit_note_mode: e.target.value })}
          >
            {modes.map((m) => (
              <option key={m} value={m}>
                {modeLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('paymentConnectors.emissionEvent')}
          <select
            value={value.emission_event}
            onChange={(e) => onChange({ ...value, emission_event: e.target.value })}
          >
            {emissionEvents.map((ev) => (
              <option key={ev} value={ev}>
                {ev}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="hint">{t('paymentConnectors.modeHint')}</p>
      <p className="hint">{t('paymentConnectors.emissionEventHint')}</p>
      <div className="row">
        <label>
          {t('paymentConnectors.vatRate')}
          <input
            type="number"
            min={0}
            max={100}
            step="0.01"
            value={value.default_vat_rate}
            onChange={(e) => onChange({ ...value, default_vat_rate: e.target.value })}
          />
        </label>
        <label>
          {t('paymentConnectors.series')}
          <input
            maxLength={20}
            value={value.series}
            onChange={(e) => onChange({ ...value, series: e.target.value })}
          />
        </label>
        <label>
          {t('paymentConnectors.paymentMethod')}
          <select
            value={value.default_payment_method_code}
            onChange={(e) =>
              onChange({ ...value, default_payment_method_code: e.target.value })
            }
          >
            <option value="">{t('paymentConnectors.inherit')}</option>
            {MODALITA.map(([code, lbl]) => (
              <option key={code} value={code}>
                {code} - {lbl}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="hint">{t('paymentConnectors.vatRateHint')}</p>
      <p className="hint">{t('paymentConnectors.seriesHint')}</p>
      <label className="row">
        <input
          type="checkbox"
          checked={value.amounts_include_vat}
          onChange={(e) => onChange({ ...value, amounts_include_vat: e.target.checked })}
        />
        {t('paymentConnectors.amountsIncludeVat')}
      </label>
      <p className="hint">{t('paymentConnectors.amountsIncludeVatHint')}</p>
      <div className="row">
        <label className="lbl--wide">
          {t('paymentConnectors.purpose')}
          <input
            maxLength={200}
            value={value.default_purpose}
            onChange={(e) => onChange({ ...value, default_purpose: e.target.value })}
          />
        </label>
      </div>
      <p className="hint">{t('paymentConnectors.purposeHint')}</p>
    </>
  )
}

/** The quarantine: events that arrived, verified, and could NOT become a valid
 * fiscal document. This is the operator's daily surface, so the stable slug
 * (client_billing_data_missing, parent_not_transmitted, ...) is shown verbatim:
 * it is the contract between the runner and the human, and paraphrasing it
 * would make a support conversation impossible.
 *
 * Two statuses are retryable, not one: ``needs_attention`` is the parked event
 * waiting for a human decision, ``dead`` is one that exhausted its attempts.
 * Both are shown -- a dead event with no surface would be a payment that
 * silently never became a document. */
function ConnectorEvents({
  profileId,
  connectorId,
}: {
  profileId: string
  connectorId: string
}) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<EventRow[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [status, setStatus] = useState<
    'no_billing_data' | 'needs_attention' | 'dead' | 'done'
  >('no_billing_data')

  useEffect(() => {
    let active = true
    void (async () => {
      // Switching the status filter re-enters the loading state, so the table
      // never shows the previous filter's rows under the new heading.
      setLoading(true)
      try {
        const data = await send<EventRow[]>(
          `/issuer-profiles/${profileId}/payment-connectors/${connectorId}` +
            `/events?status=${status}&limit=50`,
        )
        if (!active) return
        setRows(data)
      } catch (e) {
        if (active) setErr(message(e))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [profileId, connectorId, status, tick])

  async function onDownloadXml(eventId: string) {
    // authFetch, not a plain link: the route needs the tenant + auth headers,
    // and the sandboxed viewer cannot follow a download the page starts itself.
    setErr(null)
    try {
      const res = await authFetch(
        `/issuer-profiles/${profileId}/payment-connectors/${connectorId}` +
          `/events/${eventId}/dry-run-xml`,
      )
      if (!res.ok) throw new Error(String(res.status))
      const xml = await res.text()
      const url = URL.createObjectURL(new Blob([xml], { type: 'application/xml' }))
      const a = document.createElement('a')
      a.href = url
      a.download = `dryrun-${eventId}.xml`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setErr(message(e))
    }
  }

  async function onRetry(eventId: string) {
    setErr(null)
    try {
      await send<EventRow>(
        `/issuer-profiles/${profileId}/payment-connectors/${connectorId}` +
          `/events/${eventId}/retry`,
        { method: 'POST' },
      )
      setTick((n) => n + 1)
    } catch (e) {
      setErr(message(e))
    }
  }

  return (
    <div className="card card--running">
      <h4>{t('paymentConnectors.quarantine')}</h4>
      <p className="hint">{t('paymentConnectors.quarantineHint')}</p>
      <div className="row">
        <button
          type="button"
          className={status === 'no_billing_data' ? 'btn--sm' : 'btn--sm btn--ghost'}
          onClick={() => setStatus('no_billing_data')}
        >
          {t('paymentConnectors.statusNoBillingData')}
        </button>
        <button
          type="button"
          className={status === 'needs_attention' ? 'btn--sm' : 'btn--sm btn--ghost'}
          onClick={() => setStatus('needs_attention')}
        >
          {t('paymentConnectors.statusNeedsAttention')}
        </button>
        <button
          type="button"
          className={status === 'dead' ? 'btn--sm' : 'btn--sm btn--ghost'}
          onClick={() => setStatus('dead')}
        >
          {t('paymentConnectors.statusDead')}
        </button>
        <button
          type="button"
          className={status === 'done' ? 'btn--sm' : 'btn--sm btn--ghost'}
          onClick={() => setStatus('done')}
        >
          {t('paymentConnectors.statusDone')}
        </button>
      </div>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}
      {!loading && rows.length === 0 && (
        <p className="ok">{t('paymentConnectors.quarantineEmpty')}</p>
      )}
      {!loading && rows.length > 0 && (
        <table className="list">
          <thead>
            <tr>
              <th>{t('paymentConnectors.eventType')}</th>
              <th>{t('paymentConnectors.providerEventId')}</th>
              <th>{t('paymentConnectors.errorSlug')}</th>
              <th>{t('paymentConnectors.attempts')}</th>
              <th>{t('paymentConnectors.createdAt')}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>
                  <code>{r.event_type}</code>
                </td>
                <td>
                  <code>{r.provider_event_id}</code>
                </td>
                <td>
                  <code>{r.last_error ?? '—'}</code>
                  {/* The detail names WHICH fiscal datum is missing, which is
                      the difference between "fix something" and "fix this". */}
                  {r.error_detail && <div className="muted">{r.error_detail}</div>}
                </td>
                <td>
                  {r.attempt_count}/{r.max_attempts}
                </td>
                <td>{stamp(r.created_at)}</td>
                <td>
                  {/* In shadow mode the XML is the deliverable: this is the
                      artefact you diff against the incumbent provider. */}
                  {r.has_dry_run_xml && (
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() => void onDownloadXml(r.id)}
                    >
                      {t('paymentConnectors.downloadXml')}
                    </button>
                  )}
                  {r.status !== 'done' && (
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      onClick={() => void onRetry(r.id)}
                    >
                      {t('paymentConnectors.retry')}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

/** The refusal ledger. An accepted event is already visible as an event row;
 * what has no other trace is a delivery we turned away (bad signature, revoked
 * connector, malformed body), and that is exactly the case where the provider
 * insists it delivered. */
function ConnectorDeliveries({
  profileId,
  connectorId,
}: {
  profileId: string
  connectorId: string
}) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<DeliveryRow[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const data = await send<DeliveryRow[]>(
          `/issuer-profiles/${profileId}/payment-connectors/${connectorId}` +
            `/deliveries?refused_only=true&limit=50`,
        )
        if (!active) return
        setRows(data)
      } catch (e) {
        if (active) setErr(message(e))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [profileId, connectorId])

  return (
    <div className="card card--running">
      <h4>{t('paymentConnectors.deliveries')}</h4>
      <p className="hint">{t('paymentConnectors.deliveriesHint')}</p>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}
      {!loading && rows.length === 0 && (
        <p className="ok">{t('paymentConnectors.deliveriesEmpty')}</p>
      )}
      {rows.length > 0 && (
        <table className="list">
          <thead>
            <tr>
              <th>{t('paymentConnectors.outcome')}</th>
              <th>{t('paymentConnectors.httpStatus')}</th>
              <th>{t('paymentConnectors.providerEventId')}</th>
              <th>{t('paymentConnectors.receivedAt')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.id}>
                <td>
                  <code>{d.outcome}</code>
                </td>
                <td>{d.http_status}</td>
                <td>
                  <code>{d.provider_event_id ?? '—'}</code>
                </td>
                <td>{stamp(d.received_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export function PaymentConnectors({ profileId }: { profileId: string }) {
  const { t } = useTranslation()
  const base = `/issuer-profiles/${profileId}/payment-connectors`

  const [rows, setRows] = useState<Connector[]>([])
  const [vocab, setVocab] = useState<Vocabulary | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const [showForm, setShowForm] = useState(false)
  const [label, setLabel] = useState('')
  const [provider, setProvider] = useState('stripe')
  const [signingSecret, setSigningSecret] = useState('')
  const [withApiKey, setWithApiKey] = useState(false)
  const [form, setForm] = useState<Defaults>(EMPTY_DEFAULTS)

  const [editing, setEditing] = useState<string | null>(null)
  const [editLabel, setEditLabel] = useState('')
  const [editForm, setEditForm] = useState<Defaults>(EMPTY_DEFAULTS)

  // Held in state only after a successful create/rotate; never persisted,
  // cleared on dismiss and on remount.
  const [created, setCreated] = useState<ConnectorCreated | null>(null)
  // Which copy button last succeeded, so several of them can share the 2s "OK"
  // flag without all of them lighting up at once.
  const [copied, setCopied] = useState<string | null>(null)

  const [showEvents, setShowEvents] = useState<string | null>(null)
  const [showDeliveries, setShowDeliveries] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const data = await send<Connector[]>(base)
        if (!active) return
        setRows(data)
      } catch (e) {
        if (active) setErr(message(e))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [base, tick])

  // The vocabulary is immutable for the life of the card, so it is fetched once
  // and NOT tied to the reload counter: a failure here degrades to the
  // fallbacks above rather than to an error line, because it must not make the
  // connector list look broken.
  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const data = await send<Vocabulary>('/payment-connectors/vocabulary')
        if (active) setVocab(data)
      } catch {
        /* fall back to the mirrored vocabularies */
      }
    })()
    return () => {
      active = false
    }
  }, [])

  function reload() {
    setErr(null)
    setTick((n) => n + 1)
  }

  async function copy(text: string, token: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(token)
      window.setTimeout(() => setCopied((cur) => (cur === token ? null : cur)), 2000)
    } catch {
      /* clipboard may be blocked */
    }
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    try {
      const data = await send<ConnectorCreated>(
        base,
        jsonInit('POST', {
          label: label.trim(),
          provider,
          // Empty means "mint one", which is only correct for the native
          // contract; the form makes the field required for a vendor provider
          // so a connector can never be born with a secret its provider has
          // never heard of.
          signing_secret: signingSecret.trim() || null,
          with_api_key: withApiKey,
          // Always born disabled, whatever the modes say: the operator still
          // has to paste the webhook URL into the provider and re-read the
          // fiscal defaults before anything is emitted in the org's name.
          enabled: false,
          ...defaultsBody(form),
        }),
      )
      setCreated(data)
      setShowForm(false)
      setLabel('')
      setSigningSecret('')
      setWithApiKey(false)
      setForm(EMPTY_DEFAULTS)
      reload()
    } catch (e2) {
      setErr(message(e2))
    }
  }

  /** Returns whether the write landed, so a caller can keep its form open (and
   * the operator's typing in it) when the server refused. */
  async function patch(connectorId: string, body: Record<string, unknown>): Promise<boolean> {
    setErr(null)
    try {
      await send<Connector>(`${base}/${connectorId}`, jsonInit('PATCH', body))
      reload()
      return true
    } catch (e) {
      setErr(message(e))
      return false
    }
  }

  async function onToggleEnabled(c: Connector) {
    // Enabling a connector whose modes say "transmit" starts filing real fiscal
    // documents with SdI on the next webhook, with no further human step. That
    // is worth one confirmation; disabling is always safe (events keep being
    // recorded, nothing is emitted).
    const arms =
      !c.enabled && (c.invoice_mode === 'transmit' || c.credit_note_mode === 'transmit')
    if (arms && !window.confirm(t('paymentConnectors.enableTransmitConfirm'))) return
    await patch(c.id, { enabled: !c.enabled })
  }

  function startEdit(c: Connector) {
    setErr(null)
    setEditing(c.id)
    setEditLabel(c.label)
    setEditForm(defaultsOf(c))
  }

  async function onSaveEdit(e: FormEvent, connectorId: string) {
    e.preventDefault()
    const ok = await patch(connectorId, {
      label: editLabel.trim(),
      ...defaultsBody(editForm),
    })
    if (ok) setEditing(null)
  }

  async function onRotateSigning(c: Connector) {
    // A prompt, not a confirm: for Stripe the new secret is not ours to choose
    // -- it is the whsec_... the dashboard shows after a roll -- so the operator
    // must be able to paste it. Cancel returns null and aborts; an empty answer
    // mints one, which only makes sense for the native contract.
    const input = window.prompt(t('paymentConnectors.rotateSigningPrompt'), '')
    if (input === null) return
    setErr(null)
    // The secret travels in the BODY, never in the query string: a query is
    // logged verbatim by ordinary access logging and reaches proxy logs, APM
    // traces and browser history. An empty answer sends null, which mints one.
    try {
      const data = await send<ConnectorCreated>(
        `${base}/${c.id}/rotate-signing-secret`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ signing_secret: input.trim() || null }),
        },
      )
      setCreated(data)
      reload()
    } catch (e) {
      setErr(message(e))
    }
  }

  async function onRotateApiKey(connectorId: string) {
    if (!window.confirm(t('paymentConnectors.rotateApiKeyConfirm'))) return
    setErr(null)
    try {
      const data = await send<ConnectorCreated>(`${base}/${connectorId}/rotate-api-key`, {
        method: 'POST',
      })
      setCreated(data)
      reload()
    } catch (e) {
      setErr(message(e))
    }
  }

  async function onClearApiKey(connectorId: string) {
    if (!window.confirm(t('paymentConnectors.clearApiKeyConfirm'))) return
    setErr(null)
    try {
      await send<Connector>(`${base}/${connectorId}/api-key`, { method: 'DELETE' })
      reload()
    } catch (e) {
      setErr(message(e))
    }
  }

  async function onRevoke(connectorId: string) {
    if (!window.confirm(t('paymentConnectors.revokeConfirm'))) return
    setErr(null)
    try {
      await send<void>(`${base}/${connectorId}`, { method: 'DELETE' })
      reload()
    } catch (e) {
      setErr(message(e))
    }
  }

  // Hard-delete an ALREADY-revoked connector so the dead row leaves the list.
  async function onPurge(connectorId: string) {
    if (!window.confirm(t('paymentConnectors.purgeConfirm'))) return
    setErr(null)
    try {
      await send<void>(`${base}/${connectorId}?hard=true`, { method: 'DELETE' })
      reload()
    } catch (e) {
      setErr(message(e))
    }
  }

  const providers = vocab?.providers ?? FALLBACK_PROVIDERS
  const modeLabel = (m: string) => (MODE_KEYS[m] ? t(MODE_KEYS[m]) : m)

  return (
    <div className="card card--running">
      <h3>{t('paymentConnectors.title')}</h3>
      <p className="hint">{t('paymentConnectors.hint')}</p>
      {err && <p className="err">{err}</p>}
      {loading && <p>{t('home.loading')}</p>}

      {created && (
        <div className="field">
          <p className="err">{t('paymentConnectors.secretWarning')}</p>
          {/* rotate-api-key answers with an empty signing secret on purpose:
              rotating one credential must never re-expose the other. */}
          {created.signing_secret && (
            <>
              <span>{t('paymentConnectors.secretShown')}</span>
              <textarea
                readOnly
                value={created.signing_secret}
                rows={2}
                onFocus={(e) => e.currentTarget.select()}
                style={{ width: '100%', fontFamily: 'monospace' }}
              />
              <div className="row">
                <button
                  type="button"
                  className="btn--sm"
                  onClick={() => void copy(created.signing_secret, 'secret')}
                >
                  {copied === 'secret' ? 'OK' : t('paymentConnectors.copy')}
                </button>
              </div>
            </>
          )}
          {created.api_key && (
            <>
              <span>{t('paymentConnectors.apiKeyShown')}</span>
              <textarea
                readOnly
                value={created.api_key}
                rows={2}
                onFocus={(e) => e.currentTarget.select()}
                style={{ width: '100%', fontFamily: 'monospace' }}
              />
              <div className="row">
                <button
                  type="button"
                  className="btn--sm"
                  onClick={() => void copy(created.api_key ?? '', 'apikey')}
                >
                  {copied === 'apikey' ? 'OK' : t('paymentConnectors.copy')}
                </button>
              </div>
            </>
          )}
          <p className="hint">
            {t('paymentConnectors.webhookUrl')}: <code>{created.webhook_url}</code>
          </p>
          <div className="row">
            <button
              type="button"
              className="btn--sm"
              onClick={() => void copy(created.webhook_url, 'created-url')}
            >
              {copied === 'created-url' ? 'OK' : t('paymentConnectors.copyUrl')}
            </button>
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => setCreated(null)}
            >
              {t('paymentConnectors.dismiss')}
            </button>
          </div>
        </div>
      )}

      {!loading && rows.length === 0 && !created && (
        <p className="muted">{t('paymentConnectors.empty')}</p>
      )}

      {rows.length > 0 && (
        <ul className="list">
          {rows.map((c) => (
            <li key={c.id}>
              <strong>{c.label}</strong> <code>{c.provider}</code>{' '}
              <span className={c.enabled ? 'tag' : 'tag tag--muted'}>
                {c.enabled ? t('paymentConnectors.enabled') : t('paymentConnectors.disabled')}
              </span>{' '}
              <span className="muted">
                {t('paymentConnectors.invoiceMode')}: {modeLabel(c.invoice_mode)} |{' '}
                {t('paymentConnectors.creditNoteMode')}: {modeLabel(c.credit_note_mode)}
              </span>{' '}
              <span className="muted">
                | {t('paymentConnectors.lastEvent')}:{' '}
                {c.last_event_at ? stamp(c.last_event_at) : t('paymentConnectors.never')}
              </span>{' '}
              {c.has_api_key && (
                <span className="muted">| {t('paymentConnectors.apiKeyArmed')}</span>
              )}{' '}
              {c.revoked_at ? (
                <>
                  <em>({t('paymentConnectors.revoked')})</em>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void onPurge(c.id)}
                  >
                    {t('paymentConnectors.delete')}
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => void onToggleEnabled(c)}
                  >
                    {c.enabled
                      ? t('paymentConnectors.disable')
                      : t('paymentConnectors.enable')}
                  </button>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => (editing === c.id ? setEditing(null) : startEdit(c))}
                  >
                    {t('paymentConnectors.edit')}
                  </button>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void onRotateSigning(c)}
                  >
                    {t('paymentConnectors.rotateSigning')}
                  </button>{' '}
                  <button
                    type="button"
                    className="btn--sm btn--ghost"
                    onClick={() => void onRotateApiKey(c.id)}
                  >
                    {t('paymentConnectors.rotateApiKey')}
                  </button>{' '}
                  {c.has_api_key && (
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      onClick={() => void onClearApiKey(c.id)}
                    >
                      {t('paymentConnectors.clearApiKey')}
                    </button>
                  )}{' '}
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void onRevoke(c.id)}
                  >
                    {t('paymentConnectors.revoke')}
                  </button>
                </>
              )}{' '}
              {/* History stays reachable on a revoked connector: that is when
                  somebody is reconciling what it did before it was stopped. */}
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => setShowEvents(showEvents === c.id ? null : c.id)}
              >
                {t('paymentConnectors.quarantine')}
              </button>{' '}
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => setShowDeliveries(showDeliveries === c.id ? null : c.id)}
              >
                {t('paymentConnectors.deliveries')}
              </button>
              {/* The address the operator pastes into the provider dashboard. */}
              <div className="row">
                <code>{c.webhook_url}</code>
                <button
                  type="button"
                  className="btn--sm"
                  onClick={() => void copy(c.webhook_url, `url:${c.id}`)}
                >
                  {copied === `url:${c.id}` ? 'OK' : t('paymentConnectors.copyUrl')}
                </button>
              </div>
              {/* The backend builds this from the connector base URL (or the
                  frontend one); with neither configured it degrades to a bare
                  path, which pasted into a provider dashboard is unreachable
                  and fails silently -- no delivery, no refusal row, nothing to
                  read. Say so instead of handing over a broken address. */}
              {!/^https?:\/\//i.test(c.webhook_url) && (
                <p className="err">{t('paymentConnectors.webhookUrlUnset')}</p>
              )}
              <p className="hint">{t('paymentConnectors.webhookUrlHint')}</p>
              {editing === c.id && (
                <form
                  onSubmit={(e) => void onSaveEdit(e, c.id)}
                  className="card card--running"
                >
                  <h4>{t('paymentConnectors.edit')}</h4>
                  <div className="row">
                    <label className="lbl--wide">
                      {t('paymentConnectors.label')}
                      <input
                        required
                        maxLength={120}
                        value={editLabel}
                        onChange={(e) => setEditLabel(e.target.value)}
                      />
                    </label>
                  </div>
                  <DefaultsFields value={editForm} vocab={vocab} onChange={setEditForm} />
                  <div className="row">
                    <button type="submit" className="btn--sm">
                      {t('paymentConnectors.save')}
                    </button>
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      onClick={() => setEditing(null)}
                    >
                      {t('paymentConnectors.cancel')}
                    </button>
                  </div>
                </form>
              )}
              {showEvents === c.id && (
                <ConnectorEvents profileId={profileId} connectorId={c.id} />
              )}
              {showDeliveries === c.id && (
                <ConnectorDeliveries profileId={profileId} connectorId={c.id} />
              )}
            </li>
          ))}
        </ul>
      )}

      {!showForm && (
        <button type="button" className="btn--sm" onClick={() => setShowForm(true)}>
          {t('paymentConnectors.create')}
        </button>
      )}

      {showForm && (
        <form onSubmit={(e) => void onCreate(e)}>
          <div className="row">
            <label>
              {t('paymentConnectors.label')}
              <input
                required
                maxLength={120}
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
            </label>
            <label>
              {t('paymentConnectors.provider')}
              <select value={provider} onChange={(e) => setProvider(e.target.value)}>
                {providers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="hint">{t('paymentConnectors.labelHint')}</p>
          <div className="row">
            <label className="lbl--wide">
              {t('paymentConnectors.signingSecret')}
              {/* Required for every provider EXCEPT our own contract: a Stripe
                  connector holding a secret Stripe never issued would refuse
                  every delivery with signature_invalid, and the operator would
                  see an empty invoice list with no error anywhere. */}
              <input
                required={provider !== 'mycelium'}
                maxLength={200}
                autoComplete="off"
                spellCheck={false}
                placeholder="whsec_…"
                value={signingSecret}
                onChange={(e) => setSigningSecret(e.target.value)}
              />
            </label>
          </div>
          <p className="hint">{t('paymentConnectors.signingSecretHint')}</p>
          <label className="row">
            <input
              type="checkbox"
              checked={withApiKey}
              onChange={(e) => setWithApiKey(e.target.checked)}
            />
            {t('paymentConnectors.withApiKey')}
          </label>
          <p className="hint">{t('paymentConnectors.withApiKeyHint')}</p>
          <DefaultsFields value={form} vocab={vocab} onChange={setForm} />
          <div className="row">
            <button type="submit" className="btn--sm">
              {t('paymentConnectors.confirmCreate')}
            </button>
            <button
              type="button"
              className="btn--sm btn--ghost"
              onClick={() => setShowForm(false)}
            >
              {t('paymentConnectors.cancel')}
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
