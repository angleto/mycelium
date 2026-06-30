-- Autoload guard. The real wiring lives in lua/mycelium/cmds.lua and is
-- invoked lazily on first command/keymap, so this file stays cheap to
-- source even when the plugin is loaded eagerly.
if vim.g.loaded_mycelium_nvim == 1 then
  return
end
vim.g.loaded_mycelium_nvim = 1

vim.api.nvim_create_user_command("Mycelium", function(opts)
  require("mycelium.cmds").dispatch(opts.fargs)
end, {
  nargs = "*",
  complete = function(arglead, _, _)
    return require("mycelium.cmds").complete(arglead)
  end,
  desc = "Mycelium CLI bridge: :Mycelium today | tasks | notes | note-new | task-new",
})
