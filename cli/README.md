# flow-cli

A keyboard-first terminal client for [Flow](https://github.com/angleto/flow):
tasks, notes, time tracking, calendar — driven from tmux/Neovim without a
browser.

## Install

```sh
# macOS / Linuxbrew
brew install angleto/tap/flow-cli

# Anywhere with Python 3.12+
pipx install flow-cli

# From this monorepo
uv sync --all-packages && uv run flow --help
```

Optional runtime: `sox` (or `ffmpeg`) for `flow note voice`.

## First login

```sh
flow auth login --base-url https://flow.xeno.garden
```

This trades email/password (+ TOTP if configured) for a long-lived
agent token (PAT, `flow_at_…`) stored at
`~/.config/flow/credentials.toml` with mode 0600.

```sh
flow auth status     # check the saved credential and round-trip /buildinfo
flow auth whoami     # show identity behind the saved PAT
flow auth logout     # forget + server-side revoke
```

## Daily flow

```sh
# Browse + capture
flow today                              # running timer + appointments + deadlines
flow today --date tomorrow              # any day, including +N / -N offsets
flow week                               # next 7 days grouped per day
flow task list                          # open tasks only, sorted by due/priority
flow task list --tag client-acme --all  # include terminal states; filter by tag
flow task add "Write release notes" --due tomorrow --tag client-acme
flow note add -m "remember to bump cli version" --task a1b2  # link note → task
flow note voice -s 30                   # 30s voice memo (sox/ffmpeg)
flow timer start a1b2 --memo "fixing bug"
flow timer stop
flow timer status                       # running timers + today's billable total

# Edit existing entities
flow task edit a1b2 --priority 8 --due tomorrow
flow task edit a1b2 --description @     # opens $EDITOR pre-loaded with current body
flow task tag add a1b2 urgent
flow task comment add a1b2 -m "blocked on staging"
flow task remind add a1b2 60            # 60 min before due
flow task attach add a1b2 ./scan.pdf
flow note edit n9z3 --task -            # '-' clears the link

# Reports + graph
flow timer report                       # last 30d by project (hours + billable)
flow timer report -g client --since 2026-05-01
flow task graph                         # full ASCII dep tree
flow task graph a1b2                    # focused: predecessors / blocks

# Search + advisor
flow search "release v1.2"
flow what-now --duration 25 --location office
flow schedule list                      # AI-computed plan

# Browsing
flow client list
flow project list --client acme
flow workspace list
flow notif list
flow open today                         # SPA fallback (browser)
flow open a1b2                          # open task detail in the SPA

# Multi-factor (the existing login already prompts for TOTP if 401 mfa_required)
flow auth mfa setup                     # prints otpauth URI + ASCII QR + secret
flow auth mfa activate 123456           # confirm, then save the backup codes
flow auth mfa status
```

Every command supports `--json` for piping into `jq`, scripts, or the
`flow.nvim` plugin which shells out to the same binary.

## What's left in the browser (deliberately)

- **Invoicing** (issuer profiles, line items, SDI XML/PDF). Visual and
  monthly; the CLI deliberately doesn't replicate it.
- **MFA QR scan** when an authenticator can't take a paste of the
  otpauth URI. `flow auth mfa setup` renders an ASCII QR; fall back to
  the browser SPA if your authenticator is on the same device.

For everything else, run `flow open <ref>` to jump to the SPA when you
need a richer view.

## Shell completion

```sh
flow --install-completion          # zsh / bash / fish / pwsh
```

Static completion comes from Typer. **Dynamic** completion on `task_id`
/ `note_id` arguments calls the API at each `<TAB>` and caches the
result for 60 s under `$XDG_CACHE_HOME/flow/` so subsequent presses
stay snappy. The cache is per-profile, per-workspace.

Empty/failed lookups (no creds, server down, slow network) silently
return no candidates — completion never blocks or errors.

## Profiles

A profile is a `(base_url, workspace, credential)` triple. The default
profile is `default`; switch with `--profile staging`. Re-running
`flow auth login --profile X` re-binds it.

## Configuration

Two files live under `~/.config/flow/`:

| File | Purpose | Mode |
| --- | --- | --- |
| `config.toml` | non-secret preferences (base URL, workspace) | 0600 |
| `credentials.toml` | agent token (PAT), token id, identity | 0600 |

Override the config directory with `$FLOW_CONFIG_DIR`.

## License

AGPL-3.0-or-later, same as the rest of the Flow monorepo.
