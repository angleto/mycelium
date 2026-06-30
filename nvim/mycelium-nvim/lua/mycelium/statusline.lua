-- Statusline helper. Returns a short live string for lualine/heirline
-- without blocking redraws: a background timer refreshes a cache every
-- N seconds via ``vim.system()`` (async), and ``M.timer()`` reads it.

local M = {}

local cache = {
  text = "",
  ts = 0,
}

local TTL_SECONDS = 30

local function refresh()
  local cfg = require("mycelium").config
  if vim.fn.executable(cfg.bin) ~= 1 then
    cache.text = ""
    cache.ts = os.time()
    return
  end
  -- ``--json`` is a top-level flag (see cli.lua), so it must come
  -- before the sub-command. ``--profile`` is not top-level and the
  -- subcommands the plugin invokes do not accept it; profile
  -- selection flows through ``current_profile`` in config.toml.
  local argv = { cfg.bin, "--json", "timer", "status" }
  vim.system(argv, { text = true }, function(res)
    vim.schedule(function()
      cache.ts = os.time()
      if res.code ~= 0 then
        cache.text = ""
        return
      end
      local ok, data = pcall(vim.json.decode, res.stdout, { luanil = { object = true, array = true } })
      if not ok or type(data) ~= "table" then
        cache.text = ""
        return
      end
      local running = data.running or {}
      if #running == 0 then
        cache.text = ""
        return
      end
      local first = running[1]
      local started = first.started_at
      local elapsed = ""
      if type(started) == "string" then
        -- Best-effort parse: take HH:MM:SS off the ISO string.
        local t = started:match("T(%d+):(%d+):") or ""
        elapsed = t ~= "" and (" since " .. t) or ""
      end
      local short_id = tostring(first.task_id or ""):sub(1, 8)
      cache.text = "⏱ " .. short_id .. elapsed
    end)
  end)
end

-- Public: cheap synchronous read. Triggers a background refresh when
-- the cache is older than TTL_SECONDS so the statusline reflects the
-- current timer without spawning a subprocess on every redraw.
function M.timer()
  local now = os.time()
  if now - cache.ts > TTL_SECONDS then
    cache.ts = now  -- prevent storm: mark "in-flight" up front
    refresh()
  end
  return cache.text
end

-- Public: force a refresh now (e.g. after :Mycelium tasks <C-s>).
function M.refresh()
  cache.ts = 0
  M.timer()
end

return M
