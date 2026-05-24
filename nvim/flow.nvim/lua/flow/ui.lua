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

return M
