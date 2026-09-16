# ADR-0063: Possession of a task is an object, not a column

Status: Accepted (2026-09-16)
Relates to: ADR-0049 (working memory delegated to the caller — this is the
"shareable working-set object" it left open), ADR-0025 (work orchestration:
the contract-net `offer`/`claim` this does not replace), ADR-0036 (the agent
event bus, which this deliberately does not emit onto — see Consequences),
ADR-0002 (optimistic concurrency, which remains the arbiter of field writes).

## Context

A workspace here runs fifteen agent sessions at once: ten take work from the
first station, do it and move it on; five take work from the checking station
and either close it or send it back. Three failures came out of that, and they
are one absence seen from three angles.

**Two sessions take the same task.** `set_state` takes an `expected_version`
and `claim_task` was made atomic, so nothing is lost in the database: the
second writer gets a conflict. What is lost is the work it had already done
before the conflict, and no version gate gives that back. The read and the
write are two round trips; the ranking that produced the read is deterministic;
fifteen callers asking a deterministic ranking the same question get the same
answer. `what_can_i_do_now` says of itself that it is scoped to the caller's
own tasks, and in this workspace every session resolves to one identity
(`whoami`, live: one `identity_id`, and every open task carrying it as
assignee), so "the caller's own tasks" is the same list fifteen times. The
collision is the designed behaviour of two correct pieces composed, not a bug
in either.

**A session moves a task to the checking station and keeps working on it.**
Reported as a discipline problem. It is not: the session had nothing to hand
back. It moved a column. Possession never existed, so surrendering it could not
be a step, and the workflow's own description of that station — which does say
the check is done by somebody other than whoever did the work — had nothing
behind it.

**A session dies and its task is held forever.** Nothing expires and no sweep
looks. Of the three this is the only plain missing backstop.

`in_progress` had been doing a lock's job without being one: anyone may write
it, it names no holder, it does not expire, and it cannot be released because
it was never taken.

There are three leases in this codebase already — webhook delivery, payment
connectors, the SdI two-phase dispatch — and all three are on actors with an
external effect that must survive a mid-flight crash. There was none where
fifteen concurrent actors run.

## Decision

**Possession is a first-class durable row with a holder, a deadline and a
release reason, separate from the task's state.** `task_leases`, migration
0017, with `task_lease_acquire` / `task_pull` / `task_lease_renew` /
`task_lease_release` / `task_leases_list` on MCP and their REST siblings.

The exclusion lives in two places, neither of which is a check in application
code:

- `uq_task_leases_live`, a partial unique index on `task_id WHERE released_at
  IS NULL`: at most one live possession per task, decided by the datastore.
  Expiry is not in the predicate, because an index cannot read the clock; the
  service compares the deadline and a worker sweep reclaims.
- `SELECT ... FOR UPDATE OF tasks SKIP LOCKED` on the task row, taken by both
  entry points before either touches the lease table, so acquiring by id and
  pulling from a queue serialise against each other on one lock.

**A lease is held for the duration of one workflow state.** Moving the task to
a different state releases it in the same transaction — `handoff` to a
non-terminal state, `done` to a terminal one. That is what makes "moved it on
and carried on working" a refused write rather than a habit to discourage: the
mover holds nothing afterwards and its next write to the task says so. The rule
names no state, because the workflow is configuration a project can override.

**A fourth holder slot, `holder_worker_id`, supplied by the caller.** The
system already knows the caller's user, identity and token, and in this
workspace the first two are the same value for every session and the third may
be too. A lease keyed on any of them would exclude nobody: every caller would
read as the same holder and fifteen acquires would all look idempotent. A
worker is not a credential. It stays a plain label — never keyed on, never an
authorization input, dead with the row — so it is provenance on a durable
entity, not the server-side session state ADR-0049 forbids.

**`pull` is one round trip.** Choosing and taking are one statement under one
lock, ordered exactly as `list_tasks` orders by default so the head of the
queue is the head of the list the agent was shown.

## Alternatives rejected

**Leave it to optimistic concurrency, which is what the previous design
decided.** That refusal is correct and does not reach this object, by the
criterion it states itself: leases belong to actors whose external effect must
survive a mid-flight crash, and the object it weighed was a write to a note
part, which has none — a stale writer takes a conflict, a crashed one leaves
nothing to recover. An agent *executing* a task edits a working tree, runs a
gate and writes commits; the effect outlives the crash and the task row is the
only record that the work was in flight. Applied to this object the same
criterion selects for the lease. Three things that refusal rejected stay
rejected and are respected here: no host column on `agent_tokens`, no presence
table, and optimistic concurrency remains the arbiter of field writes.

**Extend `offered` + `claim_task`.** It is atomic, and it is not a queue: it
needs an owner to offer each task first, it awards to a `user_id` that is
shared, it does not move the state, and it never expires. Four properties
missing, and adding all four to it is this table with a worse name. It stays as
what it is, the human contract-net announcement.

**A column on `tasks` (`held_by`, `held_until`).** Cheaper and loses the
history, which is the part that answers the question the checking station asks:
who handed this over. It also makes the row two things at once again, which is
the defect being repaired.

**Have the sweep move reclaimed tasks back a state.** Where a task should
return to is a workflow question with a workflow answer, and a sweep guessing
would write a transition nobody chose, on a workflow a project can override. It
would also destroy the evidence: the state the task was in is what says how far
the dead session got. The sweep releases possession and leaves the task where
it is — visible, pullable, honest.

## Consequences

- A task nobody holds is movable by anyone. Possession constrains only what it
  covers, which keeps the UI, the CLI, the scheduler and any agent that never
  took a lease working unchanged. The owner may always move a task: somebody
  has to be able to unstick a workspace without waiting out a deadline.
- The default TTL (3600s) is a guess and is documented as one. Nobody has
  measured how long a task takes here. It is the first number to revisit once
  the sweep has a week of `release_reason='expired'` to count.
- **The checking-station rule is on by default, and its precondition is not
  the one it first appears to be.** `exclude_own_handoffs` refuses a task the
  caller handed off itself. It keys on the WORKER id, not on identity, so two
  sessions sharing one assistant already satisfy it as long as each names
  itself; what breaks it is callers that omit `worker_id` and fall back to the
  credential, because then every checker is formally the author and every check
  is refused. Provisioning one credential per agent closes that case too, by
  making the fallback itself distinct, and that provisioning is what this
  default is downstream of. It remains a parameter because re-checking your own
  work deliberately is legitimate, and that is the caller's decision rather
  than a state of the world.
- **Nothing is emitted onto the ADR-0036 bus, and that is a deferral with a
  measured reason rather than an oversight.** The bus read half is wired and
  granted (`list_events`, whose own docstring says it is how an agent observes
  its collaborators) and nothing writes task events to it. It was tempting to
  fix that here. Two measurements stopped it: the `kind` vocabulary is a
  five-value DB CHECK about the note graph (`read`/`propose`/`commit`/
  `reject`/`snapshot`), so a lease event has to be squeezed into a word that
  means something else; and `propose`/`commit` count against the per-actor
  anti-runaway quota, so emitting on every acquire would spend an agent's
  runaway budget on taking work. Both are decisions about the bus's own
  vocabulary and belong to the bus. Meanwhile the coordination question is
  answered: `task_leases_list(include_released=True)` says who holds what and
  who handed what off, and every acquire and release is in the activity log
  with the actor and the token.
- The lease is advisory about files. Mycelium is not a filesystem mutex: it
  says who holds the work, not who holds the file. Two agents editing the same
  tree is a client-side problem with a client-side answer (per-agent working
  trees), and nothing here should be read as covering it.
