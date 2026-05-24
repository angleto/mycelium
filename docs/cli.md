# CLI and Neovim integration

Two adapter surfaces ship alongside the SPA, both **thin clients over
the REST API** (no business logic):

| Surface | Source | Install |
| --- | --- | --- |
| `flow` CLI | [`cli/`](../cli/README.md) | `brew install angleto/tap/flow-cli` |
| `flow.nvim` plugin | [`nvim/flow.nvim/`](../nvim/flow.nvim/README.md) | `lazy.nvim` block, see plugin README |

## Why a CLI

- Stay in tmux + Neovim for capture-heavy workflows; the SPA stays for
  dense screens (invoicing, calendar grid).
- Scriptable: `flow task list --json | jq …`, tmux keybinds, shell
  functions, cron capture, etc.
- Same auth model as the MCP / SPA: agent tokens (PAT), bound per
  workspace, revocable from `/agent-tokens`.

## Design constraints (carried over)

- **No new HTTP routes, no new auth flow.** The CLI mints an agent
  token via `POST /agent-tokens` after a normal email/password login,
  then uses it like any other client.
- **No client-side i18n catalog.** The CLI follows
  [flow-english-i18n-rule](../README.md#i18n): user-facing strings live
  in English; server errors surface their MessageCode + message
  verbatim. The Neovim plugin renders strings from the CLI's `--json`
  output, so they inherit the same policy.
- **The plugin does not embed an HTTP client.** It shells out to
  `flow … --json` via `vim.system()`. One source of truth for auth,
  one binary to update.

## Auth flow

```
flow auth login -u https://flow.xeno.garden
   │
   ▼  POST /auth/login   (+ /auth/login-mfa if 401 auth.mfa_required)
   ◇  JWT in memory
   │  GET  /auth/me      (identity)
   │  GET  /workspaces   (pick workspace)
   ▼  POST /agent-tokens  (X-Workspace-Role: owner, scope=cli, ttl_days=365)
   ◇  PAT (flow_at_…) saved to ~/.config/flow/credentials.toml (0600)
```

Subsequent commands use `Authorization: Bearer flow_at_…` +
`X-Workspace-Id: <bound workspace>`. Logout revokes the PAT
server-side (`DELETE /agent-tokens/{id}`) and deletes the local file.

## Packaging

The Homebrew tap source of truth lives at
[`packaging/homebrew-tap/`](../packaging/homebrew-tap/) in this monorepo.
On every `cli-v*` tag, the
[`mirror-homebrew-tap`](../.github/workflows/mirror-homebrew-tap.yml)
GitHub Actions workflow copies the formula + helper bin into
`github.com/angleto/homebrew-tap`, which is what `brew tap angleto/tap`
fetches. `Formula/flow-cli.rb` installs the CLI in an isolated `libexec`
venv (keeps system Python tidy and lets us pin deps without colliding
with other formulae).
