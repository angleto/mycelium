import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Profile = components['schemas']['IssuerProfileOut']
type Counter = components['schemas']['InvoiceCounterOut']

// Per-(issuer, series, year) progressive override. Used when migrating
// invoices from another billing system: raise last_number to the last
// emitted-elsewhere value, so the next Flow allocation continues from
// there. The backend rejects a value below the max number already
// transmitted in Flow under the same key.
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
  denominazione: '',
  piva: '',
  codice_fiscale: '',
  indirizzo: '',
  cap: '',
  comune: '',
  provincia: '',
  regime_fiscale: 'RF01',
  default_iban: '',
  pec: '',
  email: '',
  telefono: '',
  fax: '',
  default_condizioni_pagamento: '',
  default_modalita_pagamento: '',
  default_payment_terms_days: '',
  is_default: false,
}
type Form = typeof EMPTY

// FatturaPA 1.2 closed enums (kept in sync with
// flow_core.services.payment_methods). Order matches the SdI table.
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
  const [showCounters, setShowCounters] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

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
      setForm({
        label: p.label,
        denominazione: p.denominazione,
        piva: p.piva ?? '',
        codice_fiscale: p.codice_fiscale ?? '',
        indirizzo: p.indirizzo,
        cap: p.cap,
        comune: p.comune,
        provincia: p.provincia ?? '',
        regime_fiscale: p.regime_fiscale ?? 'RF01',
        default_iban: p.default_iban ?? '',
        pec: p.pec ?? '',
        email: p.email ?? '',
        telefono: p.telefono ?? '',
        fax: p.fax ?? '',
        default_condizioni_pagamento: p.default_condizioni_pagamento ?? '',
        default_modalita_pagamento: p.default_modalita_pagamento ?? '',
        default_payment_terms_days:
          p.default_payment_terms_days != null
            ? String(p.default_payment_terms_days)
            : '',
        is_default: p.is_default,
      })
    } else {
      setEdit('new')
      setForm(EMPTY)
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const h = workspaceHeader()
    const termsDays = form.default_payment_terms_days.trim()
    const common = {
      label: form.label,
      denominazione: form.denominazione,
      piva: form.piva || null,
      codice_fiscale: form.codice_fiscale || null,
      indirizzo: form.indirizzo,
      cap: form.cap,
      comune: form.comune,
      provincia: form.provincia || null,
      default_iban: form.default_iban || null,
      pec: form.pec || null,
      email: form.email || null,
      telefono: form.telefono || null,
      fax: form.fax || null,
      default_condizioni_pagamento: form.default_condizioni_pagamento || null,
      default_modalita_pagamento: form.default_modalita_pagamento || null,
      default_payment_terms_days: termsDays ? Number(termsDays) : null,
      is_default: form.is_default,
    }
    // regime_fiscale drives forfettario (RF19) invoicing — it is a
    // hard fiscal/legal fact, NOT a constant: a flat-rate (forfettario)
    // issuer MUST be RF19 or the invoice is non-compliant (no L.190
    // causale, wrong VAT). paese/nazione stay IT constants; all are
    // required by IssuerProfileIn and must ride PATCH too (else 422).
    const body = {
      regime_fiscale: form.regime_fiscale,
      paese: 'IT',
      nazione: 'IT',
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
              <strong>{p.label}</strong> · {p.denominazione}
              {p.piva ? ` · ${p.piva}` : ''}{' '}
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
            <input
              required
              placeholder={t('invoices.label')}
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
            />
            <input
              required
              placeholder={t('invoices.denom')}
              value={form.denominazione}
              onChange={(e) =>
                setForm({ ...form, denominazione: e.target.value })
              }
            />
            <input
              placeholder={t('invoices.piva')}
              value={form.piva}
              onChange={(e) => setForm({ ...form, piva: e.target.value })}
            />
            <input
              placeholder={t('invoices.cf')}
              value={form.codice_fiscale}
              onChange={(e) =>
                setForm({ ...form, codice_fiscale: e.target.value })
              }
            />
          </div>
          <div className="row">
            <input
              placeholder={t('invoices.address')}
              value={form.indirizzo}
              onChange={(e) => setForm({ ...form, indirizzo: e.target.value })}
            />
            <input
              placeholder={t('invoices.cap')}
              value={form.cap}
              onChange={(e) => setForm({ ...form, cap: e.target.value })}
            />
            <input
              placeholder={t('invoices.comune')}
              value={form.comune}
              onChange={(e) => setForm({ ...form, comune: e.target.value })}
            />
            <input
              placeholder={t('invoices.provincia')}
              value={form.provincia}
              onChange={(e) => setForm({ ...form, provincia: e.target.value })}
            />
          </div>
          <div className="row">
            <label>
              {t('invoices.regime')}
              <select
                value={form.regime_fiscale}
                onChange={(e) =>
                  setForm({ ...form, regime_fiscale: e.target.value })
                }
              >
                <option value="RF01">{t('invoices.regimeRF01')}</option>
                <option value="RF19">{t('invoices.regimeRF19')}</option>
              </select>
            </label>
            <input
              placeholder={t('invoices.defaultIban')}
              value={form.default_iban}
              onChange={(e) =>
                setForm({ ...form, default_iban: e.target.value })
              }
            />
          </div>
          {form.regime_fiscale === 'RF19' && (
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
              value={form.telefono}
              onChange={(e) => setForm({ ...form, telefono: e.target.value })}
            />
            <input
              placeholder={t('invoices.fax')}
              value={form.fax}
              onChange={(e) => setForm({ ...form, fax: e.target.value })}
            />
          </div>
          {/* Issuer-level payment fallbacks (used only if the client
              carries no own default; the invoice itself overrides both). */}
          <div className="row">
            <label>
              {t('invoices.defaultCondizioni')}
              <select
                value={form.default_condizioni_pagamento}
                onChange={(e) =>
                  setForm({
                    ...form,
                    default_condizioni_pagamento: e.target.value,
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
                value={form.default_modalita_pagamento}
                onChange={(e) =>
                  setForm({
                    ...form,
                    default_modalita_pagamento: e.target.value,
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
            <input
              type="number"
              min={0}
              max={365}
              placeholder={t('invoices.defaultTermsDays')}
              value={form.default_payment_terms_days}
              onChange={(e) =>
                setForm({
                  ...form,
                  default_payment_terms_days: e.target.value,
                })
              }
            />
          </div>
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
