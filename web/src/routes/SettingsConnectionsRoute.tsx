import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AiAssistantsSettings } from '../components/AiAssistantsSettings'
import { OAuthClientsSettings } from '../components/OAuthClientsSettings'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { mcpServerUrl } from '../lib/aiAssistants'

// Settings -> Connections: everything about letting an agent or an MCP host
// in (ADR-0069).
//
// Its own tab rather than a card on Workspace, because connecting a host is
// a task people come to do, not a property they browse past: the page leads
// with the one URL to paste and the three ways a host can identify itself,
// then the credentials that exist. It used to sit among the workspace's mail
// account and estimate presets, which is where nobody looked for it.
//
// Assistants and hand-registered OAuth clients are PER-WORKSPACE data, so the
// page says which workspace before anything else, the way the Workspace tab
// does: switching workspace in the header shows a different list here.

export function SettingsConnectionsRoute() {
  const { t } = useTranslation()
  const { ws } = useMyWorkspace()
  const url = mcpServerUrl(window.location.origin)
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // No clipboard (older browser, insecure context): the URL is on the
      // screen to be copied by hand.
    }
  }

  return (
    <>
      <section className="card">
        <h2>{t('connections.title')}</h2>
        <p className="muted">
          {t('connections.workspaceIs', { name: ws?.name ?? t('common.dashEmpty') })}
        </p>
        <p className="hint">{t('connections.perWorkspace')}</p>
      </section>

      <section className="card card--wide">
        <h2>{t('connections.serverTitle')}</h2>
        <p>{t('connections.serverIntro')}</p>
        <div className="row aiconnector__row">
          <code className="aiconnector__url" aria-label={t('connections.serverUrlLabel')}>
            {url}
          </code>
          <button type="button" className="btn--ghost btn--sm" onClick={() => void copy()}>
            {copied ? t('aiAssistants.copied') : t('aiAssistants.copy')}
          </button>
        </div>
        <p className="muted">{t('connections.modesIntro')}</p>
        <ul>
          <li>{t('connections.modeDynamic')}</li>
          <li>{t('connections.modeMetadata')}</li>
          <li>{t('connections.modeClientId')}</li>
          <li>{t('connections.modeSecret')}</li>
        </ul>
        <p className="hint">{t('connections.consentHint')}</p>
      </section>

      <AiAssistantsSettings />

      <section className="card card--wide">
        <OAuthClientsSettings />
      </section>
    </>
  )
}
