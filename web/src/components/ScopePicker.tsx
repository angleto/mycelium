import { type CSSProperties, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { CATEGORY_ORDER, type Scope } from '../lib/aiAssistants'
import type { ScopeSelection } from '../lib/scopeSelection'

// The scope grid an assistant's permissions are chosen with: categorized
// (read / write / danger), a confirmation before a dangerous key is ticked,
// select-all that asks first when it would tick dangerous ones, clear-all.
//
// Its own component because two places decide what a credential may do --
// the assistant form in the workspace settings and the OAuth consent page
// -- and they must present the same choice in the same words. A second copy
// on the consent page would be the one that drifts, and it is the one where
// a person is deciding about a client they did not create.

function categoryColor(cat: Scope['category']): string {
  return cat === 'danger' ? '#c0392b' : cat === 'write' ? '#d68910' : '#28a745'
}

export function ScopePicker({
  catalog,
  perms,
  onChange,
}: {
  catalog: readonly Scope[] | null
  perms: ScopeSelection
  onChange: (next: ScopeSelection) => void
}) {
  const { t } = useTranslation()

  const grouped = useMemo(() => {
    if (!catalog) return null
    const out = new Map<Scope['category'], Scope[]>()
    for (const cat of CATEGORY_ORDER) out.set(cat, [])
    for (const e of catalog) out.get(e.category)?.push(e)
    return out
  }, [catalog])

  const selectedCount = useMemo(() => Object.values(perms).filter(Boolean).length, [perms])

  function togglePermission(entry: Scope, next: boolean) {
    if (next && entry.category === 'danger') {
      if (!window.confirm(t('aiAssistants.dangerousScopeConfirm', { label: entry.label }))) return
    }
    onChange({ ...perms, [entry.key]: next })
  }
  function selectAll() {
    if (!catalog) return
    const dangerCount = catalog.filter((e) => e.category === 'danger').length
    if (dangerCount > 0) {
      if (!window.confirm(t('aiAssistants.selectAllConfirm', { dangerCount }))) return
    }
    onChange(Object.fromEntries(catalog.map((e) => [e.key, true])))
  }
  function clearAll() {
    if (!catalog) return
    onChange(Object.fromEntries(catalog.map((e) => [e.key, false])))
  }

  return (
    <fieldset className="aiform__perms">
      <legend>
        <span className="aiform__perms-title">
          <strong>{t('aiAssistants.permissions')}</strong>
          {catalog && (
            <span className="muted">
              {t('aiAssistants.scopesSelected', {
                selected: selectedCount,
                total: catalog.length,
              })}
            </span>
          )}
        </span>
        {catalog && (
          <span className="aiform__perms-actions">
            <button type="button" className="btn--ghost btn--sm" onClick={selectAll}>
              {t('aiAssistants.selectAllScopes')}
            </button>
            <button type="button" className="btn--ghost btn--sm" onClick={clearAll}>
              {t('aiAssistants.clearAllScopes')}
            </button>
          </span>
        )}
      </legend>
      {!grouped && <p className="hint">…</p>}
      {grouped &&
        CATEGORY_ORDER.map((cat) => {
          const entries = grouped.get(cat) ?? []
          if (entries.length === 0) return null
          const style: CSSProperties = {
            borderLeft: `3px solid ${categoryColor(cat)}`,
            background: cat === 'danger' ? 'rgba(192,57,43,0.05)' : undefined,
          }
          return (
            <div key={cat} className="aiform__perms-group" style={style}>
              <div className="aiform__perms-cat muted">
                {t(`aiAssistants.scopeCategory_${cat}`)}
              </div>
              {entries.map((entry) => (
                <label key={entry.key} className="aiform__perm">
                  <input
                    type="checkbox"
                    checked={!!perms[entry.key]}
                    onChange={(e) => togglePermission(entry, e.target.checked)}
                  />
                  <div>
                    <div>
                      <strong>{entry.label}</strong>{' '}
                      <code className="aiform__perm-key">{entry.key}</code>
                    </div>
                    {entry.description && <div className="hint">{entry.description}</div>}
                  </div>
                </label>
              ))}
            </div>
          )
        })}
    </fieldset>
  )
}
