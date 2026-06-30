// Mycelium mark: a fungal mycelial network -- a central node with hyphae
// branching to spore nodes, plus a couple of anastomosis cross-links.
// No wordmark. Mirrors the favicon at 32×32; uses CSS vars so it adapts
// to light/dark themes.
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role="img"
      aria-label="Mycelium"
    >
      <rect width="32" height="32" rx="7" fill="var(--brand)" />
      {/* Hyphae */}
      <g fill="none" stroke="var(--brand-fg)" strokeWidth="1" strokeLinecap="round">
        <path d="M16 16.5 C13.5 14.5 12 12 11 8.5" />
        <path d="M12 12 C10 11.5 8 10.5 6 8.5" />
        <path d="M16 16.5 C18.5 14.5 20 12 21 8.5" />
        <path d="M20 12 C22 11.5 24 10.5 26 9" />
        <path d="M16 16.5 C12.5 16.6 9 17 5.5 16" />
        <path d="M16 16.5 C19.5 16.6 23 17.2 26.5 18.5" />
        <path d="M16 16.5 C14 19 12 21.5 10 24.5" />
        <path d="M12.5 21 C11 22.5 9.5 24 8.5 26.5" />
        <path d="M16 16.5 C18 19.5 19.5 22 20.5 25.5" />
        <path d="M11 8.5 C14.5 10 17.5 10 21 8.5" strokeWidth="0.6" opacity="0.5" />
        <path d="M10 24.5 C14 26.5 17 26 20.5 25.5" strokeWidth="0.6" opacity="0.5" />
      </g>
      {/* Spore nodes */}
      <g fill="var(--brand-fg)">
        <circle cx="16" cy="16.5" r="1.7" />
        <circle cx="11" cy="8.5" r="1.1" />
        <circle cx="6" cy="8.5" r="0.95" />
        <circle cx="21" cy="8.5" r="1.1" />
        <circle cx="26" cy="9" r="0.95" />
        <circle cx="5.5" cy="16" r="1" />
        <circle cx="26.5" cy="18.5" r="1.05" />
        <circle cx="10" cy="24.5" r="1.05" />
        <circle cx="8.5" cy="26.5" r="0.85" />
        <circle cx="20.5" cy="25.5" r="1.1" />
      </g>
    </svg>
  )
}
