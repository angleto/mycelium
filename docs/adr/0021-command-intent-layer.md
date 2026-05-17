# ADR-0021 Command/intent layer (natural language to deterministic actions)

Status: accepted. Extends ADR-0013 (advisory/NL frontend) and ADR-0020
(notes/conversation). Ties to ADR-0007 (isolation), ADR-0019
(metering).

## Context

The user wants to drive the system by natural commands, by voice or
text, e.g. "create a new note", "let's have a conversation in a new
session", "...a new session in project bitvision_phoenix". Also via
MCP.

## Decision

- **Two-tier intent parsing.** (1) A deterministic grammar for
  canonical commands (create note; start/append a conversation
  session; optional "in project <name>"): no LLM, works offline, not
  metered. (2) LLM-based intent extraction as fallback for free-form
  phrasing: metered (ADR-0019), online. Consistent with
  ADR-0013/0004 (deterministic core, LLM as frontend, no opaque
  magic). "create a note" must not be a metered online call every
  time.
- **Slot resolution with confirmation, never guessed.** A target
  project is resolved by name against the user's accessible
  project-kind tags in the current org (exact, then unambiguous
  fuzzy). If not found or ambiguous, the system asks a clarifying
  question; it never silently falls back to a different or default
  project. Mis-scoping would violate isolation (ADR-0007).
- **Explicit default scope.** With no project stated: the session's
  current active project context; if none, a designated personal
  "inbox". Never an arbitrary scope.
- **Surfaces.** Voice (after STT, ADR-0020), text, and MCP tools
  (`create_note`, `start_conversation_session`, ...). Same RBAC,
  isolation, metering. Offline: the command is captured and executed
  on sync (deferred, ADR-0020); LLM-dependent steps deferred +
  notified (FR-12).
- **"Session" = a conversation Note** (thread). "New session" = new
  conversation Note; sessions are (org, project)-scoped and their
  memory is isolated to that project (ADR-0007/0016).

## Consequences

- Intent commands are first-class; the canonical path is
  cheap/offline/deterministic; the LLM fallback is metered/online.
- Ambiguous/unknown project -> clarify, no mis-scoping.
- MCP tools mirror the commands one-to-one.
- Phasing: canonical intent + create-note/start-session land in F6b
  (with notes/conversation); LLM intent fallback after F5b (metered).

## Alternatives rejected

- LLM-only intent: every "create a note" becomes a metered online
  call and fails offline.
- Silent project default on ambiguous match: mis-scoping, violates
  ADR-0007.
- Storing the raw utterance without intent handling: loses the
  command UX the user asked for.
