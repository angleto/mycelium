# mycelium-nvim

Neovim front-end for the Mycelium CLI. Browse tasks, notes, today's
agenda, and capture new notes/tasks without leaving your editor.

This plugin **shells out** to `mycelium` (the CLI from this monorepo); it
does not embed an HTTP client. Install `mycelium` first.

## Install

### 1. Install the CLI

```sh
brew install angleto/mycelium/mycelium-cli
```

Log in once:

```sh
mycelium auth login --base-url https://mycelium.xeno.garden
```

### 2. Install the plugin (LazyVim / lazy.nvim)

Drop the following into `~/.config/nvim/lua/plugins/mycelium.lua`:

```lua
return {
  "angleto/mycelium-nvim",
  version = "*",           -- resolves to the latest v* tag (matches Mycelium's release)
  event = "VeryLazy",      -- load after the UI is ready. We avoid `cmd = "Mycelium"`
                           -- and key-only lazy triggers because both keep the
                           -- plugin off `runtimepath` until first invocation,
                           -- which breaks `:checkhealth mycelium` and any other
                           -- rtp-walking lookup. `plugin/mycelium.lua` is minimal
                           -- (registers the `:Mycelium` user command, defers the
                           -- rest via `require` on first call), so loading at
                           -- VeryLazy has no startup penalty.
  keys = {
    { "<leader>fo", "<cmd>Mycelium today<cr>",     desc = "Mycelium: today" },
    { "<leader>ft", "<cmd>Mycelium tasks<cr>",     desc = "Mycelium: tasks" },
    { "<leader>fN", "<cmd>Mycelium notes<cr>",     desc = "Mycelium: notes" },
    { "<leader>fn", "<cmd>Mycelium note-new<cr>",  desc = "Mycelium: new note" },
    { "<leader>fT", "<cmd>Mycelium task-new<cr>",  desc = "Mycelium: new task" },
  },
  dependencies = { "nvim-telescope/telescope.nvim" },  -- optional but recommended
  opts = {
    -- bin = "mycelium",            -- override the CLI binary
    -- profile = "default",     -- CLI profile to use
    -- picker = "auto",         -- "auto" | "telescope" | "select"
    -- open_cmd = "tabnew",     -- how to open result buffers
  },
}
```

### 3. Verify

```vim
:checkhealth mycelium
```

## Commands

| Command | What it does |
| --- | --- |
| `:Mycelium today` | Open a buffer with today's running timer, appointments and deadlines. |
| `:Mycelium week` | Next 7 days grouped by day. |
| `:Mycelium tasks` | Picker over open tasks. `<cr>` opens detail; `<C-d>` marks done; `<C-s>` starts a timer; `<C-o>` opens in the SPA. The picker re-runs after `<C-d>` so the list stays accurate. |
| `:Mycelium notes` | Picker over recent notes; `<cr>` opens the body in a buffer. |
| `:Mycelium note-new` | Open a Markdown buffer; on `:w` the body is sent to `mycelium note add`. |
| `:Mycelium task-new` | Prompt for title, then open a description buffer; `:w` calls `mycelium task add`. |

### Editing tasks / notes inline

Buffers opened by `:Mycelium tasks`/`:Mycelium notes` (`mycelium://task/<id>` and
`mycelium://note/<id>`) are **writable**. On `:w` the plugin parses the
first `# heading` as the title and everything after it as the
description/body, and shells out to `mycelium task edit <id>` /
`mycelium note edit <id>` to PATCH. The header metadata line (`_state:
... v3_`) is stripped before parsing, so you can leave it visible.

Optimistic concurrency: the CLI fetches the current version right
before the PATCH, so a stale buffer triggers a clean 409 instead of
silently overwriting.
| `:Mycelium search <q>` | Prompt for a query, run hybrid search, pick a hit to open in the SPA. |
| `:Mycelium open <ref>` | Open the SPA on a task/note id or shortcut (`today`, `notes`, `invoices`, ...). |
| `:Mycelium status` | Notification with profile + server build info. |

### Lualine integration

```lua
require("lualine").setup({
  sections = {
    lualine_x = {
      function() return require("mycelium").statusline().timer() end,
      "encoding", "filetype",
    },
  },
})
```

The function returns `⏱ <task-prefix> since HH:MM` while a timer is
running, empty otherwise. A background poll refreshes it every 30s; the
function itself is cheap and never blocks the redraw.

`:Mycelium` with no arg is equivalent to `:Mycelium today`.

## How it talks to Mycelium

Every action shells out to `mycelium <subcommand> --json` via `vim.system()`
(async, no UI freezes). The plugin does no HTTP itself, so anything
you can do with the CLI is automatically available here, and login /
profile management stays a single source of truth.

## Troubleshooting

`:checkhealth mycelium` covers the common cases:

- **`mycelium` not on PATH** — Homebrew shim missing. Run the install
  one-liner above (`brew install angleto/mycelium/mycelium-cli`).
- **`mycelium auth status` fails** — token expired, server unreachable, or
  wrong base URL. Re-run `mycelium auth login`.

## License

AGPL-3.0-or-later, same as the rest of the Mycelium monorepo.
