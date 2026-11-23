import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errMessage, workspaceHeader } from '../api/client'
import type { components } from '../shared'

type Backlink = components['schemas']['GardenWrittenBacklink']
type TargetKind = 'note' | 'task' | 'tag'

// ADR-0073 decision 5: where people WROTE a reference to this note, task or
// tag (a mention, a code chip, a bare code), with the sentence around it.
// It is the written layer beside the typed links, so it shows what the text
// says and offers no remove: the only way to take a written reference away
// is to edit the text that holds it. An ambiguous code (it also names other
// entities) is shown as such, and the person can say it means this one.
export function WrittenBacklinksPanel({
  targetKind,
  targetId,
}: {
  targetKind: TargetKind
  targetId: string
}) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<Backlink[]>([])
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    const { data, error } = await api.GET('/garden/references/backlinks', {
      params: {
        header: workspaceHeader(),
        query: { target_kind: targetKind, target_id: targetId },
      },
    })
    return { data, error }
  }, [targetKind, targetId])

  useEffect(() => {
    let active = true
    void (async () => {
      const { data, error } = await load()
      if (!active) return
      if (error) setErr(errMessage(error))
      else {
        setErr(null)
        setRows(data?.backlinks ?? [])
      }
    })()
    return () => {
      active = false
    }
  }, [load])

  async function choose(row: Backlink) {
    if (targetKind === 'tag') return
    setErr(null)
    const { error } = await api.POST('/garden/references/choose', {
      params: { header: workspaceHeader() },
      body: { reference_id: row.reference_id, target_kind: targetKind, target_id: targetId },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    const { data } = await load()
    setRows(data?.backlinks ?? [])
  }

  function sourceLink(row: Backlink): string {
    if (row.source_kind === 'task' && row.task_id) return `/tasks/${row.task_id}`
    const base = `/notes/${row.note_id ?? ''}`
    return row.part_id ? `${base}#part-${row.part_id}` : base
  }

  return (
    <div className="linkedpanel">
      <div className="linkedpanel__head">
        <h3 className="linkedpanel__h">{t('writtenBacklinks.title')}</h3>
        <span className="muted">{t('writtenBacklinks.headHint')}</span>
      </div>
      {err && <p className="error">{err}</p>}
      {rows.length === 0 ? (
        <p className="hint linkedpanel__empty">{t('writtenBacklinks.empty')}</p>
      ) : (
        <ul className="linkedpanel__list">
          {rows.map((row) => (
            <li key={row.reference_id} className="linkedpanel__item writtenref">
              <div className="writtenref__body">
                <Link className="linkedpanel__title" to={sourceLink(row)}>
                  {row.title?.trim() || t('writtenBacklinks.untitled')}
                </Link>
                {(row.quote_exact ?? '') !== '' && (
                  <q className="writtenref__quote">
                    {row.quote_prefix}
                    <mark>{row.quote_exact}</mark>
                    {row.quote_suffix}
                  </q>
                )}
              </div>
              {row.source_archived && (
                <span className="muted">{t('writtenBacklinks.archived')}</span>
              )}
              {row.also_typed && (
                <span className="muted" title={t('writtenBacklinks.alsoTypedHint')}>
                  {t('writtenBacklinks.alsoTyped')}
                </span>
              )}
              {row.status === 'ambiguous' && targetKind !== 'tag' && (
                <button
                  type="button"
                  className="btn--ghost btn--sm"
                  title={t('writtenBacklinks.ambiguousHint')}
                  onClick={() => void choose(row)}
                >
                  {t('writtenBacklinks.chooseThis')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
