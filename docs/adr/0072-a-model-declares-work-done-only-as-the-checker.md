# ADR-0072: A model declares work done only as the checker

Status: accepted (2026-11-17)

## Context

The Default workflow of the workspace this was built for says, in the
description of its `done` state, that a task enters `done` in two ways only:
a check passed, or the person decided. An agent does not take the second
decision on its own. Nothing in the code applied it. `tasks.set_state`
checked the lease (ADR-0063) and that the transition exists; it did not ask
who was closing. An agent could move its own finished work to `done`, and
`pull(transition_to=done)` took a task and closed it in one call.

Asking "who is closing" exposed a second problem: the system had four
answers to "what kind of credential is this", and they disagreed.
`task_leases.resolve_holder` treated only `mcp_token` as an agent. The bus
(`event_bus._COARSE`) and the work digest (`_AGENT_KINDS`) read the kind
alone. The annotations guard read the kind and the subject. The REST
adapter publishes an agent token as `human_api` with the token as subject,
so to three of the four that agent was a person. It skipped the lease every
other agent must take, took the owner's exemption, sat outside the bus
quota and was not marked as an agent in the digest.

## Decision

**One predicate, `services.caller`.** It reads `app.current_actor_kind` and
`app.current_actor_subject`, so it answers for a live session and for a
logged row alike. There are four classes:

- **person**: `human_api` with no subject.
- **agent**: any credential that is not a person's, and any kind the module
  does not know. That covers an agent token over MCP or REST, a capability,
  a dispatched run, an issuer key and a payment connector.
- **mediated**: `human_telegram`. The instruction is a person's, but a model
  turns it into the calls.
- **system**: `system` and `human_direct`. `human_direct` is the default kind
  of `tenant_session`: internal code that acts for a user without that user
  presenting anything gets it.

The lease holder, the owner exemption, the bus, the digest, the
instruction guard and the protected-note guard all ask it. The two guards
refuse every caller a model chose the call for: agent and mediated.

**A state attribute, `workflow_states.closes_work`** (migration 0029).
Entering such a state declares the work done. The attribute is not
`is_terminal`: `wontfix` and `duplicated` end the work without claiming it
was done, and need no check. An unstated value follows `is_terminal`,
because a model asked for a check it could have skipped is the safe error.
An edit that omits the attribute keeps the stored value, so an editor that
does not know the flag cannot reset it.

**A second state attribute, `workflow_states.checks_work`** (migration
0030, 2026-11-22). It marks a checking station: the only state a handoff
into counts as work passed on for a check. It is stated, never derived
from a name, and a terminal or closing state cannot carry it (a check
happens before the close, in a state the task then leaves). Unstated, a
state is not a station, so a workflow that names none lets no model close
anything and the person closes: forgetting the flag makes the guard
stricter, never weaker. The migration flags `verify` in each workspace's
default workflow, the one place the station was known; the workflow
`create_default_workflow` makes has none.

Whether a state is a station is read when the task enters it, not when it
is closed. `tasks.handed_over_by_worker_id` is written only on a handoff
into a state flagged at that moment. Read at the close instead, a person
flagging a state later would turn an old pull into it into work passed on
for a check, and nobody checked it. The migration clears the value on every
task whose state is not a station after the backfill, for the same reason.
Turning the flag off on a state a task is already in makes the guard refuse
that task, which is the safe direction.

**The guard, `task_leases.assert_may_close`.** `set_state` calls it whenever
a move enters a `closes_work` state, including on the pull path. A person
and the system are not asked anything. A model needs all three of:

1. a live lease on the task, held by the worker it names;
2. no handoff of this task by that worker, ever;
3. the task was handed into the state it is in now by another worker, and
   that state is a checking station (`checks_work`).

**A handoff is what the holder did, and the task records the last one.**
Leaving a state ends its lease (ADR-0063). The release is a `handoff` only
when the holder itself moves the task to a station that does not end the
work. A move by somebody else ends the lease as `preempted`, or as
`expired` when its deadline had passed. Before, every such release was the
holder's `handoff`. A checker the owner took a task back from then counted
as its author, and a session that died left a handover that any other
worker could close on.

`tasks.handed_over_by_worker_id` (migration 0029) holds the worker that
passed the task into its current state, and every other move clears it.
Rule 3 reads it rather than searching the lease history. A search would
still find a handoff from before the person reopened the task, and the
work since then is unchecked. Ordering a release against a reopen would
also mean comparing the application's clock with the database's. The
position needs no clock.

The close's activity row records the checking worker and the worker that
handed the task over. Every `set_state` row now records the worker that
moved the task.

