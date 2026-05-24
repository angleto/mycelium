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
flow auth login --base-url https://flow.leto.blue
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
flow today                        # running timer + tasks due today
flow task list --state todo
flow task add "Write release notes" --due tomorrow
flow task done a1b2               # short prefixes work when unique
flow note add -m "remember to bump cli version"
flow note voice -s 30             # 30s voice memo
flow timer start a1b2
flow timer stop
```

Every command supports `--json` for piping into `jq`, scripts, or the
`flow.nvim` plugin which shells out to the same binary.

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
