-- Async shell-out around the ``flow`` CLI. Every call is JSON-mode by
-- default so the plugin never has to parse Rich's coloured tables.

local M = {}

local function config()
  return require("flow").config
end

local function ensure_bin(bin)
  if vim.fn.executable(bin) ~= 1 then
    vim.notify(
      ("flow-cli (`%s`) not found on PATH.\nInstall with: brew install angleto/flow/flow-cli"):format(bin),
      vim.log.levels.ERROR,
      { title = "flow.nvim" }
    )
    return false
  end
  return true
end

-- Build ``{ bin, [top-level flags...], <args>... }``.
-- ``--json`` is a top-level flag on the CLI (registered on
-- ``flow_cli.main.root``), not a sub-command flag, so it MUST appear
-- before the sub-command name. Same shape regardless of how deep the
-- sub-command nesting goes (``flow task list`` etc.).
--
-- ``--profile`` deliberately NOT injected here: it is registered only
-- on a handful of auth/workspace commands (``auth login`` etc.), not
-- as a top-level flag, and the profile-selection for every other
-- command flows through ``current_profile`` in ~/.config/flow/config.toml.
-- If the plugin ever needs to drive a per-call profile, the calling
-- site has to put ``--profile`` into ``args`` at the position the
-- specific sub-command expects.
local function build_argv(args, opts)
  opts = opts or {}
  local cfg = config()
  local argv = { cfg.bin }
  if opts.json then
    table.insert(argv, "--json")
  end
  for _, a in ipairs(args) do
    table.insert(argv, a)
  end
  return argv
end

-- Run ``flow --json <args>``, decode stdout as JSON, and invoke
-- ``on_done(ok, value_or_err)`` on the main loop.
function M.json(args, on_done)
  if not ensure_bin(config().bin) then
    on_done(false, "flow-cli not on PATH")
    return
  end
  local argv = build_argv(args, { json = true })
  vim.system(argv, { text = true }, function(res)
    vim.schedule(function()
      if res.code ~= 0 then
        local err = res.stderr ~= "" and res.stderr or res.stdout
        on_done(false, vim.trim(err or "flow exited " .. res.code))
        return
      end
      local ok, decoded = pcall(vim.json.decode, res.stdout, { luanil = { object = true, array = true } })
      if not ok then
        on_done(false, "could not decode flow JSON output: " .. tostring(decoded))
        return
      end
      on_done(true, decoded)
    end)
  end)
end

-- Run ``flow <args>`` for side effect, no JSON parsing.
function M.run(args, on_done)
  if not ensure_bin(config().bin) then
    if on_done then on_done(false, "flow-cli not on PATH") end
    return
  end
  vim.system(build_argv(args), { text = true }, function(res)
    vim.schedule(function()
      local ok = res.code == 0
      if not ok then
        vim.notify(res.stderr or "flow failed", vim.log.levels.ERROR, { title = "flow.nvim" })
      end
      if on_done then on_done(ok, res.stdout) end
    end)
  end)
end

-- Send ``stdin`` to ``flow <args>`` (used by the "edit then save" flow
-- so the user's markdown buffer body becomes the note text).
function M.run_stdin(args, stdin, on_done)
  if not ensure_bin(config().bin) then
    if on_done then on_done(false, "flow-cli not on PATH") end
    return
  end
  vim.system(build_argv(args), { text = true, stdin = stdin }, function(res)
    vim.schedule(function()
      local ok = res.code == 0
      if not ok then
        vim.notify(res.stderr or "flow failed", vim.log.levels.ERROR, { title = "flow.nvim" })
      end
      if on_done then on_done(ok, res.stdout) end
    end)
  end)
end

return M
