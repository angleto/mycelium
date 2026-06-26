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
mycelium task attach add a1b2 ./scan.pdf
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

## What's left in the browser (deliberately)

- **Invoicing** (issuer profiles, line items, SDI XML/PDF). Visual and
  monthly; the CLI deliberately doesn't replicate it.
- **MFA QR scan** when an authenticator can't take a paste of the
  otpauth URI. `mycelium auth mfa setup` renders an ASCII QR; fall back to
  the browser SPA if your authenticator is on the same device.

For everything else, run `mycelium open <ref>` to jump to the SPA when you
need a richer view.

## Shell completion

```sh
mycelium --install-completion          # zsh / bash / fish / pwsh
```

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
