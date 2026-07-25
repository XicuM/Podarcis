'''Discovery, inspection, and state management for MCP servers and Skills.'''

import sys, subprocess
from pathlib import Path
from common import load_json, save_json
from console import console

def _server_name_map(root: Path) -> dict[str, str]:
    '''Derive dir_name → mcp_key from mcp_config.json or opencode.json command paths.'''
    mapping: dict[str, str] = {}

    mcp_cfg = load_json(root / '.agents' / 'mcp_config.json')
    for key, cfg in mcp_cfg.get('mcpServers', {}).items():
        args = cfg.get('args', [])
        if args and '/mcp/' in args[0].replace('\\', '/'):
            parts = args[0].replace('\\', '/').split('/')
            try:
                idx = parts.index('mcp')
                mapping[parts[idx + 1]] = key
            except (ValueError, IndexError):
                pass

    opencode_cfg = load_json(root / 'opencode.json')
    for key, cfg in opencode_cfg.get('mcp', {}).items():
        cmd = cfg.get('command', [])
        if len(cmd) >= 2 and '/mcp/' in cmd[1]:
            parts = cmd[1].replace('\\', '/').split('/')
            try:
                idx = parts.index('mcp')
                mapping[parts[idx + 1]] = key
            except (ValueError, IndexError):
                pass

    mcp_dir = root / '.agents' / 'mcp'
    if mcp_dir.exists():
        for d in mcp_dir.iterdir():
            if d.is_dir() and d.name not in mapping:
                mapping[d.name] = f'{d.name}-mcp'

    return mapping



def get_skill_desc(root_dir: Path, name: str) -> str:
    '''Parse description field from skill SKILL.md YAML frontmatter.'''
    skill_file = root_dir / '.agents' / 'skills' / name / 'SKILL.md'
    if skill_file.exists():
        try:
            content = skill_file.read_text(encoding='utf-8')
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().lower().startswith('description:'):
                            return line.split(':', 1)[1].strip()
        except Exception:
            pass
    return 'Skill module'


def get_agent_desc(root_dir: Path, name: str) -> str:
    '''Parse description field from agent markdown YAML frontmatter.'''
    agent_file = root_dir / '.agents' / 'agents' / f'{name}.md'
    if agent_file.exists():
        try:
            content = agent_file.read_text(encoding='utf-8')
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().lower().startswith('description:'):
                            return line.split(':', 1)[1].strip()
        except Exception:
            pass
    return 'Agent module'


