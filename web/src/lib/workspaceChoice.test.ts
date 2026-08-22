import { describe, expect, it } from 'vitest'
import {
  archivedWorkspaces,
  canManageWorkspace,
  canWriteWorkspace,
  deleteBlockedReason,
  effectiveRole,
  fallbackWorkspaceId,
  initialWorkspaceId,
  switchableWorkspaces,
  type WorkspaceChoice,
} from './workspaceChoice'

// The workspace you are standing in is the one piece of client state
// that, when wrong, breaks EVERY subsequent request: `workspaceHeader()`
// keeps sending a dead id and the server answers 403 forever, which is
// not a 401 so nothing clears the session. These selectors are what
// keeps that unrepresentable, so the ugly cases are asserted here.

function ws(
  id: string,
  name: string,
  over: Partial<WorkspaceChoice> = {},
): WorkspaceChoice {
  return { id, name, role: 'owner', status: 'active', ...over }
}

const PERSONAL = ws('p', 'Personal')
const CLIENT = ws('c', 'Client X')
const OLD = ws('o', 'Old', { status: 'archived' })

describe('switchableWorkspaces', () => {
  it('offers the active workspaces, by name', () => {
    expect(switchableWorkspaces([PERSONAL, CLIENT], 'p').map((w) => w.id)).toEqual([
      'c',
      'p',
    ])
  })

  it('hides archived workspaces', () => {
    expect(switchableWorkspaces([PERSONAL, OLD], 'p').map((w) => w.id)).toEqual(['p'])
  })

  it('still shows the one you are standing in when it is archived', () => {
    // Archiving does not evict you (the backend keeps an archived
    // workspace fully usable), so the control must be able to say where
    // you are instead of rendering an empty trigger.
    expect(switchableWorkspaces([PERSONAL, OLD], 'o').map((w) => w.id)).toEqual([
      'o',
      'p',
    ])
  })
})

describe('archivedWorkspaces', () => {
  it('lists the archived ones for the settings screen', () => {
    expect(archivedWorkspaces([PERSONAL, OLD], 'p').map((w) => w.id)).toEqual(['o'])
  })

  it('excludes the active workspace even when it is archived', () => {
    // The switcher already shows it; it is not "put away" while you are
    // inside it, and listing it twice invites a restore/delete on the
    // row you are standing on.
    expect(archivedWorkspaces([PERSONAL, OLD], 'o')).toEqual([])
  })
})

describe('initialWorkspaceId', () => {
  it('honours the remembered workspace', () => {
    expect(initialWorkspaceId([PERSONAL, CLIENT], 'c')).toBe('c')
  })

  it('falls back to the first active one when the remembered id is gone', () => {
    expect(initialWorkspaceId([PERSONAL, CLIENT], 'deleted')).toBe('c')
  })

  it('does not drop the user into an archived workspace', () => {
    // The old post-login pick was `data[0]` over a name-ordered list
    // that includes archived rows, so an alphabetically-first archived
    // workspace swallowed every fresh login.
    expect(initialWorkspaceId([OLD, PERSONAL], null)).toBe('p')
  })

  it('does not follow a remembered id into an archived workspace', () => {
    expect(initialWorkspaceId([OLD, PERSONAL], 'o')).toBe('p')
  })

  it('still lets you in when every workspace is archived', () => {
    expect(initialWorkspaceId([OLD], null)).toBe('o')
  })

  it('is null with no workspaces at all', () => {
    expect(initialWorkspaceId([], 'p')).toBeNull()
  })
})

describe('fallbackWorkspaceId', () => {
  it('moves you to another active workspace', () => {
    expect(fallbackWorkspaceId([PERSONAL, CLIENT], 'p')).toBe('c')
  })

  it('prefers an active workspace over an archived one', () => {
    expect(fallbackWorkspaceId([PERSONAL, CLIENT, OLD], 'c')).toBe('p')
  })

  it('accepts an archived workspace when nothing else is left', () => {
    expect(fallbackWorkspaceId([PERSONAL, OLD], 'p')).toBe('o')
  })

  it('is null when the workspace you are leaving is the last one', () => {
    expect(fallbackWorkspaceId([PERSONAL], 'p')).toBeNull()
  })
})

describe('deleteBlockedReason', () => {
  it('refuses your only workspace', () => {
    expect(deleteBlockedReason([PERSONAL], PERSONAL)).toBe('sole')
  })

  it('refuses a workspace you do not own', () => {
    const guest = ws('g', 'Shared', { role: 'member' })
    expect(deleteBlockedReason([PERSONAL, guest], guest)).toBe('not_owner')
  })

  it('allows an owned workspace when it is not the last one', () => {
    expect(deleteBlockedReason([PERSONAL, CLIENT], CLIENT)).toBeNull()
  })

  it('reports the sole-workspace block before the ownership one', () => {
    // Both would be true for a member with a single shared workspace;
    // "create or join another first" is the actionable half.
    const guest = ws('g', 'Shared', { role: 'member' })
    expect(deleteBlockedReason([guest], guest)).toBe('sole')
  })
})

describe('effectiveRole / canWriteWorkspace', () => {
  it('defaults to least privilege even for an owner', () => {
    // The sudo-style chip: you are a member until you say otherwise,
    // and the tenant-scoped writes really are refused meanwhile.
    expect(effectiveRole('owner', '')).toBe('member')
    expect(canWriteWorkspace('owner', '')).toBe(false)
  })

  it('opens the privileged writes once the owner elevates', () => {
    expect(effectiveRole('owner', 'owner')).toBe('owner')
    expect(canWriteWorkspace('owner', 'owner')).toBe(true)
  })

  it('clamps a requested role down to the membership ceiling', () => {
    expect(effectiveRole('member', 'owner')).toBe('member')
    expect(canWriteWorkspace('member', 'owner')).toBe(false)
  })
})

describe('canManageWorkspace', () => {
  it('is the raw membership role, not the elevated one', () => {
    expect(canManageWorkspace(PERSONAL)).toBe(true)
    expect(canManageWorkspace(ws('m', 'Shared', { role: 'member' }))).toBe(false)
    // Legacy role the SQL guard still accepts.
    expect(canManageWorkspace(ws('a', 'Shared', { role: 'admin' }))).toBe(true)
  })
})
