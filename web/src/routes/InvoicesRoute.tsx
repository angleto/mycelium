import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import { useSession } from '../auth/useSession'
import type { components } from '../api/schema'

type Invoice = components['schemas']['InvoiceOut']
type Tag = components['schemas']['TagOut']

export function InvoicesRoute() {
  const { t } = useTranslation()
  const session = useSession()
  const activeId = session?.workspaceId
  const [denom, setDenom] = useState('')
  const [piva, setPiva] = useState('')
  const [cf, setCf] = useState('')
  const [indirizzo, setIndirizzo] = useState('')
  const [cap, setCap] = useState('')
  const [comune, setComune] = useState('')
  const [clients, setClients] = useState<Tag[]>([])
  const [client, setClient] = useState('')
  const [series, setSeries] = useState('A')
  const [list, setList] = useState<Invoice[]>([])
  const [lineFor, setLineFor] = useState('')
  const [desc, setDesc] = useState('')
  const [price, setPrice] = useState(0)
  const [qty, setQty] = useState(1)
  const [vat, setVat] = useState(22)
  const [xml, setXml] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    const h = workspaceHeader()
    const [tg, inv, fp] = await Promise.all([
      api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
      api.GET('/invoices', { params: { header: h } }),
      api.GET('/fiscal-profile', { params: { header: h } }),
    ])
    if (tg.data) setClients(tg.data)
    if (inv.data) setList(inv.data)
    if (fp.data?.denominazione) setDenom(fp.data.denominazione)
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const h = workspaceHeader()
      const [tg, inv, fp] = await Promise.all([
        api.GET('/tags', { params: { header: h, query: { kind: 'client' } } }),
        api.GET('/invoices', { params: { header: h } }),
        api.GET('/fiscal-profile', { params: { header: h } }),
      ])
      if (!active) return
      if (tg.data) setClients(tg.data)
      if (inv.data) setList(inv.data)
      if (fp.data?.denominazione) setDenom(fp.data.denominazione)
    })()
    return () => {
      active = false
    }
  }, [activeId])

  async function onSaveProfile(e: FormEvent) {
    e.preventDefault()
    setErr(null)
    const { error } = await api.PUT('/fiscal-profile', {
      params: { header: workspaceHeader() },
      body: {
        denominazione: denom,
        piva: piva || null,
        codice_fiscale: cf || null,
        regime_fiscale: 'RF01',
        paese: 'IT',
        nazione: 'IT',
        indirizzo,
        cap,
        comune,
      },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setMsg(t('invoices.profileSaved'))
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    if (!client) return
    setErr(null)
    const { error } = await api.POST('/invoices', {
      params: { header: workspaceHeader() },
      body: { client_tag_id: client, series },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onAddLine(e: FormEvent) {
    e.preventDefault()
    if (!lineFor) return
    setErr(null)
    const { error } = await api.POST('/invoices/{invoice_id}/lines', {
      params: { header: workspaceHeader(), path: { invoice_id: lineFor } },
      body: { description: desc, unit_price: price, quantity: qty, vat_rate: vat },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setDesc('')
    await reload()
  }

  async function act(p: Promise<{ error?: unknown; response: Response }>) {
    setErr(null)
    setMsg(null)
    const { error } = await p
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onXml(id: string) {
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

  return (
    <section className="card">
      <h1>{t('invoices.title')}</h1>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}

      <form onSubmit={(e) => void onSaveProfile(e)}>
        <h2>{t('invoices.profile')}</h2>
        <div className="row">
          <input
            required
            placeholder={t('invoices.denom')}
            value={denom}
            onChange={(e) => setDenom(e.target.value)}
          />
          <input
            placeholder={t('invoices.piva')}
            value={piva}
            onChange={(e) => setPiva(e.target.value)}
          />
          <input
            placeholder={t('invoices.cf')}
            value={cf}
            onChange={(e) => setCf(e.target.value)}
          />
          <input
            placeholder="Indirizzo"
            value={indirizzo}
            onChange={(e) => setIndirizzo(e.target.value)}
          />
          <input
            placeholder="CAP"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
          />
          <input
            placeholder="Comune"
            value={comune}
            onChange={(e) => setComune(e.target.value)}
          />
          <button type="submit">{t('invoices.saveProfile')}</button>
        </div>
      </form>

      <form onSubmit={(e) => void onCreate(e)} className="row">
        <select value={client} onChange={(e) => setClient(e.target.value)}>
          <option value="">{t('invoices.client')}</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <input
          value={series}
          onChange={(e) => setSeries(e.target.value)}
          style={{ width: '3rem' }}
        />
        <button type="submit">{t('invoices.create')}</button>
      </form>

      <h2>{t('invoices.list')}</h2>
      {list.length === 0 ? (
        <p className="hint">{t('invoices.none')}</p>
      ) : (
        <ul className="list">
          {list.map((i) => (
            <li key={i.id}>
              {i.series}/{i.year}/{i.number ?? '-'}{' '}
              <span className="muted">
                · {t('invoices.state')} {i.state} · {i.total} · sdi{' '}
                {i.sdi_status}
              </span>
              <button type="button" onClick={() => setLineFor(i.id)}>
                {t('invoices.addLine')}
              </button>
              <button
                type="button"
                onClick={() =>
                  void act(
                    api.POST('/invoices/{invoice_id}/transmit', {
                      params: {
                        header: workspaceHeader(),
                        path: { invoice_id: i.id },
                      },
                      body: {},
                    }),
                  )
                }
              >
                {t('invoices.transmit')}
              </button>
              <button
                type="button"
                onClick={() =>
                  void act(
                    api.POST('/invoices/{invoice_id}/paid', {
                      params: {
                        header: workspaceHeader(),
                        path: { invoice_id: i.id },
                      },
                    }),
                  )
                }
              >
                {t('invoices.paid')}
              </button>
              <button type="button" onClick={() => void onXml(i.id)}>
                {t('invoices.xml')}
              </button>
              <button
                type="button"
                onClick={() =>
                  void act(
                    api.POST('/invoices/credit-note', {
                      params: { header: workspaceHeader() },
                      body: { parent_invoice_id: i.id },
                    }),
                  )
                }
              >
                {t('invoices.creditNote')}
              </button>
            </li>
          ))}
        </ul>
      )}

      {lineFor && (
        <form onSubmit={(e) => void onAddLine(e)} className="row">
          <input
            required
            placeholder={t('invoices.lineDesc')}
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
          />
          <input
            type="number"
            placeholder={t('invoices.price')}
            value={price}
            onChange={(e) => setPrice(Number(e.target.value))}
          />
          <input
            type="number"
            value={qty}
            onChange={(e) => setQty(Number(e.target.value))}
          />
          <input
            type="number"
            value={vat}
            onChange={(e) => setVat(Number(e.target.value))}
          />
          <button type="submit">{t('invoices.addLine')}</button>
        </form>
      )}

      {xml && (
        <pre className="xml" aria-label="FatturaPA XML">
          {xml}
        </pre>
      )}
    </section>
  )
}
