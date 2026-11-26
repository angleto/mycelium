# ADR-0078: A session is what a person steers, and it says when it needs them

Status: Accepted (2026-11-26, Angelo approved phases A, B and C as proposed)
Relates to: ADR-0063 (possession, and the worker it introduced), ADR-0049
(working memory stays with the caller), ADR-0072 (a credential predicate, and
the one-model-two-workers limit), ADR-0067 (an instruction is a comment
assigned to an agent), ADR-0068 (one operation registry for REST and MCP).

This record decides what Mycelium shows a person who runs many coding-agent
sessions at once, and how it learns it. The sessions page has to answer three
questions: which sessions exist, which one needs me now, and what each one is
working on. It records what was measured on 2.4.26, the decision (a session
entity above the worker, published by the agent host's hooks, joined to the
agent's worker by a secret only that session was given), the CLI and web
surfaces, the phases, the alternatives rejected, and what the design does not
cover.

## Context

Angelo, 2026-11-26: he runs many Claude Code sessions in parallel, one per
project, each in a tmux window, and follows them by switching windows to see
which is waiting for input. Mycelium was meant to help with this and does not:
on the web the sessions "are not understandable or useful, they look like an
API exposed at random on the UI".

Measured on 2026-11-26, on his machine and on production:

| what | measured |
|---|---|
| Claude Code sessions alive on the machine (`claude agents --json`) | 12 (10 interactive, 2 background) |
| Mycelium workers open in the workspace (`workers_list`) | 3 |
| of those 3, rows with a label | 2, both free text the agent made up (`cc-mycelium-2026-11-26`) |
| fields a row shows | label or a uuid prefix, "seen at", tasks held |
| fields a person needs and no row shows | project, machine, whether it needs them, since when |
| where the panel lives | a collapsed `<details>` on the tasks board, no route, no navigation entry |

The panel renders `WorkerOut` field by field, and `WorkerOut` is the shape of
a possession primitive. A worker exists to tell sessions apart for leasing
(ADR-0063); it is opened only by an agent that is about to take a task, so a
session reading, reviewing or waiting for permission has none. The panel
cannot answer "which one needs me", because Mycelium is never told.

What the agent host already knows, measured and checked against the Claude
Code documentation on 2026-11-26:

- `claude agents --json` lists every live session on the machine, interactive
  ones included, with `status` in `busy | waiting | idle` and, when waiting,
  `waitingFor` (`permission prompt`, `input needed`, ...). Walking each `pid`
  up to a tmux `pane_pid` mapped 10 interactive sessions out of 10 to their
  pane. It is a research preview and its shape may change.
- Hooks fire on the transitions: `SessionStart` (startup, resume, clear,
  compact, fork), `UserPromptSubmit`, `Notification` with
  `permission_prompt` (after the prompt has waited about six seconds) and
  `elicitation_dialog`, `PostToolUse`, `Stop` (not on a user interrupt),
  `StopFailure`, `SessionEnd`. A hook may be `async` and then blocks nothing.
  `SessionStart` may inject context into the session and persist environment
  for its shell commands (`CLAUDE_ENV_FILE`).

One constraint decides the shape. The hooks run the CLI, whose credential is a
bare token. The agent reaches Mycelium through the MCP connector, whose token
is bound to an assistant. `agent_workers.belongs_to` refuses to let one act
under a worker the other opened, and that refusal is correct: it is what makes
the close-check record unforgeable (ADR-0072). So one Claude Code session
involves two credentials, and the thing a person steers is not either of their
workers.

## Decision

### 1. A session is its own entity, and a worker may belong to one

`agent_sessions` is a new org-scoped, RLS-protected table: one row per
agent-host session, opened by the host's hooks. It carries:

- `host_ref`: the host's own id for the session (Claude Code's `session_id`),
  as the retry key per opening credential, so a resume or a lost reply finds
  the same row. Never an authority input, and never matched across
  credentials.
