-- :Flow <subcommand> dispatcher and the actual sub-command handlers.

local M = {}

local subcommands = {
  "today",
  "week",
  "tasks",
  "notes",
  "note-new",
  "task-new",
  "search",
  "open",
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
    if not ok then notify_err(data) return end
    local lines = { "# Today (" .. (data.date or "") .. ")", "" }
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
    local appts = data.appointments or {}
    if #appts > 0 then
      table.insert(lines, "## Appointments")
      for _, t in ipairs(appts) do
        local start = (t.start_at or ""):sub(12, 16)
        table.insert(lines, ("- %s  **%s** _(%sm, pri %s)_"):format(
          start, t.title or "?", tostring(t.duration_minutes or "?"), tostring(t.priority or "?")
        ))
      end
      table.insert(lines, "")
    end
    local deadlines = data.deadlines or {}
    table.insert(lines, "## Due / scheduled")
    if #deadlines == 0 then
      table.insert(lines, "_nothing else_")
    else
      for _, t in ipairs(deadlines) do
        table.insert(lines, ("- [%s] **%s** _(%s, due %s, pri %s)_"):format(
          ui.short_id(t.id), t.title or "?", t.state or "?",
          t.due_date or "-", tostring(t.priority or "?")
        ))
      end
    end
    ui.open_text_buffer("today", lines)
  end)
end

local function week()
  local cli = require("flow.cli")
  local ui = require("flow.ui")
  cli.json({ "week" }, function(ok, data)
    if not ok then notify_err(data) return end
    local lines = { "# Week (" .. (data["from"] or "") .. " → " .. (data["to"] or "") .. ")", "" }
    local by_day = data.by_day or {}
    -- Stable day order: sorted ISO date keys.
    local keys = {}
    for k, _ in pairs(by_day) do table.insert(keys, k) end
    table.sort(keys)
    for _, day in ipairs(keys) do
      table.insert(lines, "## " .. day)
      for _, t in ipairs(by_day[day]) do
        local when = ""
        if t.start_at and t.duration_minutes then
          when = " " .. (t.start_at or ""):sub(12, 16)
        end
        table.insert(lines, ("- [%s]%s **%s** _(%s, pri %s)_"):format(
          ui.short_id(t.id), when, t.title or "?", t.state or "?", tostring(t.priority or "?")
        ))
      end
      table.insert(lines, "")
    end
    ui.open_text_buffer("week", lines)
  end)
end

-- Per-row picker actions. The picker invokes ``fn(value)`` with the
-- selected task; after the action we re-run the view so the picker
-- reflects the new state (e.g. a marked-done task disappears).
local function task_actions(reopen)
  local cli = require("flow.cli")
  return {
    ["<C-d>"] = function(task)
      cli.run({ "task", "done", task.id }, function(ok)
        if ok then
          notify_ok("done " .. (task.title or task.id))
          if reopen then reopen() end
        end
      end)
    end,
    ["<C-s>"] = function(task)
      cli.run({ "timer", "start", task.id }, function(ok)
        if ok then
          notify_ok("timer started on " .. (task.title or task.id))
          pcall(function() require("flow.statusline").refresh() end)
        end
      end)
    end,
    ["<C-o>"] = function(task)
      cli.run({ "open", task.id }, function() end)
    end,
  }
end

local function tasks_view()
  local cli = require("flow.cli")
  cli.json({ "task", "list" }, function(ok, data)
    if not ok then notify_err(data) return end
    require("flow.pickers").pick_task(data, function(task)
      cli.json({ "task", "show", task.id }, function(ok2, full)
        if not ok2 then notify_err(full) return end
        local t = (full.task or full)
        local header = ("_state: %s  due: %s  pri: %s  v%s_"):format(
          t.state or "?", t.due_date or "-",
          tostring(t.priority or "?"), tostring(t.version or "?")
        )
        require("flow.ui").open_editable_resource(
          "task/" .. task.id, header, t.title, t.description or "",
          function(new_title, new_body, on_done)
            -- The CLI's ``flow task edit`` reads the description from
            -- stdin when given ``--description -``; we pass the title
            -- as a flag.
            local args = { "task", "edit", task.id, "--title", new_title, "--description", "-" }
            cli.run_stdin(args, new_body, function(ok3)
              if ok3 then
                notify_ok("task " .. (task.id):sub(1, 8) .. " saved")
                on_done()
              end
            end)
          end
        )
      end)
    end, task_actions(tasks_view))
  end)
end

local function notes_view()
  local cli = require("flow.cli")
  cli.json({ "note", "list" }, function(ok, data)
    if not ok then notify_err(data) return end
    require("flow.pickers").pick_note(data, function(note)
      cli.json({ "note", "show", note.id }, function(ok2, full)
        if not ok2 then notify_err(full) return end
        local header = ("_kind: %s  task: %s  v%s_"):format(
          full.kind or "?", full.task_id or "-", tostring(full.version or "?")
        )
        require("flow.ui").open_editable_resource(
          "note/" .. note.id, header, full.title or "", full.transcript or "",
          function(new_title, new_body, on_done)
            local args = { "note", "edit", note.id, "--title", new_title, "--text", "-" }
            cli.run_stdin(args, new_body, function(ok3)
              if ok3 then
                notify_ok("note " .. (note.id):sub(1, 8) .. " saved")
                on_done()
              end
            end)
          end
        )
      end)
    end)
  end)
end

local function note_new()
  require("flow.ui").open_editor_buffer("note/new", {
    "<!-- write your note below; :w to save, :q! to discard -->",
    "",
  }, function(text, on_done)
    local body = text:gsub("^<!%-%-.-%-%->\n*", "")
    require("flow.cli").run_stdin(
      { "note", "add", "--no-editor", "--text", "-" }, body,
      function(ok)
        if ok then
          notify_ok("note saved")
          on_done()
        end
      end
    )
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
      require("flow.cli").run(
        { "task", "add", title, "--description", body, "--no-editor" },
        function(ok)
          if ok then
            notify_ok("task saved")
            on_done()
          end
        end
      )
    end)
  end)
end

local function search()
  vim.ui.input({ prompt = "Search: " }, function(query)
    if not query or query == "" then return end
    require("flow.cli").json({ "search", query }, function(ok, hits)
      if not ok then notify_err(hits) return end
      local items = {}
      for _, h in ipairs(hits) do
        local blob = h.blob or {}
        local text = (blob.text or blob.summary or ""):gsub("\n", " ")
        table.insert(items, {
          id = blob.id,
          rrf = h.rrf,
          snippet = text:sub(1, 120),
        })
      end
      vim.ui.select(items, {
        prompt = "Hits (" .. #items .. ")",
        format_item = function(it)
          return ("%.3f  %s  %s"):format(it.rrf or 0, tostring(it.id):sub(1, 8), it.snippet or "")
        end,
      }, function(picked)
        if picked then
          require("flow.cli").run({ "open", tostring(picked.id) }, function() end)
        end
      end)
    end)
  end)
end

local function open_url(args)
  local target = args[2] or "today"
  require("flow.cli").run({ "open", target }, function() end)
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
  week = week,
  tasks = tasks_view,
  notes = notes_view,
  ["note-new"] = note_new,
  ["task-new"] = task_new,
  search = search,
  open = open_url,
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
  -- ``open`` takes a positional ref; pass the args through so the
  -- caller can write ``:Flow open <task-id>``.
  if sub == "open" then
    handler(args)
  else
    handler()
  end
end

return M
