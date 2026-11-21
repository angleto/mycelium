# ADR-0070: A period is reported from a collection, not from a summary

Status: Accepted (2026-11-16)
Relates to: [0002](0002-multi-tenant-optimistic-concurrency-rls.md), whose
append-only activity log is where the task events of a period are read;
[0068](0068-one-operation-registry-for-rest-and-mcp.md), on whose registry
the operation is declared once for REST and MCP.

This record decides how Mycelium answers "what happened on this client's or
this project's tasks between two dates", the question a weekly status
meeting asks: what the answer contains, where each part of it is read from,
what a credential with a narrow scope sees of it, and why the server
collects but does not summarise.

## Context

A person who reports weekly to a client keeps the record of the work in
Mycelium as it happens: the description of a task when it is created, the
comments on it while it is worked, the memo of each timer session. Before the
meeting all of that has to be gathered, and nothing gathered it.

The existing reads could not do it together. Measured on 2026-11-16:

- `list_tasks(updated_since=...)` does not list a task whose only activity in
  the period is a comment: adding a comment does not touch `tasks.updated_at`.
  That is the commonest shape of a task being worked.
- Comments were readable one task at a time, with no query across tasks by
  date.
- `list_time_entries` and `time_report` on MCP took no window and no client,
  although the service and `GET /time/entries` and `GET /time/report` did.
- The activity log recorded `set_state` with no states, so it said a task
  had moved without saying where.

## Decision

1. **One read, `tasks.work_digest`**, bound as `GET /tasks/digest` and as the
   MCP tool `work_digest`. It takes a half-open window `[start_from,
   start_to)` whose ends must carry a UTC offset, at most 92 calendar days
   long, each end read on its own offset's clock (so Q4 across the end of
   daylight saving, 92 days and one hour of elapsed time, is one call), and a
   client tag, a project tag, or neither (the whole workspace). It answers
   every task of that scope with something in the window, most recent
   activity first, each with its events in order and its hours.

2. **The events and their sources.**
   - Created, state changed, description edited: the activity log, which is
     append-only and kept. Not `entity_revision`, whose rows are purged after
     the retention window, so a report on an older period would lose its
     transitions without saying so. `set_state` now records `from_state_id`
     and `state_id`; a transition logged before that is reported with both
     empty, not reconstructed. Consecutive description edits by one actor
     fold into one event with a count, because an editor that saves as the
     person types would otherwise bury the diary.
   - Comments: the task's work-diary comments created in the window and not
     deleted, in full. Suggestions are proposed edits to the description;
     an accepted one appears as a description edit.
   - Time entries: those STARTED in the window, the rule every time report
     applies, so the hours here and there agree; with the memo. A running
     timer is listed and not counted.
   - Tasks in the bin are left out; archived tasks are kept.

3. **Each section follows the scope that reads it.** The gate admits a
   credential holding any of `tasks:read`, `comments:read`, `time:read`; the
   operation then leaves out the events of every section the credential may
   not read and names it in `withheld`. A task with no event the credential
   may see is not listed, so its title is not disclosed through this door.
   To make this possible the operation context carries the scope list of the
   assistant behind the credential (`OpCtx.scope`, None for a person and for
   a token with no assistant), set by both transports.

4. **The server collects and does not summarise.** The prose of the report
   is written by whoever reads the collection, a person or a language model
   in front of that person, citing the tasks it draws on.

## Alternatives rejected

- **A language model reads the existing tools and assembles the period.** It
  inherits the `updated_since` blind spot, opens every task one by one for
  its comments, and two runs over the same week gather different material.
  A model is the right reader of the collection, not the way to make it.
- **A summary generated and stored by the server.** Nothing on the server
  can check it against the source, and it is stale the moment a comment is
  added after it. The person's own writing is not to be rewritten by an
  automatism until the models doing it have been verified and accepted.
- **An all-of scope on the operation** (all three keys or nothing). The scope
  model has only any-of sets, and both gates, the drift guards and the
  coverage generator read them as such; adding a second meaning to the same
  shape would let a reader that does not know about it widen access
  silently. Filtering per section also gives a narrow credential the part it
  may read instead of nothing.
- **Making a comment touch `tasks.updated_at`.** It would fix `list_tasks`
  for this question and change what "updated" means for every other reader
  (the recent list, the conflict check of an open editor). Left open.

## Consequences

- A weekly report is one call. A quarter is one call too; longer is several.
- The binding of a GET operation now validates its arguments through the
  operation's own parser, so a rule across arguments (the window) is refused
  as `operation.invalid_argument` with a 422 instead of escaping as a 500.
- What this does not cover: the content of a comment is reported as written,
  internal remarks included; choosing what is fit to tell the client remains
  the reader's job. Work done on a task in another client's scope is not in
  this client's digest, and work recorded nowhere is in no digest.
