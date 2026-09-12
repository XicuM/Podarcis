'''Discovery, inspection, and state management for MCP servers and Skills.'''

# Standard library imports
import re, shutil, sys, subprocess
from pathlib import Path

# Local imports
from podarcis.common import load_json, load_yaml, save_json, save_yaml
from podarcis.console import console


# First-party primitives live in .apm/ — the APM package layout. `apm install`
# deploys them (plus any third-party dependency) into .claude/ and .opencode/,
# so those directories are build output; discovery reads the source tree.
SKILLS = lambda root: root/'.apm'/'skills'
MCPS = lambda root: root/'.agents'/'mcp'
AGENTS = lambda root: root/'.apm'/'agents'


# APM deploys a dependency's whole bundle into the harness skill directories, so
# each package still carries the pyproject.toml or package.json that declares its
# executables. Both roots are checked because skills converge on .agents/skills/
# for most targets while Claude keeps a native one.
DEPLOYED = lambda root: (root/'.agents'/'skills', root/'.claude'/'skills')


def _declared_executables(skill_dir: Path) -> list[str]:
    '''Console scripts a deployed skill bundle declares, Python or Node.'''
    names = []
    if (pyproject := skill_dir/'pyproject.toml').exists():
        try:
            import tomllib
            names += list(tomllib.loads(pyproject.read_text(encoding='utf-8'))
                          .get('project', {}).get('scripts', {}))
        except ModuleNotFoundError:  # tomllib is 3.11+; the engine supports 3.10
            block = re.search(r'^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)',
                              pyproject.read_text(encoding='utf-8'), re.M | re.S)
            if block:
                names += re.findall(r'^\s*([\w.-]+)\s*=', block.group(1), re.M)
        except Exception: pass
    if (pkg := skill_dir/'package.json').exists():
        try:
            manifest = load_json(pkg)
            binaries = manifest.get('bin', {})
            # npm: a string `bin` names the binary after the package, not the path.
            names += [manifest.get('name', '')] if isinstance(binaries, str) else list(binaries)
        except Exception: pass
    return sorted(set(names))


def external_skills(root: Path) -> dict:
    '''Skills APM deployed that this repo does not author, and whether they work.

    A skill whose CLI is missing is a dead letter: the model reads instructions
    telling it to run a command that does not exist. APM deploys files but does
    not install language runtimes, and a failing lifecycle script does not fail
    `apm install` — so nothing upstream of here notices.
    '''
    authored = ({d.name for d in SKILLS(root).iterdir() if d.is_dir()}
                if SKILLS(root).exists() else set())
    found: dict[str, dict] = {}
    for deploy in DEPLOYED(root):
        if not deploy.exists():
            continue
        for d in sorted(deploy.iterdir()):
            if not d.is_dir() or d.name in authored or d.name in found:
                continue
            executables = _declared_executables(d)
            found[d.name] = {
                'name': d.name,
                'executables': {name: _resolve(root, name) for name in executables},
            }
    return found


def _resolve(root: Path, name: str) -> str:
    '''Absolute path to an executable, preferring the project venv over PATH.'''
    if (venv_bin := root/'.venv'/'bin'/name).exists():
        return str(venv_bin)
    return shutil.which(name) or ''


def get_skill_desc(root_dir: Path, name: str) -> str:
    '''Parse description field from skill SKILL.md YAML frontmatter.'''
    if (skill_file := SKILLS(root_dir)/name/'SKILL.md').exists():
        try:
            content = skill_file.read_text(encoding='utf-8')
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().lower().startswith('description:'):
                            return line.split(':', 1)[1].strip()
        except Exception: pass
    return 'Skill module'


def get_agent_desc(root_dir: Path, name: str) -> str:
    '''Parse description field from agent markdown YAML frontmatter.'''
    if (agent_file := AGENTS(root_dir)/f'{name}.agent.md').exists():
        try:
            content = agent_file.read_text(encoding='utf-8')
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().lower().startswith('description:'):
                            return line.split(':', 1)[1].strip()
        except Exception: pass
    return 'Agent module'


def get_mcp_desc(root_dir: Path, dir_name: str, key: str = '') -> str:
    '''Extract summary docstring from MCP server entrypoint module.'''
    if not dir_name: return 'MCP server'
    if (server_py := MCPS(root_dir)/dir_name/'server.py').exists(): 
        try:
            content = server_py.read_text(encoding='utf-8')
            import re
            if (m := re.match(r'^\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', content, re.DOTALL)):
                first_line = m.group(1).strip().splitlines()[0].strip()
                if '—' in first_line: return first_line.split('—', 1)[1].strip()
                if '-' in first_line: return first_line.split('-', 1)[1].strip()
                return first_line
        except Exception: pass
    return f'{dir_name} MCP server'