**Writing a workflow is a person's act.** The workflow says which state
closes work and so where the guard asks for a check. An agent that could
edit it could clear `closes_work` on `done` and close its own work. It
could also make the initial state a closing one, so that every new task
is born done. A token with no scope restriction holds every key that is
not reserved to a person, so `workflows:write` reached that edit.

The five workflow writers in `services.workflow` now refuse a model
(`workflow.person_only`): create, update, delete, set the default, and
set a project's workflow. Import goes through create and update. REST and
MCP mark those routes and tools as a person's. The `workflows:write` key
is retired from the scope catalogue, and migration 0029 removes it from
every stored grant. An assistant a person had given it loses it: no grant
should promise what the server refuses.

**The CLI is an agent.** `mycelium auth login` stores an agent token in
a file every process of the user can read, and the CLI sends it on every
call. Under the old reading that token was a person: a CLI call moved
tasks it never took, took the owner's exemption and wrote workflows, and
so did any agent on the machine that ran the CLI or read the file. The
predicate now calls it an agent, so the CLI follows the agent protocol.
`mycelium worker open` opens a worker and prints the export of its id
(`MYCELIUM_WORKER`), and `task to`, `done` and `verify` take the task
for that worker before moving it, and give back a hold they took for a
move the server refused. The worker lives in the shell, not in the
profile: a profile is shared by every terminal and agent of the user,
and a worker stored there would make them all one worker, the collapse
ADR-0063 exists to prevent. `workflow import` is gone from the CLI;
a workflow is written from the web app.

## Alternatives rejected

**Refusing a model only the workflow edits that weaken the guard.** It
would keep the edits that do not touch `closes_work` open to an assistant
that was granted them. It leaves a hole all the same: an agent creates a
workflow whose finished state is an ordinary one and points the project at
it. Telling such a workflow apart from a legitimate one is the question
the guard exists not to ask a model.

**Counting a person's move as the handover.** A person moving a task into
a working state and a person moving it into a checking state are the same
write. Only the workflow's prose says which station is which. Counting the
move let a worker take a task the person had queued, do it and close it,
which is the case the rule exists for. Work a person did and an agent
checked is therefore closed by the person, and the refusal says so.

**Leaving the station implicit**, which this ADR did until 2026-11-22. The
objection to a second attribute was that a project override which forgot
it would weaken the guard silently. With the attribute defaulting to off,
forgetting it refuses the close instead. Meanwhile the implicit rule was
reached on the ordinary path: `pull` from `queued` into `working`
recorded the puller's handoff, and once it let go another worker closed
the task from `working`, checked by nobody (measured by the verifier of
task 84311afb on 2.4.14).

**Keying on the state name `done`.** The workflow is configuration and a
project can override it. `set_state` already refuses to hardcode a state
name for the same reason.

**Treating Telegram as a person.** The server receives the person's
message, but a model decides the calls. A misread "yes, close it" would be
indistinguishable from the person's decision. Until the person confirms
the act through a channel the model does not write (task 04d8b905, an
inline button), a Telegram turn closes nothing that needs a check.

## Limits

The same model can open a second worker and check its own work. On the
agent side this is hygiene, not evidence of independence.

A worker answers only to the credential that opened it. An assistant's token
is bound through the assistant's identity, because its secret is replaced
under a running session; sessions of the same assistant share it, so the
limit above is unchanged. Any other token (the CLI's, a legacy one) is bound
by the token itself: those credentials have only the person's identity,
which every other credential of that person shares, and binding on it let
one CLI token act under another's worker. A person's own session, which has
no token, uses only a worker no token opened. A model with no token (a
Telegram turn, a dispatched run) has no credential of its own to be bound
to: it is refused a worker, and uses none, because it would otherwise share
the person's workers and two runs each other's. Until
2026-11-21 any worker id of the workspace was accepted; until 2026-11-22 the
binding was the identity for every credential, and closing a worker checked
nothing. A model now closes only its own worker; a person closes any, which
is how a stuck session is cleared.

`pull` with `transition_to` is a take followed by a move, and the move is
recorded as the puller's handoff like any other. Before 2026-11-21 it ran
before any lease existed and recorded nobody, so a task pulled into a
checking station could be closed by no agent. A pull whose move ends the
work is still refused, as rule 1 requires. A pull into a working state
records no handoff, because the state is not a station.

The stdio MCP transport opens its session as `human_api` with no subject,
because only a person's session token may open it. It is a person to this
predicate, and so are its calls, although an MCP client is a model choosing
them. That applies to every act reserved to a person, not only to this
close, so it is left to its own decision.
