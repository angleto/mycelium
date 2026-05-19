// Flow mark: a crescent moon over a tide wave — the moon pulls the
// tide, the assistant keeps your work flowing. Accent app-tile with
// white marks (reads on light/dark since the tile is the accent).
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role="img"
      aria-label="Flow"
    >
      <rect width="32" height="32" rx="7" fill="var(--accent)" />
      {/* Crescent: outer disc minus an offset disc. */}
      <path
        d="M17 4a9 9 0 1 0 0 18 7 7 0 1 1 0-18z"
        fill="#fff"
      />
      {/* Tide: a calm two-crest wave under the moon. */}
      <path
        d="M5 25q3.25-4.5 6.5 0t6.5 0 6.5 0"
        fill="none"
        stroke="#fff"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
