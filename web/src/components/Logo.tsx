// Flow mark: a flowing curve linking two nodes (input -> output),
// the through-line of the assistant. currentColor-friendly.
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
      <path
        d="M6 21c5 0 5-10 10-10s5 10 10 10"
        fill="none"
        stroke="#fff"
        strokeWidth="3"
        strokeLinecap="round"
      />
      <circle cx="6" cy="21" r="2.6" fill="#fff" />
      <circle cx="26" cy="11" r="2.6" fill="#fff" />
    </svg>
  )
}
