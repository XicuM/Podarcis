'''Discovery, inspection, and state management for MCP servers and Skills.'''

# Standard library imports
import sys, subprocess
from pathlib import Path

# Local imports
from common import load_json, save_json
from console import console
from backends import _server_name_map


SKILLS = lambda root: root/'.agents'/'skills'
MCPS = lambda root: root/'.agents'/'mcp'
AGENTS = lambda root: root/'.agents'/'agents'


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
    if (agent_file := AGENTS(root_dir)/f'{name}.md').exists():
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
    '''Dynamically load and run setup.py for an MCP server if present.

    Resolution order (first match wins):
      1. setup_{dir_name}   – canonical entry-point; orchestrates all config
                              questions for that server in one place.
      2. setup_{key}        – alternate name derived from the MCP registry key.
      3. Legacy names       – setup_wiki, setup_research_credentials,
                              setup_google_drive (kept for backwards compat).

    The function is responsible only for *configuration* (prompts, writing
    credentials/config.yaml).  Dependency installation is always performed
    afterwards by the caller (set_mcp_server_status / _configure_mcp_servers).
    '''
    smap = _server_name_map(root)
    # smap is dir_name → key; build inverse (key → dir_name) to resolve path from key
    inv_smap = {v: k for k, v in smap.items()}
    dir_name = inv_smap.get(name) or smap.get(name) or name
    mcp_dir = MCPS(root)/dir_name
    if not (setup_script := mcp_dir/'setup.py').exists(): return True

    if (req_file := mcp_dir / 'requirements.txt').exists():
        install_deps(root, str(req_file), True, f'Verifying dependencies for {name}...')

    import importlib.util
    spec = importlib.util.spec_from_file_location(f'{dir_name}_setup', setup_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    key_name = name.replace('-', '_')
    setup_fn = (
        # 1. Canonical entry-point: setup_<dir_name>
        getattr(mod, f'setup_{dir_name.replace("-", "_")}', None) or
        # 2. Alternate name derived from registry key
        getattr(mod, f'setup_{key_name}', None) or
        # 3. Legacy / explicitly-named helpers (backwards compat)
        getattr(mod, 'setup_wiki', None) or
        getattr(mod, 'setup_research_credentials', None) or
        getattr(mod, 'setup_google_drive', None)
    )
    if setup_fn:
        res = setup_fn(root)
        return True if res is None else bool(res)
    return True


def build_component_choices(root: Path, comp_type: str, items: dict, enabled_set: set[str] = None) -> list:
    '''Build standardized Questionary choices with grey descriptions for components.'''
    import questionary
    if not items: return []

    max_len = max(len(k) for k in items) if items else 15
    choices = []
    for k in sorted(items):
        if comp_type == 'mcp':
            desc = items[k].get('desc') or get_mcp_desc(root, items[k]['dir_name'], k)
            checked = (k in enabled_set) if enabled_set is not None else False
        elif comp_type == 'skill':
            desc = get_skill_desc(root, k)
            checked = items[k].get('enabled', False)
        elif comp_type == 'agent':
            desc = get_agent_desc(root, k)
            checked = items[k].get('enabled', False)
        elif comp_type == 'job':
            desc = f"{items[k].get('description', '')} [{items[k].get('schedule', '')}]"
            checked = items[k].get('enabled', False)
        else:
            desc = ''
            checked = False

        choices.append(questionary.Choice(
            title=[('', f'{k:<{max_len + 2}}'), ('fg:#888888', f'— {desc}')],
            value=k, checked=checked,
        ))
    return choices


def install_deps(root: Path, target: str, is_req: bool, message: str) -> None:
    '''Quietly install python dependencies via virtual environment pip.'''

    # Determine pip command based on virtual environment presence
    venv_pip = root/'.venv'/('Scripts/pip.exe' if sys.platform == 'win32' else 'bin/pip')
    pip_cmd = [str(venv_pip)] if venv_pip.exists() else [sys.executable, '-m', 'pip']

    # Construct pip install command and execute with status logging
    cmd = pip_cmd + ['install', '-r', target] if is_req else pip_cmd + ['install', target]
    with console.status(f'[#29b8db]{message}[/#29b8db]', spinner='dots'):
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


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


def discover_components(root: Path) -> tuple[dict, dict]:
    '''Scan filesystem to discover registered MCP servers and skills, using persistent mtime token cache.'''

    token_cache = load_json(root/'.agents'/'token_cache.json')
    cache_modified = False

    mcp_servers = {}
    if (mcp_dir := root/'.agents'/'mcp').exists():
        for d in mcp_dir.iterdir():
            if d.is_dir():
                key = _server_name_map(root).get(d.name, d.name)
                req_file = d / 'requirements.txt'
                server_py = d / 'server.py'
                mtime = server_py.stat().st_mtime if server_py.exists() else 0
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
                    'req': req_file if req_file.exists() else None,
                    'type': 'mcp',
                    'tokens': tok_count,
                    'desc': get_mcp_desc(root, d.name),
                }

    skills = {}
    if (skills_dir := SKILLS(root)).exists():
        for d in skills_dir.iterdir():
            if d.is_dir():
                req_file = d/'requirements.txt'
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
                    'req': req_file if req_file.exists() else None,
                    'type': 'skill',
                    'enabled': is_skill_enabled(d),
                    'tokens': tok_count,
                    'decl_tokens': decl_tok,
                    'chars': len(content),
                    'words': len(content.split())
                }

    agents = {}
    if (agents_dir := AGENTS(root)).exists():
        for f in agents_dir.iterdir():
            if f.is_file() and f.suffix == '.md':
                name = f.stem
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

    if cache_modified: save_json(root/'.agents'/'token_cache.json', token_cache)

    return mcp_servers, skills, agents



