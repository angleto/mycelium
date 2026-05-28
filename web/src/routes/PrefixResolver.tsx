import { useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { isFullUuid, lookupPrefix, type LookupOut } from '../lib/prefixLookup'

// Route shim for the short URLs ``/n/:prefix`` and ``/t/:prefix``,
// plus the prefix-upgrade behaviour on the canonical ``/notes/:id``
// / ``/tasks/:id`` routes when the caller pasted a prefix instead of
// a full UUID (the long-standing "I can't substitute the id in the
// URL bar" friction).
//
// One match → ``<Navigate replace>``. The replace=true keeps the
// browser back-button useful (the short URL doesn't appear in
// history; the canonical one does).
//
// Zero matches → a small 404 panel with a hint to use search. We
// intentionally don't redirect to /notes: silently swallowing a
// failed resolve would mask the bad link.
//
// More than one match → a compact disambiguator. We don't auto-pick
// even though the resolver orders by recency, because going to the
// wrong entity silently is the worst outcome of the three. The
// disambiguator is read-only: it lists kind + title + state and the
// user clicks the correct row.

interface Props {
  kind: 'task' | 'note'
}

export function PrefixResolver({ kind }: Props) {
  const { prefix: raw = '' } = useParams<{ prefix?: string }>()
  return <Resolver prefix={raw} kind={kind} />
}

interface ResolverProps {
  prefix: string
  kind: 'task' | 'note'
}

function Resolver({ prefix, kind }: ResolverProps) {
  const [data, setData] = useState<LookupOut | null | undefined>(undefined)
  // Derive-from-prop reset (React docs "Adjusting state on prop change").
  // When the route param changes mid-mount we must drop the stale
  // resolution so the UI shows a loading state instead of yesterday's
  // hit; setting state during render is the idiomatic pattern here
  // and avoids the cascading-render lint that setState-in-effect
  // would trigger.
  const propKey = `${prefix}|${kind}`
  const [lastKey, setLastKey] = useState(propKey)
  if (lastKey !== propKey) {
    setLastKey(propKey)
    setData(undefined)
  }
  useEffect(() => {
    let alive = true
    void lookupPrefix(prefix, { kinds: [kind] }).then((res) => {
      if (alive) setData(res)
    })
    return () => {
      alive = false
    }
  }, [prefix, kind])

  if (data === undefined) {
    return (
      <div className="route-loading" role="status">
        Risolvo {kind} <code>{prefix}</code>…
      </div>
    )
  }
  if (!data || data.matches.length === 0) {
    return (
      <div className="route-not-found">
        <h2>Nessun {kind} con prefisso <code>{prefix}</code></h2>
        <p>
          Controlla il prefisso, oppure usa la <Link to="/notes">ricerca</Link> sulle note.
        </p>
      </div>
    )
  }
  if (data.matches.length === 1) {
    return <Navigate to={data.matches[0].route_url} replace />
  }
  return (
    <div className="route-disambig">
      <h2>
        Il prefisso <code>{prefix}</code> combacia con {data.matches.length} entit{data.matches.length === 2 ? 'à' : 'à'}
      </h2>
      <ul className="route-disambig__list">
        {data.matches.map((m) => (
          <li key={`${m.kind}-${m.id}`}>
            <Link to={m.route_url} className="chip">
              <span className="chip__glyph" aria-hidden="true">
                {m.kind === 'task' ? '✓' : '◆'}
              </span>
              <span className="chip__label">{m.title ?? m.id}</span>
              {m.kind === 'task' && m.state_name && (
                <span className="chip__state"> ({m.state_name})</span>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Used by the canonical ``/tasks/:id`` and ``/notes/:id`` routes to
// allow prefix substitution in the URL bar. When the URL-supplied
// id is already a full UUID we delegate to the original route
// component (passed as ``children``); otherwise we run the prefix
// resolver and Navigate to the canonical id.
export function PrefixOrUuid({
  kind,
  paramName = 'id',
  children,
}: {
  kind: 'task' | 'note'
  paramName?: string
  children: React.ReactNode
}) {
  const params = useParams()
  const raw = (params[paramName] ?? '').trim()
  if (raw && !isFullUuid(raw) && raw.length >= 4) {
    return <Resolver prefix={raw} kind={kind} />
  }
  return <>{children}</>
}
