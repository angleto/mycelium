# flow.nvim

Neovim front-end for the Flow CLI. Browse tasks, notes, today's
agenda, and capture new notes/tasks without leaving your editor.

This plugin **shells out** to `flow` (the CLI from this monorepo); it
does not embed an HTTP client. Install `flow` first.

## Install

### 1. Install the CLI

```sh
brew install angleto/tap/flow-cli
# or
pipx install flow-cli
```

Log in once:

```sh
flow auth login --base-url https://flow.leto.blue
```

### 2. Install the plugin (LazyVim / lazy.nvim)

Drop the following into `~/.config/nvim/lua/plugins/flow.lua`:

```lua
return {
  "angleto/flow.nvim",
  cmd = "Flow",
  keys = {
    { "<leader>fo", "<cmd>Flow today<cr>",     desc = "Flow: today" },
    { "<leader>ft", "<cmd>Flow tasks<cr>",     desc = "Flow: tasks" },
    { "<leader>fN", "<cmd>Flow notes<cr>",     desc = "Flow: notes" },
    { "<leader>fn", "<cmd>Flow note-new<cr>",  desc = "Flow: new note" },
    { "<leader>fT", "<cmd>Flow task-new<cr>",  desc = "Flow: new task" },
  },
  dependencies = { "nvim-telescope/telescope.nvim" },  -- optional but recommended
  opts = {
    -- bin = "flow",            -- override the CLI binary
    -- profile = "default",     -- CLI profile to use
    -- picker = "auto",         -- "auto" | "telescope" | "select"
    -- open_cmd = "tabnew",     -- how to open result buffers
  },
}
```

### 3. Verify

```vim
:checkhealth flow
```

## Commands

| Command | What it does |
| --- | --- |
| `:Flow today` | Open a buffer with today's running timer, appointments and deadlines. |
| `:Flow week` | Next 7 days grouped by day. |
| `:Flow tasks` | Picker over open tasks. `<cr>` opens detail; `<C-d>` marks done; `<C-s>` starts a timer; `<C-o>` opens in the SPA. The picker re-runs after `<C-d>` so the list stays accurate. |
| `:Flow notes` | Picker over recent notes; `<cr>` opens the body in a buffer. |
| `:Flow note-new` | Open a Markdown buffer; on `:w` the body is sent to `flow note add`. |
| `:Flow task-new` | Prompt for title, then open a description buffer; `:w` calls `flow task add`. |
| `:Flow search <q>` | Prompt for a query, run hybrid search, pick a hit to open in the SPA. |
| `:Flow open <ref>` | Open the SPA on a task/note id or shortcut (`today`, `notes`, `invoices`, ...). |
| `:Flow status` | Notification with profile + server build info. |

### Lualine integration

```lua
require("lualine").setup({
  sections = {
    lualine_x = {
      function() return require("flow").statusline().timer() end,
      "encoding", "filetype",
    },
  },
})
```

The function returns `⏱ <task-prefix> since HH:MM` while a timer is
running, empty otherwise. A background poll refreshes it every 30s; the
function itself is cheap and never blocks the redraw.

`:Flow` with no arg is equivalent to `:Flow today`.

## How it talks to Flow

Every action shells out to `flow <subcommand> --json` via `vim.system()`
(async, no UI freezes). The plugin does no HTTP itself, so anything
you can do with the CLI is automatically available here, and login /
profile management stays a single source of truth.

## Troubleshooting

`:checkhealth flow` covers the common cases:

- **`flow` not on PATH** — Homebrew shim missing or `pipx` venv not
  exported. Run the install one-liner.
- **`flow auth status` fails** — token expired, server unreachable, or
  wrong base URL. Re-run `flow auth login`.

## License

AGPL-3.0-or-later, same as the rest of the Flow monorepo.
