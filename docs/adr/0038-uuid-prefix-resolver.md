# ADR-0038: UUID-prefix entity resolver (`/lookup/{prefix}`)

Status: Accepted (v1 shipped)
Date: 2026-05-28
Relates to: ADR-0015 (RLS two-role), ADR-0028 (identity-first
addressing), ADR-0029 (note garden ecosystem). Implements the analysis
recorded in Flow task `b9924a0d` (Note → task / note ID prefixes:
resolver + UI linking). Shipped in commits 9d6c016 (backend resolver),
04360e7 (markdown chips + short routes).

## Context

Roadmap and planning notes cross-reference tasks and notes by an 8-char
UUID prefix in backticks, e.g. `` `91cf6aaa` v2.0.5 regression… ``. This
convention is used pervasively (the three-phase roadmap note
`d3ffc7fd` cites ~30 tasks this way). Before this ADR the references
were inert:

- they were not clickable in the rendered markdown;
- the canonical SPA URL is `/notes/{full-uuid}` / `/tasks/{full-uuid}`,
  so hand-substituting an 8-char prefix into the URL produced a blank /
  404, because the route did not resolve a prefix;
- the Notes screen had no quick-switcher accepting a prefix.

A note that cites 30 tasks was therefore not navigable: the user had to
search title-by-title or rebuild the full URL outside Flow. This
violates *Resilienza progettata* (dirty signals teach the user that
synthesis notes are useless) and *Tutto è connesso* (the textual
mycelium never becomes a navigable mycelium).

The backend already *knew* how to resolve a prefix: `POST /search`
with `q="91cf6aaa"` returns the right task as the first hit. The gap
was **exposure**, not knowledge: a clean lookup primitive plus the
consuming surfaces.

## Decision

### 1. Backend primitive — `GET /lookup/{prefix}`

A dedicated lexical resolver, **not** semantic search.

- **Input**: `prefix` ∈ [4, 36] chars, hex with optional dashes
  (`^[0-9a-f][0-9a-f-]{2,34}[0-9a-f]$`, case-insensitive). Below 4
  chars → 400 (`DOMAIN_ERROR`); a 4-char floor is the smallest prefix
  with a useful chance of uniqueness, 8 is the convention, 36 is a full
  canonical UUID. Dashes are **not** stripped: `id::text` is canonical,
  the SQL `LIKE` is char-accurate, so a caller going past the first
  dash boundary must use the canonical form.
- **Query params**: `kinds` (CSV, default `task,note`, unknown kinds
  dropped not 400; lets the SPA forward an enum it doesn't fully
  recognise), `include_archived` (default false), `include_deleted`
  (default false), `limit` (1..50, default 20).
- **Match**: `id::text LIKE 'prefix%'` on `tasks` then `notes`.
- **Response** (`LookupOut`): `{prefix (normalised), matches: [{kind,
  id, title, state_name, is_terminal, is_archived, is_deleted,
  route_url}]}`. For tasks the workflow state is resolved
  (`state_name`, `is_terminal`) so consumers can render closed tasks
  struck-through with the state in parens (see ADR-0029 / task
  `25fa3ab3`).
- **Ordering**: `kind` first (tasks before notes, mirroring the most
  common authoring intent in roadmap notes), then most-recent
  `updated_at`, so the freshest candidate wins the disambiguator's
  first row. With an 8-hex prefix collisions are rare (2^32 space) but
  the list form handles them deterministically.
- **RBAC / RLS**: the resolver runs on the tenant session and inherits
  RLS (ADR-0015). It never reaches across workspaces and never
  bypasses the row filters that `get_task` / `get_note` honour.
  Archived / deleted rows are excluded by default; the override flags
  exist for the Trash and archive views.

The response carries `route_url` server-side so every consumer
(renderer, router, future palette) navigates the same way.

### 2. Short routes — `/n/:prefix`, `/t/:prefix`

