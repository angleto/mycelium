// Punto 4 ADR-0028 (+ migration 0093): distinguish at a glance who an
// assignee or a creator is — a human user, an AI assistant bound to an
// identity, or a bare MCP agent_token (legacy). Visually distinct from
// TagChip (no tag color, no kind glyph) because identity is a separate
// axis from tags. The icon doubles as the screen-reader label; the
// label (or handle) is rendered next to it when present.

type Kind = 'user' | 'ai_assistant' | 'mcp_token' | string | null | undefined

function HumanIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="8" r="3.2" />
      <path d="M5 20c.8-3.6 3.7-6 7-6s6.2 2.4 7 6" />
    </svg>
  )
}

function BotIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="5" y="8" width="14" height="11" rx="2.2" />
      <path d="M12 5v3M9 13h.01M15 13h.01M9 17h6" />
      <path d="M3 13v3M21 13v3" />
    </svg>
  )
}

export function IdentityBadge({
  kind,
  handle,
  label,
  title,
  className,
}: {
  kind: Kind
  // Workspace-unique handle (user identity or ai_assistant handle).
  // Used when no ``label`` is available.
  handle?: string | null
  // Human-readable display name (ai_assistants.label, or
  // agent_tokens.name for a bare MCP token). Wins over ``handle`` for
  // rendering when present, so the SPA shows the friendly name the
  // operator picked in Settings rather than the slug.
  label?: string | null
  title?: string
  className?: string
}) {
  // "mcp_token" is a bare agent_token (no ai_assistants row bound); it
  // is still an AI for badging purposes — same icon, label from the
  // token name.
  const isBot = kind === 'ai_assistant' || kind === 'mcp_token'
  const isHuman = kind === 'user'
  if (!isBot && !isHuman) return null
  const cls = ['idbadge', `idbadge--${isBot ? 'bot' : 'human'}`, className]
    .filter(Boolean)
    .join(' ')
  const display = label || handle || (isBot ? 'AI' : 'User')
  const t =
    title ??
    (isBot ? `Bot: ${display}` : `User: ${display}`)
  return (
    <span className={cls} title={t}>
      {isBot ? <BotIcon /> : <HumanIcon />}
      <span className="idbadge__handle">{display}</span>
    </span>
  )
}
