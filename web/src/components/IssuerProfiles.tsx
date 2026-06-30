import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

// Logo formats the backend accepts (PNG/JPEG; reportlab raster). Kept in
// sync with mycelium_core.services.invoice.LOGO_MIMES.
const LOGO_ACCEPT = 'image/png,image/jpeg'

type Profile = components['schemas']['IssuerProfileOut']
type Counter = components['schemas']['InvoiceCounterOut']

// Per-(issuer, series, year) progressive override. Used when migrating
// invoices from another billing system: raise last_number to the last
// emitted-elsewhere value, so the next Mycelium allocation continues from
// there. The backend rejects a value below the max number already
// transmitted in Mycelium under the same key.
function IssuerCounters({ profileId }: { profileId: string }) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Counter[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [adding, setAdding] = useState(false)
  const [addSeries, setAddSeries] = useState('')
  const [addYear, setAddYear] = useState(String(new Date().getFullYear()))
  const [addLast, setAddLast] = useState('')

  const load = useCallback(async () => {
    const { data, error } = await api.GET(
      '/issuer-profiles/{profile_id}/counters',
      { params: { header: workspaceHeader(), path: { profile_id: profileId } } },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setRows(data ?? [])
    setEdits({})
  }, [profileId])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await api.GET(
        '/issuer-profiles/{profile_id}/counters',
        { params: { header: workspaceHeader(), path: { profile_id: profileId } } },
      )
      if (!active) return
      if (error) {
        setErr(errMessage(error))
        return
      }
      setRows(data ?? [])
      setEdits({})
    })()
    return () => {
      active = false
    }
  }, [profileId])

  async function save(series: string, year: number) {
    setErr(null)
    setMsg(null)
    const raw = edits[`${series}/${year}`]
    if (raw == null || raw.trim() === '') return
    const n = Number(raw)
    if (!Number.isFinite(n) || n < 0) {
      setErr(t('invoices.countersFloor'))
      return
    }
    const { error } = await api.PUT(
      '/issuer-profiles/{profile_id}/counters/{series}/{year}',
      {
        params: {
          header: workspaceHeader(),
          path: { profile_id: profileId, series, year },
        },
        body: { last_number: n },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('invoices.saved'))
    await load()
  }

  async function add() {
    setErr(null)
    setMsg(null)
    const s = addSeries.trim()
    const y = Number(addYear)
    const n = Number(addLast)
    if (!s || !Number.isFinite(y) || !Number.isFinite(n)) return
    const { error } = await api.PUT(
      '/issuer-profiles/{profile_id}/counters/{series}/{year}',
      {
        params: {
          header: workspaceHeader(),
          path: { profile_id: profileId, series: s, year: y },
        },
        body: { last_number: n },
      },
    )
    if (error) {
      setErr(errMessage(error))
      return
    }
    setAdding(false)
    setAddSeries('')
    setAddLast('')
    setMsg(t('invoices.saved'))
    await load()
  }

  return (
    <div className="card card--running">
      <h4>{t('invoices.counters')}</h4>
      <p className="hint">{t('invoices.countersHint')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {rows.length === 0 ? (
        <p className="hint">—</p>
      ) : (
        <table className="list">
          <thead>
            <tr>
              <th>{t('invoices.countersSeries')}</th>
              <th>{t('invoices.countersYear')}</th>
              <th>{t('invoices.countersLast')}</th>
              <th>{t('invoices.countersFloor')}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const key = `${r.series}/${r.year}`
              const v = edits[key] ?? String(r.last_number)
              return (
                <tr key={key}>
                  <td>{r.series}</td>
                  <td>{r.year}</td>
                  <td>
                    <input
                      type="number"
                      min={r.max_emitted}
                      max={9999999}
                      value={v}
                      onChange={(e) =>
                        setEdits({ ...edits, [key]: e.target.value })
                      }
                    />
                  </td>
                  <td>{r.max_emitted}</td>
                  <td>
                    <button
                      type="button"
                      className="btn--sm"
                      onClick={() => void save(r.series, r.year)}
                    >
                      {t('invoices.countersSave')}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {adding ? (
        <div className="row">
          <input
            placeholder={t('invoices.countersSeries')}
            value={addSeries}
            onChange={(e) => setAddSeries(e.target.value)}
          />
          <input
            type="number"
            min={2000}
            max={2100}
            placeholder={t('invoices.countersYear')}
            value={addYear}
            onChange={(e) => setAddYear(e.target.value)}
          />
          <input
            type="number"
            min={0}
            placeholder={t('invoices.countersLast')}
            value={addLast}
            onChange={(e) => setAddLast(e.target.value)}
          />
          <button type="button" className="btn--sm" onClick={() => void add()}>
            {t('invoices.countersSave')}
          </button>
          <button
            type="button"
            className="btn--sm btn--ghost"
            onClick={() => setAdding(false)}
          >
            {t('invoices.cancel')}
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="btn--sm btn--ghost"
          onClick={() => setAdding(true)}
        >
          {t('invoices.countersAdd')}
        </button>
      )}
    </div>
  )
}

const EMPTY = {
  label: '',
  legal_name: '',
  first_name: '',
  last_name: '',
  country_code: 'IT',
  vat_number: '',
  tax_code: '',
  address: '',
  civic_number: '',
  postal_code: '',
  city: '',
  province: '',
  country: 'IT',
  sdi_code: '',
  tax_regime: 'RF01',
  default_iban: '',
  pec: '',
  email: '',
  phone: '',
  fax: '',
  show_phone: true,
  show_email: true,
  show_pec: true,
  default_payment_conditions_code: '',
  default_payment_method_code: '',
  default_payment_terms_days: '',
  letterhead: '',
  is_default: false,
}
type Form = typeof EMPTY

// FatturaPA 1.2 closed enums (kept in sync with
// mycelium_core.services.payment_methods). Order matches the SdI table.
const CONDIZIONI: ReadonlyArray<readonly [string, string]> = [
  ['TP01', 'pagamento a rate'],
  ['TP02', 'pagamento completo'],
  ['TP03', 'anticipo'],
]
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

// Issuer profiles (invoice letterhead) — managed in Settings. An
// invoice cannot be issued without one; the default is pre-selected.
export function IssuerProfiles() {
  const { t } = useTranslation()
  const session = useSession()
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [form, setForm] = useState<Form>(EMPTY)
  const [edit, setEdit] = useState<string | 'new' | null>(null)
  // FatturaPA Anagrafica is a choice: a legal entity uses Ragione sociale
  // (Denominazione), a persona fisica uses Nome+Cognome. This toggle picks one
  // so the form never submits both (the API stays lenient for legacy rows).
  const [subjectType, setSubjectType] = useState<'company' | 'person'>('company')
  const [showCounters, setShowCounters] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  // Object URL of the currently-edited profile's logo (auth-fetched as a
  // blob: the endpoint is bearer-protected, so an <img src> to it would
  // 401). Revoked before replacing / on close.
  const [logoUrl, setLogoUrl] = useState<string | null>(null)
  const [logoBusy, setLogoBusy] = useState(false)
  // Monotonic token so overlapping showLogo calls (rapid profile
  // switching) don't race: only the latest commits its blob URL, and a
  // superseded fetch neither creates a leaked URL nor shows a stale logo.
  const logoSeq = useRef(0)

  const showLogo = useCallback(async (id: string | null) => {
    const seq = ++logoSeq.current
    setLogoUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return null
    })
    if (!id) return
    const res = await authFetch(`/issuer-profiles/${id}/logo`)
    if (!res.ok || seq !== logoSeq.current) return
    const blob = await res.blob()
    if (seq !== logoSeq.current) return
    const url = URL.createObjectURL(blob)
    setLogoUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return url
    })
  }, [])

  // Revoke the last object URL when the component unmounts.
  useEffect(() => () => setLogoUrl((p) => (p ? (URL.revokeObjectURL(p), null) : null)), [])

  const load = useCallback(async () => {
    const { data } = await api.GET('/issuer-profiles', {
      params: { header: workspaceHeader() },
    })
    if (data) setProfiles(data)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/issuer-profiles', {
        params: { header: workspaceHeader() },
      })
      if (active && data) setProfiles(data)
    })()
    return () => {
      active = false
    }
  }, [session?.workspaceId])

  function start(p: Profile | null) {
    setErr(null)
    setMsg(null)
    if (p) {
      setEdit(p.id)
      // Both filled (a legacy row) infers persona fisica, matching the XML's
      // Nome/Cognome precedence.
      setSubjectType(p.first_name && p.last_name ? 'person' : 'company')
      setForm({
        label: p.label,
        legal_name: p.legal_name ?? '',
        first_name: p.first_name ?? '',
        last_name: p.last_name ?? '',
        country_code: p.country_code ?? 'IT',
        vat_number: p.vat_number ?? '',
        tax_code: p.tax_code ?? '',
        address: p.address,
        civic_number: p.civic_number ?? '',
        postal_code: p.postal_code,
        city: p.city,
        province: p.province ?? '',
        country: p.country ?? 'IT',
        sdi_code: p.sdi_code ?? '',
        tax_regime: p.tax_regime ?? 'RF01',
        default_iban: p.default_iban ?? '',
        pec: p.pec ?? '',
        email: p.email ?? '',
        phone: p.phone ?? '',
        fax: p.fax ?? '',
        show_phone: p.show_phone ?? true,
        show_email: p.show_email ?? true,
        show_pec: p.show_pec ?? true,
        default_payment_conditions_code: p.default_payment_conditions_code ?? '',
        default_payment_method_code: p.default_payment_method_code ?? '',
        default_payment_terms_days:
          p.default_payment_terms_days != null
            ? String(p.default_payment_terms_days)
            : '',
        letterhead: p.letterhead ?? '',
        is_default: p.is_default,
      })
      void showLogo(p.has_logo ? p.id : null)
    } else {
      setEdit('new')
      setSubjectType('company')
      setForm(EMPTY)
      void showLogo(null)
    }
  }

  // Switch Anagrafica mode: clear the OTHER mode's fields so the payload is
  // single-mode (legal entity XOR persona fisica), never both.
  function setSubject(next: 'company' | 'person') {
    setSubjectType(next)
    setForm((f) =>
      next === 'person'
        ? { ...f, legal_name: '' }
        : { ...f, first_name: '', last_name: '' },
    )
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    // FatturaPA Anagrafica choice: the active subject type is authoritative.
    // Validate (and below, submit) only that mode, so a legacy both-filled row
    // is normalised to a single mode on save and never re-sends a stale field.
    const isPerson = subjectType === 'person'
    const nameOk = isPerson
      ? !!(form.first_name.trim() && form.last_name.trim())
      : !!form.legal_name.trim()
    if (!nameOk) {
      setErr(t('cp.nameRequired'))
      return
    }
    const h = workspaceHeader()
    const termsDays = form.default_payment_terms_days.trim()
    const common = {
      label: form.label,
      legal_name: isPerson ? null : form.legal_name || null,
      first_name: isPerson ? form.first_name || null : null,
      last_name: isPerson ? form.last_name || null : null,
      vat_number: form.vat_number || null,
      tax_code: form.tax_code || null,
      address: form.address,
      civic_number: form.civic_number || null,
      postal_code: form.postal_code,
      city: form.city,
      province: form.province || null,
      sdi_code: form.sdi_code || null,
      default_iban: form.default_iban || null,
      pec: form.pec || null,
      email: form.email || null,
      phone: form.phone || null,
      fax: form.fax || null,
      show_phone: form.show_phone,
      show_email: form.show_email,
      show_pec: form.show_pec,
      default_payment_conditions_code: form.default_payment_conditions_code || null,
      default_payment_method_code: form.default_payment_method_code || null,
      default_payment_terms_days: termsDays ? Number(termsDays) : null,
      letterhead: form.letterhead || null,
      is_default: form.is_default,
    }
    // tax_regime drives forfettario (RF19) invoicing — it is a hard
    // fiscal/legal fact, NOT a constant: a flat-rate (forfettario)
    // issuer MUST be RF19 or the invoice is non-compliant (no L.190
    // purpose, wrong VAT). country_code/country default IT but are
    // editable (VAT country, symmetric with the client); all are
    // required by IssuerProfileIn and must ride PATCH too (else 422).
    const body = {
      tax_regime: form.tax_regime,
      country_code: form.country_code || 'IT',
      country: form.country || 'IT',
      ...common,
    }
    const res =
      edit === 'new'
        ? await api.POST('/issuer-profiles', {
            params: { header: h },
            body,
          })
        : await api.PATCH('/issuer-profiles/{profile_id}', {
            params: { header: h, path: { profile_id: edit as string } },
            body,
          })
    if (res.error) {
      setErr(errMessage(res.error))
      return
    }
    setEdit(null)
    setMsg(t('invoices.saved'))
    await load()
  }

  async function setDefault(id: string) {
    setErr(null)
    const { error } = await api.POST('/issuer-profiles/{profile_id}/default', {
      params: { header: workspaceHeader(), path: { profile_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  async function remove(id: string) {
    if (!window.confirm(t('invoices.confirmDeleteProfile'))) return
    setErr(null)
    const { error } = await api.DELETE('/issuer-profiles/{profile_id}', {
      params: { header: workspaceHeader(), path: { profile_id: id } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  async function uploadLogo(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = '' // let the same file be re-picked after a failure
    if (!file || edit === 'new' || edit === null) return
    setErr(null)
    setMsg(null)
    setLogoBusy(true)
    try {
      const body = new FormData()
      body.append('file', file)
      const res = await authFetch(`/issuer-profiles/${edit}/logo`, {
        method: 'POST',
        body,
      })
      if (!res.ok) {
        setErr(errMessage(await res.json().catch(() => null)))
        return
      }
      await showLogo(edit)
      await load()
      setMsg(t('invoices.saved'))
    } finally {
      setLogoBusy(false)
    }
  }

  async function removeLogo() {
    if (edit === 'new' || edit === null) return
    setErr(null)
    const { error } = await api.DELETE('/issuer-profiles/{profile_id}/logo', {
      params: { header: workspaceHeader(), path: { profile_id: edit } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await showLogo(null)
    await load()
  }

  return (
    <section className="card">
      <h2>{t('invoices.profiles')}</h2>
      <p className="hint">{t('invoices.profilesHint')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {profiles.length === 0 ? (
        <p className="hint">{t('invoices.noProfiles')}</p>
      ) : (
        <ul className="list">
          {profiles.map((p) => (
            <li key={p.id}>
              <strong>{p.label}</strong> ·{' '}
              {p.legal_name || `${p.first_name ?? ''} ${p.last_name ?? ''}`.trim()}
              {p.vat_number ? ` · ${p.vat_number}` : ''}{' '}
              {p.is_default && (
                <span className="muted">[{t('invoices.isDefault')}]</span>
              )}
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() => start(p)}
              >
                {t('invoices.edit')}
              </button>
              {!p.is_default && (
                <button
                  type="button"
                  className="btn--sm btn--ghost"
                  onClick={() => void setDefault(p.id)}
                >
                  {t('invoices.setDefault')}
                </button>
              )}
              {!p.is_default && (
                <button
                  type="button"
                  className="btn--sm btn--danger"
                  onClick={() => void remove(p.id)}
                >
                  {t('invoices.delete')}
                </button>
              )}
              <button
                type="button"
                className="btn--sm btn--ghost"
                onClick={() =>
                  setShowCounters(showCounters === p.id ? null : p.id)
                }
              >
                {t('invoices.counters')}
              </button>
              {showCounters === p.id && <IssuerCounters profileId={p.id} />}
            </li>
          ))}
        </ul>
      )}
      {edit === null ? (
        <button type="button" className="btn--sm" onClick={() => start(null)}>
          {t('invoices.newProfile')}
        </button>
      ) : (
        <form onSubmit={(e) => void save(e)} className="card card--running">
          <h3>
            {edit === 'new'
              ? t('invoices.newProfile')
              : t('invoices.editProfile')}
          </h3>
          <div className="row">
            <label>
              {t('invoices.label')}
              <input
                required
                value={form.label}
                onChange={(e) => setForm({ ...form, label: e.target.value })}
              />
            </label>
            {/* FatturaPA Anagrafica is a choice: a legal entity emits
                Denominazione (Ragione sociale), a persona fisica Nome+Cognome.
                The mode picks which field(s) show, so they are never both
                filled (setSubject clears the inactive one). */}
            <label>
              {t('cp.f.subjectType')}
              <select
                value={subjectType}
                onChange={(e) => setSubject(e.target.value as 'company' | 'person')}
              >
                <option value="company">{t('cp.f.company')}</option>
                <option value="person">{t('cp.f.person')}</option>
              </select>
            </label>
          </div>
          {subjectType === 'company' ? (
            <div className="row">
              <label>
                {t('cp.f.legal_name')}
                <input
                  value={form.legal_name}
                  onChange={(e) =>
                    setForm({ ...form, legal_name: e.target.value })
                  }
                />
              </label>
            </div>
          ) : (
            <div className="row">
              <label>
                {t('cp.f.first_name')}
                <input
                  value={form.first_name}
                  onChange={(e) =>
                    setForm({ ...form, first_name: e.target.value })
                  }
                />
              </label>
              <label>
                {t('cp.f.last_name')}
                <input
                  value={form.last_name}
                  onChange={(e) =>
                    setForm({ ...form, last_name: e.target.value })
                  }
                />
              </label>
            </div>
          )}
          {/* Identity, symmetric with the client: Country (VAT) + VAT
              number split, plus tax code. */}
          <div className="row">
            <label>
              {t('cp.f.country_code')}
              <input
                value={form.country_code}
                onChange={(e) =>
                  setForm({ ...form, country_code: e.target.value })
                }
              />
            </label>
            <label>
              {t('cp.f.vat_number')}
              <input
                value={form.vat_number}
                onChange={(e) =>
                  setForm({ ...form, vat_number: e.target.value })
                }
              />
            </label>
            <label>
              {t('cp.f.tax_code')}
              <input
                value={form.tax_code}
                onChange={(e) =>
                  setForm({ ...form, tax_code: e.target.value })
                }
              />
            </label>
          </div>
          <div className="row">
            <label>
              {t('cp.f.address')}
              <input
                value={form.address}
                onChange={(e) => setForm({ ...form, address: e.target.value })}
              />
            </label>
            <label>
              {t('cp.f.civic_number')}
              <input
                value={form.civic_number}
                onChange={(e) => setForm({ ...form, civic_number: e.target.value })}
              />
            </label>
            <label>
              {t('cp.f.postal_code')}
              <input
                value={form.postal_code}
                onChange={(e) =>
                  setForm({ ...form, postal_code: e.target.value })
                }
              />
            </label>
            <label>
              {t('cp.f.city')}
              <input
                value={form.city}
                onChange={(e) => setForm({ ...form, city: e.target.value })}
              />
            </label>
            <label>
              {t('cp.f.province')}
              <input
                value={form.province}
                onChange={(e) =>
                  setForm({ ...form, province: e.target.value })
                }
              />
            </label>
            <label>
              {t('cp.f.country')}
              <input
                value={form.country}
                onChange={(e) => setForm({ ...form, country: e.target.value })}
              />
            </label>
          </div>
          <div className="row">
            <label>
              {t('cp.f.sdi_code')}
              <input
                value={form.sdi_code}
                onChange={(e) =>
                  setForm({ ...form, sdi_code: e.target.value })
                }
              />
            </label>
            <label>
              {t('invoices.regime')}
              <select
                value={form.tax_regime}
                onChange={(e) =>
                  setForm({ ...form, tax_regime: e.target.value })
                }
              >
                <option value="RF01">{t('invoices.regimeRF01')}</option>
                <option value="RF19">{t('invoices.regimeRF19')}</option>
              </select>
            </label>
            <label>
              {t('invoices.defaultIban')}
              <input
                value={form.default_iban}
                onChange={(e) =>
                  setForm({ ...form, default_iban: e.target.value })
                }
              />
            </label>
          </div>
          {form.tax_regime === 'RF19' && (
            <p className="hint">{t('invoices.regimeRF19Hint')}</p>
          )}
          <div className="row">
            <input
              type="email"
              placeholder={t('invoices.pec')}
              value={form.pec}
              onChange={(e) => setForm({ ...form, pec: e.target.value })}
            />
            <input
              type="email"
              placeholder={t('invoices.email')}
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
            />
            <input
              placeholder={t('invoices.phone')}
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
            />
            <input
              placeholder={t('invoices.fax')}
              value={form.fax}
              onChange={(e) => setForm({ ...form, fax: e.target.value })}
            />
          </div>
          <p className="hint">{t('invoices.showContactsTip')}</p>
          <div className="row">
            <label>
              <input
                type="checkbox"
                checked={form.show_phone}
                onChange={(e) => setForm({ ...form, show_phone: e.target.checked })}
              />{' '}
              {t('invoices.showPhone')}
            </label>
            <label>
              <input
                type="checkbox"
                checked={form.show_email}
                onChange={(e) => setForm({ ...form, show_email: e.target.checked })}
              />{' '}
              {t('invoices.showEmail')}
            </label>
            <label>
              <input
                type="checkbox"
                checked={form.show_pec}
                onChange={(e) => setForm({ ...form, show_pec: e.target.checked })}
              />{' '}
              {t('invoices.showPec')}
            </label>
          </div>
          {/* Letterhead: header text + optional logo printed at the top of
              the courtesy PDF. The logo attaches to a saved profile (it
              needs the profile id), so it shows only when editing. */}
          <div className="row">
            <label className="lbl--wide">
              {t('invoices.letterhead')}
              <textarea
                rows={3}
                placeholder={t('invoices.letterheadPlaceholder')}
                value={form.letterhead}
                onChange={(e) =>
                  setForm({ ...form, letterhead: e.target.value })
                }
              />
            </label>
          </div>
          {edit !== 'new' && (
            <div className="field">
              {t('invoices.logo')}
              <p className="hint">{t('invoices.logoHint')}</p>
              {logoUrl && (
                <img
                  src={logoUrl}
                  alt={t('invoices.logo')}
                  className="issuer-logo"
                />
              )}
              <div className="row">
                <label className="btn--sm btn--ghost">
                  {logoBusy ? '…' : t('invoices.logoUpload')}
                  <input
                    type="file"
                    accept={LOGO_ACCEPT}
                    hidden
                    onChange={(e) => void uploadLogo(e)}
                  />
                </label>
                {logoUrl && (
                  <button
                    type="button"
                    className="btn--sm btn--danger"
                    onClick={() => void removeLogo()}
                  >
                    {t('invoices.logoRemove')}
                  </button>
                )}
              </div>
            </div>
          )}
          {/* Issuer-level payment fallbacks (used only if the client
              carries no own default; the invoice itself overrides both). */}
          <div className="row">
            <label>
              {t('invoices.defaultCondizioni')}
              <select
                value={form.default_payment_conditions_code}
                onChange={(e) =>
                  setForm({
                    ...form,
                    default_payment_conditions_code: e.target.value,
                  })
                }
              >
                <option value="">{t('invoices.inherit')}</option>
                {CONDIZIONI.map(([code, lbl]) => (
                  <option key={code} value={code}>
                    {code} - {lbl}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('invoices.defaultModalita')}
              <select
                value={form.default_payment_method_code}
                onChange={(e) =>
                  setForm({
                    ...form,
                    default_payment_method_code: e.target.value,
                  })
                }
              >
                <option value="">{t('invoices.inherit')}</option>
                {MODALITA.map(([code, lbl]) => (
                  <option key={code} value={code}>
                    {code} - {lbl}
                  </option>
                ))}
              </select>
            </label>
            <label title={t('invoices.defaultTermsDaysTip')}>
              {t('invoices.defaultTermsDays')}
              <input
                className="inp--netdays"
                type="number"
                min={0}
                max={365}
                placeholder={t('invoices.defaultTermsDaysPlaceholder')}
                title={t('invoices.defaultTermsDaysTip')}
                value={form.default_payment_terms_days}
                onChange={(e) =>
                  setForm({
                    ...form,
                    default_payment_terms_days: e.target.value,
                  })
                }
              />
            </label>
          </div>
          <p className="hint">{t('invoices.defaultTermsDaysTip')}</p>
          <label className="row">
            <input
              type="checkbox"
              checked={form.is_default}
              onChange={(e) =>
                setForm({ ...form, is_default: e.target.checked })
              }
            />
            {t('invoices.isDefault')}
          </label>
          <div className="row">
            <button type="submit">{t('invoices.saveProfile')}</button>
            <button
              type="button"
              className="btn--ghost"
              onClick={() => setEdit(null)}
            >
              {t('invoices.cancel')}
            </button>
          </div>
        </form>
      )}
    </section>
  )
}
