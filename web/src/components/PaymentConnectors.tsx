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
  refund_event: string
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
  /** The events to enable in the provider alongside that URL, derived by the
   * backend from THIS connector's settings. Never hard-coded here: the answer
   * changes with the configuration, and a checklist copied into the SPA would
   * keep recommending yesterday's events after a switch is flipped. */
  subscription: SubscriptionEvent[]
}

/** One event the provider has to be told to deliver. ``purpose`` is a stable
 * key and the wording lives in the translations, so the backend never ships
 * user-facing prose in one language. */
type SubscriptionEvent = {
  event_type: string
  purpose: string
  required: boolean
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
  refund_events: string[]
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
  provider_customer_id: string | null
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
const FALLBACK_REFUND_EVENTS = ['refund.created', 'charge.refunded']

/** Why each subscribed event is needed. A closed set mirrored from
 * ``EVENT_PURPOSES`` in the backend; an unknown value renders a neutral line
 * rather than a blank, so widening the set server-side is never a silent gap. */
const PURPOSE_KEYS: Record<string, string> = {
  emission: 'paymentConnectors.purposeEmission',
  customer: 'paymentConnectors.purposeCustomer',
  credit_note: 'paymentConnectors.purposeCreditNote',
  payment_sync: 'paymentConnectors.purposePaymentSync',
}

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
  refund_event: string
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
  refund_event: 'refund.created',
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
    refund_event: c.refund_event,
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
    refund_event: d.refund_event,
    default_vat_rate: d.default_vat_rate.trim() || null,
    amounts_include_vat: d.amounts_include_vat,
    series: d.series.trim() || null,
    default_purpose: d.default_purpose.trim() || null,
    default_payment_method_code: d.default_payment_method_code || null,
  }
}

/** Floor enforced by the API on a supplied signing secret. Mirrored here so the
 * form refuses before the round trip; the API is still the authority. */
const MIN_SIGNING_SECRET = 16

/** A credential input that is not readable over a shoulder.
 *
 * Masked by default with an explicit reveal, because a signing secret is the
 * entire authority of a public unauthenticated endpoint and these forms get
 * filled in on shared screens. Reveal stays available: a mistyped secret is
 * indistinguishable from a correct one until deliveries start failing, so
 * "check what I pasted" has to be possible.
 */
function MaskedInput({
  value,
  onChange,
  required,
  minLength,
  placeholder,
}: {
  value: string
  onChange: (next: string) => void
  required?: boolean
  minLength?: number
  placeholder?: string
}) {
  const { t } = useTranslation()
  const [shown, setShown] = useState(false)
  return (
    <span className="row">
      <input
        type={shown ? 'text' : 'password'}
        required={required}
        minLength={minLength}
        maxLength={200}
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <button type="button" className="btn--sm btn--ghost" onClick={() => setShown(!shown)}>
        {shown ? t('paymentConnectors.hide') : t('paymentConnectors.show')}
      </button>
    </span>
  )
}

/** A credential shown exactly once, at the moment it is minted.
 *
 * It has to be readable (the sender needs it) without being on screen by
 * default: copy works while masked, so the common path never displays it at
 * all. */
function SecretReveal({
  label,
  value,
  token,
  copy,
  copied,
}: {
  label: string
  value: string
  token: string
  copy: (text: string, token: string) => void
  copied: string | null
}) {
  const { t } = useTranslation()
  const [shown, setShown] = useState(false)
  return (
    <div className="field">
      <span>{label}</span>
      <textarea
        readOnly
        value={shown ? value : '•'.repeat(Math.min(value.length, 48))}
        rows={2}
        onFocus={(e) => shown && e.currentTarget.select()}
        style={{ width: '100%', fontFamily: 'monospace' }}
      />
      <div className="row">
        <button type="button" className="btn--sm" onClick={() => copy(value, token)}>
          {copied === token ? 'OK' : t('paymentConnectors.copy')}
        </button>
        <button type="button" className="btn--sm btn--ghost" onClick={() => setShown(!shown)}>
          {shown ? t('paymentConnectors.hide') : t('paymentConnectors.show')}
        </button>
      </div>
    </div>
  )
}

/** What to do in the provider's dashboard, for THIS connector.
 *
 * The list of events is the part an operator cannot derive and cannot verify:
 * subscribing too few is silent (documents that never appear, refunds that
 * never reverse) and subscribing the wrong refund announcement files the same
 * refund twice. It is therefore served by the backend, generated from the
 * mapper that will receive the traffic, rather than transcribed here.
 *
 * Open by default until the connector has actually received something: that is
 * exactly the window where the instructions matter, and it gets out of the way
 * on its own afterwards.
 */
