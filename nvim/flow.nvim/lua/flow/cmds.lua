-- :Flow <subcommand> dispatcher and the actual sub-command handlers.

local M = {}

local subcommands = {
  "today",
  "tasks",
  "notes",
  "note-new",
  "task-new",
  "status",
}

function M.complete(arglead)
  local out = {}
  for _, c in ipairs(subcommands) do
    if c:find("^" .. vim.pesc(arglead)) then
      table.insert(out, c)
    end
  end
  return out
end

local function notify_err(msg)
  vim.notify(msg, vim.log.levels.ERROR, { title = "flow.nvim" })
end

local function notify_ok(msg)
  vim.notify(msg, vim.log.levels.INFO, { title = "flow.nvim" })
end

local function today()
  local cli = require("flow.cli")
  local ui = require("flow.ui")
  cli.json({ "today" }, function(ok, data)
    if not ok then
      notify_err(data)
      return
    end
    local lines = { "# Today (" .. (data.today or "") .. ")", "" }
    local running = data.running or {}
    if #running > 0 then
      table.insert(lines, "## Running")
      for _, r in ipairs(running) do
        table.insert(lines, ("- %s  started %s  %s"):format(
          ui.short_id(r.task_id), r.started_at or "?", r.memo or ""
        ))
      end
      table.insert(lines, "")
    end
    table.insert(lines, "## Tasks")
    local tasks = data.tasks or {}
    if #tasks == 0 then
      table.insert(lines, "_nothing scheduled_")
    else
      for _, t in ipairs(tasks) do
        local due = t.due_date or "-"
        table.insert(lines, ("- [%s] **%s** _(%s, due %s, pri %s)_"):format(
          ui.short_id(t.id), t.title or "?", t.state or "?", due, tostring(t.priority or "?")
        ))
      end
    end
    ui.open_text_buffer("today", lines)
  end)
end

-- Per-row actions: the picker invokes ``fn(value, bufnr)`` with the
-- currently selected task, so the closure must read it from the
-- argument, not from outer scope.
local function task_actions()
  local cli = require("flow.cli")
  return {
    ["<C-d>"] = function(task)
      cli.run({ "task", "done", task.id }, function(ok)
        if ok then notify_ok("done " .. (task.title or task.id)) end
      end)
    end,
    ["<C-s>"] = function(task)
      cli.run({ "timer", "start", task.id }, function(ok)
        if ok then notify_ok("timer started on " .. (task.title or task.id)) end
      end)
    end,
  }
end

local function tasks_view()
  local cli = require("flow.cli")
  cli.json({ "task", "list" }, function(ok, data)
    if not ok then
      notify_err(data)
      return
    end
    require("flow.pickers").pick_task(data, function(task)
      cli.json({ "task", "show", task.id }, function(ok2, full)
        if not ok2 then notify_err(full) return end
        local lines = {
          "# " .. (full.title or "<untitled>"),
          ("_state: %s  due: %s  pri: %s_"):format(
            full.state or "?", full.due_date or "-", tostring(full.priority or "?")
          ),
          "",
          full.description or "",
        }
        require("flow.ui").open_text_buffer("task/" .. task.id, vim.split(table.concat(lines, "\n"), "\n"))
      end)
    end, task_actions())
  end)
end

local function notes_view()
  local cli = require("flow.cli")
  cli.json({ "note", "list" }, function(ok, data)
    if not ok then notify_err(data) return end
    require("flow.pickers").pick_note(data, function(note)
      cli.json({ "note", "show", note.id }, function(ok2, full)
        if not ok2 then notify_err(full) return end
        local body = full.transcript or ""
        local lines = {
          "# " .. (full.title or "<untitled>"),
          ("_kind: %s  task: %s_"):format(full.kind or "?", full.task_id or "-"),
          "",
        }
        for _, l in ipairs(vim.split(body, "\n")) do
          table.insert(lines, l)
        end
        require("flow.ui").open_text_buffer("note/" .. note.id, lines)
      end)
    end)
  end)
end

local function note_new()
  require("flow.ui").open_editor_buffer("note/new", {
    "<!-- write your note below; :w to save, :q! to discard -->",
    "",
  }, function(text, on_done)
    local cli = require("flow.cli")
    -- Strip the leading HTML comment instruction line(s).
    local body = text:gsub("^<!%-%-.-%-%->\n*", "")
    cli.run_stdin({ "note", "add", "--no-editor", "--text", "-" }, body, function(ok)
      if ok then
        notify_ok("note saved")
        on_done()
      end
    end)
  end)
end

local function task_new()
  vim.ui.input({ prompt = "Task title: " }, function(title)
    if not title or title == "" then return end
    require("flow.ui").open_editor_buffer("task/new", {
      "<!-- task description; :w to save, :q! to discard -->",
      "",
    }, function(text, on_done)
      local body = text:gsub("^<!%-%-.-%-%->\n*", "")
      require("flow.cli").run({ "task", "add", title, "--description", body, "--no-editor" }, function(ok)
        if ok then
          notify_ok("task saved")
          on_done()
        end
      end)
    end)
  end)
end

local function status_view()
  require("flow.cli").json({ "auth", "status" }, function(ok, data)
    if not ok then notify_err(data) return end
    local server = data.server or {}
    notify_ok(("profile %s @ %s  server: %s"):format(
      data.profile or "?", data.base_url or "?", server.version or "?"
    ))
  end)
end

local handlers = {
  today = today,
  tasks = tasks_view,
  notes = notes_view,
  ["note-new"] = note_new,
  ["task-new"] = task_new,
  status = status_view,
}

function M.dispatch(args)
  local sub = args[1]
  if not sub or sub == "" then
    sub = "today"
  end
  local handler = handlers[sub]
  if not handler then
    notify_err("unknown subcommand '" .. tostring(sub) .. "'. Try: " .. table.concat(subcommands, ", "))
    return
  end
  handler()
end

return M
