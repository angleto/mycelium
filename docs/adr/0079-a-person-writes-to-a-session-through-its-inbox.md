# ADR-0079: A person writes to a session through its own inbox, and the page says whether it arrived

Status: Accepted (2026-11-27, Angelo approved it with the 30-day retention;
revised the same day on his instructions that the server stay host-neutral
and that nothing be overbuilt, and after an adversarial review)
Relates to: ADR-0078 (the session entity, its hooks and its page, which left
this record to be written), ADR-0067 (an instruction is a comment assigned to
an agent), ADR-0072 (the caller classes, and which acts are a person's),
ADR-0068 (one operation registry).

This record decides how a person sends an instruction from the sessions page
to one of their agent sessions running on a machine, and how the page tells
them whether it arrived. It records what was measured on 2.4.28 and Claude
Code 2.1.332, the decision (a host-neutral message record on the server; per
host, a relay that polls it; for Claude Code, a relay per session started by
its hooks, writing into the session's documented inbox socket), the delivery
states, who may send and who may read, the phases, the alternatives
rejected, and what the design does not cover.

## Context

ADR-0078 made the page answer "which session needs me". It does not let the
person answer: to reply they still go to the machine, find the tmux window,
and type. Angelo's need, stated on 2026-11-26, was both halves: follow the
sessions and give them input. Mycelium must not be tied to one agent host or
model provider (Angelo, 2026-11-27): other LLMs are to be integrated.

Measured on 2026-11-27, on Angelo's machine:

| what | measured |
|---|---|
| live Claude Code sessions (`claude agents --json`) | 10 |
| inbox sockets under `/run/user/1000/cc-socks/` | 10, one per session |
| of the 10, sessions launched with `--channels` | 0 |
| variables in a session's Bash | `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_SESSION_ID`, `TMUX_PANE` |
| a line written to a session's own socket from its Bash, after the auth line | delivered between two tool calls of a busy turn, shown to the model as a message from another session, not as the person's words |
| what the socket answers | nothing, on success too (connection closed with no bytes) |
| push channel from the server to a client | none (no SSE or WebSocket route) |
| writes made by any authenticated call with an agent token | one: `UPDATE agent_tokens SET last_used_at = now()` in the token resolution (`core/migrations/versions/0012_agent_token_workspace_binding.py`) |
| `kernel.yama.ptrace_scope` | 0: any process of the user may attach to any other |
| transcripts under `~/.claude/projects` | mode 600, owned by the user: readable by every process the user runs |

What Claude Code documents, read on 2026-11-27
(`code.claude.com/docs/en/cross-session-messaging`, `/channels`,
`/channels-reference`, `/hooks`, `/remote-control`, `/agent-view`):

- **The inbox socket** is bound by every session from 2.1.224 (macOS, Linux,
  WSL 2), with no launch flag; its path and a per-session token are exported
  to hooks and Bash before any hook runs. "A script or hook to post into a
  session" is a stated use; such a process proves it is the session's own
  child with `{"type":"auth","token":"..."}` as its first line. An idle
  session starts a turn; a busy one reads the message between tool calls. A
  message is plain text, about a million characters at most, and it "can't
  approve anything": permission prompts still fire. The frame after the auth
  line is not on the public page; the binary's own debug line prints it as
  `{"type":"user","message":{"role":"user","content":"..."}}`, which is what
  the measurement used.
- **Channels** (research preview) push events into a session and can relay
  permission prompts, but only into a session launched with `--channels`, and
  a server outside the allowlisted marketplace also needs
  `--dangerously-load-development-channels`.
- **Hooks** cannot wake an idle session: their output waits for the next
  turn.
- **Remote Control** joins a session to claude.ai, with no API for a third
  party. `claude agents` sends only from its own interactive panel.

## Decision

### 1. A session message is its own record, written by a person to their own session

`agent_session_messages` is a new org-scoped, RLS-protected table: the
session it is for, the person who wrote it, the text (at most 8 000
characters), and its state with the time it was entered.

It is not an ADR-0067 instruction. That one is a comment anchored on a
document, closed by the person when the work it asked for is done. This one
has no document, and its lifecycle is delivery, not completion.

**Only a person sends, and only to an open session they opened.** The send
requires `caller.is_person`, so every other class is refused: an agent
token, the CLI's included (it sits in a file every process of the user can
read), a capability, a dispatched run, a message turned into calls by a
model, a system caller. The reason is what a message reaches: a session with
local tools, and in auto mode one that acts without asking. If an agent
could send, an agent misled by what it read in one project could drive the
person's sessions in every other project. A session someone else opened, or
one already closed, is answered as absent, never as forbidden.

**Only the person who wrote a message reads it.** The sessions list is read
by every member and, over MCP, by their agents (ADR-0078 §6); a message may
hold anything the person types, personal data included. Messages are
therefore never in `sessions.list`. A separate read, REST only and person
only, returns the caller's own messages to their open sessions. Each list row
gains `can_send`, true when the caller is a person, opened the session, and
the session accepts messages (§3), so the page shows a composer exactly where
a send will succeed.

### 2. The Claude Code relay belongs to its session

This section describes the one relay written now; §6 says what of it is the
server's and what is the host's.

`SessionStart` launches `mycelium-hook --relay` detached, as it already
launches the reconcile. The relay inherits the socket path, the token and
`CLAUDE_PID`, the Claude Code process the hook is a child of (all three
measured in a hook's environment, phase B), and keeps them in memory; they
never reach the server or a file. One relay per Claude Code process, not one
per machine, because the token is per process and exists only in that
process's children; a machine daemon would have to gather every session's
token in one place.

One relay per process and not per session, because `/clear` and `/resume`
give the process a new session id and fire `SessionStart` again in it: a
relay per session would leave one more behind at each. The relay takes an
exclusive `flock` on a lock file named after the process, and a second relay
for the same process exits at once; the lock goes with the process, so a
crashed relay never leaves it held. Before starting a relay, `SessionStart`
writes which host session the process is in now, and the relay reads it at
every poll, so after a `/clear` the running relay serves the new session.
The relay exits at the first poll that finds `CLAUDE_PID` gone: a relay is
detached from the terminal, so without this an orphan of a killed session
would keep polling. A dead process's pid is not taken again within one
poll: on the machine of the measurement pids advanced by about a hundred a
second against a `pid_max` of 4 194 304, so a pid returns after hours.

### 3. Delivery: poll, claim, write, report, at most once

1. Every 5 seconds the relay calls `POST /sessions/messages/claim`
   with the credential that opened the session (the same predicate as
   `report`). The server answers with no message, or with the oldest
   `queued` one, which it moves to `claimed` under the session's row lock.
2. The relay writes the auth line and one frame to the socket. The text
   begins with a line the server composed and returned with the message,
   naming the person and the message ("Message from Angelo, sent from the
   Mycelium sessions page, message 3f2a"), so the receiving model knows where
   it came from and every relay says it the same way.
3. The relay reports `delivered`, or `failed` with the socket error, to
   `POST /sessions/messages/outcome`. The outcome is accepted
   only from `claimed`, and only the first one counts; a repeat answers the
   stored state.

`delivered` means written to the socket without an error, and no more: the
socket answers nothing on success either, so a write Claude Code accepted
and dropped looks the same. The evidence that the session received it is the
row's attention moving to working, which the page shows beside it.

**States.** Five are stored: `queued`, `claimed`, `delivered`, `failed`, and
`not_delivered`, which every path that closes a session (`close_session`,
`reconcile`, `close_idle`) sets on its `queued` messages in the same
transaction. It is stored, not read off the closed session, because a closed
session can be reopened (a resume, ADR-0080), and its new relay must not
deliver, days later, an instruction the page had shown as not delivered. A
`claimed` message is left as it is: its relay may already have written it,
and the outcome it reports, accepted even after the close, says so; without
one it reads as uncertain.
One state is read off the stored ones: `claimed` for more than two minutes
is shown as **uncertain**. Nothing is sent again on its own: an instruction
carried out twice ("rebase and push", "delete the branch") cannot be undone,
and one not carried out is visible on the page and costs one click.
Resending creates a new message.

**Polled every five seconds, not pushed.** Ten sessions on a machine make two
requests a second, each an indexed read, and a message waits five seconds at
most. Each authenticated call also touches its token's `last_used_at`, and
ten relays share the CLI's token: two updates a second on one row, for a
timestamp read at minute resolution. Phase A corrects that at its origin, in
the token resolution: the column is written only when it is a minute old. A
push (a long-poll woken by `LISTEN`) would cut the requests and the wait at
the cost of a listener connection per API process and waits that must not
hold a pooled connection (15 per process); worth building when the request
rate is measured to matter, not before.

**Whether a session accepts messages is said by its host, once.** The hooks
open the session with `accepts_messages` when the machine runs relays (§5).
A relay that died while its session lives leaves its messages `queued`, and
the page says so ("waiting to be delivered"); no heartbeat is kept for it.
A claim that answers 404 (a live session the idle sweep closed after three
silent days) is not an exit: the relay keeps polling, and the session's next
hook reopens it.

### 4. The page

A row with `can_send` has a one-line composer, including rows blocked on a
permission: the message waits in Claude Code until the prompt is answered in
the terminal, which is the honest outcome, and the row says the session is
blocked. Under the composer, the row shows the person's last five messages
to it, each with its state in words ("delivered 12:41", "not delivered: the
session ended", "may not have arrived, check the terminal"). The answer
itself is in the terminal and the transcript; this record does not copy it.

### 5. Operations

On the registry of ADR-0068, REST only:

- `sessions.send` (person only, §1);
- `sessions.messages` (person only: the caller's messages to their open
  sessions, last five each);
- `sessions.messages_claim` and `sessions.message_outcome` (the opening
  credential, like the ADR-0078 writes).

`sessions.open` gains `accepts_messages`; the list row gains `can_send`. No
MCP tool is added: a model has no part in this path. `install-hooks --relay`
is how a machine opts in: it writes the hooks with the relay enabled, and the
hooks open sessions with `accepts_messages`.

### 6. The server knows no host; the relay is the host's adapter

The server's record, states and operations name no socket, token, frame or
host, and the session's `agent` is displayed and never branched on in this
path. Everything specific to Claude Code is in the relay, a client of the two
relay operations like any other. Another host whose session can receive text
from a local process gets a relay of its own against the same operations; on
the server it adds only its value to the `agent` type, which is closed by
design (a value nobody declared must fail, not pass as a default). A host
with no such path opens its sessions without `accepts_messages`, and they
show no composer.

### 7. Retention

Messages are deleted 30 days after their session closes, by the idle sweep
that already closes stale sessions, and with the session if it is erased.

## Phases

- **A.** Table, migration, service, the four operations on the registry,
  `accepts_messages` on open, `can_send` on the row, `not_delivered` on the
  three close paths, the uncertain state, the retention sweep, and the
  minute throttle on `last_used_at`. Tests against Postgres, including the
  refusal to every non-person caller and to another member, a claim raced by
  two callers that only one wins, a resumed session whose old messages stay
  `not_delivered`, and a second outcome that changes nothing.
- **B.** The Claude Code relay in `mycelium-hook`: start on `SessionStart`,
  the lock, the process check, the claim loop, the two lines it writes
  (pinned by a test), `install-hooks --relay`. One test runs against a real
  Claude Code session in tmux, armed by an environment flag, and is recorded
  failing against a frame Claude Code does not read. (The record first said
  "against a relay without the auth line"; measured, that relay delivers on
  Linux, so it is no negative control.)
- **C.** Web: the composer on rows with `can_send`, the messages and their
  states, i18n in it and en, the browser suite selecting by role.

## Alternatives rejected

- **Channels, the documented push path.** They also relay permission
  prompts, which the inbox cannot. But a session hears a channel only if it
  was launched with `--channels`, plus the flag that turns off the allowlist,
  on every launch; none of the 10 sessions measured were. They remain the
  route for answering a permission prompt from the page, a later record.
- **tmux `send-keys` into the pane.** It types into whatever the pane shows
  (a permission dialog, a half-written prompt, a pager), cannot know whether
  the text arrived, and mixes the person's keystrokes with the page's.
- **A `Stop` hook that blocks the end of a turn with the pending message.**
  It reaches a session only while it ends a turn, never one already waiting
  for its person, which is the session the page lists first.
- **One relay per machine.** It would have to hold every session's token.
- **Sending from the CLI or over MCP.** Both are model-reachable; see §1.
- **Messages in the sessions list.** Every member and their agents read it;
  see §1.
- **Automatic retry of an unconfirmed message.** An instruction is not
  idempotent; see §3.
- **A long-poll woken by `LISTEN`, and a relay heartbeat.** See §3.
- **A secret of the relay's own, so that only the relay can claim**
  (proposed by the review). It would stop another process holding the CLI
  token from claiming a session's messages, reading them and reporting them
  delivered. It does not stop anything those processes cannot already do on
  this machine: with `ptrace_scope` 0 any of them can attach to the relay and
  read the secret, any of them can read every session's transcript, and any
  of them can kill the relay. A process of the person's own user is inside
  the boundary this design protects, so a key against it would be ceremony.

## What this does not cover

- **Answering a permission prompt from the page.** The inbox cannot approve
  anything, by Claude Code's design; a session blocked on a permission still
  needs its terminal. The route is the channel permission relay, which needs
  sessions launched with `--channels`; it is the next record.
- **Processes of the same user.** Any of them can claim, read or swallow a
  message meant for a session on that machine, using the CLI token; see the
  last rejected alternative. The boundary this design holds is between
  people and between workspaces, and between a model and the send.
- **A stolen web session.** The web app authenticates with a bearer token,
  not a cookie, so a forged cross-site request carries no credential; a
  script running in the page (XSS) or a stolen token can send to every
  session of that person that runs a relay, with the person's own power. The
  relay is an opt-in per machine for this reason.
- **A compromised Mycelium server.** It could write text into every session
  that runs a relay, at any time, including idle ones. It already writes the
  instructions every connected session reads at its start and every tool
  result it returns, so what is new is the timing, not the reach. It cannot
  approve a permission; in a session in auto mode, text is enough to cause
  actions. Signing messages in the browser with a key only the person holds,
  checked by the relay, would close this; it is not designed here.
- **The frame format.** It is not on Claude Code's public page. If it
  changes, the socket accepts the write and drops the message, and the
  relay reports `delivered`; the page would show a delivered message on a
  row whose attention did not move. Measured in phase B: a frame with
  another `type` is written without an error and never reaches the
  session. The armed end-to-end test notices it first, and was recorded
  failing against that frame.
- **The auth line proves nothing on Linux.** Measured in phase B on 2.1.332:
  a message with no auth line, and one with a wrong token, reach the
  session all the same. The relay sends the documented line, which Windows
  requires, but on Linux any process of the user can write into any
  session, with or without this design (see processes of the same user).
- **`crossSessionInbound` set to `hold` or `refuse`.** Measured in phase B:
  with either, the message did not reach the session within a minute, and
  the socket took the write without an error, so the relay reports
  `delivered`. A person who sets either should not run the relay on that
  machine.
- **A session without hooks**, or started before they were installed, has no
  relay and shows no composer.
- **Windows**, where the auth line is required and the socket is a named
  pipe, is not built.