- `agent`: which host it is (`claude_code` today), a closed type.
- `project`: the basename of the git toplevel, else of the working directory.
  Never the path: a path carries the user's home directory into a store every
  member of the workspace reads.
- `machine`: a label for the machine, the short hostname unless
  `MYCELIUM_MACHINE_LABEL` overrides it.
- `title`: the host's name for the session when it has one, decoration
  only. Claude Code does not pass it to hooks; `mycelium sessions` fills it
  from `claude agents --json`, so it is empty until that runs once.
- `attention`: a closed type, `working | blocked | your_turn`, with
  `attention_since` and, for `blocked`, `blocked_on` (`permission | question`).
- `opened_at`, `last_seen_at`, `closed_at`, `closed_reason`.

`agent_workers` gains a nullable `session_id`. A worker belongs to at most one
session; a session may have several workers (the agent's over MCP, the CLI's
in the session's shell).

### 2. The agent's worker joins the session by a secret only that session holds

Opening a session returns a `join_key`: random, stored as a hash. The
`SessionStart` hook injects into the session's context the line telling the
agent to pass it to `worker_open`, and writes it to `CLAUDE_ENV_FILE` so the
CLI in the session's shell joins too (`mycelium worker open` reads
`MYCELIUM_SESSION_KEY`). `worker_open(join_key=...)` binds the new worker to
the session.

This is a capability, not a declaration. A sibling session on the same
credential cannot name another's session, because it was never given its key;
another member of the workspace holding a leaked key joins nothing, because
the key must come from the person who opened the session. The worker stays
bound to the credential that opened it, so nothing in ADR-0072 moves: the join
adds membership, never authority, and a test takes a task under the agent's
worker with the hooks' credential and is refused. A key that is unknown, or
whose session closed, is refused with its own code and no worker is opened,
so the agent learns its context is stale instead of silently working
unjoined; the CLI in the shell opens its worker unjoined and says so.

**The key survives a reopening.** The hooks keep the key in their state file
and present it whenever they open the session again; the server keeps a key
that matches its hash and issues a new one otherwise. Measured end to end on
2026-11-26, before this: a session the server had closed was reopened by its
next hook with a new key, and the agent's `worker_open` was refused. The same
run found the second way to lose it: the reconcile a `SessionStart` launches
runs before the new session is in the host's registry, and the hooks dropped
the state file of every session the registry did not list. They now drop only
the files of sessions the server closed, and the server applies the grace.

### 3. Attention is reported by the host, on transitions, and the server reads it as a claim with a date

| hook | attention |
|---|---|
| `SessionStart` | open (or reopen) the session, `your_turn` |
| `UserPromptSubmit`, `PostToolUse` | `working` |
| `Notification: permission_prompt` | `blocked`, on `permission` |
| `Notification: elicitation_dialog` | `blocked`, on `question` |
| `Stop`, `StopFailure` | `your_turn`, and a reconcile of the machine |
| `SessionEnd` | close |

Every hook runs one command, `mycelium-hook`, which reads the payload on
stdin. `SessionStart` is synchronous, because it injects the key and an async
hook cannot; `SessionEnd` is synchronous, because the process is exiting;
every other hook is `async` and makes nobody wait. The hook never fails the
session it observes: every error goes to a log in the state directory and the
exit code is 0, because an observer that can break what it observes is worse
than none.

The hook is cheap when there is nothing to say. Measured 2026-11-26: the full
CLI takes 0.7 to 0.9 s to start (importing the HTTP client alone takes most of
it), a bare interpreter 0.1 s. The hook decides with the standard library and
the state file whether the state changed, which costs 150 to 170 ms for the
`PostToolUse` that fires on every tool call; only a transition loads the
client and calls the server, 0.5 to 0.9 s in an async hook.

Only the credential that opened a session may report on it or close it, the
same predicate shape as `belongs_to`. Any member may read it. A repeated state
keeps the time it was entered, so "working for 40 min" is not reset by every
tool call.

