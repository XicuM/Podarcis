--- @sync entry
-- cd $PROJECT_ROOT/{wiki,sources,workspace,tmp}. Relative `cd wiki` fails from wiki/health/.

local ALLOWED = {
	wiki = true,
	sources = true,
	workspace = true,
	tmp = true,
}

local function emit_cd(target)
	local payload = { target }
	if ya.emit then
		ya.emit("cd", payload)
	else
		ya.manager_emit("cd", payload)
	end
end

local function dest_of(job)
	if type(job) == "table" and type(job.args) == "table" then
		return job.args[1]
	end
	return nil
end

return {
	entry = function(self, job)
		if job == nil then
			job = self
		end
		local dest = dest_of(job)
		if type(dest) ~= "string" or not ALLOWED[dest] then
			return
		end
		local root = os.getenv("PROJECT_ROOT") or ""
		if root == "" then
			return
		end
		emit_cd(root .. "/" .. dest)
	end,
}
