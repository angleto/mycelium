// Flow mark: a crescent moon in the TOP-RIGHT over two wave lines.
// The crescent is a white disc with a tile-coloured disc cut out of
// it (always renders as a clean crescent). Accent app-tile.
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
      {/* Crescent moon, top-right: white disc minus an offset
          tile-coloured disc. */}
      <circle cx="23" cy="8" r="6" fill="#fff" />
      <circle cx="29.2" cy="3.6" r="6" fill="var(--accent)" />
      {/* Two wave lines = the sea / flow. */}
      <path
        d="M5 20q3-3.4 6 0t6 0 6 0 6 0"
        fill="none"
        stroke="#fff"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M5 25.5q3-3.4 6 0t6 0 6 0 6 0"
        fill="none"
        stroke="#fff"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
