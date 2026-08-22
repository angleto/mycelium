// Which workspace you are in, and where you land when that stops being
// a place you can be.
//
// These are pure selectors over the `/workspaces` list, deliberately
// kept out of the components: the interesting cases are the awkward
// ones (you deleted the workspace you were standing in; the remembered
// one is archived; every workspace you have left is archived) and they
// are worth asserting rather than arguing about. See
// workspaceChoice.test.ts.

// Structural, not `components['schemas']['WorkspaceSummaryOut']`: the
// helpers only need these four fields, and typing them structurally is
// what lets the tests build fixtures without dragging the generated
// API surface in.
export type WorkspaceChoice = {
  id: string
  name: string
  role: string
  status: string
}

export const ARCHIVED = 'archived'

function byName<T extends { name: string }>(a: T, b: T): number {
  return a.name.localeCompare(b.name)
}

/** What the sidebar switcher offers: every ACTIVE workspace, plus the
 * one you are currently in even when it is archived. A switcher that
 * hides where you are standing is a switcher that lies — archiving the
 * active workspace does not evict you (the backend keeps it fully
 * usable), so the control has to be able to say so. */
export function switchableWorkspaces<T extends WorkspaceChoice>(
  list: readonly T[],
  activeId: string | null,
): T[] {
  return list
    .filter((w) => w.status !== ARCHIVED || w.id === activeId)
    .slice()
    .sort(byName)
}

/** The archived ones, which only the settings screen lists — that is
 * where you restore or delete them. The active workspace is excluded
 * even when archived: the switcher above already shows it, and it is
 * not "put away" while you are inside it. */
export function archivedWorkspaces<T extends WorkspaceChoice>(
  list: readonly T[],
  activeId: string | null,
): T[] {
  return list
    .filter((w) => w.status === ARCHIVED && w.id !== activeId)
    .slice()
    .sort(byName)
}

/** Post-login landing: the remembered workspace when it is still there
 * and still active, else the first active one by name, else the first
 * one at all. The last fallback matters — an account whose workspaces
 * are ALL archived must still be able to log in and un-archive one. */
export function initialWorkspaceId<T extends WorkspaceChoice>(
  list: readonly T[],
  remembered: string | null,
): string | null {
  if (list.length === 0) return null
  const kept = remembered ? list.find((w) => w.id === remembered) : undefined
  if (kept && kept.status !== ARCHIVED) return kept.id
  const active = list.filter((w) => w.status !== ARCHIVED).sort(byName)
  if (active.length > 0) return active[0].id
  // Every remaining workspace is archived: honour the remembered one if
  // it is one of them, so a reload does not silently move the user.
  if (kept) return kept.id
  return list.slice().sort(byName)[0].id
}

/** Where to go when `leavingId` stops being habitable — you deleted it,
 * or you archived it and there is somewhere better to be. `null` means
 * there is nowhere left (the caller must not strand the session: with
 * no candidate it should keep the current context rather than write an
 * empty workspace id).
 *
 * `list` is the roster BEFORE the removal; the leaving workspace is
 * skipped here so callers do not have to pre-filter it. */
export function fallbackWorkspaceId<T extends WorkspaceChoice>(
  list: readonly T[],
  leavingId: string,
): string | null {
  const rest = list.filter((w) => w.id !== leavingId)
  if (rest.length === 0) return null
  const active = rest.filter((w) => w.status !== ARCHIVED).sort(byName)
  if (active.length > 0) return active[0].id
  return rest.slice().sort(byName)[0].id
}

/** Deleting a workspace is refused by the server when it is your only
 * one (`workspace.sole`) or when you are not its owner
 * (`workspace.not_owner`). The UI states both up front instead of
 * offering a button whose only outcome is a 4xx. */
export function deleteBlockedReason<T extends WorkspaceChoice>(
  list: readonly T[],
  target: T,
): 'sole' | 'not_owner' | null {
  if (list.length <= 1) return 'sole'
  if (target.role !== 'owner') return 'not_owner'
  return null
}

const RANK: Record<string, number> = {
  guest: 0,
  member: 1,
  admin: 2,
  owner: 3,
}

/** The role you are actually acting with: the one you asked for (the
 * "acting as" chip), clamped DOWN to your membership ceiling. The
 * server computes the same thing from `X-Workspace-Role`, so a forged
 * higher value cannot escalate — this mirrors it only to decide what
 * the UI offers. */
export function effectiveRole(ceiling: string, requested: string): string {
  const want = requested || 'member'
  const cap = RANK[ceiling] ?? RANK.member
  return (RANK[want] ?? RANK.member) <= cap ? want : ceiling
}

/** Whether the TENANT-SCOPED privileged writes are open: renaming the
 * workspace, its settings bag, its members. All of them are
 * `ensure_role(ctx.role, owner)` server-side, and `ctx.role` is the
 * EFFECTIVE role — so an owner who has not raised the chip really is
 * refused, and the UI has to say so rather than offer a button that
 * 403s. Contrast `canManageWorkspace`, which governs the pre-tenant
 * lifecycle endpoints and reads the raw membership instead. */
export function canWriteWorkspace(ceiling: string, requested: string): boolean {
  return (RANK[effectiveRole(ceiling, requested)] ?? RANK.member) >= RANK.owner
}

/** Archive/unarchive is owner-gated too (the SQL guard also accepts the
 * legacy `admin` role, which the product model no longer issues). Note
 * this is the RAW membership role from the pre-tenant list, NOT the
 * elevated "acting as" role: these endpoints carry no workspace header,
 * so an owner does not have to flip the mode chip to use them. */
export function canManageWorkspace<T extends WorkspaceChoice>(target: T): boolean {
  return target.role === 'owner' || target.role === 'admin'
}
