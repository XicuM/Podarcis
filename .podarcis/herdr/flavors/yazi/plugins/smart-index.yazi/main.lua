--- @sync entry
-- Enter a directory (prefer _index.md / index.md) or open a file via wiki edit.

local HIDDEN = {
	tmp = true,
	[".git"] = true,
	[".venv"] = true,
	__pycache__ = true,
	node_modules = true,
	[".obsidian"] = true,
	[".claude"] = true,
	[".opencode"] = true,
}

local function exists(path)
	local f = io.open(path, "r")
	if f then
		f:close()
		return true
	end
	return false
end

local function quote(s)
	if ya.quote then
		return ya.quote(s)
	end
	return string.format("%q", s)
end

local function emit_shell(cmd)
	local payload = { cmd, orphan = true }
	if ya.emit then
		ya.emit("shell", payload)
	else
		ya.manager_emit("shell", payload)
	end
end

local function emit_enter()
	if ya.emit then
		ya.emit("enter", {})
	else
		ya.manager_emit("enter", {})
	end
end

local function wiki_edit(path)
	emit_shell("podarcis wiki edit -- " .. quote(path))
end

return {
	entry = function()
		local h = cx.active.current.hovered
		if not h then
			return
		end
		local path = tostring(h.url)
		if h.cha.is_dir then
			if HIDDEN[h.name] then
				emit_enter()
				return
			end
			local index = path .. "/_index.md"
			if not exists(index) then
				index = path .. "/index.md"
			end
			if exists(index) then
				wiki_edit(index)
			else
				emit_enter()
			end
			return
		end
		wiki_edit(path)
	end,
}