function SetupGuide({
  connector,
  copy,
  copied,
}: {
  connector: Connector
  copy: (text: string, token: string) => void
  copied: string | null
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(connector.last_event_at === null)
  const events = connector.subscription
  const token = `events:${connector.id}`
  // On our own contract there is no dashboard and no vendor-issued secret: the
  // counterpart is whoever implements the contract, and the key is ours to
  // hand out. Two of the four steps are therefore a different instruction, not
  // the same instruction with the word "provider" swapped.
  const native = connector.provider === 'mycelium'

  return (
    <div className="card card--quiet">
      <button type="button" className="btn--sm btn--ghost" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} {t('paymentConnectors.setupTitle', { provider: connector.provider })}
      </button>
      {open && (
        <>
          <ol className="setup-steps">
            <li>
              {native
                ? t('paymentConnectors.setupStepEndpointNative')
                : t('paymentConnectors.setupStepEndpoint')}
            </li>
            <li>
              {t('paymentConnectors.setupStepEvents')}
              {events.length > 0 && (
                <>
                  {' '}
                  <button
                    type="button"
                    className="btn--sm"
                    onClick={() => copy(events.map((e) => e.event_type).join('\n'), token)}
                  >
                    {copied === token ? 'OK' : t('paymentConnectors.copyEvents')}
                  </button>
                  <ul className="setup-events">
                    {events.map((e) => (
                      <li key={e.event_type}>
                        <code>{e.event_type}</code>{' '}
                        <span className="hint">
                          {t(PURPOSE_KEYS[e.purpose] ?? 'paymentConnectors.purposeUnknown')}
                        </span>
                        {!e.required && (
                          <span className="hint"> {t('paymentConnectors.purposeOptional')}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </li>
            <li>
              {native
                ? t('paymentConnectors.setupStepSecretNative')
                : t('paymentConnectors.setupStepSecret')}
            </li>
            <li>{t('paymentConnectors.setupStepDryRun')}</li>
          </ol>
          {/* The one instruction that is a refusal rather than a step. Stripe's
              event picker makes "select all" a single click, and the two refund
              announcements describe the SAME refund without deduplicating
              against each other. */}
          {connector.provider === 'stripe' && (
            <p className="hint">
              {t('paymentConnectors.setupNoExtraEvents', { refundEvent: connector.refund_event })}
            </p>
          )}
        </>
      )}
    </div>
  )
}

/** The fiscal settings, shared by the create form and the per-connector edit
 * form. There is deliberately no codice-destinatario default: 0000000 cannot be
 * used to deliver, so a connector-wide value would only make invoices look
 * emittable. A recipient is addressable when the customer supplied a real code
 * or a PEC (a non-Italian one is addressed by the standard's XXXXXXX). */
function DefaultsFields({
  value,
  vocab,
  provider,
  onChange,
}: {
  value: Defaults
  vocab: Vocabulary | null
  /** Which dialect this connector speaks. The event selectors below are a
   * VENDOR problem: our own contract defines exactly one event per outcome, so
   * offering a choice there would be a control that changes nothing. */
  provider: string
  onChange: (next: Defaults) => void
}) {
  const { t } = useTranslation()
  const modes = vocab?.automation_modes ?? FALLBACK_MODES
  const emissionEvents = vocab?.emission_events ?? FALLBACK_EMISSION_EVENTS
  const refundEvents = vocab?.refund_events ?? FALLBACK_REFUND_EVENTS
  const native = provider === 'mycelium'
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
        {!native && (
          <>
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
            {/* One of the two refund announcements, never both: see the hint. */}
            <label>
              {t('paymentConnectors.refundEvent')}
              <select
                value={value.refund_event}
                onChange={(e) => onChange({ ...value, refund_event: e.target.value })}
              >
                {refundEvents.map((ev) => (
                  <option key={ev} value={ev}>
                    {ev}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>
      <p className="hint">{t('paymentConnectors.modeHint')}</p>
      {native ? (
        <p className="hint">{t('paymentConnectors.nativeEventsHint')}</p>
      ) : (
        <>
          <p className="hint">{t('paymentConnectors.emissionEventHint')}</p>
          <p className="hint">{t('paymentConnectors.refundEventHint')}</p>
        </>
      )}
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
  const [msg, setMsg] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [status, setStatus] = useState<
    'no_billing_data' | 'needs_attention' | 'ignored' | 'dead' | 'done'
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

  async function onAssignClient(r: EventRow) {
    // The manual half of "the customer sent their billing data late". When the
    // data arrives outside the provider there is nothing tying the mycelium
    // client to the provider's customer id, so Retry alone can never unblock
    // the payment -- it re-derives the counterpart from the frozen payload.
    if (!r.provider_customer_id) return
    const tagId = window.prompt(
      t('paymentConnectors.assignPrompt', { customer: r.provider_customer_id }),
      '',
    )
    if (tagId === null || !tagId.trim()) return
    setErr(null)
    setMsg(null)
    try {
      const out = await send<{ rearmed: number }>(
        `/issuer-profiles/${profileId}/payment-connectors/${connectorId}/assign-customer`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider_customer_id: r.provider_customer_id,
            client_tag_id: tagId.trim(),
          }),
        },
      )
      setMsg(t('paymentConnectors.assignDone', { count: out.rearmed }))
      setTick((n) => n + 1)
    } catch (e) {
      setErr(message(e))
    }
  }

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

  async function onPromote(eventId: string) {
    // Confirmed because it converts a comparison artefact into a document that
    // can be filed in the workspace's name: the opposite of the shadow run's
    // whole promise, and the one action here that is not reversible by
    // discarding.
    if (!window.confirm(t('paymentConnectors.promoteConfirm'))) return
    setErr(null)
    setMsg(null)
    try {
      await send<{ invoice_id: string }>(
        `/issuer-profiles/${profileId}/payment-connectors/${connectorId}` +
          `/events/${eventId}/promote`,
        { method: 'POST' },
      )
      setMsg(t('paymentConnectors.promoted'))
      setTick((n) => n + 1)
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
        {/* Recognised, verified, and deliberately not acted on: the refund
            announcement this connector does not listen to, an event type the
            mapper has no rule for. Reachable because "nothing happened" needs
            somewhere to be read -- an operator who subscribed the other refund
            event finds the reason here and nowhere else. */}
        <button
          type="button"
          className={status === 'ignored' ? 'btn--sm' : 'btn--sm btn--ghost'}
          onClick={() => setStatus('ignored')}
        >
          {t('paymentConnectors.statusIgnored')}
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
      {msg && <p className="ok">{msg}</p>}
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
                  {r.provider_customer_id && (
                    <div className="muted">
                      <code>{r.provider_customer_id}</code>
                    </div>
                  )}
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
                  {/* The other exit from a parallel run. Discard throws every
                      shadow away; this promotes ONE, for the payment the
                      incumbent provider did not invoice after all. It moves the
                      claim with the document, so a later redelivery cannot
                      compose a second invoice for the same money, and it
                      refuses if a real document already covers it. */}
                  {r.has_dry_run_xml && (
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() => void onPromote(r.id)}
                    >
                      {t('paymentConnectors.promote')}
                    </button>
                  )}
                  {r.status === 'no_billing_data' && r.provider_customer_id && (
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() => void onAssignClient(r)}
                    >
                      {t('paymentConnectors.assignClient')}
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
  // Only meaningful for the native contract; a vendor provider always
  // supplies its own, so the vendor form forces this false on switch.
  const [mintSecret, setMintSecret] = useState(true)
  // Which connector's signing secret is being rotated, and the value typed
  // for it. Held here rather than in a prompt so it can be masked.
  const [rotating, setRotating] = useState<string | null>(null)
  const [rotateSecret, setRotateSecret] = useState('')
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

  // Minting is a native-contract option only, so the choice is DERIVED
  // rather than remembered: switching the provider to a vendor cannot leave
  // a stale "mint one" behind, which the backend would refuse anyway.
  const minting = provider === 'mycelium' && mintSecret

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    try {
      const data = await send<ConnectorCreated>(
        base,
        jsonInit('POST', {
          label: label.trim(),
          provider,
          // Null means "mint one", which the backend allows ONLY for the
          // native contract; the vendor form makes the field required so a
          // connector can never be born with a secret its provider never
          // issued -- it would refuse every delivery as signature_invalid.
          signing_secret: minting ? null : signingSecret.trim() || null,
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
      setMintSecret(true)
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

  async function onRotateSigning(c: Connector, secret: string | null) {
    // An inline masked form rather than window.prompt: a prompt renders what is
    // typed in clear text, and the new secret is not ours to choose for a
    // vendor -- it is the whsec_... the dashboard shows after a roll -- so it
    // gets pasted here. ``null`` asks the backend to mint one, which it accepts
    // only on the native contract.
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
          body: JSON.stringify({ signing_secret: secret }),
        },
      )
      setCreated(data)
      setRotating(null)
      setRotateSecret('')
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
            <SecretReveal
              label={t('paymentConnectors.secretShown')}
              value={created.signing_secret}
              token="secret"
              copy={(text, token) => void copy(text, token)}
              copied={copied}
            />
          )}
          {created.api_key && (
            <SecretReveal
              label={t('paymentConnectors.apiKeyShown')}
              value={created.api_key}
              token="apikey"
              copy={(text, token) => void copy(text, token)}
              copied={copied}
            />
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
                    onClick={() => {
                      setRotateSecret('')
                      setRotating(rotating === c.id ? null : c.id)
                    }}
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
              <SetupGuide connector={c} copy={(text, token) => void copy(text, token)} copied={copied} />
              {rotating === c.id && (
                <form
                  className="card card--quiet"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void onRotateSigning(c, rotateSecret.trim() || null)
                  }}
                >
                  <h4>{t('paymentConnectors.rotateSigning')}</h4>
                  <p className="hint">
                    {c.provider === 'mycelium'
                      ? t('paymentConnectors.rotateSigningNativeHint')
                      : t('paymentConnectors.rotateSigningVendorHint')}
                  </p>
                  <div className="row">
                    <label className="lbl--wide">
                      {t('paymentConnectors.signingSecret')}
                      <MaskedInput
                        // Required for a vendor: an empty submission would ask
                        // us to mint a secret the provider never issued, which
                        // the backend refuses -- better to say so in the form.
                        required={c.provider !== 'mycelium'}
                        minLength={MIN_SIGNING_SECRET}
                        value={rotateSecret}
                        onChange={setRotateSecret}
                        placeholder={c.provider === 'mycelium' ? '' : 'whsec_…'}
                      />
                    </label>
                  </div>
                  <div className="row">
                    <button type="submit" className="btn--sm">
                      {t('paymentConnectors.save')}
                    </button>
                    {c.provider === 'mycelium' && (
                      <button
                        type="button"
                        className="btn--sm btn--ghost"
                        onClick={() => void onRotateSigning(c, null)}
                      >
                        {t('paymentConnectors.secretMint')}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn--sm btn--ghost"
                      onClick={() => setRotating(null)}
                    >
                      {t('paymentConnectors.cancel')}
                    </button>
                  </div>
                </form>
              )}
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
                  <DefaultsFields
                    value={editForm}
                    vocab={vocab}
                    provider={c.provider}
                    onChange={setEditForm}
                  />
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
          {/* The credential question is genuinely DIFFERENT per provider, so it
              is a different form rather than one form with a conditional
              asterisk. For a vendor the secret is issued elsewhere and must be
              copied in; on our own contract Mycelium is the authority, so
              minting is the default and pasting a key the sender already uses
              is the alternative. */}
          {provider === 'mycelium' ? (
            <>
              <div className="row">
                <label>
                  <input
                    type="radio"
                    name="secret-source"
                    checked={mintSecret}
                    onChange={() => setMintSecret(true)}
                  />
                  {t('paymentConnectors.secretMint')}
                </label>
                <label>
                  <input
                    type="radio"
                    name="secret-source"
                    checked={!mintSecret}
                    onChange={() => setMintSecret(false)}
                  />
                  {t('paymentConnectors.secretProvide')}
                </label>
              </div>
              {!mintSecret && (
                <div className="row">
                  <label className="lbl--wide">
                    {t('paymentConnectors.signingSecret')}
                    <MaskedInput
                      required
                      minLength={MIN_SIGNING_SECRET}
                      value={signingSecret}
                      onChange={setSigningSecret}
                      placeholder={t('paymentConnectors.secretPlaceholder')}
                    />
                  </label>
                </div>
              )}
              <p className="hint">{t('paymentConnectors.nativeSecretHint')}</p>
            </>
          ) : (
            <>
              <div className="row">
                <label className="lbl--wide">
                  {t('paymentConnectors.signingSecret')}
                  {/* Required: a Stripe connector holding a secret Stripe never
                      issued would refuse every delivery with signature_invalid,
                      and the operator would see an empty invoice list with no
                      error anywhere. */}
                  <MaskedInput
                    required
                    minLength={MIN_SIGNING_SECRET}
                    value={signingSecret}
                    onChange={setSigningSecret}
                    placeholder="whsec_…"
                  />
                </label>
              </div>
              <p className="hint">{t('paymentConnectors.signingSecretHint')}</p>
            </>
          )}
          <label className="row">
            <input
              type="checkbox"
              checked={withApiKey}
              onChange={(e) => setWithApiKey(e.target.checked)}
            />
            {t('paymentConnectors.withApiKey')}
          </label>
          <p className="hint">{t('paymentConnectors.withApiKeyHint')}</p>
          <DefaultsFields value={form} vocab={vocab} provider={provider} onChange={setForm} />
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
