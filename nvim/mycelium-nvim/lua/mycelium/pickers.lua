-- Telescope-first picker. Falls back to vim.ui.select when telescope is
-- not installed, so the plugin works out of the box for users on a
-- minimal LazyVim profile.

local M = {}

local function has_telescope()
  return pcall(require, "telescope.pickers")
end

local function backend()
  local cfg = require("mycelium").config.picker
  if cfg == "select" then return "select" end
  if cfg == "telescope" then return "telescope" end
  return has_telescope() and "telescope" or "select"
end

local function fmt_task(t)
  local id = require("mycelium.ui").short_id(t.id)
  local title = t.title or "<untitled>"
  local state = t.state or "?"
  local due = t.due_date or ""
  return string.format("%-8s  %-12s  %-10s  %s", id, state, due, title)
end

local function fmt_note(n)
  local id = require("mycelium.ui").short_id(n.id)
  local kind = n.kind or "?"
  local title = n.title or (n.transcript or ""):sub(1, 80)
  return string.format("%-8s  %-7s  %s", id, kind, title)
end

-- Generic select fallback using vim.ui.select. ``items`` is a list of
-- tables; ``display(item)`` returns the line; ``on_pick(item)`` is
-- invoked with the chosen entry.
local function ui_select(items, display, on_pick, prompt)
  vim.ui.select(items, {
    prompt = prompt or "Pick",
    format_item = display,
  }, function(choice)
    if choice ~= nil then
      on_pick(choice)
    end
  end)
end

local function telescope_pick(items, display, on_pick, prompt, actions_map)
  local pickers = require("telescope.pickers")
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local action_state = require("telescope.actions.state")

  pickers
    .new({}, {
      prompt_title = prompt or "Mycelium",
      finder = finders.new_table({
        results = items,
        entry_maker = function(item)
          local line = display(item)
          return {
            value = item,
            display = line,
            ordinal = line,
          }
        end,
      }),
      sorter = conf.generic_sorter({}),
      attach_mappings = function(bufnr, map)
        actions.select_default:replace(function()
          local entry = action_state.get_selected_entry()
          actions.close(bufnr)
          if entry then on_pick(entry.value) end
        end)
        if actions_map then
          for lhs, fn in pairs(actions_map) do
            map({ "i", "n" }, lhs, function()
              local entry = action_state.get_selected_entry()
              if entry then fn(entry.value, bufnr) end
            end)
          end
        end
        return true
      end,
    })
    :find()
end

local function pick(items, display, on_pick, prompt, actions_map)
  if backend() == "telescope" then
    telescope_pick(items, display, on_pick, prompt, actions_map)
  else
    ui_select(items, display, on_pick, prompt)
  end
end

function M.pick_task(tasks, on_pick, actions_map)
  pick(tasks, fmt_task, on_pick, "Mycelium tasks", actions_map)
end

function M.pick_note(notes, on_pick, actions_map)
  pick(notes, fmt_note, on_pick, "Mycelium notes", actions_map)
end

return M
