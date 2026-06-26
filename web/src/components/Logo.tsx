// Mycelium mark: Mycelium Trio. Tree + two mushrooms above the soil,
// mycelium network of four connected nodes below. Mirrors the
// favicon at 32×32; uses CSS vars so it adapts to light/dark themes.
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
      {/* Tree (left) */}
      <path
        d="M7 6.5 C5 11.5 4 16 4 19 L10 19 C10 16 9 11.5 7 6.5 Z"
        fill="var(--brand-fg)"
      />
      <rect x="6.4" y="19" width="1.2" height="2" fill="var(--brand-fg)" />
      {/* Mushroom (center) */}
      <path d="M12.5 13 C12.5 10 17.5 10 17.5 13 Z" fill="var(--brand-fg)" />
      <ellipse cx="15" cy="13" rx="2.6" ry="0.55" fill="var(--brand-fg)" />
      <rect x="14.2" y="13" width="1.6" height="6.5" fill="var(--brand-fg)" />
      {/* Mushroom (right, smaller) */}
      <path d="M21.5 15 C21.5 12.5 26 12.5 26 15 Z" fill="var(--brand-fg)" />
      <ellipse cx="23.75" cy="15" rx="2.35" ry="0.45" fill="var(--brand-fg)" />
      <rect x="23" y="15" width="1.5" height="4.5" fill="var(--brand-fg)" />
      {/* Soil line */}
      <path
        d="M2 21 L30 21"
        stroke="var(--brand-fg)"
        strokeWidth="0.5"
        strokeLinecap="round"
        strokeDasharray="0.8 1.2"
        opacity="0.55"
      />
      {/* Mycelium roots */}
      <path d="M7 21 Q6 24 4.5 26" stroke="var(--brand-fg)" strokeWidth="0.55" strokeLinecap="round" fill="none" opacity="0.7" />
      <path d="M7 21 Q9 25 12 27" stroke="var(--brand-fg)" strokeWidth="0.55" strokeLinecap="round" fill="none" opacity="0.75" />
      <path d="M15 21 Q13 24 11 26.5" stroke="var(--brand-fg)" strokeWidth="0.55" strokeLinecap="round" fill="none" opacity="0.75" />
      <path d="M15 21 Q17 24 19 26.5" stroke="var(--brand-fg)" strokeWidth="0.55" strokeLinecap="round" fill="none" opacity="0.75" />
      <path d="M23.75 21 Q22 24.5 19.5 27" stroke="var(--brand-fg)" strokeWidth="0.55" strokeLinecap="round" fill="none" opacity="0.75" />
      <path d="M23.75 21 Q26 24 28 25.5" stroke="var(--brand-fg)" strokeWidth="0.55" strokeLinecap="round" fill="none" opacity="0.7" />
      {/* Mycelium nodes */}
      <circle cx="4.5" cy="26" r="0.9" fill="var(--brand-fg)" opacity="0.85" />
      <circle cx="11.5" cy="26.7" r="1.05" fill="var(--brand-fg)" />
      <circle cx="19.5" cy="26.7" r="1.05" fill="var(--brand-fg)" />
      <circle cx="28" cy="25.5" r="0.9" fill="var(--brand-fg)" opacity="0.85" />
      {/* Inter-node connections */}
      <path d="M11.5 26.7 Q15.5 28.8 19.5 26.7" stroke="var(--brand-fg)" strokeWidth="0.45" strokeLinecap="round" fill="none" opacity="0.55" />
      <path d="M4.5 26 Q8 27.5 11.5 26.7" stroke="var(--brand-fg)" strokeWidth="0.4" strokeLinecap="round" fill="none" opacity="0.45" />
      <path d="M19.5 26.7 Q23.5 27.5 28 25.5" stroke="var(--brand-fg)" strokeWidth="0.4" strokeLinecap="round" fill="none" opacity="0.45" />
    </svg>
  )
}
