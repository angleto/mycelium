-- Autoload guard. The real wiring lives in lua/flow/cmds.lua and is
-- invoked lazily on first command/keymap, so this file stays cheap to
-- source even when the plugin is loaded eagerly.
if vim.g.loaded_flow_nvim == 1 then
  return
end
vim.g.loaded_flow_nvim = 1

vim.api.nvim_create_user_command("Flow", function(opts)
  require("flow.cmds").dispatch(opts.fargs)
end, {
  nargs = "*",
  complete = function(arglead, _, _)
    return require("flow.cmds").complete(arglead)
  end,
  desc = "Flow CLI bridge: :Flow today | tasks | notes | note-new | task-new",
})
