import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Profile = components['schemas']['IssuerProfileOut']

const EMPTY = {
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
type Form = typeof EMPTY

// Issuer profiles (invoice letterhead) — managed in Settings. An
// invoice cannot be issued without one; the default is pre-selected.
export function IssuerProfiles() {
  const { t } = useTranslation()
  const session = useSession()
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [form, setForm] = useState<Form>(EMPTY)
  const [edit, setEdit] = useState<string | 'new' | null>(null)
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
    const common = {
      label: form.label,
      denominazione: form.denominazione,
      piva: form.piva || null,
      codice_fiscale: form.codice_fiscale || null,
      indirizzo: form.indirizzo,
      cap: form.cap,
      comune: form.comune,
      provincia: form.provincia || null,
      is_default: form.is_default,
    }
    // regime_fiscale/paese/nazione are required by IssuerProfileIn (not
    // user-editable here, IT-fiscal constants). They must be sent on
    // PATCH too, otherwise the update 422s ("field required").
    const body = { regime_fiscale: 'RF01', paese: 'IT', nazione: 'IT', ...common }
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
