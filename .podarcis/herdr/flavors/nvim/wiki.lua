-- Injected via `nvim -c "luafile …/wiki.lua"`. Does not replace XDG_CONFIG_HOME.

local group = vim.api.nvim_create_augroup("PodarcisWikiContext", { clear = true })

local function skip(buf, path)
	if path == nil or path == "" then
		return true
	end
	if vim.startswith(path, "term://") or vim.startswith(path, "oil://") then
		return true
	end
	local ok, bt = pcall(function()
		return vim.bo[buf].buftype
	end)
	if ok and bt ~= nil and bt ~= "" then
		return true
	end
	return false
end

local function write_context(path)
	local cmd = { "podarcis", "wiki", "context", "--", path }
	local root = vim.env.PROJECT_ROOT
	if root and root ~= "" then
		cmd = { "podarcis", "--root", root, "wiki", "context", "--", path }
	end
	pcall(vim.fn.jobstart, cmd, { detach = true })
end

vim.api.nvim_create_autocmd("BufEnter", {
	group = group,
	callback = function(args)
		local path = vim.api.nvim_buf_get_name(args.buf)
		if skip(args.buf, path) then
			return
		end
		write_context(path)
	end,
})
