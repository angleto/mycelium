// Flow mark: a moon (top-right) over two wave lines — the moon pulls
// the tide, work keeps flowing. Accent app-tile, white marks (reads
// on light/dark since the tile is the accent).
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
      {/* Crescent moon, top-right. */}
      <path d="M23 4a6 6 0 1 0 0 12 4.6 4.6 0 1 1 0-12z" fill="#fff" />
      {/* Two wave lines = the sea/flow. */}
      <path
        d="M5 19q3-3.4 6 0t6 0 6 0 6 0"
        fill="none"
        stroke="#fff"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M5 25q3-3.4 6 0t6 0 6 0 6 0"
        fill="none"
        stroke="#fff"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
