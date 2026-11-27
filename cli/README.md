# mycelium-cli

A keyboard-first terminal client for [Mycelium](https://github.com/angleto/mycelium):
tasks, notes, time tracking, calendar — driven from tmux/Neovim without a
browser.

## Install

```sh
# macOS / Linuxbrew
brew install angleto/mycelium/mycelium-cli

# From this monorepo
uv sync --all-packages && uv run mycelium --help
```

Optional runtime: `sox` (or `ffmpeg`) for `mycelium note voice`.

Every command has help under `-h`, and `mycelium help <command>` prints the
same thing (`mycelium help task edit`).

## First login

```sh
mycelium auth login --base-url https://mycelium.xeno.garden
```

This trades email/password (+ TOTP if configured) for a long-lived
agent token (PAT, `mycelium_at_…`) stored at
`~/.config/mycelium/credentials.toml` with mode 0600.

```sh
mycelium auth status     # check the saved credential and round-trip /buildinfo
mycelium auth whoami     # show identity behind the saved PAT
mycelium auth logout     # forget + server-side revoke
```

That token is readable by every process you run, agents included, so the
server treats the CLI as an agent: it moves a task only while holding it,
and it holds it as a *worker*, one per shell. Open one before moving tasks:

```sh
eval "$(mycelium worker open --label term-1)"   # exports MYCELIUM_WORKER
mycelium task to a1b2 in_progress               # takes the task, then moves it
mycelium worker list                            # your open sessions
eval "$(mycelium worker close)"                 # gives back what it holds, unsets it
```

The worker lives in the shell, not in the profile: a profile is shared by
every terminal and agent on the machine, and one worker for all of them
would make them indistinguishable. A task moves to `done` from the CLI only
when another worker handed it over; your own work you hand over, and a
person closes it from the web app.

## Daily flow

```sh
# Browse + capture
mycelium today                              # running timer + appointments + deadlines
mycelium today --date tomorrow              # any day, including +N / -N offsets
mycelium week                               # next 7 days grouped per day
mycelium task list                          # open tasks only, sorted by due/priority
mycelium task list --tag client-acme --all  # include terminal states; filter by tag
mycelium task add "Write release notes" --due tomorrow --tag client-acme
mycelium note add -m "remember to bump cli version" --task a1b2  # link note → task
mycelium note voice -s 30                   # 30s voice memo (sox/ffmpeg)
mycelium timer start a1b2 --memo "fixing bug"
mycelium timer stop
mycelium timer status                       # running timers + today's billable total

# Edit existing entities
mycelium task edit a1b2 --priority 8 --due tomorrow
mycelium task edit a1b2 --description @     # opens $EDITOR pre-loaded with current body
mycelium task tag add a1b2 urgent
mycelium task comment add a1b2 -m "blocked on staging"
mycelium task remind add a1b2 60            # 60 min before due
mycelium task attach add a1b2 ./scan.pdf ./referto.pdf   # one markdown ref per file
mycelium note edit n9z3 --task -            # '-' clears the link

# Reports + graph
mycelium timer report                       # last 30d by project (hours + billable)
mycelium timer report -g client --since 2026-05-01
mycelium task graph                         # full ASCII dep tree
mycelium task graph a1b2                    # focused: predecessors / blocks

# Search + advisor
mycelium search "release v1.2"
mycelium what-now --duration 25 --location office
mycelium schedule list                      # AI-computed plan

# Workflows (states + transitions, portable between workspaces)
mycelium workflow list
mycelium workflow export 4a1b -f delivery.json   # portable doc, no database ids
mycelium workflow export 4a1b -f - | jq .states  # '-' is stdout

# Browsing
mycelium client list
mycelium project list --client acme
mycelium workspace list
mycelium notif list
mycelium open today                         # SPA fallback (browser)
mycelium open a1b2                          # open task detail in the SPA

# Multi-factor (the existing login already prompts for TOTP if 401 mfa_required)
mycelium auth mfa setup                     # prints otpauth URI + ASCII QR + secret
mycelium auth mfa activate 123456           # confirm, then save the backup codes
mycelium auth mfa status
```

Every command supports `--json` for piping into `jq`, scripts, or the
`mycelium-nvim` plugin which shells out to the same binary.

## Your agent sessions

When several Claude Code sessions run at once, `mycelium sessions` says which
one needs you, on every machine, and what each is working on
(docs/adr/0078-a-session-is-what-a-person-steers.md):

```sh
mycelium session install-hooks --apply    # once per machine: the hooks that report
mycelium sessions                         # who needs you first, then who is working
mycelium sessions --pick                  # press a row's key to jump to its tmux pane
mycelium sessions --watch 10              # keep it open in a pane, redrawn every 10 s
```

```
   NEEDS YOU (2)
1  symbolum      waiting for a permission · 4 min   Prove the bound    symbolum:0 symbolum
2  kiwi          finished, your turn · 3 h          -                  kiwi:0 FA
   WORKING (2)
3  humus         working · 12 min                   Fix the sweep      humus:1 dev
4  mycelium      working · 2 min                    -                  on laptop
```

`install-hooks` without `--apply` prints the settings block instead of
merging it; with it, the hooks are added to `~/.claude/settings.json` next to
any you already have, and the previous file is kept beside it. For a popup
from anywhere in tmux, add to `~/.tmux.conf`:

```
bind-key S display-popup -E -w 90% -h 60% "mycelium sessions --pick"
```

What the hooks send: whether the session is working, blocked on a
permission or a question, or finished and waiting for you, with the
repository's name and the machine's (`MYCELIUM_MACHINE_LABEL` overrides the
hostname). Never a path. The agent inside each session is told a key at its
start, so the worker it opens joins the session's row and the tasks it holds
show there; `mycelium worker open` in the session's shell joins the same way.
A hook that cannot reach Mycelium writes to `~/.local/state/mycelium/hooks.log`
and never slows or breaks the session.

### When to compact or restart: the reset shadow

When you turn it on, in a project you allow, the same hooks also record what a decision rule
recommends at each prompt (continue or compact, on cost, with the quality
terms it could not weigh named beside it) and what the session then did:
compacted manually or automatically, cleared, delegated to a subagent, or
went on (docs/adr/0081-a-reset-is-recommended-in-the-shadow-before-it-is-decided.md).
It acts on nothing and sends nothing. The log in
`~/.local/state/mycelium/reset_shadow/` holds numbers only: no prompt text,
no file content, no path.

```sh
mycelium session reset-shadow allow ~/work/humus   # this root may be recorded
mycelium session reset-shadow on                   # start recording (it starts off)
mycelium session reset-shadow status               # on or off, the roots, the log
mycelium session reset-shadow summary              # recommended against done
mycelium session reset-shadow off                  # stop everywhere; roots and log stay
mycelium session reset-shadow deny ~/work/humus    # stop for this root
MYCELIUM_RESET_SHADOW=off claude                   # one session not recorded
```

The rule behind the recommendations is not yet tested on real sessions, so
nothing is recorded until you run `on`, and each of the three switches
(the session, everywhere, the project) can stop it alone.

Run `install-hooks --apply` again after upgrading: the shadow also reads the
`PreCompact` event, the only one that tells a manual compaction from an
automatic one.

## What's left in the browser (deliberately)

- **Invoicing** (issuer profiles, line items, SDI XML/PDF). Visual and
  monthly; the CLI deliberately doesn't replicate it.
- **Writing a workflow**, import included. A workflow says where an agent's
  work needs another worker's check, so a person writes it, and the CLI's
  token is not a person's.
- **MFA QR scan** when an authenticator can't take a paste of the
  otpauth URI. `mycelium auth mfa setup` renders an ASCII QR; fall back to
  the browser SPA if your authenticator is on the same device.

For everything else, run `mycelium open <ref>` to jump to the SPA when you
need a richer view.

## Shell completion

The Homebrew formula installs completion for bash, zsh and fish into
`$(brew --prefix)/share/zsh/site-functions` and its siblings. The system zsh
on macOS does not search that directory: put
`fpath=($(brew --prefix)/share/zsh/site-functions $fpath)` above `compinit`
in `~/.zshrc` if `mycelium <TAB>` offers nothing.

Installed any other way (`uv tool install` on Linux, for example), print the
script with `mycelium completion <shell>` and put it where your shell loads
it from:

```sh
# zsh: a directory in $fpath, added before compinit runs in ~/.zshrc
mkdir -p ~/.zfunc && mycelium completion zsh > ~/.zfunc/_mycelium
#   ~/.zshrc:  fpath=(~/.zfunc $fpath)   # above: autoload -Uz compinit && compinit

# bash (bash-completion)
mycelium completion bash > ~/.local/share/bash-completion/completions/mycelium

# fish
mycelium completion fish > ~/.config/fish/completions/mycelium.fish
```

The script only calls back into `mycelium`, so it does not need regenerating
when the CLI is upgraded. Use it rather than `mycelium --show-completion` or
`--install-completion`: on zsh those write a script that completes nothing on
the first TAB of each shell.

Static completion comes from Typer. **Dynamic** completion on `task_id`
/ `note_id` arguments calls the API at each `<TAB>` and caches the
result for 60 s under `$XDG_CACHE_HOME/mycelium/` so subsequent presses
stay snappy. The cache is per-profile, per-workspace.

Empty/failed lookups (no creds, server down, slow network) silently
return no candidates — completion never blocks or errors.

## Profiles

A profile is a `(base_url, workspace, credential)` triple. The default
profile is `default`; switch with `--profile staging`. Re-running
`mycelium auth login --profile X` re-binds it.

## Configuration

Two files live under `~/.config/mycelium/`:

| File | Purpose | Mode |
| --- | --- | --- |
| `config.toml` | non-secret preferences (base URL, workspace) | 0600 |
| `credentials.toml` | agent token (PAT), token id, identity | 0600 |

Override the config directory with `$MYCELIUM_CONFIG_DIR`.

## License

AGPL-3.0-or-later, same as the rest of the Mycelium monorepo.