**Waiting and dead look the same from the server, so the machine says which.**
A session that waits for its person sends nothing, and neither does one whose
terminal was killed; there is no periodic hook (checked against the hooks
reference on 2026-11-26), and `SessionEnd` is not documented to fire on a
killed terminal. `sessions.reconcile` takes the list of sessions the host has
alive on a machine and closes, as `gone`, the open sessions this credential
opened on that machine that are not on it, and confirms the rest. It is run by
the hooks at most once a minute per machine, on `Stop` and, detached, on
`SessionStart`, and by every `mycelium sessions`. A session heard from in the
last two minutes is never closed by it: the host reads its list before the
call arrives, and a session opened in between is alive and unlisted. The
margin is on the server's clock, so a host whose clock is off cannot close
what it should not.

The server derives nothing else. A `working` session not heard from in 15
minutes is shown as gone quiet; a waiting one never is, because its silence
is the expected kind. The idle sweep closes a session silent for three days
with no open worker, for the machine that never comes back to reconcile.

### 4. The CLI view is local first

`mycelium sessions` reads three sources, in this order of authority:

1. `claude agents --json`: which sessions are alive on this machine and their
   live status, including `waiting` with what for. Its tmux pane is found by
   walking each pid up to a `pane_pid` through `ps`, which answers the same
   columns on Linux and macOS.
2. The hooks' state files: which Mycelium session each host session opened.
3. `GET /sessions`: the tasks each session's agent holds, and the sessions on
   other machines.

Before reading the page it corrects the server for this machine: a live
session the hooks never opened (hooks installed after it started) is opened,
a state that differs from the registry's is sent, the machine is reconciled.
The registry is the fresher source, and this is what clears the stale
`working` an interrupt leaves (`Stop` does not fire on one). The page is then
drawn in the server's order and groups, the same as the web: **Needs you**,
**Working**, **Gone quiet**. A session on another machine is in its group like
any other, with the machine where a local one shows its tmux window; a group
per machine would put "which one needs me" second.

`--pick` is for `tmux display-popup -E`: each row carries a key, and the key
switches the client to that session's pane. `--watch N` redraws every N
seconds, for a pane kept open beside the work. `--json` prints the rows with
their tmux target. An unknown shape from `claude agents --json` is an error
naming the command, never an empty list; without the server, the view shows
this machine from the registry alone and says so.

`mycelium session install-hooks` prints the settings block and the tmux
binding; with `--apply` it merges the hooks into `~/.claude/settings.json`,
adds them only to events that do not run them yet, leaves every other hook as
it was, and keeps a copy of the previous file.

### 5. The web has a sessions page, not a panel of rows

A `/sessions` route in the navigation, and a count of sessions that need the
person in the app shell, visible from every page.

- The page reads top to bottom as the questions are asked: **Needs you**
  (blocked first, then your turn, each with how long it has waited), then
  **Working**, then **Gone quiet**, collapsed.
