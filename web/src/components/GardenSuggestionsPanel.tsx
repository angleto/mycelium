import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../api/schema'
import { GardenIcon, type GardenIconName } from './GardenIcon'

type Classify = components['schemas']['GardenClassifyOut']
type SuggestionType = 'tag' | 'link' | 'maturity'

// ADR-0040 4-verb note<->note link model: each kind maps to its own
// forest glyph in GardenIcon; unknown kinds fall back to 'related'
// (the neutral undirected connector). Task links are always 'related'.
const LINK_ICON_KINDS: readonly GardenIconName[] = [
  'hypha_of',
  'related',
  'supersedes',
  'contradicts',
] as const

function linkIcon(kind: string): GardenIconName {
  return (LINK_ICON_KINDS as readonly string[]).includes(kind)
    ? (kind as GardenIconName)
    : 'related'
}

// ADR-0032 / ADR-0042 proposal engine, consumer side. Read-only suggestions
// {tags, links, maturity, cluster} surfaced as accept/dismiss chips on the
// open NOTE or TASK; hover shows the rationale + confidence. Accept/dismiss
// POST /garden/apply, which mutates (accept) or just records the decision
// (dismiss=reject) and writes the classification_feedback event. Cluster is
// informational (clusters are computed, not stored), shown read-only.
//
// The panel serves the persisted on-create suggestions when fresh
// (source='precomputed') or a live recompute (source='live'); the head shows
// the source + freshness and a refresh control (ADR-0042 D6).
export function GardenSuggestionsPanel({
  nodeId,
  nodeKind = 'note',
  onApplied,
}: {
  nodeId: string
  nodeKind?: 'note' | 'task'
  onApplied?: () => void
}) {
  const { t } = useTranslation()
  const [data, setData] = useState<Classify | null>(null)
  const [tagsById, setTagsById] = useState<Record<string, string>>({})
  // Link targets resolve to the node's OWN kind: a note suggests note links,
  // a task suggests related-task links (ADR-0042 D2).
  const [targetsById, setTargetsById] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(
    async (refresh = false) => {
      setErr(null)
      const { data: res, error } = await api.GET('/garden/classify/{node_id}', {
        params: {
          path: { node_id: nodeId },
          query: refresh ? { refresh: true } : {},
          header: workspaceHeader(),
        },
      })
      if (error) {
        setErr(errMessage(error))
        setData(null)
      } else {
        setData(res ?? null)
      }
    },
    [nodeId],
  )

  useEffect(() => {
    let active = true
    void (async () => {
      setLoading(true)
      const [cls, tags, targets] = await Promise.all([
        api.GET('/garden/classify/{node_id}', {
          params: { path: { node_id: nodeId }, header: workspaceHeader() },
        }),
        api.GET('/tags', { params: { header: workspaceHeader() } }),
        nodeKind === 'task'
          ? api.GET('/tasks', { params: { header: workspaceHeader() } })
          : api.GET('/notes', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (cls.error) setErr(errMessage(cls.error))
      else setData(cls.data ?? null)
      if (tags.data) {
        setTagsById(Object.fromEntries(tags.data.map((x) => [x.id, x.name])))
      }
      if (targets.data) {
        setTargetsById(
          Object.fromEntries(
            targets.data.map((x) => [x.id, x.title?.trim() || t('gardenSuggest.unknownTarget')]),
          ),
        )
      }
      setLoading(false)
    })()
    return () => {
      active = false
    }
  }, [nodeId, nodeKind, t])

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
          node_id: nodeId,
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
    [nodeId, reload, onApplied],
  )

  const doRefresh = useCallback(() => {
    setRefreshing(true)
    void reload(true).finally(() => setRefreshing(false))
  }, [reload])

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
        {data && (
          <span
            className="muted"
            title={t('gardenSuggest.computedAt', {
              when: new Date(data.generated_at).toLocaleString(),
            })}
          >
            {t(
              data.source === 'precomputed'
                ? 'gardenSuggest.sourcePrecomputed'
                : 'gardenSuggest.sourceLive',
            )}
          </span>
        )}
        <button
          type="button"
          className="btn--ghost btn--sm"
          disabled={refreshing}
          title={t('gardenSuggest.refresh')}
          onClick={doRefresh}
        >
          {refreshing ? '…' : '⟳'}
        </button>
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
              <GardenIcon name="related" size={14} />
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
                    {targetsById[s.target_id] ?? t('gardenSuggest.unknownTarget')}
                  </span>
                  <span
                    className="muted"
                    title={t(`garden.mindmap.linkKindHint.${s.link_kind}`)}
                  >
                    {t(`garden.mindmap.linkKind.${s.link_kind}`)}
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
