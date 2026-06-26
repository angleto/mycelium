# CLI and Neovim integration

Two adapter surfaces ship alongside the SPA, both **thin clients over
the REST API** (no business logic):

| Surface | Source | Install |
| --- | --- | --- |
| `mycelium` CLI | [`cli/`](../cli/README.md) | `brew install angleto/mycelium/mycelium-cli` |
| `mycelium-nvim` plugin | [`nvim/mycelium-nvim/`](../nvim/mycelium-nvim/README.md) | `lazy.nvim` block, see plugin README |

## Why a CLI

- Stay in tmux + Neovim for capture-heavy workflows; the SPA stays for
  dense screens (invoicing, calendar grid).
- Scriptable: `mycelium task list --json | jq …`, tmux keybinds, shell
  functions, cron capture, etc.
- Same auth model as the MCP / SPA: agent tokens (PAT), bound per
  workspace, revocable from `/agent-tokens`.

## Design constraints (carried over)

- **No new HTTP routes, no new auth flow.** The CLI mints an agent
  token via `POST /agent-tokens` after a normal email/password login,
  then uses it like any other client.
- **No client-side i18n catalog.** The CLI follows
  [mycelium-english-i18n-rule](../README.md#i18n): user-facing strings live
  in English; server errors surface their MessageCode + message
  verbatim. The Neovim plugin renders strings from the CLI's `--json`
  output, so they inherit the same policy.
- **The plugin does not embed an HTTP client.** It shells out to
  `flow … --json` via `vim.system()`. One source of truth for auth,
  one binary to update.

## Auth flow

```
mycelium auth login -u https://mycelium.xeno.garden
   │
   ▼  POST /auth/login   (+ /auth/login-mfa if 401 auth.mfa_required)
   ◇  JWT in memory
   │  GET  /auth/me      (identity)
   │  GET  /workspaces   (pick workspace)
   ▼  POST /agent-tokens  (X-Workspace-Role: owner, scope=cli, ttl_days=365)
   ◇  PAT (mycelium_at_…) saved to ~/.config/mycelium/credentials.toml (0600)
```

Subsequent commands use `Authorization: Bearer mycelium_at_…` +
`X-Workspace-Id: <bound workspace>`. Logout revokes the PAT
server-side (`DELETE /agent-tokens/{id}`) and deletes the local file.

## Packaging

CLI and plugin share **the monorepo's release tag**. When you cut
Mycelium `v2.0.6`, two mirror workflows fire on the tag push:

- [`mirror-homebrew-mycelium`](../.github/workflows/mirror-homebrew-mycelium.yml)
  renders [`packaging/homebrew-mycelium/Formula/mycelium-cli.rb`](../packaging/homebrew-mycelium/Formula/mycelium-cli.rb)
  for `v2.0.6` (downloads the tarball, computes the sha256, substitutes
  the `__TAG__` / `__SHA256__` placeholders) and pushes the result into
  `github.com/angleto/homebrew-mycelium`. Users `brew install
  angleto/mycelium/mycelium-cli` and pick up the new version.
- [`mirror-mycelium-nvim`](../.github/workflows/mirror-mycelium-nvim.yml)
  copies [`nvim/mycelium-nvim/`](../nvim/mycelium-nvim/) into
  `github.com/angleto/mycelium-nvim` and re-tags it `v2.0.6` too, so
  lazy.nvim can pin a version.

The CLI version is single-sourced in `cli/pyproject.toml` (read at
runtime via `importlib.metadata`), so a tag bump is the only edit
required for a release. `Formula/mycelium-cli.rb` installs the CLI in an
isolated `libexec` venv (keeps system Python tidy and pins deps
without colliding with other formulae).