def run_mcp_setup(root: Path, name: str) -> bool:
    '''Run a module's setup.py, if it ships one.

    The entry point is `setup_<dir_name>`. There used to be a three-tier
    fallback chain behind it — an alternate key spelling plus hardcoded
    `setup_wiki` / `setup_research_credentials` / `setup_google_drive` names —
    kept for modules that no longer exist.

    Setup handles *configuration* only (prompts, writing config.yaml).
    Dependencies come from pyproject.toml at install time.
    '''
    dir_name = name.removesuffix('-mcp')
    if not (setup_script := MCPS(root) / dir_name / 'setup.py').exists():
        return True

    import importlib.util
    spec = importlib.util.spec_from_file_location(f'{dir_name}_setup', setup_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    setup_fn = getattr(mod, f'setup_{dir_name.replace("-", "_")}', None)
    if setup_fn is None:
        console.print(
            f'[yellow]{setup_script.relative_to(root)} defines no '
            f'setup_{dir_name}() entry point; skipping.[/yellow]'
        )
        return True
    res = setup_fn(root)
    return True if res is None else bool(res)


def build_job_choices(jobs: dict) -> list:
    '''Questionary checkbox choices for the jobs picker.

    Was a generic build_component_choices(comp_type=...) with 'mcp'/'job'/else
    branches; the MCP picker builds its own choices inline and nothing ever
    passed a third type, so two of the three branches were unreachable.
    '''
    import questionary
    if not jobs:
        return []

    width = max(len(k) for k in jobs) + 2
    return [
        questionary.Choice(
            title=[('', f'{k:<{width}}'),
                   ('fg:#888888', f'— {v.get("description", "")} [{v.get("schedule", "")}]')],
            value=k, checked=v.get('enabled', False),
        )
        for k, v in sorted(jobs.items())
    ]


def is_skill_enabled(skill_path: Path) -> bool:
    '''Check if skill is active based on SKILL.md frontmatter flags.'''

    # Check if SKILL.md exists and has a YAML frontmatter
    if not (skill_file := skill_path/'SKILL.md').exists(): return False
    if not (content := skill_file.read_text(encoding='utf-8')).startswith('---'): return True

    parts = content.split('---', 2)
    if len(parts) < 3: return True
    for line in parts[1].splitlines():
        line_clean = line.strip().lower()
        if 'disable-model-invocation:' in line_clean and 'true' in line_clean: 
            return False
        if 'user-invocable:' in line_clean and 'false' in line_clean: 
            return False
    return True


def is_agent_enabled(agent_file: Path) -> bool:
    '''Check if agent is active based on markdown frontmatter flags.'''

    # Check if agent markdown file exists and has a YAML frontmatter
    if not agent_file.exists(): return False
    if not (content := agent_file.read_text(encoding='utf-8')).startswith('---'): return True

    parts = content.split('---', 2)
    if len(parts) < 3: return True
    for line in parts[1].splitlines():
        line_clean = line.strip().lower()
        if 'disable-model-invocation:' in line_clean and 'true' in line_clean:
            return False
        if 'user-invocable:' in line_clean and 'false' in line_clean:
            return False
        if 'disabled:' in line_clean and 'true' in line_clean:
            return False
    return True



def count_tokens(text: str) -> int:
    '''Calculate token count using fast character heuristic (~4 chars/tok).'''
    if not text: return 0
    return max(1, round(len(text) / 4.0))


def count_mcp_tokens(mcp_dir: Path) -> int:
    '''Estimate tool schema token size for an MCP server entrypoint.'''

    if not (server_py := mcp_dir/'server.py').exists(): return 0
    try:
        content = server_py.read_text(encoding='utf-8')
        import re
        pieces = []
        fast_tools = re.findall(
            r'@(?:mcp|app)\.tool\(.*?\)\s*(?:async\s+)?def\s+([a-zA-Z0-9_]+)\((.*?)\):', 
            content, re.DOTALL
        )
        for tname, args in fast_tools:
            pieces.append(f'tool: {tname} ({args.strip()})')

        std_tools = re.findall(r'name=[\"\']([a-zA-Z0-9_]+)[\"\']', content)
        for tname in std_tools: pieces.append(f'tool: {tname}')

        for doc in (
            re.findall(r'\"\"\"(.*?)\"\"\"', content, re.DOTALL) +
            re.findall(r'\'\'\'(.*?)\'\'\'', content, re.DOTALL)
        ): pieces.append(doc.strip())

        if pieces: return count_tokens('\n'.join(pieces))
        return max(100, round(count_tokens(content) * 0.35))
    except Exception: return 500


def discover_components(root: Path) -> tuple[dict, dict, dict]:
    '''Scan filesystem to discover registered MCP servers and skills, using persistent mtime token cache.'''

    token_cache = load_json(root/'.agents'/'token_cache.json')
    cache_modified = False

    mcp_servers = {}
    if (mcp_dir := root/'.agents'/'mcp').exists():
        for d in sorted(mcp_dir.iterdir()):
            # A module is its server.py. Requiring it keeps deleted modules deleted:
            # a leftover __pycache__/ or data/ dir is not a phantom module.
            if (server_py := d/'server.py').is_file():
                key = d.name if d.name.endswith('-mcp') else f'{d.name}-mcp'
                mtime = server_py.stat().st_mtime
                cache_key = f'mcp:{d.name}'

                cached = token_cache.get(cache_key)
                if cached and cached.get('mtime') == mtime:
                    tok_count = cached['tokens']
                else:
                    tok_count = count_mcp_tokens(d)
                    token_cache[cache_key] = {'mtime': mtime, 'tokens': tok_count}
                    cache_modified = True

                mcp_servers[key] = {
                    'dir_name': d.name,
                    'path': d,
                    'type': 'mcp',
                    'tokens': tok_count,
                    'desc': get_mcp_desc(root, d.name),
                }

    skills = {}
    if (skills_dir := SKILLS(root)).exists():
        for d in skills_dir.iterdir():
            if d.is_dir():
                skill_file = d/'SKILL.md'
                content = skill_file.read_text(encoding='utf-8') if skill_file.exists() else ''
                desc = get_skill_desc(root, d.name)
                decl_text = f'- {d.name} ({skill_file}): {desc}'
                mtime = skill_file.stat().st_mtime if skill_file.exists() else 0
                cache_key = f'skill:{d.name}'

                cached = token_cache.get(cache_key)
                if cached and cached.get('mtime') == mtime:
                    tok_count = cached['tokens']
                    decl_tok = cached.get('decl_tokens', count_tokens(decl_text))
                else:
                    tok_count = count_tokens(content)
                    decl_tok = count_tokens(decl_text)
                    token_cache[cache_key] = {'mtime': mtime, 'tokens': tok_count, 'decl_tokens': decl_tok}
                    cache_modified = True

                skills[d.name] = {
                    'dir_name': d.name,
                    'path': d,
                    'type': 'skill',
                    'enabled': is_skill_enabled(d),
                    'tokens': tok_count,
                    'decl_tokens': decl_tok,
                    'chars': len(content),
                    'words': len(content.split())
                }

    agents = {}
    if (agents_dir := AGENTS(root)).exists():
        for f in sorted(agents_dir.glob('*.agent.md')):
            name = f.name.removesuffix('.agent.md')
            content = f.read_text(encoding='utf-8')
            desc = get_agent_desc(root, name)
            decl_text = f'- {name} ({f}): {desc}'
            mtime = f.stat().st_mtime
            cache_key = f'agent:{name}'

            cached = token_cache.get(cache_key)
            if cached and cached.get('mtime') == mtime:
                tok_count = cached['tokens']
                decl_tok = cached.get('decl_tokens', count_tokens(decl_text))
            else:
                tok_count = count_tokens(content)
                decl_tok = count_tokens(decl_text)
                token_cache[cache_key] = {'mtime': mtime, 'tokens': tok_count, 'decl_tokens': decl_tok}
                cache_modified = True

            agents[name] = {
                'name': name,
                'path': f,
                'type': 'agent',
                'enabled': is_agent_enabled(f),
                'tokens': tok_count,
                'decl_tokens': decl_tok,
                'chars': len(content),
                'words': len(content.split())
            }

    # Drop cache entries for components deleted from disk, so the cache tracks
    # reality instead of growing a tail of every module ever removed.
    live_keys = (
        {f'mcp:{v["dir_name"]}' for v in mcp_servers.values()} |
        {f'skill:{k}' for k in skills} | {f'agent:{k}' for k in agents}
    )
    if stale := set(token_cache) - live_keys:
        for k in stale: del token_cache[k]
        cache_modified = True

    if cache_modified: save_json(root/'.agents'/'token_cache.json', token_cache)

    return mcp_servers, skills, agents



def get_enabled_mcp_servers(root: Path) -> set[str]:
    '''Active MCP module identifiers, both bare and -mcp suffixed.

    Delegates discovery and the enable rule to the gateway's own router so
    `podarcis status` can never disagree with what `podarcis-mcp` binds. This
    function previously held a hardcoded copy of the module set that drifted
    and reported live modules as disabled.
    '''
    from podarcis.gateway.router import discover_modules, is_enabled

    section = load_yaml(root / '.podarcis' / 'config.yaml').get('mcp_modules')
    return {
        alias
        for name in discover_modules(root) if is_enabled(section, name)
        for alias in (name, f'{name}-mcp')
    }


def set_mcp_server_status(root: Path, server_key: str, enable: bool, mcp_info: dict) -> None:
    '''Persist enabled state for specified MCP module in .podarcis/config.yaml.'''
    clean_key = server_key.removesuffix('-mcp')
    cfg_path = root / '.podarcis' / 'config.yaml'
    data = load_yaml(cfg_path)
    mcp_mods = data.setdefault('mcp_modules', {})
    mod_entry = mcp_mods.setdefault(clean_key, {})
    if isinstance(mod_entry, dict):
        mod_entry['enabled'] = enable
    else:
        mcp_mods[clean_key] = {'enabled': enable}
    save_yaml(cfg_path, data)


