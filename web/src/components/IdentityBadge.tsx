// Punto 4 ADR-0028: distinguish at a glance who an assignee is — a
// human user or an AI agent — in task/note list rows. Visually
// distinct from TagChip (no tag color, no kind glyph) because identity
// is a separate axis from tags. The icon doubles as the screen-reader
// label; the handle is rendered next to it when present.

type Kind = 'user' | 'ai_assistant' | string | null | undefined

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
  title,
  className,
}: {
  kind: Kind
  handle?: string | null
  title?: string
  className?: string
}) {
  const isBot = kind === 'ai_assistant'
  const isHuman = kind === 'user'
  if (!isBot && !isHuman) return null
  const cls = ['idbadge', `idbadge--${isBot ? 'bot' : 'human'}`, className]
    .filter(Boolean)
    .join(' ')
  const t =
    title ??
    (isBot
      ? handle
        ? `Bot: ${handle}`
        : 'Bot'
      : handle
        ? `User: ${handle}`
        : 'User')
  return (
    <span className={cls} title={t}>
      {isBot ? <BotIcon /> : <HumanIcon />}
      {handle ? <span className="idbadge__handle">{handle}</span> : null}
    </span>
  )
}
