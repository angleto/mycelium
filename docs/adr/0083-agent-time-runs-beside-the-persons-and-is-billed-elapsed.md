# ADR-0083: An agent's time runs beside the person's, and is billed as elapsed

Status: Accepted (2026-11-27; Angelo asked for it, answered the four
questions of the analysis in note cb5d9e96, project mycelium, and approved it)
Relates to: ADR-0063 (possession: the worker an agent works as), ADR-0078
(the session above the worker, and how it closes), ADR-0072 (the caller
classes), ADR-0070 (report windows count an entry by its start), ADR-0025
(a person is serial, an LLM agent is a parallel resource).

This record decides how an agent's time is tracked and billed. An agent's
timer never touches the person's, and records which agent ran it. It ends
with the agent's last sign of life. Reports bill agents by default as the
union of their intervals per client, added to the person's own time, at an
agent rate that defaults to the person's.

## Context

Angelo, 2026-11-27: agents tracking time must not interrupt his. They should
track in parallel by default. Billing their work should be possible as
elapsed time (one hour with two agents in parallel bills one hour) or as a
sum (two hours). Elapsed is the default.

Measured on 2.4 at f30f20f4:

| what | measured |
|---|---|
| an agent starts a timer over MCP with default arguments while the person's runs | the person's entry is stopped; probe test against Postgres, run twice |
| why | an agent token's subject is its owner (`security.py`), so `start_timer` writes the person's `user_id`; `parallel` defaults to false on MCP, REST, CLI and service, and the serial timer is found by `user_id` alone |
| who did the work, on an entry | nothing: no worker, session, run or token column; `executor_kind` is copied from the task, not the caller |
| how reports aggregate | a sum of `duration_seconds`, rounded per entry; no union of intervals anywhere |
| where the rate lives | only on the client (`ClientProfile.hourly_rate`, via `time_tracking._rate`), snapshotted on the entry |
| invoices | read no time entries today |
| a forgotten timer | nothing closes it; today the next serial start does, by accident |
| `start_timer` over MCP | takes no `worker_id`, unlike the tools that move tasks |
| a worker with no lease, silent | closed by the sweep after 6 h (`agent_worker_idle_close_seconds`) |
| a session whose process died | closed as `gone` by the machine's next reconcile, within about a minute (ADR-0078) |
| what refreshes a session's `last_seen_at` | a report, sent when its attention changes (a turn starts, ends or blocks), and the machine's reconcile; not each tool call, which sends nothing when the session is already working |

## Decision

### 1. The server decides who runs serial, from the caller class

`start_timer` reads `caller.current`. A person, the only serial caller,
keeps today's behaviour. Every other caller (agent, mediated, system) starts
a parallel timer whatever `parallel` says. Such a caller cannot stop, pause,
resume or edit an entry it did not start, and cannot add a manual entry: a
model writing past intervals into a person's billable time is the one kind
of entry nobody can check afterwards. The flag stays for a person who wants
a parallel timer of their own.

### 2. An agent's timer belongs to its worker

An agent starts a timer with the `worker_id` it works as (ADR-0063), like
every possession verb. A timer started by an agent without one is refused:
an agent entry nobody can end is the stale timer this record exists to
prevent. `time_entries` gains `worker_id` (nullable, null for a person's
entries). `user_id` stays the person: they are who the work is billed for,
and RLS and every report already key on it. "Agent time" means an entry with
a worker. `executor_kind` is no longer read to answer that question.

Agents do not pause. A paused entry stores only its active total, not its
segments, so its interval cannot enter a union. An agent stops and starts
again instead.

### 3. It ends with the agent's last sign of life

An open agent timer is closed when its worker closes, or when the worker's
session closes, whichever comes first. A close the agent asked for
(`stop_timer`, `worker_close`, a session ended by its person) ends the
entry at that moment. A close by a sweep (a worker idle for 6 hours, a
session `gone` or idle) ends it at the agent's last sign of life: the later
of the worker's and the session's `last_seen_at`. The session's is
refreshed when a turn starts, ends or blocks, and by the reconcile, so an
agent that dies mid-turn stops counting at the start of that turn: what is
lost is at most the turn it died in, never billed as work nobody saw.

### 4. Billing: elapsed by default, sum on request

A report bills, per client and over its window (entries counted by their
start, as ADR-0070 says):

- the person's own entries, summed, as today;
- plus the agents' entries, by default as the **union** of their intervals:
  an hour in which at least one agent worked for that client counts once.
  With `agent_mode=sum` they are summed instead.

`ClientProfile` gains `agent_hourly_rate`, nullable. An agent entry
snapshots it at its start, or the client's `hourly_rate` when it is null
(Angelo, 2026-11-27). Where overlapping agent intervals carry different
snapshots (the rate changed while both ran), the union is split at the
boundaries and each piece takes the rate of the entries covering it.
Money is rounded once per client and rate, not per entry: per-entry
rounding has no meaning for a union.

The figure is computed when the report is read, never stored. No invoice
reads time today. The invoice that first does will freeze the mode and the
figures on its lines (ADR-0009).

## Phases

- **A.** Section 1, the caller-class rule; `worker_id` on `time_entries` and
  on the timer tools; section 3, closing with the worker and the session.
  Tests against Postgres: an agent's start leaves the person's timer
  running (recorded failing on 2.4 first); an agent cannot stop, pause or
  edit the person's entry; a dead session's timer ends at its last tool
  call, not at the sweep.
- **B.** `agent_hourly_rate`, the union and `agent_mode` in the reports,
  the CLI and the web report. Tests: one hour with two agents gives one
  hour elapsed and two summed; a person's hour plus an agents' hour gives
  two; a rate change mid-overlap splits.

## Alternatives rejected

- **Parallel as the flag's default.** It is what fails today: the default is
  chosen by whoever calls, and a model's default call stops the person's
  timer.
- **Agent entries under a user of their own.** Billing is the person's, and
  every report and RLS policy keys on the person; a second user would split
  one client's work in two places.
- **One union over the person and the agents.** Measured against the
  request: a person's hour and two agents in the same hour would bill one
  hour, so the agents would cost nothing (Angelo chose the sum of the two,
  2026-11-27).
- **A union per task or per project.** Two agents on two tasks of one client
  in the same hour would bill two hours, which is the sum mode under another
  name. The client is where the rate and the invoice live.
- **Closing an agent's timer on the worker alone.** A worker with no lease
  is closed after six silent hours, and an agent can work that long without
  calling Mycelium; the session's turns are the better sign of life.

## What this does not cover

- **What an agent's hour is worth** beyond the rate: factors per agent or
  model are a rate on the client, or a later record.
- **Agents that never open a worker.** They cannot start a timer; their
  work is metered in credits (ADR-0019), which is not billing.
- **Time entries already written** by agents under the person's name. They
  carry no worker and stay the person's time.
