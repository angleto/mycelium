import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  getCachedLookup,
  lookupPrefix,
  type LookupMatch,
  type LookupOut,
} from '../lib/prefixLookup'

// Renders a backtick-prefix-in-markdown (e.g. `91cf6aaa`) as a
// clickable chip once the resolver returns. Three terminal states:
//
//   * resolved=1 match  → Link to the entity, label = title, with
//     kind glyph and (for tasks) the workflow state in parens. Closed
//     tasks render the same strikethrough-gray as TaskMentionChip so
//     the visual language is uniform across mention DSL and prefix
//     references.
//   * resolved=0        → fall back to the plain ``<code>`` so the
//     reader still sees the prefix; unresolved means either the
//     prefix is a SHA / hex value with no entity behind it, or the
//     row is in another workspace.
//   * resolved>1        → first match is the Link target (most-recent
//     wins per resolver order) and a "+N" badge signals ambiguity.
//     Long-term we'll route this through a disambiguator (Cmd+K) but
//     in MVP the visible badge is enough to flag the case.

const KIND_GLYPH: Record<LookupMatch['kind'], string> = {
  task: '✓',
  note: '◆',
}

function pickPrimary(matches: LookupMatch[]): LookupMatch | null {
  if (!matches.length) return null
  // Tasks before notes (resolver already does this, but be defensive).
  const task = matches.find((m) => m.kind === 'task')
  return task ?? matches[0]
}

export function PrefixMentionChip({ prefix }: { prefix: string }) {
  const [data, setData] = useState<LookupOut | null | undefined>(() =>
    getCachedLookup(prefix) ?? undefined,
  )
  useEffect(() => {
    if (data !== undefined) return
    let alive = true
    void lookupPrefix(prefix).then((res) => {
      if (alive) setData(res)
    })
    return () => {
      alive = false
    }
  }, [prefix, data])

  if (data === undefined) {
    // Loading: render the bare prefix so the layout doesn't jump.
    return <code className="md-prefix md-prefix--pending">{prefix}</code>
  }
  if (!data || data.matches.length === 0) {
    return <code className="md-prefix md-prefix--unresolved">{prefix}</code>
  }

  const primary = pickPrimary(data.matches)
  if (!primary) {
    return <code className="md-prefix md-prefix--unresolved">{prefix}</code>
  }
  const extras = data.matches.length - 1
  const closed = primary.kind === 'task' && primary.is_terminal === true
  const cls =
    'chip md-prefix-chip' +
    (closed ? ' chip--task-closed' : '') +
    (!closed && primary.state_name ? ' chip--task-open' : '')
  const label = primary.title?.trim() || prefix
  const title =
    `${primary.kind} ${primary.id}` +
    (primary.title ? ` — ${primary.title}` : '') +
    (extras > 0 ? `  (+${extras} other match${extras === 1 ? '' : 'es'})` : '')

  return (
    <Link className={cls} to={primary.route_url} title={title}>
      <span className="chip__glyph" aria-hidden="true">
        {KIND_GLYPH[primary.kind]}
      </span>
      <span className="chip__label">{label}</span>
      {primary.kind === 'task' && primary.state_name && (
        <span className="chip__state" aria-label={`state ${primary.state_name}`}>
          {' '}
          ({primary.state_name})
        </span>
      )}
      {extras > 0 && (
        <span className="chip__more" aria-label={`${extras} more matches`}>
          {' '}
          +{extras}
        </span>
      )}
    </Link>
  )
}
