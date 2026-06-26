-- Public surface: ``require("mycelium").setup(opts)`` and a small helper
-- API that other plugins can consume. The split is deliberate: the
-- moduli with vim.system / telescope live behind require-on-demand so
-- the setup itself stays a few microseconds.

local M = {}

---@class mycelium.Config
---@field bin string Path or name of the mycelium CLI binary (default: "mycelium")
---@field profile string|nil --profile to pass through to the CLI
---@field default_limit integer Default list limit
---@field picker "auto"|"telescope"|"select" Picker backend
---@field open_cmd string Command used to open results buffers (e.g. "tabnew")
local defaults = {
  bin = "mycelium",
  profile = nil,
  default_limit = 100,
  picker = "auto",
  open_cmd = "tabnew",
}

M.config = vim.deepcopy(defaults)

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", vim.deepcopy(defaults), opts or {})
end

-- Re-exports for plugin authors / personal config
function M.cli()
  return require("mycelium.cli")
end

function M.pickers()
  return require("mycelium.pickers")
end

function M.statusline()
  return require("mycelium.statusline")
end

return M