def generate_opencode_json(root: Path) -> Path:
    '''Dynamically generate opencode.json. Delegates to backends adapter.'''
    from backends import generate_for_backend
    result = generate_for_backend(root, 'opencode')
    return result or root / 'opencode.json'



def get_enabled_mcp_servers(root: Path) -> set[str]:
    '''Retrieve set of active MCP server identifiers from the active backend's config.'''
    from common import get_config_value
    from backends import read_enabled

    backend = get_config_value(root, 'backend', default='opencode')
    enabled_map = read_enabled(root, backend)

    smap = _server_name_map(root)
    enabled = set()
    for key, is_on in enabled_map.items():
        if is_on:
            enabled.add(key)
            for dir_name, mapped in smap.items():
                if mapped == key:
                    enabled.add(dir_name)
                elif dir_name == key:
                    enabled.add(mapped)
    return enabled


def set_mcp_server_status(root: Path, server_key: str, enable: bool, mcp_info: dict) -> None:
    '''Persist enabled state for specified MCP server in the active backend's config.'''
    from common import get_config_value
    from backends import write_enabled, generate_for_backend

    # Write enable/disable to the ACTIVE backend's native config
    backend = get_config_value(root, 'backend', default='opencode')
    write_enabled(root, backend, server_key, enable)

    # Regenerate the active backend's config file
    generate_for_backend(root, backend)

    if enable and mcp_info.get('req'):
        install_deps(root, str(mcp_info['req']), True, f'Verifying deps for {server_key}...')
        console.print(f'[green]✓ Dependencies verified for {server_key}.[/green]')



def set_skill_status(root: Path, skill_name: str, enable: bool, skill_info: dict) -> None:
    '''Update SKILL.md frontmatter flags to enable or disable model invocation.'''
    
    if not (skill_file := skill_info['path']/'SKILL.md').exists(): return

    content = skill_file.read_text(encoding='utf-8')
    if not content.startswith('---'):
        if not enable:
            content = f'---\ndisable-model-invocation: true\nuser-invocable: false\ndisabled: true\n---\n\n{content}'
    else:
        parts = content.split('---', 2)
        if len(parts) >= 3:
            fm_lines = parts[1].strip().splitlines()
            new_fm = [
                line for line in fm_lines
                if (line.split(':')[0].strip() if ':' in line else line.strip()) not in (
                    'disable-model-invocation', 'user-invocable', 'disabled'
                )
            ]
            if not enable:
                new_fm.extend(['disable-model-invocation: true', 'user-invocable: false', 'disabled: true'])
            body = parts[2].lstrip('\r\n')
            content = f'---\n{"\n".join(new_fm)}\n---\n\n{body}'

    skill_file.write_text(content, encoding='utf-8')
    if enable and skill_info.get('req'):
        install_deps(root, str(skill_info['req']), True, f'Installing dependencies for skill {skill_name}...')


def set_agent_status(root: Path, agent_name: str, enable: bool, agent_info: dict) -> None:
    '''Update agent frontmatter flags to enable or disable model invocation.'''
    agent_file = agent_info['path']
    if not agent_file.exists():
        return

    content = agent_file.read_text(encoding='utf-8')
    if not content.startswith('---'):
        if not enable:
            content = f'---\ndisable-model-invocation: true\nuser-invocable: false\ndisabled: true\n---\n\n{content}'
    else:
        parts = content.split('---', 2)
        if len(parts) >= 3:
            fm_lines = parts[1].strip().splitlines()
            new_fm = [
                line for line in fm_lines
                if (line.split(':')[0].strip() if ':' in line else line.strip()) not in (
                    'disable-model-invocation', 'user-invocable', 'disabled'
                )
            ]
            if not enable:
                new_fm.extend(['disable-model-invocation: true', 'user-invocable: false', 'disabled: true'])
            body = parts[2].lstrip('\r\n')
            content = f'---\n{"\n".join(new_fm)}\n---\n\n{body}'

    agent_file.write_text(content, encoding='utf-8')


def sync_all_backends(root: Path) -> dict[str, Path | None]:
    '''Regenerate MCP config for ALL supported backends from canonical definitions.

    Each backend's config is regenerated using its own enable/disable state.
    Returns {backend_name: path_written} for each backend.
    '''
    from backends import generate_all
    return generate_all(root)

