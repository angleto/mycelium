import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, workspaceHeader } from '../api/client'
import { saveWorkspaceSettings, useMyWorkspace } from '../auth/useMyWorkspace'
import type { components } from '../shared'

// ADR-0076 phase B: in general an assistant does not have the last word on
// what enters the memory; for the decisions the person considers simple, they
// hand it over here, one kind at a time. Every kind listed is reversible: each
// decision an assistant takes is listed on the review page and can be undone
// there. Declining a fact is not offered because a declined fact cannot be
// taken back. Beside each kind, how often the person has reversed the
// assistants so far: the measure of whether the hand-over is working.

type Kind = components['schemas']['DecisionKind']
type Stats = components['schemas']['GardenDelegationStatsOut']

const KINDS: readonly Kind[] = [
  'synthesis_decline',
  'synthesis_approve_checked',
  'link_proposal_accept',
  'link_proposal_reject',
  'fact_approve',
]

export function GardenDelegationSettings() {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const [draft, setDraft] = useState<Kind[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [stats, setStats] = useState<Stats[]>([])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data } = await api.GET('/garden/delegation/stats', {
        params: { header: workspaceHeader() },
      })
      if (active) setStats(data ?? [])
    })()
    return () => {
      active = false
    }
  }, [])

  const stored = (ws?.settings?.garden_delegation ?? []) as Kind[]
  const chosen = draft ?? stored

  function toggle(k: Kind) {
    setMsg(null)
    setDraft(chosen.includes(k) ? chosen.filter((x) => x !== k) : [...chosen, k])
  }

  async function save() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const res = await saveWorkspaceSettings({ garden_delegation: chosen })
    setBusy(false)
    if (!res.ok) {
      setErr(res.message)
      return
    }
    setDraft(null)
    setMsg(t('gardenDelegation.saved'))
  }

  return (
    <section className="card" aria-labelledby="gdeleg-title">
      <h2 id="gdeleg-title">{t('gardenDelegation.title')}</h2>
      <p className="hint">{t('gardenDelegation.intro')}</p>
      {err && <p className="err">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      <fieldset className="gdeleg">
        <legend className="sr-only">{t('gardenDelegation.title')}</legend>
        {KINDS.map((k) => {
          const s = stats.find((x) => x.kind === k)
          return (
            <label key={k} className="gdeleg__kind">
              <input type="checkbox" checked={chosen.includes(k)} onChange={() => toggle(k)} />
              <span className="gdeleg__text">
                <span className="gdeleg__name">{t(`gardenDelegation.kind.${k}.name`)}</span>
                <span className="hint">{t(`gardenDelegation.kind.${k}.what`)}</span>
                {s && (
                  <span className="hint">
                    {t('gardenDelegation.stats', { decided: s.decided, reversed: s.reversed })}
                  </span>
                )}
              </span>
            </label>
          )
        })}
      </fieldset>
      <p className="hint">{t('gardenDelegation.never')}</p>
      <div className="row">
        <button type="button" className="btn--sm" disabled={busy || !ws} onClick={() => void save()}>
          {busy ? t('wsmgr.saving') : t('wsmgr.save')}
        </button>
      </div>
    </section>
  )
}
