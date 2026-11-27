# ADR-0080: A session the machine lost is reopened by its id, in its window

Status: Accepted (2026-11-27, Angelo approved it; revised the same day on his
instructions that nothing be tied to one host and nothing be overbuilt, and
after an adversarial review)
Relates to: ADR-0078 (the session entity, its hooks, their state files and
the reconcile that closes a session as `gone`), ADR-0079 (the relay a
session's hooks start).

This record decides how a person gets back the agent sessions a machine lost
in a crash, a reboot or a killed terminal: each conversation reopened as it
was, in the directory and the tmux window it ran in, with no guessing between
two sessions in one directory. It records what the crash of 2026-11-27
showed and what Claude Code 2.1.332 does when it is stopped, the decision
(the hooks keep a session's local state unless the person ended it, and
`mycelium sessions restore` reopens the rest through the host's adapter,
`claude --resume <id>` for Claude Code), the alternatives rejected, and what
it does not cover.

## Context

Angelo, 2026-11-27: after a crash he reboots, opens tmux, goes into each
directory and starts `claude` asking it to pick up where it was. It works
badly, in particular when two sessions were open in the same directory.

Measured on 2026-11-27, after the crash (the previous boot ended at 12:01:24
by `journalctl --list-boots`):

| what | measured |
|---|---|
| transcripts last written between 12:01:10 and 12:01:20 | 16, in 13 directories |
| directories with more than one of them | 3 (`engineering-handbook` 3, `mycelium` 2, `symbolum` 2) |
| of the 16, sessions live again at 12:35 | 0: every session started after the reboot was a new one |
| Claude Code's own registry (`~/.claude/sessions/<pid>.json`) | keyed by pid; it lists the live sessions and holds nothing about the lost ones |
| hook state directory on this machine | absent: the ADR-0078 hooks are not installed here yet, so this crash is not restorable by this record |

"Alive at the crash" is inferred from the burst of writes in the last
seconds of the boot, not from a signal.

Measured on Claude Code 2.1.332 with a throwaway session in tmux and hooks
that log their payload (no prompt sent):

| how the session stopped | `SessionEnd` fired | `reason` |
|---|---|---|
| `SIGTERM` (what a shutdown sends) | yes | `other` |
| `SIGHUP` (a closed terminal, a killed tmux window) | yes | `other` |
| `/exit` | yes | `prompt_input_exit` |
| Ctrl-C twice | yes | `prompt_input_exit` |

`TMUX_PANE` reached the `SessionStart` hook in both runs. The documented
reasons are `clear`, `resume`, `logout`, `prompt_input_exit` and `other`
(`code.claude.com/docs/en/hooks`); what produces `other` is not documented
beyond "other exit reasons".

Three facts decide the shape:

- **A directory does not name a session; its id does.** `claude --continue`
  takes the most recent session in a directory. Only `claude --resume <id>`
  reopens the one that was in a given window, and `claude --help` says
  `--fork-session` exists to "create a new session ID instead of reusing the
  original" when resuming.
- **The conversations survive.** All 16 transcripts were intact. What is lost
  is the mapping from window to session id, which lived in the running
  processes.
- **ADR-0078 throws that mapping away twice.** Its `SessionEnd` hook closes
  the session and deletes its state on every reason, so an orderly shutdown,
  which sends `SIGTERM`, makes every session look ended by its person. And a
  session that died without a `SessionEnd` loses its state at the first
  reconcile that closes it as `gone` (`session_sync.reconcile`).

## Decision

### 1. The hooks keep a session's state unless its person ended it

The state file of a host session (`HookState`) gains `agent`, `cwd`, the tmux
place (session name, window index and name) and `opened_at`. The
`SessionStart` hook reads the place from the pane in `TMUX_PANE` with one
`tmux display-message`, and `Stop` refreshes it, because a window can be
renamed or moved. The directory and the place are local facts and stay in the
state directory (mode 0700); none of them goes to the server, which keeps
paths out of a store every member reads (ADR-0078).

`SessionEnd` acts on its reason:

| reason | server | state file |
|---|---|---|
| `prompt_input_exit`, `logout` (the person quit) | closed as `ended` | deleted |
| `clear`, `resume` (the same terminal goes on) | closed as `ended`, as today; the next `SessionStart` opens what follows | deleted |
| `other` (a signal: shutdown, closed terminal) | nothing; the next reconcile on the machine closes it as `gone` | kept |

The reconcile no longer deletes state files. **Restorable** is then read off
what is on disk, with no new state and no server call: a state file younger
than seven days whose session the host's adapter does not list as alive. A
clean exit has no file; a lost session has one. Files older than seven days
are deleted by the next `restore` or reconcile.

### 2. `mycelium sessions restore` reopens them

It lists the restorable sessions of the machine, grouped by the tmux session
they were in, each with its project, window name, when its file was last
written and its attention at the time: the ones that were working when they
stopped are the ones most likely to have been cut mid-action, and the list
says so.

With `--apply` (all) or `--pick` (choose), for each one it creates the tmux
session if it does not exist and a window with the saved name in the saved
directory, and runs there the command its host's adapter gives for that id.
A session already alive is skipped, so running it twice opens nothing twice;
a directory that no longer exists is listed with the reason and skipped. A
session whose directory already has a new one started after the reboot is
still offered, and the line says so, because that is the case where the
person's current method loses work.

The reopened session keeps its id, so its `SessionStart` finds its own state
file, and the server reopens the same row with the same join key (ADR-0078
§2): the row, its history and the tasks its agent holds stay one session,
and messages left `not_delivered` stay so (ADR-0079 §3).

`mycelium sessions` prints one line above its page when restorable sessions
exist ("16 sessions can be reopened: mycelium sessions restore"), so the
person learns it where they already look. Nothing reopens on its own at boot:
a reboot can be deliberate, and a dozen agents resuming work nobody asked for
is the wrong default.

### 3. The restore is generic; resuming is the host adapter's

What is the same for every host is written once in the CLI: the state file,
the restorable rule, the tmux windows, the list, `--pick`, `--apply`,
`--json`. Two questions are asked of the adapter of the session's `agent`:

| question | Claude Code adapter |
|---|---|
| is this id alive on the machine | `claude agents --json` |
| the command that reopens this id | `claude --resume <id>` |

A restorable session of a host with no adapter is listed with that reason
and not reopened, rather than reopened by a guess. Only the Claude Code
adapter is written. A new host adds an adapter here, its hooks, and its
value to the server's closed `agent` type (ADR-0079 §6).

## Phase

One phase: the new state fields and where they are filled, `SessionEnd` by
reason, the reconcile that keeps files, the seven-day prune, `restore` with
`--pick`, `--apply` and `--json`, the line above `mycelium sessions`. Tests:
a session stopped with `SIGHUP` keeps its file and is offered; one ended with
`prompt_input_exit` is not; a restore reopens the same id in a window with
the saved name and directory, and a second restore opens nothing. The guard
on `SIGHUP` is recorded failing against the ADR-0078 hook. One test runs
against a real Claude Code session in tmux, armed by an environment flag,
and asserts that `--resume` keeps the id.

## Alternatives rejected

- **tmux-resurrect and tmux-continuum.** They restore windows and can rerun a
  command, but the command is `claude` without the id: two sessions in one
  directory are exactly what they cannot tell apart, and `--continue` as a
  restore strategy reopens the most recent one twice.
- **Keeping the directory on the server, so another machine could restore.**
  A conversation is on the machine that ran it; another machine has no
  transcript to resume.
- **Reopening automatically at login.** See §2.
- **A `restorable/` directory filled by the reconcile** (the first draft).
  It missed every session the idle sweep closed first, since the reconcile
  touches only open rows, and needed a server read of closed sessions to
  prune. Not deleting is simpler and misses nothing.
- **Rebuilding the list from the transcripts on a machine without hooks.**
  It can only infer from write times what was alive, read differently on
  Linux and macOS, and it serves only machines not yet set up, until they
  are. Installing the hooks is the fix.

## What this does not cover

- **What a session was doing when it stopped.** A tool call cut halfway (a
  migration, a push, a write) is not rolled back or replayed; the resumed
  session sees its transcript and the person sees "was working" on the list.
  Checking the work tree before saying "continue" stays the person's.
- **A session the person closed by killing its window on purpose.** It ends
  with `other`, like a crash, and is offered for seven days; offering it is
  a line in a list, never a reopening.
- **What else produces `other`.** Only the two signals were measured.
- **Pane layouts.** One window per session, with its name; split panes and
  their sizes are not restored.
- **Background sessions** (`claude --bg`) are Claude Code's own to respawn
  (`claude agents respawn`), and are not listed.
- **A machine that is not coming back.** The sessions are on its disk.
