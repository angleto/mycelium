import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useLinkedClientProject } from '../lib/linkedClientProject'
import { ClientSearch } from './ClientSearch'
import type { components } from '../shared'

type Tag = components['schemas']['TagOut']
type Project = components['schemas']['ProjectOut']
export type TagBriefLike = {
  id: string
  name: string
  color?: string | null
  kind?: string
}

// Stable empty list for the structural-less callers: the linked
// client/project hook must be called on every render, so it always
// needs a project catalogue -- an inline ``[]`` would be a fresh
// identity each time.
const NO_PROJECTS: Project[] = []

// docs/adr/0050: the structural pair is NOT a free-form facet. A task
// carries exactly one client and one project; a note exactly one client
// and AT MOST one project (the empty option is the un-share path, which
// rescopes the note's blobs -- memory_blobs.project_id NULL, ADR-0021);
// an email account's default bag is replayed through
// ``resolve_structural`` at ingest, so both of its axes may stay empty.
export type TagPickerStructural = {
  mode: 'task' | 'note' | 'defaults'
  // ProjectOut, not the project tags: only the profile carries
  // ``client_tag_id``, which is what couples the two selects.
  projects: Project[]
  onSetClient: (clientTagId: string | null) => void
  onSetProject: (projectTagId: string | null) => void
}

