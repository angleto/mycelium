-- Public surface: ``require("flow").setup(opts)`` and a small helper
-- API that other plugins can consume. The split is deliberate: the
-- moduli with vim.system / telescope live behind require-on-demand so
-- the setup itself stays a few microseconds.

local M = {}

---@class flow.Config
---@field bin string Path or name of the flow CLI binary (default: "flow")
---@field profile string|nil --profile to pass through to the CLI
---@field default_limit integer Default list limit
---@field picker "auto"|"telescope"|"select" Picker backend
---@field open_cmd string Command used to open results buffers (e.g. "tabnew")
local defaults = {
  bin = "flow",
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
  return require("flow.cli")
end

function M.pickers()
  return require("flow.pickers")
end

return M
