-- Podarcis yazi flavor. Missing optional APIs must not hang or crash yazi.

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

local function repo_of(cwd)
	local root = os.getenv("PROJECT_ROOT") or ""
	local path = tostring(cwd or "")
	if root ~= "" then
		local prefix = root
		if path:sub(1, #prefix) == prefix then
			path = path:sub(#prefix + 1)
			path = path:gsub("^/+", "")
		end
	end
	local top = path:match("^([^/]+)")
	if top == "wiki" or top == "sources" or top == "workspace" then
		return top
	end
	return "engine"
end

local lint_cache, lint_stamp = {}, -1

local function normalize_url(url)
	url = tostring(url or "")
	url = url:gsub("^file://", "")
	return url
end

local function lint_map()
	local now = os.time()
	if now == lint_stamp then
		return lint_cache
	end
	lint_stamp = now
	lint_cache = {}
	local root = os.getenv("PROJECT_ROOT")
	if not root or root == "" then
		return lint_cache
	end
	local f = io.open(root .. "/tmp/tui/lint.json", "r")
	if not f then
		return lint_cache
	end
	local raw = f:read("*a") or ""
	f:close()
	if raw == "" then
		return lint_cache
	end
	for path in raw:gmatch('"([^"]+)"%s*:%s*%[') do
		lint_cache[path] = true
		if not path:match("^/") then
			lint_cache[root .. "/" .. path] = true
		end
	end
	return lint_cache
end

function Linemode:lint()
	local url = normalize_url(self._file.url)
	local badges = lint_map()
	if badges[url] then
		return "✗"
	end
	local root = os.getenv("PROJECT_ROOT") or ""
	if root ~= "" and url:sub(1, #root) == root then
		local rel = url:sub(#root + 2)
		if badges[rel] then
			return "✗"
		end
	end
	return ""
end

pcall(function()
	Header:children_add(function(self)
		local name = repo_of(self._current.cwd)
		return ui.Span(" " .. name .. " "):fg("cyan"):bold()
	end, 0, Header.LEFT)
end)

pcall(function()
	local orig = Entity.style
	function Entity:style()
		if HIDDEN[self._file.name] then
			return ui.Style():fg("reset"):dim()
		end
		if type(orig) == "function" then
			return orig(self)
		end
	end
end)