// One reusable tag widget: the structural client/project pair as two
// coupled single selects, then the free-form facets as removable chips
// plus a searchable, browsable list of the ones that can still be added.
// The list is GENERIC-only on purpose -- attaching a client or a project
// is a cardinality-constrained move the selects own (and memory_channel
// tags are system bookkeeping, never picked by hand: see MemoryRoute /
// GardenMindmap, which hide them too). Already-attached facets stay
// removable whatever their kind so an out-of-band channel tag can still
// be cleaned up.
export function TagPicker({
  selected,
  all,
  onAdd,
  onRemove,
  disabled,
  structural,
  error,
}: {
  selected: TagBriefLike[]
  all: Tag[]
  onAdd: (id: string) => void
  onRemove: (id: string) => void
  disabled?: boolean
  structural?: TagPickerStructural
  // Rendered inside the widget: a rejected client/project change (the
  // API answers DomainError -> 400 {code, detail}) has to surface next
  // to the control that caused it, not in the page-level banner.
  error?: string | null
}) {
  const { t } = useTranslation()
  const [q, setQ] = useState('')

  const clientTag = selected.find((s) => s.kind === 'client') ?? null
  const projectTag = selected.find((s) => s.kind === 'project') ?? null
  const clientTagId = clientTag?.id ?? ''
  const projectTagId = projectTag?.id ?? ''

  // The client<->project coupling (snap the client to the picked
  // project, drop a project the picked client does not own) is the
  // shared hook used by /tasks and /time; this widget only re-seeds it
  // from what the server wrote back. Keyed on the two ids and never on
  // ``selected``: callers rebuild that array on every render, which
  // would wipe a staged client pick before it can be committed.
  const linked = useLinkedClientProject(structural?.projects ?? NO_PROJECTS)
  const { setClientId, setProjectId } = linked
  useEffect(() => {
    // External-state sync (the server's answer wins over the staged
    // pick). The react-hooks/set-state-in-effect rule only fires on the
    // CI runner for now; disable it explicitly so the build is green
    // there, as TasksRoute already does for its own sync effects.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setClientId(clientTagId)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProjectId(projectTagId)
  }, [clientTagId, projectTagId, setClientId, setProjectId])

  // Options: active tags of the kind, plus the attached one when it is
  // archived (archiving hides a tag from the pickers, it never orphans
  // what it holds) so the select never renders a blank current value.
  const clientOpts = all
    .filter((g) => g.kind === 'client' && g.status !== 'archived')
    .map((g) => ({ id: g.id, name: g.name }))
  if (clientTag && !clientOpts.some((c) => c.id === clientTag.id))
    clientOpts.unshift({ id: clientTag.id, name: clientTag.name })
  const projectOpts = linked
    .filterProjectsByClient(structural?.projects ?? NO_PROJECTS)
    .filter((p) => p.status !== 'archived')
    .map((p) => ({ id: p.id, name: p.name }))
  if (projectTag && !projectOpts.some((p) => p.id === projectTag.id))
    projectOpts.unshift({ id: projectTag.id, name: projectTag.name })

  // A client the entity's project does not own cannot be committed on
  // its own (the API answers TAG_CLIENT_PROJECT_MISMATCH): the pick only
  // narrows the project list, and the move happens when a project of
  // that client is picked. Same staging for all three modes -- an email
  // default bag mixing client X with a project of client Y would blow up
  // at ingest instead, which is worse (it fails far from here).
  // ``linked.clientId`` is empty until the seeding effect above has run,
  // which is not a staged pick -- it would flash the hint on mount.
  const movePending =
    !!structural &&
    !!projectTagId &&
    !!linked.clientId &&
    linked.clientId !== clientTagId

  function pickClient(id: string) {
    linked.onPickClient(id)
    if (!structural || projectTagId) return
    // task / note always carry a client: "no client" is only a legal
    // end state for the email default bag.
    if (!id && structural.mode !== 'defaults') return
    structural.onSetClient(id || null)
  }

  function pickProject(id: string) {
    linked.onPickProject(id)
    if (!structural) return
    if (id) structural.onSetProject(id)
    else if (projectTagId) structural.onSetProject(null)
  }

  // Free-form facets only: the pair lives in the selects above, and its
  // chips would offer a remove the API rejects (TAG_STRUCTURAL_REQUIRED).
  const facets = structural
    ? selected.filter((s) => s.kind !== 'client' && s.kind !== 'project')
    : selected

  const matches = useMemo(() => {
    const sel = new Set(selected.map((s) => s.id))
    const needle = q.trim().toLowerCase()
    return all
      .filter(
        (g) => g.kind === 'generic' && g.status !== 'archived' && !sel.has(g.id),
      )
      .filter((g) => !needle || g.name.toLowerCase().includes(needle))
      .slice(0, 50)
  }, [all, q, selected])

  return (
    <div className="tagpick">
      {structural && (
        <>
          <div className="row">
            <label>
              {t('tagpicker.client', { defaultValue: 'Client' })}
              {/* A search, not a dropdown. This is the control that attaches a
                  client to a note or a task, and with one client per paying
                  customer the option list is unbounded -- so it enumerates
                  nothing and looks the picked client up by name, VAT or codice
                  fiscale. Nothing is hidden: every client is one word away. */}
              <ClientSearch
                currentName={
                  clientOpts.find((c) => c.id === linked.clientId)?.name ?? ''
                }
                allLabel={
                  structural.mode === 'defaults'
                    ? t('tagpicker.noClient', { defaultValue: 'No client' })
                    : undefined
                }
                onChange={(id) => pickClient(id)}
              />
            </label>
            <label>
              {t('tagpicker.project', { defaultValue: 'Project' })}
              <select
                value={linked.projectId}
                disabled={disabled}
                onChange={(e) => pickProject(e.target.value)}
              >
                {structural.mode === 'task' ? (
                  !linked.projectId && (
                    <option value="" disabled>
                      …
                    </option>
                  )
                ) : (
                  <option value="">
                    {t('tagpicker.noProject', { defaultValue: 'No project' })}
                  </option>
                )}
                {projectOpts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {movePending && (
            <p className="hint">
              {structural.mode === 'task'
                ? t('tagpicker.pickProjectToMove', {
                    defaultValue:
                      'Pick a project of this client: the project decides the client.',
                  })
                : t('tagpicker.pickProjectOrNone', {
                    defaultValue:
                      'Pick a project of this client, or drop the project first: the project decides the client.',
                  })}
            </p>
          )}
        </>
      )}
      {error && <p className="err">{error}</p>}
      <div className="chips">
        {facets.length === 0 && (
          <span className="hint">
            {structural
              ? t('tagpicker.noFacets', { defaultValue: 'No other tags' })
              : t('tagpicker.none')}
          </span>
        )}
        {facets.map((s) => (
          <button
            key={s.id}
            type="button"
            className="chip chip--rm"
            disabled={disabled}
            title={t('tagpicker.remove')}
            onClick={() => onRemove(s.id)}
          >
            {s.name} ✕
          </button>
        ))}
      </div>
      <input
        className="tagpick__search"
        placeholder={t('tagpicker.search')}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        disabled={disabled}
      />
      <ul className="tagpick__list">
        {matches.length === 0 ? (
          <li className="hint tagpick__empty">{t('tagpicker.noMatch')}</li>
        ) : (
          matches.map((g) => (
            <li key={g.id}>
              <button
                type="button"
                className="tagpick__opt"
                disabled={disabled}
                onClick={() => {
                  onAdd(g.id)
                  setQ('')
                }}
              >
                <span
                  className="chip__dot"
                  style={{ background: g.color || 'var(--accent)' }}
                />
                {g.name}
              </button>
            </li>
          ))
        )}
      </ul>
    </div>
  )
}
