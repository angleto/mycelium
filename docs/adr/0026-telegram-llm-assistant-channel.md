# ADR-0026: Telegram as an in-process LLM assistant channel

Status: Proposed
Date: 2026-05-22
Relates to: #125 (Telegram bot), ADR-0025 (orchestration/governance), ADR-0017 (i18n), ADR-0001 (MCP/service parity), ADR-0020 (voice notes)

## Context

The Telegram bot (#125) currently captures input: `/note` → note, voice
→ voice note, `/task` → task; free-text replies with a hint and is NOT
stored (v1.2.53). The desired end state is conversational: the user
writes natural language to the bot and an LLM **does things** (CRUD on
notes/tasks, answers questions) on the user's behalf, in the user's
workspace.

Constraints discovered (see telegram_link.py, agent_runtime.py,
ai_providers.py, dispatch_loop.py, rbac.py, billing.py, i18n.py):

1. **`agent_runtime` is task-centric, not conversational.** It takes a
   `task_id`, drives a bounded loop to *complete* it, and emits an
   artifact note. Its tool allowlist is 7 task-scoped tools. Mapping a
   chat message to "create a task and complete it" produces junk tasks
   and forces dispatch approval per message. Not a fit.
2. **The right tool surface already exists** at the service layer (the
   same one MCP wraps): `list_tasks`, `get_task`, `set_task_state` (with
   workflow-transition validation), `list_notes`, `get_note`,
   `create_note`, `update_note`, task comments, memory recall/write.
3. **The LLM provider has no native tool-calling.** `LLMProvider.complete(
   system, messages) -> text` only. `agent_runtime` already uses a
   JSON-in-text (ReAct) protocol; that pattern is provider-neutral and
   works with free local Ollama models that lack function-calling.
4. **Governance (ADR-0025):** default `approval_required`, never silent
   auto-spend; per-executor `credit_budget`; idempotent metering that is
   free when the model has no rate card.
5. **Tenancy/RBAC:** per-user `tenant_session` + RLS + `require_role`;
   the webhook resolves the linked user via `resolve_telegram_chat`.
6. **i18n (ADR-0017):** user-facing strings go through `MessageCode`. The
   current Telegram replies are hardcoded English — pre-existing debt.

## Decision

### D1. In-process conversational assistant inside Mycelium

A free-text Telegram message from a linked user is handled by a new
**conversational agent loop in the Mycelium backend** (not `agent_runtime`,
not an external host). It reuses `LLMProvider`, the service-layer tools,
RLS/tenant scoping, and billing. Rationale: self-contained, ships
without new deployable infra, and it is *Mycelium's own assistant* (not a
third-party vendor integration), so it does not violate the
"Mycelium vendor-neutral, LLM glue outside" stance taken for the kiwiprocess
integration — that stance is about not embedding *other vendors'*
connectors in Mycelium. (An external multi-MCP "glue" host remains a valid
future evolution; see Alternatives.)

### D2. Provider-neutral ReAct (JSON-in-text) tool protocol

The loop prompts the model to emit `{"tool": name, "args": {...}}` and
parses it (same shape as `agent_runtime._parse_action`), rather than
relying on native function-calling. Keeps free local Ollama models and
premium models on the same path. Native tool-calling can be added later
behind the provider abstraction for models that support it.

### D3. Curated tool set (read + scoped write, no destructive/admin)

The agent may call: `list_tasks`, `get_task`, `list_task_transitions`
(discover allowed next states), `set_task_state`, `create_task`,
`update_task` (safe field subset), `list_notes`, `get_note`,
`create_note`, `update_note`, `add_task_comment`, `recall_memory`,
`finish`. Hard allowlist enforced after parse (unknown tool → blocked,
no side effect), mirroring `agent_runtime`. **No** delete, archive,
billing, executor/dispatch, user-management, network, or shell tools.
Every tool runs inside the linked user's `tenant_session`; `require_role`
re-checks membership, so the agent cannot exceed the user's own role and
RLS confines it to their workspace.

### D4. Spend follows the configured model; no per-message approval

The user picks the assistant model in configuration. The loop meters
each step via `billing.meter_if_billable`: a free model (local Ollama,
no rate card) costs nothing; a premium model deducts credits. A
configurable per-channel/budget cap (reusing the executor `credit_budget`
mechanism) bounds spend and blocks the loop when exhausted
(`blocked_reason="budget_exhausted"`). The Telegram channel therefore
runs without per-message human approval (consistent with single-user,
local-Ollama prod), while remaining safe-by-default because cost is
gated by *model choice* + *budget cap*, and the tool set excludes
destructive ops. This is an explicit, configurable channel policy, not a
silent global `auto` flip of ADR-0025 dispatch.

### D5. Bounded, stateful conversation

Per-chat conversation state (recent turns) is kept so the assistant is
multi-turn, with a hard cap on turns/steps per message (mirroring
`MAX_STEPS`) and a token/step budget. Conversation context is treated as
**untrusted data**: the system prompt frames user text and any
note/task content read back as data, never as instructions
(prompt-injection mitigation). The allowlist + RLS are the hard
boundaries; the framing is defense-in-depth.

### D6. i18n posture

Mycelium-*generated* feedback (errors, "saved", usage) goes through
`MessageCode`; the assistant's *generated* natural-language answer is
passthrough (not catalogable). As part of this work, retrofit the
existing hardcoded Telegram replies (link/help/note/task, incl. the
v1.2.53 shim) to `MessageCode` to clear the ADR-0017 debt.

## Consequences

- New module (e.g. `core/src/mycelium_core/services/assistant.py`) with the
  conversational loop + tool dispatch; wired into
  `telegram_link.handle_webhook_update` on the free-text branch.
- Assistant model selection added to config / assistant settings.
- Conversation-state storage (table or memory channel keyed by chat/user).
- Telegram reply path stays best-effort (already `send_message`).
- Latency: synchronous loop may exceed Telegram's webhook timeout for
  long turns → likely need to answer the webhook fast and send the reply
  asynchronously (worker), or a "typing"/ack then follow-up message.
- i18n retrofit touches telegram_link.py replies.

## Alternatives considered

- **A. Reuse `agent_runtime`** (task-per-message). Rejected: junk tasks,
  wrong shape, dispatch-approval friction.
- **C. External LLM-glue host over MCP** (one assistant brain across Mycelium
  + kiwiprocess + other MCP servers; Telegram → that host → Mycelium MCP).
  Most composable and most aligned with the kiwiprocess "MCP + LLM glue"
  decision, but needs a new always-on deployable component and Telegram
  routing to it. Deferred: revisit when a second MCP server (kiwiprocess)
  is live and a shared assistant brain is wanted. D1 does not preclude
  it — the in-process tools are the same surface the external host would
  call over MCP.

## Phasing

- **P0 (done, v1.2.53):** stop free-text→note; commands + voice capture;
  free-text returns a hint.
- **P1:** conversational loop skeleton — LLMProvider + ReAct parse +
  read-only tools (`list_tasks`, `get_task`, `list_notes`, `get_note`,
  `list_task_transitions`, `recall_memory`, `finish`), single-turn,
  metering + budget cap, prompt-injection framing, tests with FakeLLM.
- **P2:** scoped writes (`create_note`, `update_note`, `create_task`,
  `update_task`, `set_task_state`, `add_task_comment`).
- **P3:** multi-turn conversation state per chat + async reply (worker)
  to avoid webhook timeouts.
- **P4:** i18n retrofit of Telegram replies (ADR-0017).
- **P5 (optional/future):** external multi-MCP glue host (Alternative C).
```
