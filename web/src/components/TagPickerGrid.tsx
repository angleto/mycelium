import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { kindGlyph } from '../lib/tagGlyph'
import type { components } from '../api/schema'

type Tag = components['schemas']['TagOut']

// Visual tag-picker that lays every available tag as a toggleable chip
// (vs. the search-as-you-type TagPicker). When the catalog is small
// the chip grid is faster to scan than a dropdown and matches the
// pattern already used in /memory; reusing it across routes keeps the
// selection UX consistent.
//
// - ``selected`` and ``onToggle`` are controlled by the parent; this
//   component never mutates external state itself.
// - When ``groupByKind`` is on, chips are split into one section per
//   TagKind (client, project, generic, memory_channel), each section
//   sorted by name. Otherwise the order is whatever the parent passed.
// - A search input filters the visible chips by name (and kind prefix
//   like ``project:`` so ``project:`` typed alone groups projects).
// - Archived tags are excluded by the parent — this widget renders
//   exactly what it gets.
export function TagPickerGrid({
  tags,
  selected,
  onToggle,
  groupByKind = false,
  searchable = true,
  emptyHint,
}: {
  tags: Tag[]
  selected: ReadonlySet<string> | string[]
  onToggle: (id: string) => void
  groupByKind?: boolean
  searchable?: boolean
  emptyHint?: string
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')
  const selSet =
    selected instanceof Set ? selected : new Set<string>(selected)

  const needle = q.trim().toLowerCase()
  const filtered = needle
    ? tags.filter((g) =>
        `${g.kind} ${g.name}`.toLowerCase().includes(needle),
      )
    : tags

  const groups: { kind: string; tags: Tag[] }[] = groupByKind
    ? (() => {
        const order = ['client', 'project', 'memory_channel', 'generic']
        const byKind = new Map<string, Tag[]>()
        for (const g of filtered) {
          const k = g.kind || 'generic'
          if (!byKind.has(k)) byKind.set(k, [])
          byKind.get(k)!.push(g)
        }
        return order
          .filter((k) => byKind.has(k))
          .map((k) => ({
            kind: k,
            tags: byKind.get(k)!.slice().sort((a, b) => a.name.localeCompare(b.name)),
          }))
      })()
    : [{ kind: '', tags: filtered }]

  if (tags.length === 0) {
    return <p className="hint">{emptyHint ?? t('tagpicker.none')}</p>
  }

  return (
    <div className="tagpickgrid">
      {searchable && (
        <input
          className="tagpickgrid__search"
          placeholder={t('tagpicker.search')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      {groups.map((grp) => (
        <div key={grp.kind || '_'} className="tagpickgrid__group">
          {grp.kind && (
            <span className="tagpickgrid__kind muted">{grp.kind}</span>
          )}
          {grp.tags.length === 0 ? (
            <span className="hint">{t('tagpicker.noMatch')}</span>
          ) : (
            grp.tags.map((g) => {
              const on = selSet.has(g.id)
              const color = g.color || undefined
              const style: React.CSSProperties = on
                ? color
                  ? {
                      background: color,
                      borderColor: color,
                      color: '#fff',
                    }
                  : {}
                : color
                  ? { borderColor: `${color}66` }
                  : {}
              return (
                <button
                  key={g.id}
                  type="button"
                  aria-pressed={on}
                  className={
                    'chip ' + (on ? 'chip--on' : 'chip--off')
                  }
                  style={style}
                  title={`${g.kind}: ${g.name}`}
                  onClick={() => onToggle(g.id)}
                >
                  <span
                    className="chip__glyph"
                    style={{ color: color || 'currentColor' }}
                    aria-hidden="true"
                  >
                    {kindGlyph(g.kind)}
                  </span>
                  {g.name}
                </button>
              )
            })
          )}
        </div>
      ))}
    </div>
  )
}
