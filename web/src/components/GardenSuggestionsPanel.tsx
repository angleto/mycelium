import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { GardenIcon, type GardenIconName } from './GardenIcon'

type Classify = components['schemas']['GardenClassifyOut']
type SuggestionType = 'tag' | 'link' | 'maturity'

const LINK_ICON_KINDS: readonly GardenIconName[] = [
  'atom_of',
  'references',
  'contradicts',
  'extends',
  'derives_from',
  'cites',
] as const

function linkIcon(kind: string): GardenIconName {
  return (LINK_ICON_KINDS as readonly string[]).includes(kind)
    ? (kind as GardenIconName)
    : 'references'
}

// ADR-0032 proposal engine, consumer side. Read-only suggestions
// {tags, links, maturity, cluster} surfaced as accept/dismiss chips on
// the open note; hover shows the rationale + confidence. Accept/dismiss
// POST /garden/apply, which mutates (accept) or just records the
// decision (dismiss=reject) and writes the classification_feedback
// event. Cluster is informational in v1 (clusters are computed, not
// stored), so it is shown read-only with no action.
export function GardenSuggestionsPanel({
  noteId,
  onApplied,
}: {
  noteId: string
  onApplied?: () => void
}) {
  const { t } = useTranslation()
  const [data, setData] = useState<Classify | null>(null)
  const [tagsById, setTagsById] = useState<Record<string, string>>({})
  const [notesById, setNotesById] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr(null)
    const { data: res, error } = await api.GET('/garden/classify/{node_id}', {
      params: { path: { node_id: noteId }, header: workspaceHeader() },
    })
    if (error) {
      setErr(errMessage(error))
      setData(null)
    } else {
      setData(res ?? null)
    }
  }, [noteId])

  useEffect(() => {
    let active = true
    void (async () => {
      setLoading(true)
      const [cls, tags, notes] = await Promise.all([
        api.GET('/garden/classify/{node_id}', {
          params: { path: { node_id: noteId }, header: workspaceHeader() },
        }),
        api.GET('/tags', { params: { header: workspaceHeader() } }),
        api.GET('/notes', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (cls.error) setErr(errMessage(cls.error))
      else setData(cls.data ?? null)
      if (tags.data) {
        setTagsById(Object.fromEntries(tags.data.map((x) => [x.id, x.name])))
      }
      if (notes.data) {
        setNotesById(
          Object.fromEntries(
            notes.data.map((n) => [n.id, n.title?.trim() || t('notes.untitled')]),
          ),
        )
      }
      setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [noteId, t])

  const apply = useCallback(
    async (
      suggestionType: SuggestionType,
      suggestionValue: Record<string, unknown>,
      action: 'accept' | 'reject',
      key: string,
    ) => {
      setBusy(key)
      setErr(null)
      const { error } = await api.POST('/garden/apply', {
        params: { header: workspaceHeader() },
        body: {
          node_id: noteId,
          suggestion_type: suggestionType,
          suggestion_value: suggestionValue,
          action,
        },
      })
      setBusy(null)
      if (error) {
        setErr(errMessage(error))
        return
      }
      await reload()
      onApplied?.()
    },
    [noteId, reload, onApplied],
  )

  const pct = (c: number) => Math.round(c * 100)

  const hasAny = useMemo(
    () =>
      !!data &&
      (data.tags.length > 0 || data.links.length > 0 || data.maturity != null),
    [data],
  )

  if (loading) {
    return (
      <div className="linkedpanel">
        <p className="hint">{t('gardenSuggest.loading')}</p>
      </div>
    )
  }

  return (
    <div className="linkedpanel">
      <div className="linkedpanel__head">
        <strong>{t('gardenSuggest.title')}</strong>
        <span className="muted">{t('gardenSuggest.headHint')}</span>
      </div>
      {err && <p className="error">{err}</p>}
      {!hasAny && !err && !data?.cluster && (
        <p className="hint">{t('gardenSuggest.empty')}</p>
      )}

      {data && data.tags.length > 0 && (
        <section className="linkedpanel__section">
          <header className="linkedpanel__sectionhead">
            <span className="chip">
              <GardenIcon name="leaf" size={14} />
              {t('gardenSuggest.tags')}
            </span>
            <span className="muted">({data.tags.length})</span>
          </header>
          <ul className="linkedpanel__list">
            {data.tags.map((s) => {
              const k = `tag:${s.tag_id}`
              return (
                <li
                  key={k}
                  className="linkedpanel__item"
                  title={`${s.rationale} · ${t('gardenSuggest.confidence', { pct: pct(s.confidence) })}`}
                >
                  <span className="linkedpanel__title">
                    {tagsById[s.tag_id] ?? t('gardenSuggest.unknownTag')}
                  </span>
                  <span className="muted">{pct(s.confidence)}%</span>
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    disabled={busy === k}
                    title={t('gardenSuggest.accept')}
                    onClick={() => void apply('tag', { tag_id: s.tag_id }, 'accept', k)}
                  >
                    ✓
                  </button>
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    disabled={busy === k}
                    title={t('gardenSuggest.dismiss')}
                    onClick={() => void apply('tag', { tag_id: s.tag_id }, 'reject', k)}
                  >
                    ×
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {data && data.links.length > 0 && (
        <section className="linkedpanel__section">
          <header className="linkedpanel__sectionhead">
            <span className="chip">
              <GardenIcon name="references" size={14} />
              {t('gardenSuggest.links')}
            </span>
            <span className="muted">({data.links.length})</span>
          </header>
          <ul className="linkedpanel__list">
            {data.links.map((s) => {
              const k = `link:${s.target_id}`
              return (
                <li
                  key={k}
                  className="linkedpanel__item"
                  title={`${s.rationale} · ${t('gardenSuggest.confidence', { pct: pct(s.confidence) })}`}
                >
                  <GardenIcon name={linkIcon(s.link_kind)} size={14} />
                  <span className="linkedpanel__title">
                    {notesById[s.target_id] ?? t('gardenSuggest.unknownNote')}
                  </span>
                  <span className="muted">{pct(s.confidence)}%</span>
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    disabled={busy === k}
                    title={t('gardenSuggest.accept')}
                    onClick={() =>
                      void apply(
                        'link',
                        { target_id: s.target_id, link_kind: s.link_kind },
                        'accept',
                        k,
                      )
                    }
                  >
                    ✓
                  </button>
                  <button
                    type="button"
                    className="btn--ghost btn--sm"
                    disabled={busy === k}
                    title={t('gardenSuggest.dismiss')}
                    onClick={() =>
                      void apply(
                        'link',
                        { target_id: s.target_id, link_kind: s.link_kind },
                        'reject',
                        k,
                      )
                    }
                  >
                    ×
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {data?.maturity && (
        <section className="linkedpanel__section">
          <header className="linkedpanel__sectionhead">
            <span className="chip">
              <GardenIcon name="leaf" size={14} />
              {t('gardenSuggest.maturity')}
            </span>
          </header>
          <div
            className="linkedpanel__item"
            title={`${data.maturity.rationale} · ${t('gardenSuggest.confidence', { pct: pct(data.maturity.confidence) })}`}
          >
            <span className="linkedpanel__title">
              {t('gardenSuggest.promoteMature')}
            </span>
            <span className="muted">{pct(data.maturity.confidence)}%</span>
            {data.maturity.auto_apply && (
              <span className="muted">{t('gardenSuggest.autoHint')}</span>
            )}
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={busy === 'maturity'}
              title={t('gardenSuggest.accept')}
              onClick={() =>
                void apply(
                  'maturity',
                  { value: data.maturity?.value ?? 'mature' },
                  'accept',
                  'maturity',
                )
              }
            >
              ✓
            </button>
            <button
              type="button"
              className="btn--ghost btn--sm"
              disabled={busy === 'maturity'}
              title={t('gardenSuggest.dismiss')}
              onClick={() =>
                void apply(
                  'maturity',
                  { value: data.maturity?.value ?? 'mature' },
                  'reject',
                  'maturity',
                )
              }
            >
              ×
            </button>
          </div>
        </section>
      )}

      {data?.cluster && (
        <p className="hint linkedpanel__empty">
          <GardenIcon name="cluster" size={14} />{' '}
          {t('gardenSuggest.clusterInfo', {
            id: data.cluster.leiden_id ?? '—',
            mod:
              data.cluster.modularity != null
                ? data.cluster.modularity.toFixed(2)
                : '—',
          })}
        </p>
      )}

      {data && data.signals_used.length > 0 && (
        <p className="hint linkedpanel__empty">
          {t('gardenSuggest.basedOn', { signals: data.signals_used.join(', ') })}
        </p>
      )}
    </div>
  )
}