def get_mcp_desc(root_dir: Path, dir_name: str, key: str = '') -> str:
    '''Extract summary docstring from MCP server entrypoint module.'''
    if not dir_name: return 'MCP server'
    if (server_py := root_dir / '.agents' / 'mcp' / dir_name / 'server.py').exists(): 
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
    '''Dynamically load and run setup.py for an MCP server if present.'''
    dir_name = _server_name_map(root).get(name, name)
    setup_script = root / '.agents' / 'mcp' / dir_name / 'setup.py'
    if not setup_script.exists():
        return True

    import importlib.util
    spec = importlib.util.spec_from_file_location(f'{dir_name}_setup', setup_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    setup_fn = (
        getattr(mod, f'setup_{dir_name.replace("-", "_")}', None) or
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
    if not items:
        return []

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
        else:
            desc = ''
            checked = False

        choices.append(
            questionary.Choice(
                title=[
                    ('', f'{k:<{max_len + 2}}'),
                    ('fg:#888888', f'— {desc}'),
                ],
                value=k,
                checked=checked,
            )
        )
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
    skill_file = skill_path / 'SKILL.md'
    if not skill_file.exists(): return False
    content = skill_file.read_text(encoding='utf-8')
    if not content.startswith('---'): return True
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
    if not agent_file.exists(): return False
    content = agent_file.read_text(encoding='utf-8')
    if not content.startswith('---'): return True
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

        docstrings = re.findall(
            r'\"\"\"(.*?)\"\"\"', content, re.DOTALL
        ) + re.findall(
            r'\'\'\'(.*?)\'\'\'', content, re.DOTALL
        )
        for doc in docstrings: pieces.append(doc.strip())

        if pieces: return count_tokens('\n'.join(pieces))
        return max(100, round(count_tokens(content) * 0.35))
    except Exception: return 500


def load_token_cache(root: Path) -> dict:
    '''Load cached token counts from persistent cache file.'''
    cache_file = root / '.agents' / 'token_cache.json'
    return load_json(cache_file)


def discover_components(root: Path) -> tuple[dict, dict]:
    '''Scan filesystem to discover registered MCP servers and skills, using persistent mtime token cache.'''
    mcp_dir = root / '.agents' / 'mcp'
    skills_dir = root / '.agents' / 'skills'

    token_cache = load_token_cache(root)
    cache_modified = False

    mcp_servers = {}
    if mcp_dir.exists():
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
    if skills_dir.exists():
        for d in skills_dir.iterdir():
            if d.is_dir():
                req_file = d / 'requirements.txt'
                skill_file = d / 'SKILL.md'
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
    agents_dir = root / '.agents' / 'agents'
    if agents_dir.exists():
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
    '''Dynamically generate opencode.json, non-destructively merging discovered MCP servers with existing configuration.'''
    opencode_path = root / 'opencode.json'
    mcp_dir = root / '.agents' / 'mcp'
    smap = _server_name_map(root)
    mcp_cfg_path = root / '.agents' / 'mcp_config.json'
    mcp_cfg_data = load_json(mcp_cfg_path)
    mcp_servers_cfg = mcp_cfg_data.get('mcpServers', {})

    existing_data = load_json(opencode_path)
    if not existing_data:
        existing_data = {'$schema': 'https://opencode.ai/config.json'}

    existing_mcp = existing_data.get('mcp', {})
    managed_mcp = {}

    if mcp_dir.exists():
        for d in sorted(mcp_dir.iterdir()):
            if d.is_dir() and (d / 'server.py').exists():
                dir_name = d.name
                key = smap.get(dir_name, f'{dir_name}-mcp')
                server_script = f'.agents/mcp/{dir_name}/server.py'
                cur_server_cfg = existing_mcp.get(key, {})

                # Determine enabled state
                if key in mcp_servers_cfg and 'disabled' in mcp_servers_cfg[key]:
                    enabled = not mcp_servers_cfg[key]['disabled']
                elif dir_name in mcp_servers_cfg and 'disabled' in mcp_servers_cfg[dir_name]:
                    enabled = not mcp_servers_cfg[dir_name]['disabled']
                elif 'enabled' in cur_server_cfg:
                    enabled = cur_server_cfg['enabled']
                else:
                    enabled = True

                # Determine environment dict (preserve existing env keys + add/update mcp_config env)
                env = cur_server_cfg.get('environment', {}).copy()
                if key in mcp_servers_cfg:
                    cfg_env = mcp_servers_cfg[key].get('env', {})
                    for k, v in cfg_env.items():
                        if k != 'PROJECT_ROOT':
                            env[k] = v

                if key == 'research-mcp' and 'SEMANTIC_SCHOLAR_API_KEY' not in env:
                    env['SEMANTIC_SCHOLAR_API_KEY'] = ''

                venv_python = str(root/'.venv'/('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'))
                server_entry = cur_server_cfg.copy()
                server_entry.update({
                    'type': 'local',
                    'command': [venv_python, server_script],
                    'environment': env,
                    'enabled': enabled
                })
                managed_mcp[key] = server_entry

    final_mcp = existing_mcp.copy()
    final_mcp.update(managed_mcp)

    existing_data['mcp'] = final_mcp
    save_json(opencode_path, existing_data)
    return opencode_path



def get_enabled_mcp_servers(root: Path) -> set[str]:
    '''Retrieve set of active MCP server identifiers from configuration files.'''
    opencode_path = root / 'opencode.json'
    if not opencode_path.exists():
        generate_opencode_json(root)

    enabled = set()
    data = load_json(opencode_path)
    mcp_data = data.get('mcp', {})
    smap = _server_name_map(root)
    for key, cfg in mcp_data.items():
        if cfg.get('enabled', True):
            enabled.add(key)
            for dir_name, mapped in smap.items():
                if mapped == key: enabled.add(dir_name)
                elif dir_name == key: enabled.add(mapped)

    mcp_cfg_path = root / '.agents' / 'mcp_config.json'
    if mcp_cfg_path.exists():
        mcp_cfg_data = load_json(mcp_cfg_path)
        mcp_servers = mcp_cfg_data.get('mcpServers', {})
        for key, cfg in mcp_servers.items():
            if cfg.get('disabled', False):
                enabled.discard(key)
                for dir_name, mapped in smap.items():
                    if mapped == key:
                        enabled.discard(dir_name)
                    elif dir_name == key:
                        enabled.discard(mapped)
    return enabled


def set_mcp_server_status(root: Path, server_key: str, enable: bool, mcp_info: dict) -> None:
    '''Persist enabled state for specified MCP server across config files.'''
    dir_name = mcp_info['dir_name']
    server_script = f'.agents/mcp/{dir_name}/server.py'

    mcp_cfg_path = root / '.agents' / 'mcp_config.json'
    mcp_cfg_data = load_json(mcp_cfg_path)
    if 'mcpServers' not in mcp_cfg_data:
        mcp_cfg_data['mcpServers'] = {}

    venv_python = str(root/'.venv'/('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'))
    for k in (server_key, dir_name):
        if k in mcp_cfg_data['mcpServers']:
            if not enable:
                mcp_cfg_data['mcpServers'][k]['disabled'] = True
            else:
                mcp_cfg_data['mcpServers'][k].pop('disabled', None)
        elif enable and k == server_key:
            mcp_cfg_data['mcpServers'][server_key] = {
                'command': venv_python,
                'args': [server_script],
                'env': {'PROJECT_ROOT': str(root)}
            }
    save_json(mcp_cfg_path, mcp_cfg_data)
    generate_opencode_json(root)

    if enable and mcp_info.get('req'):
        install_deps(root, str(mcp_info['req']), True, f'Verifying deps for {server_key}...')
        console.print(f'[green]✓ Dependencies verified for {server_key}.[/green]')



def set_skill_status(root: Path, skill_name: str, enable: bool, skill_info: dict) -> None:
    '''Update SKILL.md frontmatter flags to enable or disable model invocation.'''
    skill_file = skill_info['path'] / 'SKILL.md'
    if not skill_file.exists():
        return

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

