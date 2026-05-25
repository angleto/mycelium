-- ``:checkhealth flow`` — verify the CLI is installed, on PATH, and
-- has a working credential.

local M = {}

function M.check()
  local health = vim.health or require("health")
  local start = health.start or health.report_start
  local ok = health.ok or health.report_ok
  local warn = health.warn or health.report_warn
  local err = health.error or health.report_error

  local bin = require("flow").config.bin
  start("flow-nvim")
  if vim.fn.executable(bin) ~= 1 then
    err(("`%s` not found on PATH"):format(bin), {
      "Install: brew install angleto/flow/flow-cli",
      "Or:      pipx install flow-cli",
    })
    return
  end
  ok(("`%s` found at %s"):format(bin, vim.fn.exepath(bin)))

  local version = vim.fn.system({ bin, "--version" })
  if vim.v.shell_error == 0 then
    ok(vim.trim(version))
  else
    warn("could not read `--version` output")
  end

  -- ``--json`` is a top-level flag on the CLI (set in the root callback),
  -- not a sub-command flag, so it must come BEFORE ``auth``.
  local res = vim.system({ bin, "--json", "auth", "status" }, { text = true }):wait()
  if res.code == 0 then
    ok("credential present and reachable")
  else
    warn("flow auth status failed", {
      "Run `flow auth login --base-url <https://...>` to set up.",
      "Last error: " .. vim.trim(res.stderr or ""),
    })
  end
end

return M
