-- Buffer / window helpers. Kept stateless so the plugin can be reloaded
-- without leaking buffers.

local M = {}

local function set_lines(buf, lines)
  vim.api.nvim_set_option_value("modifiable", true, { buf = buf })
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
end

function M.open_text_buffer(title, lines, opts)
  opts = opts or {}
  local open_cmd = opts.open_cmd or require("flow").config.open_cmd
  vim.cmd(open_cmd)
  local buf = vim.api.nvim_get_current_buf()
  vim.api.nvim_buf_set_name(buf, "flow://" .. title)
  vim.api.nvim_set_option_value("buftype", "nofile", { buf = buf })
  vim.api.nvim_set_option_value("bufhidden", "wipe", { buf = buf })
  vim.api.nvim_set_option_value("swapfile", false, { buf = buf })
  vim.api.nvim_set_option_value("filetype", opts.filetype or "markdown", { buf = buf })
  set_lines(buf, lines)
  if opts.readonly ~= false then
    vim.api.nvim_set_option_value("modifiable", false, { buf = buf })
    vim.api.nvim_set_option_value("readonly", true, { buf = buf })
  end
  vim.api.nvim_win_set_cursor(0, { 1, 0 })
  return buf
end

-- A Markdown scratch buffer the user can write (``:w``). When written
-- it invokes ``on_save(text)``; the buffer is wiped afterwards.
function M.open_editor_buffer(title, initial_lines, on_save)
  vim.cmd(require("flow").config.open_cmd)
  local buf = vim.api.nvim_get_current_buf()
  vim.api.nvim_buf_set_name(buf, "flow://" .. title)
  vim.api.nvim_set_option_value("buftype", "acwrite", { buf = buf })
  vim.api.nvim_set_option_value("bufhidden", "wipe", { buf = buf })
  vim.api.nvim_set_option_value("swapfile", false, { buf = buf })
  vim.api.nvim_set_option_value("filetype", "markdown", { buf = buf })
  set_lines(buf, initial_lines or { "" })
  vim.api.nvim_create_autocmd("BufWriteCmd", {
    buffer = buf,
    callback = function()
      local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
      local text = vim.trim(table.concat(lines, "\n"))
      if text == "" then
        vim.notify("flow.nvim: empty buffer; not saving.", vim.log.levels.WARN)
        return
      end
      vim.api.nvim_set_option_value("modified", false, { buf = buf })
      on_save(text, function()
        if vim.api.nvim_buf_is_valid(buf) then
          vim.api.nvim_buf_delete(buf, { force = true })
        end
      end)
    end,
  })
  return buf
end

function M.short_id(id)
  if not id then return "" end
  local s = tostring(id)
  return s:sub(1, s:find("-") and (s:find("-") - 1) or 8)
end

-- A writable resource buffer (e.g. ``flow://task/<id>``) whose ``:w``
-- diffs the first Markdown ``# heading`` as the title and everything
-- after it as the body, then invokes ``on_save(title, body, on_done)``.
-- on_save is responsible for calling the right ``flow ... edit`` and
-- calling ``on_done()`` on success; we just clear the modified flag.
function M.open_editable_resource(name, header_line, title, body, on_save)
  vim.cmd(require("flow").config.open_cmd)
  local buf = vim.api.nvim_get_current_buf()
  vim.api.nvim_buf_set_name(buf, "flow://" .. name)
  vim.api.nvim_set_option_value("buftype", "acwrite", { buf = buf })
  vim.api.nvim_set_option_value("bufhidden", "wipe", { buf = buf })
  vim.api.nvim_set_option_value("swapfile", false, { buf = buf })
  vim.api.nvim_set_option_value("filetype", "markdown", { buf = buf })

  local lines = {}
  if header_line and header_line ~= "" then
    table.insert(lines, header_line)
    table.insert(lines, "")
  end
  table.insert(lines, "# " .. (title or ""))
  table.insert(lines, "")
  if body and body ~= "" then
    for _, l in ipairs(vim.split(body, "\n")) do
      table.insert(lines, l)
    end
  end
  vim.api.nvim_set_option_value("modifiable", true, { buf = buf })
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.api.nvim_set_option_value("modified", false, { buf = buf })

  vim.api.nvim_create_autocmd("BufWriteCmd", {
    buffer = buf,
    callback = function()
      local all = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
      -- Strip any leading metadata line (rendered as ``_state: ..._``).
      while #all > 0 and (all[1]:match("^_") or all[1] == "") do
        table.remove(all, 1)
      end
      local title_line = all[1] or ""
      local new_title = title_line:match("^#%s*(.*)$") or ""
      table.remove(all, 1)
      -- Skip the blank line after the title if present.
      if all[1] == "" then table.remove(all, 1) end
      local new_body = table.concat(all, "\n")
      on_save(new_title, new_body, function()
        if vim.api.nvim_buf_is_valid(buf) then
          vim.api.nvim_set_option_value("modified", false, { buf = buf })
        end
      end)
    end,
  })
  return buf
end

return M
