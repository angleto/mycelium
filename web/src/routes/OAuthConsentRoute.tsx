import { type FormEvent, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { type OAuthConsent, type Scope, aiApi, oauthApi } from '../lib/aiAssistants'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { ScopePicker } from '../components/ScopePicker'
import { type ScopeSelection, selectedKeys, selectionOf } from '../lib/scopeSelection'

// The OAuth consent page (ADR-0069): the only thing standing between a
// registered client and a credential.
//
// An MCP host sends the browser here from ``/authorize`` with an opaque
// ``?request=``. The route sits behind the login guard, which carries the
// query through the login form, so a person who was not signed in comes back
// to the same request. Everything shown is read from the SERVER's copy of
// the request, never from this URL: the page cannot be told a different
// client or redirect than the one the code will go to.
//
// What the page must make impossible to miss is WHERE the code goes. The
// client's name is whatever its registrant typed and can impersonate
// anybody; the redirect target (scheme and host: ``https://claude.ai``,
// ``cursor://anysphere.cursor-mcp``) cannot be faked, so it is the largest
// thing on the card. The scheme is shown too because a native app's scheme
// is the whole of its identity. The workspace is shown because the assistant is created in the
// one that is active, and the header switcher is how to change it before
// approving.
//
// Approve and Deny are JSON calls carrying the SPA's bearer, not form posts:
// a page on another origin cannot make the browser attach that bearer, which
// is what keeps this from being forged. The server answers with the URL to
// go to next (the client's redirect URI with a code, or with
// ``error=access_denied``); this page navigates there and does not parse it.

type Navigate = (url: string) => void

/** ``https://claude.ai`` -> ``claude.ai``; ``com.example.app:`` stays whole. */
function targetHost(target: string): string {
  const i = target.indexOf('://')
  return i >= 0 ? target.slice(i + 3) : target
}

const leave: Navigate = (url) => window.location.assign(url)

export function OAuthConsentRoute({ navigate = leave }: { navigate?: Navigate }) {
  const { t } = useTranslation()
  const [params] = useSearchParams()
  const requestId = params.get('request') ?? ''
  const { ws } = useMyWorkspace()

  // Tagged with the id it answers, so a stale answer for another request
  // can never be rendered under this one.
  const [loaded, setLoaded] = useState<{ id: string; value: OAuthConsent | null } | null>(null)
  const [catalog, setCatalog] = useState<Scope[] | null>(null)
  const [label, setLabel] = useState('')
  const [perms, setPerms] = useState<ScopeSelection>({})
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!requestId) return
    let live = true
    void (async () => {
      try {
        const [consent, scopes] = await Promise.all([
          oauthApi.consent(requestId),
          aiApi.scopeCatalog(),
        ])
        if (!live) return
        setCatalog(scopes)
        // Not the client's name alone: that is whatever its registrant typed,
        // and the label is what the person will later read in their list of
        // credentials to decide what to revoke. The host the codes go to is
        // the part a registrant cannot choose.
        setLabel(
          t('oauthConsent.defaultLabel', {
            name: consent.client_name,
            host: targetHost(consent.redirect_target),
          }).slice(0, 255),
        )
        setPerms(selectionOf(scopes, consent.suggested_scope))
        setLoaded({ id: requestId, value: consent })
      } catch {
        // Every shape of "not a live request" is one answer from the server
        // (never existed, answered, expired), so the page says one thing.
        if (live) setLoaded({ id: requestId, value: null })
      }
    })()
    return () => {
      live = false
    }
  }, [requestId, t])

  const consent = loaded?.id === requestId ? loaded.value : undefined

  async function onApprove(e: FormEvent) {
    e.preventDefault()
    if (!consent) return
    const scope = selectedKeys(catalog ?? [], perms)
    if (scope.length === 0) {
      setErr(t('aiAssistants.atLeastOnePerm'))
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const answer = await oauthApi.approve(requestId, label.trim(), scope)
      navigate(answer.redirect_url)
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
      setBusy(false)
    }
  }

  async function onDeny() {
    if (!consent) return
    setBusy(true)
    setErr(null)
    try {
      const answer = await oauthApi.deny(requestId)
      navigate(answer.redirect_url)
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
      setBusy(false)
    }
  }

  if (!requestId || consent === null) {
    return (
      <section className="card">
        <h2>{t('oauthConsent.title')}</h2>
        <p className="muted">{t('oauthConsent.noRequest')}</p>
      </section>
    )
  }
  if (consent === undefined) {
    return (
      <section className="card">
        <h2>{t('oauthConsent.title')}</h2>
        <p className="hint">{t('common.loading')}</p>
      </section>
    )
  }

  return (
    <form className="card card--wide oauthconsent" onSubmit={(e) => void onApprove(e)}>
      <h2>{t('oauthConsent.title')}</h2>
      <p>{t('oauthConsent.asking', { name: consent.client_name })}</p>

      <p className="muted">{t('oauthConsent.sendsTo')}</p>
      <p className="oauthconsent__host" aria-label={t('oauthConsent.redirectHostLabel')}>
        {consent.redirect_target}
      </p>
      <p className="hint">
        <code>{consent.redirect_uri}</code>
      </p>
      <p className="hint">{t('oauthConsent.hostWarning')}</p>
      {consent.registration !== 'manual' && (
        <p className="hint">{t('oauthConsent.selfRegistered')}</p>
      )}

      <dl className="ext__facts">
        <dt>{t('oauthConsent.workspace')}</dt>
        <dd>{ws?.name ?? t('common.dashEmpty')}</dd>
        <dt>{t('oauthConsent.expiresAt')}</dt>
        <dd>{new Date(consent.expires_at).toLocaleTimeString()}</dd>
      </dl>
      <p className="hint">{t('oauthConsent.workspaceHint')}</p>

      <label className="aiform__full">
        <span className="muted">{t('oauthConsent.labelLabel')}</span>
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          required
          minLength={1}
          maxLength={255}
        />
      </label>

      <ScopePicker catalog={catalog} perms={perms} onChange={setPerms} />

      <p className="hint">{t('oauthConsent.revokeHint')}</p>
      {err && (
        <p className="err" role="alert">
          {err}
        </p>
      )}
      <div className="row aiform__actions">
        <button type="button" className="btn--ghost" disabled={busy} onClick={() => void onDeny()}>
          {t('oauthConsent.deny')}
        </button>
        <button type="submit" className="btn" disabled={busy || !label.trim()}>
          {t('oauthConsent.approve')}
        </button>
      </div>
    </form>
  )
}