- A row leads with the project and the task it holds (linked), then the state
  in words ("waiting for a permission for 12 min", "finished, waiting for your
  next instruction"), then machine and title in a quieter line. No uuid is
  ever shown; a session without a project shows its machine and title.
- The tasks board keeps one line, "3 sessions, 1 needs you", linking to the
  page, in place of the collapsed panel.
- Workers opened outside any session (another MCP client) appear as sessions
  of agent `unknown` with no attention, so the page stays complete rather
  than tidy.
- The page refreshes every 15 seconds while visible and on focus. A push
  channel does not exist in the app (no SSE endpoint), and adding one for a
  page that a 15-second poll serves is not justified yet.

### 6. One operation registry

`sessions.open`, `sessions.report`, `sessions.reconcile`, `sessions.close`
and `sessions.list` are declared once on the operation registry of ADR-0068,
as task pins were: new operations arrive on the registry rather than beside
it. The four writes are REST-only, declared so with their reason: they are
made by the host, which sees the session's prompts, never by the model inside
it, whose word on whether it is blocked is not one to show a person as fact.
The read is on both surfaces (`sessions_list` over MCP), so an agent can see
who else is working and on what. The order and the groups are computed once,
in the service, so the CLI and the web cannot disagree about who comes first.

The worker operations stay where they are, written by hand on both surfaces,
and this record does not move them. Measured while starting phase A: the two
surfaces answer a worker with different keys (`id` over REST, `worker_id`
over MCP), and the REST close takes the worker in its path, which the
registry's body binding refuses. Moving them means changing what released
CLIs and running agents already read, for a change that only adds one
argument. `join_key` is added to the route and to the tool, and a test
asserts that both surfaces accept it and answer the session id, so the pair
that nothing held together is held on the part this record touches.

## Phases

- **A.** Table, migration, service, the session operations on the
  registry, `join_key` on the worker route and tool with the test that holds
  the two together, the idle sweep, tests against Postgres.
- **B.** CLI: `mycelium-hook` (the one entry point every hook calls),
  `mycelium sessions` with `--pick`, `--watch` and `--json`,
  `mycelium session install-hooks` with `--apply`.
- **C.** Web: the route, the shell count, the board line, i18n in it and en,
  the browser suite selecting by role.

## Alternatives rejected

- **Make the CLI token an assistant token so both credentials share the
  worker.** It would make the hook-side and the agent one principal by
  identity, which is exactly the limit ADR-0072 states as a cost (one model,
  two workers, each able to name the other's). It widens a hole to avoid a
  table.
- **Group workers by a session id the client declares.** Free and automatic,
  and the reason ADR-0063 minted ids: a declared string is one any sibling
  can repeat. The join key is server-minted and given to one session only.
- **Keep liveness local and leave the server out (the first candidate of
  this analysis).** It holds for the CLI and fails the stated need: the web
  is where Angelo looks when he is not at that machine, and the server is the
  only place that sees every machine. What stays local is the detection;
  what is published is a claim with a date.
- **Scrape tmux panes, or adopt a tool that does (claude-squad, recon,
  claude-tmux).** Scraping breaks on any change of the TUI's text, cannot
  tell silent thinking from idle, and sees one machine. The host's own
  registry and hooks are structured and documented.
- **A heartbeat from Claude Code's status line.** `statusLine.refreshInterval`
  is the only timer the host offers, and it would tell waiting from dead
  directly. It replaces the person's own status line, which is theirs to
  configure, and it runs a command per session every few seconds to learn what
  one reconcile per minute per machine already says.
- **Adopt a tool that owns the sessions (agent view, vibe-kanban,
  agent-deck).** They require every session to be born under them, and none
  knows what task a session is on.

## What this does not cover

- **Steering from the web.** Sending an instruction to a session from the
  page needs a delivery path into a running local process. ADR-0067 already
  stores an instruction as a comment assigned to an agent; delivering it is a
  local relay this record does not design. It is the next record, and the
  page is built so that the action has a place on the row.
- **Interrupts.** `Stop` does not fire on a user interrupt, so a session
  interrupted and left alone stays `working` on the server until the next
  prompt, or until a `mycelium sessions` on that machine corrects it. The web
  says "since" next to every state for this reason.
- **How long a session already open has waited.** A session that was running
  before the hooks were installed is opened by the first `mycelium sessions`,
  and its state is dated from then: the host's registry says what a session is
  doing, not since when.
- **A machine where nothing runs.** If every session on a machine waits and
  none ends a turn, nothing reconciles it; a terminal killed there stays on the
  page as waiting until something runs on that machine, or for three days.
- **Hosts other than Claude Code.** The table names the host; only the
  Claude Code hooks are written.
- **The key in the transcript.** The join key enters the session's context,
  so it is in the transcript on disk and in what the host sends to its model
  provider. What it grants is membership of a worker in one session's row,
  never an action; a leaked key lets its holder put a worker of its own
  under that row until the session closes.
- **A dishonest host.** Attention is a claim by the credential that opened
  the session. It is shown as one, never used to decide anything.
