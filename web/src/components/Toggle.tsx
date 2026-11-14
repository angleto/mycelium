import type { ReactNode } from 'react'

// A stateful switch drawn as a control, not as a sentence.
//
// The eleven switches this replaces wrote their state into their label
// ("Date focus: off", "Hide done: on"). That made every label two words
// longer than its meaning, so on a phone the button wrapped to three
// lines inside a pill-shaped border, and it made the state something to
// READ rather than something to see. Here the state is the box (empty or
// ticked, in the accent when on) and `aria-checked` carries it to a
// screen reader, which is what `role="switch"` is for; the label is just
// the name of the thing being switched.
export function Toggle({
  on,
  onChange,
  title,
  small,
  className,
  children,
}: {
  on: boolean
  onChange: (next: boolean) => void
  title?: string
  small?: boolean
  className?: string
  children: ReactNode
}) {
  const cls = [
    'toggle-pill',
    on ? 'toggle-pill--on' : '',
    small ? 'toggle-pill--sm' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      className={cls}
      title={title}
      onClick={() => onChange(!on)}
    >
      <span className="toggle-pill__box" aria-hidden="true" />
      <span className="toggle-pill__label">{children}</span>
    </button>
  )
}