React routes alongside (not replacing) the canonical `/notes/:id` /
`/tasks/:id`. The loader calls `/lookup/{prefix}` scoped to the
matching kind:

- exactly one match → `navigate(route_url, {replace: true})` so the
  canonical URL lands in the bar and a refresh does not flicker;
- multiple → a compact disambiguation screen (title + kind + state,
  keyboard 1..9 + Enter);
- zero → a friendly 404 with a full-text suggestion for the prefix.

The canonical routes also accept a prefix and upgrade in-place
(`PrefixOrUuid` wrapper): hand-substituting a prefix into the URL bar
becomes *the correct action*, not a workaround.

### 3. Markdown chips

A renderer plugin intercepts inline `code` nodes matching the
UUID-prefix pattern and renders an `EntityRef`/`PrefixMentionChip`:
batched resolution (collect the document's prefixes → resolve →
render), a kind glyph (task / note), the resolved title, the workflow
state in parens, a `+N` badge on ambiguity, strikethrough+grey when the
task is terminal, click → `route_url` (Cmd/Ctrl-click → new tab). The
plugin matches **only** backtick-code, never free text, so legitimate
hex in prose (colour codes, SHAs) is not falsely linked.

## Decision on typed wikilinks (analysis item #F): **deferred, not adopted now**

The analysis raised an optional sixth layer: a typed wikilink syntax
`[[task:91cf6aaa]]` / `[[note:fecfc1c8]]` alongside the backtick-prefix
convention.

**Decision: do not introduce typed wikilinks in this iteration.**
Rationale:

- The backtick-prefix convention is already adopted pervasively across
  roadmap notes; introducing a second syntax fragments the convention
  and forces a migration with no immediate user-visible payoff once
  A+B+C make the existing convention navigable.
- A+B+C already deliver clickable, resolvable references with zero
  migration on existing notes.
- The pro of wikilinks (disambiguation, recognisability for the
  chunker / distillation pipeline, reuse as a backlink anchor) becomes
  relevant only once the decomposition pipeline (task `4a718dc4`, see
  the forthcoming decomposition ADR) consumes references structurally.

Re-open #F when the decomposition pipeline needs structured mention
parsing; at that point evaluate wikilinks as the canonical structured
form, with the backtick-prefix kept as a rendered alias. Until then it
is explicitly out of scope.

## Consequences

- Synthesis notes (e.g. `d3ffc7fd`) became navigable with zero
  migration the moment the chip renderer shipped.
- The Cmd+K palette (analysis item D) and editor autocomplete (item E)
  remain deferred per the MVP scope; the primitive and `route_url`
  contract are designed to back them when built.
- Performance: `id::text LIKE 'prefix%'` does not use the UUID PK
  B-tree index (a functional index would be needed). The payload is
  bounded (short prefix, single-workspace RLS scope), so the
  sequential scan is cheap in practice. If a profile ever shows this
  hot, the upgrade is a single expression index
  `ON tasks ((id::text) text_pattern_ops)` (and the same on `notes`) —
  noted here so the fix is obvious, not rediscovered.
- Terminal-state rendering depends on the workflow-state join being
  correct; `WorkflowState.is_terminal` is the single source of truth.

## Alternatives rejected

- **Reuse `POST /search` directly from the SPA.** Works for a single
  lookup but is semantic, not deterministic: it ranks by relevance, can
  return non-prefix hits, and gives no clean unique/ambiguous contract.
  The chip renderer needs a lexical, batchable, deterministic primitive.
- **Strip dashes and resolve against a normalised hex column.** Adds a
  derived column / migration for no benefit: `id::text` is already
  canonical and the `LIKE` is exact; callers past the first dash
  boundary simply use the canonical form.
- **Regex over the whole note body (not just backtick-code).** Rejected
  for false positives on legitimate hex (colour codes, commit SHAs).
  The backtick is already the adopted convention and is an unambiguous
  opt-in.
- **Typed wikilinks now** — see the dedicated decision above.
